#!/usr/bin/env python3
"""
Sensibo Fallback Scheduler – final spec
======================================
• Turns Climate-React ON  at 09:00 and OFF at 20:00 (Thu/Fri/Sat).
• Runs a 5-min fallback monitor only inside that 09–20 window.
• Nightly hard window: CR ON 01:30, CR OFF + AC OFF 03:00 (no monitoring).
• Fallback sends **AC ON** if CR thinks AC is on but the room keeps heating.
• Cool-down 15 min, high threshold 24.5 °C.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from typing import Dict, Final, Optional, Tuple

import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─────────── Config ───────────
_API_BASE: Final[str] = "https://home.sensibo.com/api/v2"
_API_KEY:  Final[str] = os.getenv("SENSIBO_API_KEY")
_POD_ID:   Final[str | None] = os.getenv("SENSIBO_POD_ID")

if not _API_KEY:
    sys.exit("SENSIBO_API_KEY env var missing — aborting.")

_TEMP_HIGH      = 24.5      # °C → trigger fallback
_COOLDOWN_MIN   = 15        # minutes between interventions
_STATE_TTL      = 600       # seconds to cache CR / AC state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sensibo")

# ─────────── HTTP session ───────────
_session = requests.Session()
_session.headers["User-Agent"] = "SensiboFallback/1.0"
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
            f"{_API_BASE}{path}", params={"apiKey": _API_KEY}, json=json, timeout=10
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
            return None
    except Exception:
        pass
    return temp

# ─────────── Temp monitor ───────────
class _TempMon:
    def __init__(self, window: int = 3):
        self.window = window
        self.history: list[Tuple[dt.datetime, float]] = []
        self.last_intervention: Optional[dt.datetime] = None

    def add(self, v: float):
        now = dt.datetime.utcnow()
        self.history.append((now, v))
        del self.history[:-24]

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

# ─────────── Helpers ───────────
def _within_day_window(now: dt.datetime) -> bool:
    dow = now.weekday()  # 3=Thu, 4=Fri, 5=Sat
    t = now.time()
    return dow in (3, 4, 5) and dt.time(9) <= t < dt.time(20)

# ─────────── Actuators ───────────
def _set_cr(pod_id: str, enable: bool):
    _api_post(f"/pods/{pod_id}/smartmode", {"enabled": enable})
    log.info("Climate React → %s", "ON" if enable else "OFF")
    _state_cache.pop(f"cr:{pod_id}", None)

def _set_ac_power(pod_id: str, on: bool):
    _api_post(f"/pods/{pod_id}/acStates", {"acState": {"on": on}})
    log.info("AC power     → %s", "ON" if on else "OFF")
    _state_cache.pop(f"ac:{pod_id}", None)

# ─────────── Fallback monitor ───────────
def fallback_monitor(pod_id: str, tz):
    now = dt.datetime.now(tz)
    if not _within_day_window(now):
        return

    cr_on = _is_cr_enabled(pod_id)
    if not cr_on:
        return  # user turned CR off; respect that

    ac_on = _ac_state(pod_id).get("on", False)
    temp  = _temperature(pod_id)
    if temp is None:
        return
    _temp_mon.add(temp)

    reason = None
    if _temp_mon.cooldown_ok():
        if temp >= _TEMP_HIGH and not ac_on:
            reason = "AC OFF & temp ≥ threshold"
        elif ac_on and _temp_mon.rising():
            reason = "Temp rising while AC ON"

    if reason:
        log.warning("Intervention: %s (%.1f °C)", reason, temp)
        _set_ac_power(pod_id, True)
        _temp_mon.mark()

# ─────────── Scheduled jobs ───────────
def day_start(pod_id: str):
    log.info("09:00 — enabling Climate React")
    _set_cr(pod_id, True)

def day_end(pod_id: str):
    log.info("20:00 — disabling CR and powering AC OFF")
    _set_cr(pod_id, False)
    _set_ac_power(pod_id, False)

def nightly_on(pod_id: str):
    log.info("01:30 — nightly ON (CR enable)")
    _set_cr(pod_id, True)

def nightly_off(pod_id: str):
    log.info("03:00 — nightly OFF (CR disable + AC OFF)")
    _set_cr(pod_id, False)
    _set_ac_power(pod_id, False)

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
        timezone=tz, job_defaults={"coalesce": True, "max_instances": 1}
    )

    # Daily 5-min monitor (checked every cycle but active only in window)
    sched.add_job(
        fallback_monitor,
        IntervalTrigger(minutes=5),
        args=[pod_id, tz],
        id="fallback_monitor",
    )

    # Daytime on/off Thu/Fri/Sat
    sched.add_job(day_start, CronTrigger(day_of_week="thu,fri,sat", hour=9, minute=0),
                  args=[pod_id], id="day_start")
    sched.add_job(day_end,   CronTrigger(day_of_week="thu,fri,sat", hour=20, minute=0),
                  args=[pod_id], id="day_end")

    # Nightly hard window (every day)
    sched.add_job(nightly_on,  CronTrigger(hour=1, minute=30),
                  args=[pod_id], id="nightly_on")
    sched.add_job(nightly_off, CronTrigger(hour=3, minute=0),
                  args=[pod_id], id="nightly_off")

    log.info("Scheduler running (pod %s, zone %s)", pod_id, tz_name)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down…")

if __name__ == "__main__":
    main()
