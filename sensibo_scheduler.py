#!/usr/bin/env python3
"""
Sensibo Fallback Scheduler – “no-cooling” rule
=============================================

WHAT IT DOES
------------
1. **Schedules Climate React**  
   • Thu/Fri/Sat 09 :00   → CR ON  
   • Thu/Fri/Sat 20 :00   → CR OFF + AC OFF  
   • Every night 01 :30   → CR ON  
   • Every night 03 :00   → CR OFF + AC OFF

2. **Fallback monitor (only Thu/Fri/Sat 09–20)** – every 5 min  
   • If CR says AC OFF **and** temp ≥ 24.5 °C   → force AC ON  
   • If CR says AC ON but **room hasn’t cooled ≥ 0.3 °C in 10 min**  
     → force AC ON  
   • Cool-down 15 min between interventions  
   • Fallback never turns AC OFF.

3. **Totally idle** outside those windows, so manual night-time use is untouched.

Python ≥ 3.9 (needs zoneinfo).
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

_TEMP_HIGH      = 24.5      # °C absolute threshold
_DELTA_COOL     = 0.3       # °C temp should drop within 10 min
_COOLDOWN_MIN   = 15        # min between forced-ON events
_STATE_TTL      = 600       # sec cache for CR/AC state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sensibo")

# ─────────── HTTP session w/ retries ───────────
session = requests.Session()
session.headers["User-Agent"] = "SensiboFallback/1.0"
session.mount(
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

def _get(path: str, **params):
    params["apiKey"] = _API_KEY
    try:
        r = session.get(f"{_API_BASE}{path}", params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        log.warning("GET %s failed: %s", path, e)
        return None

def _post(path: str, json: Dict):
    try:
        r = session.post(
            f"{_API_BASE}{path}",
            params={"apiKey": _API_KEY},
            json=json,
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("POST %s failed: %s", path, e)

# ─────────── Pod discovery ───────────
def _pod_id() -> str:
    if _POD_ID:
        return _POD_ID
    pods = _get("/users/me/pods", fields="id") or []
    if len(pods) != 1:
        sys.exit(
            f"Expected exactly 1 pod, found {len(pods)} — set SENSIBO_POD_ID."
        )
    return pods[0]["id"]

# ─────────── Cached helpers ───────────
_state: Dict[str, Tuple[dt.datetime, Dict]] = {}

def _cached(key: str, ttl: int, fetcher):
    now = dt.datetime.utcnow()
    ts, val = _state.get(key, (dt.datetime.fromtimestamp(0), None))
    if (now - ts).total_seconds() < ttl:
        return val
    val = fetcher()
    _state[key] = (now, val)
    return val

def _cr_enabled(pid: str) -> bool:
    return bool(
        _cached(
            f"cr:{pid}",
            _STATE_TTL,
            lambda: (_get(f"/pods/{pid}/smartmode", fields="enabled") or {}).get(
                "enabled", False
            ),
        )
    )

def _ac_state(pid: str) -> Dict:
    return _cached(
        f"ac:{pid}",
        _STATE_TTL,
        lambda: (_get(f"/pods/{pid}/acStates", limit=1, fields="acState") or [{}])[0].get(
            "acState", {}
        ),
    )

def _temperature(pid: str) -> Optional[float]:
    meas = _get(f"/pods/{pid}/measurements", limit=1, fields="temperature,time")
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

# ─────────── Temperature monitor ───────────
class _TempMon:
    def __init__(self):
        self.history: list[Tuple[dt.datetime, float]] = []
        self.last_intervention: Optional[dt.datetime] = None

    def add(self, v: float):
        now = dt.datetime.utcnow()
        self.history.append((now, v))
        del self.history[:-24]  # keep last ~2 h

    def delta_since(self, minutes: int) -> Optional[float]:
        if not self.history:
            return None
        now = dt.datetime.utcnow()
        past_val = next(
            (v for t, v in reversed(self.history)
             if (now - t).total_seconds() >= minutes * 60),
            None,
        )
        if past_val is None:
            return None
        return self.history[-1][1] - past_val  # positive = warming

    def cooldown_ok(self) -> bool:
        if not self.last_intervention:
            return True
        return (
            dt.datetime.utcnow() - self.last_intervention
        ).total_seconds() >= _COOLDOWN_MIN * 60

    def mark(self):
        self.last_intervention = dt.datetime.utcnow()

_temp = _TempMon()

# ─────────── Helpers ───────────
def _day_window(now: dt.datetime) -> bool:
    dow = now.weekday()                 # Thu=3, Fri=4, Sat=5
    return dow in (3, 4, 5) and dt.time(9) <= now.time() < dt.time(20)

# ─────────── Actuators ───────────
def _set_cr(pid: str, enable: bool):
    _post(f"/pods/{pid}/smartmode", {"enabled": enable})
    log.info("CR → %s", "ON" if enable else "OFF")
    _state.pop(f"cr:{pid}", None)

def _set_ac(pid: str, on: bool):
    _post(f"/pods/{pid}/acStates", {"acState": {"on": on}})
    log.info("AC → %s", "ON" if on else "OFF")
    _state.pop(f"ac:{pid}", None)

# ─────────── Fallback monitor ───────────
def fallback(pid: str, tz):
    now = dt.datetime.now(tz)
    if not _day_window(now):
        return

    cr_on = _cr_enabled(pid)
    if not cr_on:
        return  # user disabled CR

    ac_on = _ac_state(pid).get("on", False)
    temp  = _temperature(pid)
    if temp is None:
        return
    _temp.add(temp)

    reason = None
    if _temp.cooldown_ok():
        # Rule 1: CR thinks AC off but room hot
        if not ac_on and temp >= _TEMP_HIGH:
            reason = "AC OFF & temp ≥ threshold"
        # Rule 2: CR thinks AC on but no cooling in 10 min
        else:
            delta = _temp.delta_since(10)
            if ac_on and delta is not None and delta >= _DELTA_COOL:
                reason = f"No cooling ({delta:+.2f} °C in 10 min)"

    if reason:
        log.warning("Fallback → %s", reason)
        _set_ac(pid, True)
        _temp.mark()

# ─────────── Scheduled jobs ───────────
def day_start(pid: str):
    _set_cr(pid, True)

def day_end(pid: str):
    _set_cr(pid, False)
    _set_ac(pid, False)

def nightly_on(pid: str):
    _set_cr(pid, True)

def nightly_off(pid: str):
    _set_cr(pid, False)
    _set_ac(pid, False)

# ─────────── Main ───────────
def main():
    pid = _pod_id()

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

    # Fallback monitor (runs always, acts only inside 09-20 window)
    sched.add_job(fallback, IntervalTrigger(minutes=5), args=[pid, tz],
                  id="fallback")

    # Daytime CR on/off Thu/Fri/Sat
    sched.add_job(day_start,
                  CronTrigger(day_of_week="thu,fri,sat", hour=9, minute=0),
                  args=[pid], id="day_start")
    sched.add_job(day_end,
                  CronTrigger(day_of_week="thu,fri,sat", hour=20, minute=0),
                  args=[pid], id="day_end")

    # Nightly hard window
    sched.add_job(nightly_on,
                  CronTrigger(hour=1, minute=30),
                  args=[pid], id="nightly_on")
    sched.add_job(nightly_off,
                  CronTrigger(hour=3, minute=0),
                  args=[pid], id="nightly_off")

    log.info("Scheduler running (pod %s, zone %s)", pid, tz_name)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down…")

if __name__ == "__main__":
    main()
