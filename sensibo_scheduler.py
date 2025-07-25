#!/usr/bin/env python3
"""
Sensibo Scheduler for Raspberry Pi
=================================
Runs as a background process (or systemd service) and handles three things:

1. **Weekly daytime schedule**
   • **Thursday**  – ON 10:00, OFF 20:00.
   • **Friday**    – ON 10:00, OFF 23:00.
   • **Saturday**  – ON 10:00, OFF 20:00.
2. **Nightly cooling window** – Every night at 01:30 Climate React is enabled
   for 1.5 hours and then switched off together with the A/C (OFF at 03:00).
3. **Immediate self-test** – When the script starts it flips Climate React ON
   one minute after launch, waits two minutes, then turns it OFF alongside the
   A/C to verify everything works.

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
# --- Python ≤ 3.7 compatibility: Final from typing_extensions ---
try:
    from typing import Final as _Final  # Python ≥ 3.8
except ImportError:  # Python ≤ 3.7
    from typing_extensions import Final as _Final  # type: ignore

import requests as _rq
from apscheduler.schedulers.blocking import BlockingScheduler as _Scheduler
from apscheduler.triggers.cron import CronTrigger as _Cron

# ─────────── Utility helpers ───────────


def _is_climate_react_enabled(pod_id: str) -> bool:
    """Return True if Climate React is currently enabled for *pod_id*."""
    resp = _rq.get(
        f"{_API_BASE}/pods/{pod_id}/smartmode",
        params={"apiKey": _API_KEY, "fields": "enabled"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("result", {}).get("enabled", False)


def _is_ac_on(pod_id: str) -> bool:
    """Return True if the AC controlled by *pod_id* is currently ON."""
    # Fetch the most recent AC state (limit=1 ensures a single record)
    resp = _rq.get(
        f"{_API_BASE}/pods/{pod_id}/acStates",
        params={"apiKey": _API_KEY, "limit": 1, "fields": "acState"},
        timeout=10,
    )
    resp.raise_for_status()
    result = resp.json().get("result", [])
    if not result:
        return False
    return result[0].get("acState", {}).get("on", False)


def _in_active_window(now: _dt.datetime) -> bool:
    """Return True if *now* (aware datetime) falls into an *ON* schedule window."""

    dow = now.weekday()  # Monday=0 … Sunday=6
    t = now.time()

    # Nightly window: 01:30–03:00 daily
    if _dt.time(1, 30) <= t < _dt.time(3, 0):
        return True

    # Weekly windows (Thu/Fri/Sat daytimes)
    if dow == 3 and _dt.time(10, 0) <= t < _dt.time(20, 0):  # Thursday
        return True
    if dow == 4 and _dt.time(10, 0) <= t < _dt.time(23, 0):  # Friday
        return True
    if dow == 5 and _dt.time(10, 0) <= t < _dt.time(20, 0):  # Saturday
        return True

    return False

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

    # ─────────── Startup consistency check ───────────
    now_local = _dt.datetime.now(tz_obj)
    if _in_active_window(now_local):
        print("Startup detected within an active climate window – verifying state…")
        changed = False
        if not _is_climate_react_enabled(pod_id):
            enable_cr(pod_id)
            changed = True
        if not _is_ac_on(pod_id):
            _set_ac_power(pod_id, True)
            changed = True
        if not changed:
            print("State already correct – nothing to adjust.")
        skip_validation_test = True
    else:
        skip_validation_test = False

    # ─────────── Recurring schedules ───────────
    # Weekly daytime windows (ON at 10:00)
    sched.add_job(enable_cr, _Cron(day_of_week="thu", hour=10, minute=0), args=[pod_id], id="thu_enable")
    sched.add_job(enable_cr, _Cron(day_of_week="fri", hour=10, minute=0), args=[pod_id], id="fri_enable")
    sched.add_job(enable_cr, _Cron(day_of_week="sat", hour=10, minute=0), args=[pod_id], id="sat_enable")

    # Corresponding OFF times
    sched.add_job(disable_cr_and_ac, _Cron(day_of_week="thu", hour=20, minute=0), args=[pod_id], id="thu_disable")
    sched.add_job(disable_cr_and_ac, _Cron(day_of_week="fri", hour=23, minute=0), args=[pod_id], id="fri_disable")
    sched.add_job(disable_cr_and_ac, _Cron(day_of_week="sat", hour=20, minute=0), args=[pod_id], id="sat_disable")

    # Nightly cooling window – ON at 01:30, OFF at 03:00 (every day)
    sched.add_job(enable_cr, _Cron(hour=1, minute=30), args=[pod_id], id="nightly_enable")
    sched.add_job(disable_cr_and_ac, _Cron(hour=3, minute=0), args=[pod_id], id="nightly_disable")

    # Immediate test: turn ON 1 minute after startup, OFF 2 minutes later
    if not skip_validation_test:
        now = _dt.datetime.now(tz_obj)
        test_on = now + _dt.timedelta(minutes=1)
        test_off = test_on + _dt.timedelta(minutes=2)

        sched.add_job(enable_cr, trigger="date", run_date=test_on, args=[pod_id], id="test_enable")
        sched.add_job(disable_cr_and_ac, trigger="date", run_date=test_off, args=[pod_id], id="test_disable")
        print("One-off validation scheduled: ON in 1 min, OFF 2 min later.")
    else:
        print("Skipping one-off validation because we are already in an active window.")

    print("Scheduler started. Press Ctrl-C to exit.")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        print("Shutting down scheduler…")


if __name__ == "__main__":
    main() 