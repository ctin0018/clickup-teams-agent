# ClickUp Teams Agent — Setup Guide

## What this does

| Trigger | Who gets it | Card contains |
|---|---|---|
| Task due **today** | PIC | "Mark Delayed / On Hold / Already Done" buttons |
| Task due in **3 days** | PIC | Reminder + form (start date, task type, account, blockers) |
| Task **overdue** | Manager | "Mark Complete / Delayed / Reassign" buttons |

All actions update ClickUp automatically — no one needs to log in.

---

## 1. Install dependencies

```bash
pip install -r requirements.txt
```

---

## 2. Fill in your config (agent.py top section)

```python
CALLBACK_BASE_URL = "https://YOUR-PUBLIC-URL-HERE"  # see step 3
MANAGER_WEBHOOK   = "https://..."                   # your manager's Teams webhook
```

Webhooks for team members with empty strings (`""`) are already skipped gracefully.

---

## 3. Expose the Flask server publicly

### Option A — Local dev with ngrok (free, quick)
```bash
# Terminal 1: start the server
python agent.py --serve

# Terminal 2: expose it
ngrok http 5000
# Copy the https://xxxx.ngrok-free.app URL into CALLBACK_BASE_URL
```

### Option B — Deploy to Render (free tier, permanent)
1. Push this folder to a GitHub repo
2. Go to render.com → New Web Service → connect your repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `python agent.py --serve`
5. Copy the `.onrender.com` URL into `CALLBACK_BASE_URL`

### Option C — Deploy to Railway
```bash
npm install -g railway
railway login
railway init
railway up
```

---

## 4. Run the daily check

### Manually
```bash
python agent.py --check
```

### As a cron job (runs every day at 8 AM)
```bash
# Edit crontab
crontab -e

# Add this line (adjust path to your Python and script)
0 8 * * 1-5 /usr/bin/python3 /path/to/clickup_agent/agent.py --check >> /tmp/clickup_agent.log 2>&1
```

### Keep server + run check together
```bash
python agent.py --both
```

---

## 5. How the two-way button flow works

```
Teams card button click
        ↓
Power Automate (your existing webhooks)
        ↓
  [Action.Http in Adaptive Card]
        ↓
Flask server (this app) /action or /confirm_details
        ↓
ClickUp API — status/field updated
```

**Note:** Teams' `Action.Http` in Adaptive Cards requires the URL to be accessible from
Microsoft's servers. This is why you need either ngrok (dev) or a deployed server (prod).

---

## 6. Adding missing webhooks

Edit `TEAM_WEBHOOKS` in `agent.py`. Each person needs a Power Automate flow that:
1. Has an HTTP trigger (gives you the webhook URL)
2. Posts the received payload to `{CALLBACK_BASE_URL}/action`

---

## 7. Test individual cards

You can test a card without running the full check:

```python
# Quick test in Python console
from agent import build_due_today_card, send_to_teams, TEAM_WEBHOOKS

card = build_due_today_card("TASK123", "Test Project", "Lik Ming", "15 Jun 2025")
send_to_teams(TEAM_WEBHOOKS["Lik Ming"], card, label="test")
```

---

## File structure

```
clickup_agent/
├── agent.py          ← main file (scheduler + server + card builders)
├── requirements.txt
└── SETUP.md          ← this file
```