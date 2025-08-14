#!/usr/bin/env python3
"""
Sensibo Shabbat Scheduler
=========================
Single 5-minute monitor job that:

* **Enters** a climate window → turns Climate React (CR) ON.
* **Leaves** the window        → CR OFF **and** AC OFF immediately.
* While inside a window, applies smart fallback:
    • Temp ≥ 23.5 °C **and** AC OFF  → force AC ON.  
    • AC ON **but** temperature rising → force AC ON again.
* Cool-down: 15 min between forced-ON interventions.
* Caches CR/AC state for 10 min to spare API quota.
* Resilient: every API call retried; failures only log,
  never crash the scheduler.
* Handles one pod automatically or a specific one via
  `SENSIBO_POD_ID`.

Climate windows (local time, default Asia/Jerusalem)
----------------------------------------------------
* Nightly 01:30-03:00 **every day**
* Thu 10-20, Fri 10-23, Sat 10-20

Python ≥ 3.9 required (needs zoneinfo).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from typing import Dict, Final, Optional, Tuple

import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─────────── Configuration ───────────
_API_BASE: Final[str] = "https://home.sensibo.com/api/v2"
_API_KEY:  Final[str] = os.getenv("SENSIBO_API_KEY")
_POD_ID:   Final[str | None] = os.getenv("SENSIBO_POD_ID")

if not _API_KEY:
    sys.exit("SENSIBO_API_KEY env var missing — aborting.")

_TEMP_HIGH = 23.5      # °C → trigger AC ON
_COOLDOWN_MIN = 15     # minutes between interventions
_STATE_TTL   = 600     # seconds to cache CR/AC state

# ─────────── Logging ───────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sensibo")

# ─────────── HTTP session (retry) ───────────
_session = requests.Session()
_session.headers["User-Agent"] = "SensiboScheduler/1.0"
_session.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False,
        )
    ),
)

def _api_get(path: str, **params):
    params["apiKey"] = _API_KEY
    try:
        r = _session.get(f"{_API_BASE}{path}", params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        log.warning("GET %s failed: %s", path, e)
        return None

def _api_post(path: str, json: Dict):
    try:
        r = _session.post(
            f"{_API_BASE}{path}",
            params={"apiKey": _API_KEY},
            json=json,
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("POST %s failed: %s", path, e)

# ─────────── Pod discovery ───────────
def _resolve_pod_id() -> str:
    if _POD_ID:
        return _POD_ID
    pods = _api_get("/users/me/pods", fields="id") or []
    if len(pods) != 1:
        sys.exit(
            f"Expected exactly 1 pod, found {len(pods)}. "
            "Set SENSIBO_POD_ID env var."
        )
    return pods[0]["id"]

# ─────────── Cached helpers ───────────
_state_cache: Dict[str, Tuple[dt.datetime, Dict]] = {}

def _cached(key: str, ttl: int, fetcher):
    now = dt.datetime.utcnow()
    ts, val = _state_cache.get(key, (dt.datetime.fromtimestamp(0), None))
    if (now - ts).total_seconds() < ttl:
        return val
    val = fetcher()
    _state_cache[key] = (now, val)
    return val

def _is_cr_enabled(pod_id: str) -> bool:
    return bool(
        _cached(
            f"cr:{pod_id}",
            _STATE_TTL,
            lambda: (_api_get(f"/pods/{pod_id}/smartmode", fields="enabled") or {}).get(
                "enabled", False
            ),
        )
    )

def _ac_state(pod_id: str) -> Dict:
    return _cached(
        f"ac:{pod_id}",
        _STATE_TTL,
        lambda: (
            _api_get(f"/pods/{pod_id}/acStates", limit=1, fields="acState") or [{}]
        )[0].get("acState", {}),
    )

def _temperature(pod_id: str) -> Optional[float]:
    meas = _api_get(
        f"/pods/{pod_id}/measurements", limit=1, fields="temperature,time"
    )
    if not meas:
        return None
    rec = meas[0]
    temp = rec.get("temperature")
    ts_raw = rec.get("time") or rec.get("sensiboTime")
    try:
        ts = dt.datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        if (dt.datetime.utcnow() - ts).total_seconds() > 600:
            return None    # stale reading
    except Exception:
        pass
    return temp

# ─────────── Temperature monitor ───────────
class _TempMon:
    def __init__(self, window: int = 3):
        self.window = window
        self.history: list[Tuple[dt.datetime, float]] = []
        self.last_intervention: Optional[dt.datetime] = None

    def add(self, value: float):
        now = dt.datetime.utcnow()
        self.history.append((now, value))
        del self.history[:-24]  # keep last ~2 h

    def rising(self) -> bool:
        if len(self.history) < self.window:
            return False
        recent = self.history[-self.window :]
        return all(recent[i][1] < recent[i + 1][1] for i in range(self.window - 1))

    def cooldown_ok(self) -> bool:
        if not self.last_intervention:
            return True
        return (
            dt.datetime.utcnow() - self.last_intervention
        ).total_seconds() >= _COOLDOWN_MIN * 60

    def mark(self):
        self.last_intervention = dt.datetime.utcnow()

_temp_mon = _TempMon()

# ─────────── Window helper ───────────
def _in_active_window(now: dt.datetime) -> bool:
    dow = now.weekday()      # 0=Mon … 6=Sun
    t = now.time()
    if dt.time(1, 30) <= t < dt.time(3, 0):
        return True
    if dow == 3 and dt.time(10) <= t < dt.time(20):
        return True
    if dow == 4 and dt.time(10) <= t < dt.time(23):
        return True
    if dow == 5 and dt.time(10) <= t < dt.time(20):
        return True
    return False

# ─────────── Actuators ───────────
def _set_cr(pod_id: str, enable: bool):
    _api_post(f"/pods/{pod_id}/smartmode", {"enabled": enable})
    log.info("Climate React → %s", "ON" if enable else "OFF")
    _state_cache.pop(f"cr:{pod_id}", None)

def _set_ac_power(pod_id: str, on: bool):
    _api_post(f"/pods/{pod_id}/acStates", {"acState": {"on": on}})
    log.info("AC power     → %s", "ON" if on else "OFF")
    _state_cache.pop(f"ac:{pod_id}", None)

# ─────────── Monitor job ───────────
def monitor_job(pod_id: str, tz):
    now_local = dt.datetime.now(tz)
    in_window = _in_active_window(now_local)

    cr_on  = _is_cr_enabled(pod_id)
    ac_on  = _ac_state(pod_id).get("on", False)

    # ----- Outside any window: make sure everything is OFF -----
    if not in_window:
        if cr_on or ac_on:
            log.info("Outside window — turning CR + AC OFF")
            if cr_on:
                _set_cr(pod_id, False)
            if ac_on:
                _set_ac_power(pod_id, False)
        return

    # ----- Inside a window -----
    temp = _temperature(pod_id)
    if temp is None:
        log.warning("No fresh temperature reading; skipping this cycle")
        return

    _temp_mon.add(temp)
    log.debug(
        "%s  Temp %.1f °C  CR %s  AC %s",
        now_local.strftime("%a %H:%M"),
        temp,
        "ON" if cr_on else "OFF",
        "ON" if ac_on else "OFF",
    )

    # Ensure CR enabled at window start
    if not cr_on:
        _set_cr(pod_id, True)
        cr_on = True

    # Fallback logic
    reason = None
    if _temp_mon.cooldown_ok():
        if temp >= _TEMP_HIGH and not ac_on:
            reason = "AC OFF & temp high"
        elif ac_on and _temp_mon.rising():
            reason = "Temp rising while AC ON"

    if reason:
        log.warning("Intervention: %s (%.1f °C)", reason, temp)
        _set_ac_power(pod_id, True)
        _temp_mon.mark()

# ─────────── Main ───────────
def main():
    pod_id = _resolve_pod_id()

    tz_name = os.getenv("TZ", "Asia/Jerusalem")
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = dt.timezone.utc
        log.warning("Timezone %s not found — using UTC", tz_name)

    sched = BlockingScheduler(
        timezone=tz,
        job_defaults={"coalesce": True, "max_instances": 1},
    )

    # 5-min unified monitor
    sched.add_job(
        monitor_job,
        IntervalTrigger(minutes=5),
        args=[pod_id, tz],
        id="sensibo_monitor",
    )

    log.info("Scheduler started (pod %s, zone %s)", pod_id, tz_name)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down…")

if __name__ == "__main__":
    main()
