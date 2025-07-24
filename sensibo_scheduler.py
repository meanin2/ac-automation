#!/usr/bin/env python3
"""
Sensibo Scheduler for Raspberry Pi
=================================
Runs as a background process (or systemd service) and handles two things:

1. **Weekly schedule** – Every Saturday at 10:00 it enables Climate React, and
   at 20:00 it disables Climate React (and turns the A/C off).
2. **Immediate test** – On the day the script starts it will also perform a
   quick ON/OFF test at 09:00–09:02 to confirm everything works.

Usage
-----
$ python sensibo_scheduler.py

(Use a virtual-env and install requirements: `pip install -r requirements.txt`)

Environment variables:
  SENSIBO_API_KEY – Your Sensibo API key (falls back to hard-coded default).

Deploy as a systemd service for autostart (see README for snippet).
"""
from __future__ import annotations

import datetime as _dt
import os as _os
import sys as _sys
from typing import Final as _Final

import requests as _rq
from apscheduler.schedulers.blocking import BlockingScheduler as _Scheduler
from apscheduler.triggers.cron import CronTrigger as _Cron

_API_BASE: _Final[str] = "https://home.sensibo.com/api/v2"
_API_KEY: _Final[str] = _os.getenv("SENSIBO_API_KEY")

if not _API_KEY:
    _sys.exit("Environment variable SENSIBO_API_KEY not set. Aborting.")


def _single_pod_id() -> str:
    resp = _rq.get(
        f"{_API_BASE}/users/me/pods",
        params={"apiKey": _API_KEY, "fields": "id"},
        timeout=10,
    )
    resp.raise_for_status()
    pods = resp.json().get("result", [])
    if len(pods) != 1:
        _sys.exit(f"Expected exactly 1 pod, found {len(pods)}. Aborting.")
    return pods[0]["id"]


# ─────────── Core Sensibo actions ───────────

def _set_climate_react(pod_id: str, enable: bool) -> None:
    _rq.put(
        f"{_API_BASE}/pods/{pod_id}/smartmode",
        params={"apiKey": _API_KEY},
        json={"enabled": enable},
        timeout=10,
    ).raise_for_status()
    status = "ON " if enable else "OFF"
    print(f"[{_dt.datetime.now():%Y-%m-%d %H:%M:%S}] Climate React → {status}")


def _set_ac_power(pod_id: str, on: bool) -> None:
    _rq.post(
        f"{_API_BASE}/pods/{pod_id}/acStates",
        params={"apiKey": _API_KEY},
        json={"acState": {"on": on}},
        timeout=10,
    ).raise_for_status()
    status = "ON " if on else "OFF"
    print(f"[{_dt.datetime.now():%Y-%m-%d %H:%M:%S}] AC Power      → {status}")


# ─────────── Job wrappers ───────────

def enable_cr(pod_id: str):
    _set_climate_react(pod_id, True)


def disable_cr_and_ac(pod_id: str):
    _set_climate_react(pod_id, False)
    _set_ac_power(pod_id, False)


# ─────────── Scheduler setup ───────────

def main() -> None:
    pod_id = _single_pod_id()

    # Robust timezone handling -------------------------------------------------
    tz_name = _os.getenv("TZ") or "Asia/Jerusalem"
    try:
        from zoneinfo import ZoneInfo

        tz_obj = ZoneInfo(tz_name)
    except Exception:
        # Fallback to UTC if zone unavailable
        from datetime import timezone as _tz

        print(f"Warning: timezone '{tz_name}' not found; falling back to UTC.")
        tz_obj = _tz.utc

    sched = _Scheduler(timezone=tz_obj)

    # Weekly Saturday jobs
    sched.add_job(enable_cr, _Cron(day_of_week="sat", hour=10, minute=0), args=[pod_id], id="sat_enable")
    sched.add_job(disable_cr_and_ac, _Cron(day_of_week="sat", hour=20, minute=0), args=[pod_id], id="sat_disable")

    # Immediate test: turn ON 1 minute after startup, OFF 2 minutes later
    now = _dt.datetime.now()
    test_on = now + _dt.timedelta(minutes=1)
    test_off = test_on + _dt.timedelta(minutes=2)

    sched.add_job(enable_cr, trigger="date", run_date=test_on, args=[pod_id], id="test_enable")
    sched.add_job(disable_cr_and_ac, trigger="date", run_date=test_off, args=[pod_id], id="test_disable")
    print("One-off validation scheduled: ON in 1 min, OFF 2 min later.")

    print("Scheduler started. Press Ctrl-C to exit.")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        print("Shutting down scheduler…")


if __name__ == "__main__":
    main() 