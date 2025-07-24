#!/usr/bin/env python3
"""
Sensibo quick test script.

This helper enables Climate React at 08:40 today and disables it two minutes
later (08:42) while also switching the AC itself off. Designed for an ad-hoc
run on your PC or Raspberry Pi to verify that API access works before setting
up cron/systemd scheduling.

Prerequisites:
  pip install -r requirements.txt   # installs requests

Environment variables (or edit the constants below):
  SENSIBO_API_KEY   – Your personal Sensibo API key

The script assumes you have exactly one Sensibo device (pod) attached to your
account and will abort otherwise.
"""
import datetime as _dt
import os as _os
import sys as _sys
import time as _time
from typing import Final as _Final

import requests as _rq

_API_BASE: _Final[str] = "https://home.sensibo.com/api/v2"
_API_KEY: _Final[str] = _os.getenv("SENSIBO_API_KEY")

if not _API_KEY:
    _sys.exit("Environment variable SENSIBO_API_KEY not set. Aborting.")


# ──────────────────────────────────────────────────────────────────────────────
# Low-level helpers
# ──────────────────────────────────────────────────────────────────────────────

def _single_pod_id() -> str:
    """Return the sole Sensibo pod ID on the account or abort if not exactly one."""
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


def _set_climate_react(pod_id: str, enable: bool) -> None:
    """Enable or disable Climate React for *pod_id*."""
    _rq.put(
        f"{_API_BASE}/pods/{pod_id}/smartmode",
        params={"apiKey": _API_KEY},
        json={"enabled": enable},
        timeout=10,
    ).raise_for_status()
    action = "ON " if enable else "OFF"
    print(f"[{_dt.datetime.now():%H:%M:%S}] Climate React → {action}")


def _set_ac_power(pod_id: str, on: bool) -> None:
    """Turn the AC controlled by *pod_id* on/off.

    Uses POST /pods/{pod_id}/acStates with partial state payload.
    """
    _rq.post(
        f"{_API_BASE}/pods/{pod_id}/acStates",
        params={"apiKey": _API_KEY},
        json={"acState": {"on": on}},
        timeout=10,
    ).raise_for_status()
    action = "ON " if on else "OFF"
    print(f"[{_dt.datetime.now():%H:%M:%S}] AC Power      → {action}")


def _sleep_until(target: _dt.datetime) -> None:
    """Block until *target* time using small sleeps to stay responsive."""
    while True:
        now = _dt.datetime.now()
        remaining = (target - now).total_seconds()
        if remaining <= 0:
            break
        _time.sleep(min(remaining, 30))


# ──────────────────────────────────────────────────────────────────────────────
# Main routine
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    pod_id = _single_pod_id()

    today = _dt.date.today()
    start_at = _dt.datetime.combine(today, _dt.time(hour=8, minute=40))
    stop_at = _dt.datetime.combine(today, _dt.time(hour=8, minute=42))

    now = _dt.datetime.now()
    if now >= stop_at:
        _sys.exit("The stop time (08:42) is already past. Adjust the script times and rerun.")

    print(f"Current time: {now:%H:%M:%S}. Waiting for scheduled actions…")

    # Enable Climate React
    _sleep_until(start_at)
    _set_climate_react(pod_id, True)

    # Disable after 2 minutes and ensure AC is off
    _sleep_until(stop_at)
    _set_climate_react(pod_id, False)
    _set_ac_power(pod_id, False)

    print("All tasks completed. Exiting.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted by user. Exiting.") 