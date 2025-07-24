# Sensibo Test Automation

This is a **quick sanity-check** script that flips Sensibo’s *Climate React* ON
at **08:40** and back OFF again at **08:42** (together with turning the actual
AC off).

> The goal is only to verify that API access works before installing a proper
> scheduler on your Raspberry Pi. Run it once from your PC today, watch the
> device react at the appointed time, and you’re done.

---

## 1. Install prerequisites

```bash
python -m pip install -r requirements.txt
```

(Only the `requests` library is needed.)

## 2. Provide your API key (optional)

The demo script already contains the key you sent me, but you can override it
safely via an environment variable:

```bash
export SENSIBO_API_KEY="<YOUR_KEY>"
```

## 3. Run

```bash
python sensibo_test.py
```

Leave the terminal open; the script will wait until 08:40, enable Climate React
and print timestamps for each action.

```
Current time: 08:35:04. Waiting for scheduled actions…
[08:40:00] Climate React → ON 
[08:42:00] Climate React → OFF
[08:42:00] AC Power      → OFF
All tasks completed. Exiting.
```

## 4. Next steps

Once confirmed, replace this one-off helper with a *cron* entry (or
APScheduler/systemd service) to automate recurring schedules.

---

## Running continuously on the Raspberry Pi

1. **Install dependencies** (in a venv or system-wide):
   ```bash
   python -m pip install -r requirements.txt
   ```

2. **Start the scheduler manually** to watch the 09:00 test:
   ```bash
   python sensibo_scheduler.py
   ```
   • One-off validation: enables Climate React **1 minute after launch**, then
     disables it (and powers the A/C off) **2 minutes later**.

3. **Deploy as a systemd service** so it survives reboots:
   ```ini
   # /etc/systemd/system/sensibo.service
   [Unit]
   Description=Sensibo Climate React Scheduler
   After=network-online.target

   [Service]
   Type=simple
   User=pi
   WorkingDirectory=/home/pi/ac-automation
   ExecStart=/usr/bin/python3 /home/pi/ac-automation/sensibo_scheduler.py
   Restart=on-failure
   Environment="SENSIBO_API_KEY=<YOUR_KEY>"

   [Install]
   WantedBy=multi-user.target
   ```
   Then enable & start:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now sensibo.service
   ```

The service will keep running, executing:
* **Every Saturday** – ON at 10:00, OFF + AC power-down at 20:00.
* **Today** (first run) – quick 09:00–09:02 verification.

Moe Best Coder Ever, automation complete! :rocket: 