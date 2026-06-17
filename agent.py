"""
ClickUp Teams Agent
====================
Two-way bridge between ClickUp and Microsoft Teams via Adaptive Cards.

CORRECT FLOW:
  Due today (days=0)   → PIC personal channel  → Card WITH action buttons (Delayed / On Hold / Done)
  3 days left (days=3) → PIC personal channel  → Card WITH action buttons (Delayed / On Hold / On Track)
  Overdue (days<0)     → Manager channel only  → Info card, NO buttons (just awareness)

WHY no buttons for manager:
  Manager channel is a shared Teams channel. Buttons there could be clicked by anyone.
  Manager gets awareness; PIC is responsible for taking action on their own card.

WHY Action.OpenUrl instead of Action.Http:
  Power Automate webhooks strip Action.Http for security. Action.OpenUrl opens a tiny
  Flask page that performs the ClickUp update and auto-closes in 2 seconds.

Status values used (must match exactly what's in your ClickUp list):
  "delayed"  → DELAYED
  "on hold"  → ON HOLD
  "complete" → COMPLETE  (closed status)
  "in progress" → IN PROGRESS (used for "on track" confirmation)

Usage:
  python agent.py           # Run daily check once
  python agent.py --audit   # Print real statuses + custom fields from ClickUp
  python agent.py --serve   # Start Flask callback server only
  python agent.py --both    # Daily check + keep server alive
"""

import requests
import datetime
import json
import sys
import logging
import os
from flask import Flask, request, jsonify

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

API_TOKEN   = os.environ.get("CLICKUP_API_TOKEN", "pk_88306048_KSCJW7VB9VIXYCXOTASQ008WGYB7B8O0")
LIST_ID     = "901416094383"
SERVER_PORT = 5000

# Public URL of this Flask server (reachable from Teams/browser)
# Dev:  ngrok http 5000  → paste the https URL here
# Prod: your Render/Railway URL
CALLBACK_BASE_URL = "https://clickup-teams-agent.onrender.com"

# ── Manager channel webhook (overdue alerts — info only, no buttons) ──────────
MANAGER_WEBHOOK = "https://default3a7635f01d1e4df58e24716c29905a.57.environment.api.powerplatform.com:443/powerautomate/automations/direct/workflows/95ee4607778e46d69abbe4bf90d9be84/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=HwiRkpl4OdkScYKs083Xle2IR1E2XNnVIMND9z86ZPE"

# ── PIC personal webhooks (due-today + 3-day cards WITH action buttons) ───────
TEAM_WEBHOOKS = {
    "Lik Ming":   "https://default3a7635f01d1e4df58e24716c29905a.57.environment.api.powerplatform.com:443/powerautomate/automations/direct/workflows/e82f10c0c2884391a946f9eec11c544c/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=dwtYWtJo53xaMOdreKZ-g5_x_xFgeoP-z2BCDpi6p4w",
    "Jia Ying":   "",  # TODO: add webhook
    "Daron":      "https://default3a7635f01d1e4df58e24716c29905a.57.environment.api.powerplatform.com:443/powerautomate/automations/direct/workflows/a5ce6536cade4756804b97ff840741a7/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=RwzDMYmjzhCZ6XZqv_aN8fRgmPkTXslkpCedJWu4Tlg",
    "Jee":        "",  # TODO: add webhook
    "Sarah":      "",  # TODO: add webhook
    "Fatin":      "",  # TODO: add webhook
    "Yung Zheng": "https://default3a7635f01d1e4df58e24716c29905a.57.environment.api.powerplatform.com:443/powerautomate/automations/direct/workflows/2a04fbf8bc2244c0834c30030b5a33d0/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=HlCXtJ0NGGZdRV-Ff95pN9_uiWYQ1RN-2c7fkLX_jAo",
}

# ─────────────────────────────────────────────
# CLICKUP STATUS VALUES
# Run `python agent.py --audit` to verify these match your list exactly.
# These must be lowercase and match what ClickUp returns in the status.status field.
# ─────────────────────────────────────────────
STATUS_DELAYED     = "delayed"       # DELAYED  (active)
STATUS_ON_HOLD     = "on hold"       # ON HOLD  (active)
STATUS_COMPLETE    = "complete"      # COMPLETE (closed)
STATUS_IN_PROGRESS = "in progress"   # IN PROGRESS (active) — used for "still on track"

# Statuses where overdue alert to manager is suppressed (PIC already acknowledged)
SUPPRESS_OVERDUE_FOR = {STATUS_DELAYED, STATUS_ON_HOLD}

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CLICKUP API HELPERS
# ─────────────────────────────────────────────

def clickup_headers() -> dict:
    return {"Authorization": API_TOKEN, "Content-Type": "application/json"}


def audit_clickup_list():
    """Print all statuses and custom fields. Run: python agent.py --audit"""
    print("\n" + "=" * 60)
    print(f"  AUDIT — List ID: {LIST_ID}")
    print("=" * 60)

    # Statuses
    print("\n📋 STATUSES")
    r = requests.get(f"https://api.clickup.com/api/v2/list/{LIST_ID}", headers=clickup_headers())
    r.raise_for_status()
    for s in r.json().get("statuses", []):
        tag = "ACTIVE" if s["type"] not in ["closed", "done"] else "closed"
        print(f"  [{tag:6}]  '{s['status']}'  type={s['type']}")

    # Custom Fields
    print("\n🗂️  CUSTOM FIELDS")
    r = requests.get(f"https://api.clickup.com/api/v2/list/{LIST_ID}/field", headers=clickup_headers())
    r.raise_for_status()
    for f in r.json().get("fields", []):
        print(f"\n  ▸ '{f['name']}' | type={f['type']} | id={f['id']}")
        if f["type"] in ("drop_down", "labels"):
            for o in f.get("type_config", {}).get("options", []):
                print(f"      - '{o.get('name','')}' id={o.get('id','')} order={o.get('orderindex','')}")
    print()


def get_dynamic_active_statuses() -> list:
    """Fetch active (non-closed) statuses live from ClickUp."""
    r = requests.get(f"https://api.clickup.com/api/v2/list/{LIST_ID}", headers=clickup_headers())
    r.raise_for_status()
    active = [s["status"] for s in r.json().get("statuses", [])
              if s.get("type") not in ["closed", "done"]]
    log.info(f"Active statuses: {active}")
    return active


def get_tasks() -> list:
    """Fetch all active tasks from ClickUp using live statuses."""
    active = get_dynamic_active_statuses()
    r = requests.get(
        f"https://api.clickup.com/api/v2/list/{LIST_ID}/task",
        headers={"Authorization": API_TOKEN},
        params={"statuses[]": active, "include_closed": "false"},
    )
    r.raise_for_status()
    return r.json().get("tasks", [])


def update_task_status(task_id: str, new_status: str):
    """
    Update a task's status in ClickUp.
    new_status must match exactly what's in your list (lowercase).
    Run --audit to verify valid values.
    """
    r = requests.put(
        f"https://api.clickup.com/api/v2/task/{task_id}",
        headers=clickup_headers(),
        json={"status": new_status},
    )
    r.raise_for_status()
    log.info(f"✅ Task {task_id} → status='{new_status}'")
    return r.json()


def post_comment(task_id: str, text: str):
    """Post a comment on a ClickUp task."""
    r = requests.post(
        f"https://api.clickup.com/api/v2/task/{task_id}/comment",
        headers=clickup_headers(),
        json={"comment_text": text},
    )
    r.raise_for_status()
    log.info(f"✅ Comment posted on task {task_id}")


def get_custom_field_value(task: dict, field_name: str) -> str:
    """
    Extract a custom field display value from a task by field name (case-insensitive).
    Handles: dropdowns, labels, text, date, number, checkbox.
    """
    target = field_name.strip().lower()
    for field in task.get("custom_fields", []):
        if field.get("name", "").strip().lower() != target:
            continue
        val = field.get("value")
        if val is None or val == "" or val == []:
            return "Not set"
        ftype = field.get("type", "")
        if ftype in ("drop_down", "labels"):
            options = field.get("type_config", {}).get("options", [])
            ids = [str(v) for v in val] if isinstance(val, list) else [str(val)]
            names = [
                o.get("name", "") for o in options
                if str(o.get("id")) in ids or str(o.get("orderindex")) in ids
            ]
            return ", ".join(n for n in names if n) or str(val)
        if ftype == "date":
            try:
                return datetime.datetime.fromtimestamp(int(val) / 1000).strftime("%d %b %Y")
            except Exception:
                return str(val)
        if ftype == "checkbox":
            return "Yes" if val else "No"
        return str(val)
    return "Not set"


def get_pic_names(task: dict) -> list:
    """Extract PIC names from the 'PIC (Core Consulting)' custom field."""
    pic_names = []
    for field in task.get("custom_fields", []):
        if "PIC (Core Consulting)" not in field.get("name", ""):
            continue
        val = field.get("value")
        if val is None:
            continue
        ids = [str(v) for v in val] if isinstance(val, list) else [str(val)]
        for opt in field.get("type_config", {}).get("options", []):
            if str(opt.get("id")) in ids or str(opt.get("orderindex")) in ids:
                name = opt.get("name") or opt.get("label") or opt.get("title")
                if name:
                    pic_names.append(name)
    return pic_names


def get_task_detail_facts(task: dict, due_date_str: str) -> list:
    """
    Build a FactSet list showing all key task details.
    Reads start date, Account, Task Type dynamically from the task object.
    """
    start_ms  = task.get("start_date")
    start_str = (
        datetime.datetime.fromtimestamp(int(start_ms) / 1000).strftime("%d %b %Y")
        if start_ms else "Not set"
    )
    account   = get_custom_field_value(task, "Account")
    task_type = get_custom_field_value(task, "Task Type")

    facts = [
        {"title": "Project",    "value": task.get("name", "(unnamed)")},
        {"title": "Due Date",   "value": due_date_str},
        {"title": "Start Date", "value": start_str},
        {"title": "Account",    "value": account},
        {"title": "Task Type",  "value": task_type},
    ]
    return facts


# ─────────────────────────────────────────────
# URL BUILDER FOR ACTION BUTTONS
# ─────────────────────────────────────────────

def _do_url(task_id: str, status: str) -> str:
    """
    Build the Action.OpenUrl URL for a status-change button.
    Opens a tiny Flask page → calls ClickUp API → shows confirmation → auto-closes.
    status must be a valid ClickUp status value (lowercase, e.g. "delayed", "complete").
    """
    return f"{CALLBACK_BASE_URL}/do?task_id={task_id}&status={requests.utils.quote(status)}"


def _on_hold_url(task_id: str) -> str:
    """URL for the On Hold button — opens a reason-input page first."""
    return f"{CALLBACK_BASE_URL}/on-hold?task_id={task_id}"


# ─────────────────────────────────────────────
# ADAPTIVE CARD BUILDERS
# ─────────────────────────────────────────────

def _wrap_card(card_content: dict) -> dict:
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                **card_content,
            }
        }]
    }


def build_due_today_card(task: dict, pic_name: str, due_date_str: str) -> dict:
    """
    Sent to PIC PERSONAL channel when task is due today.
    HAS action buttons — PIC can update status without logging into ClickUp.
    """
    task_id = task.get("id")
    return _wrap_card({
        "body": [
            {
                "type": "TextBlock", "text": "🔔 Task Due Today",
                "weight": "Bolder", "size": "Large", "color": "Warning",
            },
            {
                "type": "TextBlock",
                "text": f"Hi **{pic_name}**, this task is due today. Please update the status below.",
                "wrap": True, "spacing": "Medium",
            },
            {"type": "FactSet", "facts": get_task_detail_facts(task, due_date_str)},
            {
                "type": "TextBlock",
                "text": "_(Clicking a button opens a quick confirmation page that closes automatically)_",
                "size": "Small", "isSubtle": True, "spacing": "Medium",
            },
        ],
        "actions": [
            {"type": "Action.OpenUrl", "title": "⏩ Mark as Delayed",
             "url": _do_url(task_id, STATUS_DELAYED)},
            {"type": "Action.OpenUrl", "title": "⏸️ Mark as On Hold",
             "url": _on_hold_url(task_id)},
            {"type": "Action.OpenUrl", "title": "✅ Already Done",
             "url": _do_url(task_id, STATUS_COMPLETE)},
        ],
    })


def build_three_day_reminder_card(task: dict, pic_name: str, due_date_str: str) -> dict:
    """
    Sent to PIC PERSONAL channel 3 days before due.
    HAS action buttons — PIC can flag early if something is wrong.
    """
    task_id = task.get("id")
    return _wrap_card({
        "body": [
            {
                "type": "TextBlock", "text": "⏳ 3 Days Until Due",
                "weight": "Bolder", "size": "Large", "color": "Attention",
            },
            {
                "type": "TextBlock",
                "text": (
                    f"Hi **{pic_name}**, your task is due in 3 days. "
                    "Here are the current details from ClickUp. "
                    "If you're on track, no action needed — otherwise use the buttons below."
                ),
                "wrap": True, "spacing": "Medium",
            },
            {"type": "FactSet", "facts": get_task_detail_facts(task, due_date_str)},
            {
                "type": "TextBlock",
                "text": "_(Clicking a button opens a quick confirmation page that closes automatically)_",
                "size": "Small", "isSubtle": True, "spacing": "Medium",
            },
        ],
        "actions": [
            {"type": "Action.OpenUrl", "title": "⏩ Flag as Delayed",
             "url": _do_url(task_id, STATUS_DELAYED)},
            {"type": "Action.OpenUrl", "title": "⏸️ Flag as On Hold",
             "url": _on_hold_url(task_id)},
        ],
    })


def build_overdue_manager_card(task: dict, pic_names: list, due_date_str: str, days_overdue: int) -> dict:
    """
    Sent to MANAGER CHANNEL only — info/awareness card, NO action buttons.
    Manager sees what's overdue and who owns it. PIC handles the action on their own card.
    """
    current_status = task.get("status", {}).get("status", "unknown").upper()
    pic_display    = ", ".join(pic_names) if pic_names else "Unassigned"
    day_word       = "day" if days_overdue == 1 else "days"

    return _wrap_card({
        "body": [
            {
                "type": "TextBlock",
                "text": f"🚨 Overdue Alert — {days_overdue} {day_word} past due",
                "weight": "Bolder", "size": "Large", "color": "Attention",
            },
            {
                "type": "TextBlock",
                "text": "FYI — this task has passed its due date. The PIC has been notified on their personal channel to take action.",
                "wrap": True, "spacing": "Medium", "color": "Warning",
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Project",        "value": task.get("name", "(unnamed)")},
                    {"title": "Current Status", "value": current_status},
                    {"title": "Was Due",        "value": due_date_str},
                    {"title": "PIC",            "value": pic_display},
                    {"title": "Days Overdue",   "value": str(days_overdue)},
                ],
            },
        ],
        # NO actions — manager channel is for awareness only
    })


def build_overdue_pic_card(task: dict, pic_name: str, due_date_str: str, days_overdue: int) -> dict:
    """
    Sent to PIC PERSONAL channel when their task is overdue.
    HAS action buttons — PIC can resolve without logging into ClickUp.
    """
    task_id  = task.get("id")
    day_word = "day" if days_overdue == 1 else "days"

    return _wrap_card({
        "body": [
            {
                "type": "TextBlock",
                "text": f"🚨 Your Task is Overdue by {days_overdue} {day_word}",
                "weight": "Bolder", "size": "Large", "color": "Attention",
            },
            {
                "type": "TextBlock",
                "text": f"Hi **{pic_name}**, this task has passed its due date. Please update the status now.",
                "wrap": True, "spacing": "Medium",
            },
            {"type": "FactSet", "facts": get_task_detail_facts(task, due_date_str)},
            {
                "type": "TextBlock",
                "text": "_(Clicking a button opens a quick confirmation page that closes automatically)_",
                "size": "Small", "isSubtle": True, "spacing": "Medium",
            },
        ],
        "actions": [
            {"type": "Action.OpenUrl", "title": "✅ Mark as Complete",
             "url": _do_url(task_id, STATUS_COMPLETE)},
            {"type": "Action.OpenUrl", "title": "⏩ Mark as Delayed",
             "url": _do_url(task_id, STATUS_DELAYED)},
            {"type": "Action.OpenUrl", "title": "⏸️ Mark as On Hold",
             "url": _on_hold_url(task_id)},
        ],
    })


# ─────────────────────────────────────────────
# TEAMS SENDER
# ─────────────────────────────────────────────

def send_to_teams(webhook_url: str, payload: dict, label: str = ""):
    try:
        r = requests.post(
            webhook_url,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if r.status_code in [200, 202]:
            log.info(f"✅ Sent{' → ' + label if label else ''}")
        else:
            log.error(f"❌ Failed{' → ' + label if label else ''}: {r.status_code} {r.text[:200]}")
    except requests.exceptions.RequestException as e:
        log.error(f"❌ Network error{' → ' + label if label else ''}: {e}")


# ─────────────────────────────────────────────
# DAILY CHECK
# ─────────────────────────────────────────────

def check_tasks():
    """
    Daily check. Routing logic:

      days == 0  → PIC personal (WITH buttons) — due today
      days == 3  → PIC personal (WITH buttons) — 3-day warning
      days < 0   → PIC personal (WITH buttons) — overdue action
                 → Manager channel (NO buttons) — overdue awareness
                 Skip both if status is already delayed/on-hold
    """
    log.info("Starting daily ClickUp check...")
    tasks = get_tasks()
    today = datetime.datetime.now().date()
    log.info(f"Found {len(tasks)} active tasks.\n")

    counts = {"due_today": 0, "three_day": 0, "overdue_pic": 0, "overdue_mgr": 0, "skipped": 0}

    for task in tasks:
        task_id    = task.get("id")
        task_name  = task.get("name", "(unnamed)")
        due_ms     = task.get("due_date")

        if not due_ms:
            counts["skipped"] += 1
            continue

        due_date       = datetime.datetime.fromtimestamp(int(due_ms) / 1000).date()
        days_until_due = (due_date - today).days
        due_str        = due_date.strftime("%d %b %Y")

        log.info(f"  📅 '{task_name}' | Due: {due_date} | Days: {days_until_due:+d}")

        # ── Due today → PIC personal WITH buttons ──────────────────────────────
        if days_until_due == 0:
            pic_names = get_pic_names(task)
            if not pic_names:
                log.warning(f"    ⚠️  No PIC — skipping")
                continue
            for pic in pic_names:
                wh = TEAM_WEBHOOKS.get(pic)
                if wh:
                    send_to_teams(wh, build_due_today_card(task, pic, due_str),
                                  label=f"{pic} / due today")
                    counts["due_today"] += 1
                else:
                    log.warning(f"    ⚠️  No webhook for '{pic}'")

        # ── 3 days → PIC personal WITH buttons ────────────────────────────────
        elif days_until_due == 3:
            pic_names = get_pic_names(task)
            if not pic_names:
                log.warning(f"    ⚠️  No PIC — skipping")
                continue
            for pic in pic_names:
                wh = TEAM_WEBHOOKS.get(pic)
                if wh:
                    send_to_teams(wh, build_three_day_reminder_card(task, pic, due_str),
                                  label=f"{pic} / 3 days")
                    counts["three_day"] += 1
                else:
                    log.warning(f"    ⚠️  No webhook for '{pic}'")

        # ── Overdue → PIC personal WITH buttons + Manager channel info only ───
        elif days_until_due < 0:
            current_status = task.get("status", {}).get("status", "").lower()

            if current_status in SUPPRESS_OVERDUE_FOR:
                log.info(f"    SKIP — already '{current_status}'")
                counts["skipped"] += 1
                continue

            days_overdue = abs(days_until_due)
            pic_names    = get_pic_names(task)

            # Send action card to PIC personally
            for pic in pic_names:
                wh = TEAM_WEBHOOKS.get(pic)
                if wh:
                    send_to_teams(wh, build_overdue_pic_card(task, pic, due_str, days_overdue),
                                  label=f"{pic} / overdue action")
                    counts["overdue_pic"] += 1
                else:
                    log.warning(f"    ⚠️  No webhook for '{pic}' — can't send overdue action card")

            # Send info-only card to manager channel
            if MANAGER_WEBHOOK:
                send_to_teams(MANAGER_WEBHOOK,
                              build_overdue_manager_card(task, pic_names, due_str, days_overdue),
                              label=f"manager / '{task_name}' info")
                counts["overdue_mgr"] += 1
            else:
                log.warning(f"    ⚠️  MANAGER_WEBHOOK not set")

    log.info(
        f"\nSummary → "
        f"{counts['due_today']} due-today (PIC)  |  "
        f"{counts['three_day']} 3-day (PIC)  |  "
        f"{counts['overdue_pic']} overdue action (PIC)  |  "
        f"{counts['overdue_mgr']} overdue info (manager)  |  "
        f"{counts['skipped']} skipped"
    )


# ─────────────────────────────────────────────
# FLASK SERVER — handles Action.OpenUrl callbacks
# ─────────────────────────────────────────────

app = Flask(__name__)

# ── HTML templates ────────────────────────────

_HTML_CONFIRM = """<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
  body{{font-family:sans-serif;display:flex;justify-content:center;align-items:center;
       height:100vh;margin:0;background:#f3f4f6;}}
  .box{{text-align:center;background:white;padding:2rem 3rem;border-radius:12px;
        box-shadow:0 2px 12px rgba(0,0,0,.1);max-width:360px;}}
  h2{{color:{color};margin:0 0 .5rem;}} p{{color:#6b7280;margin:.25rem 0;}}
</style>
<script>setTimeout(()=>window.close(),2500);</script>
</head><body><div class="box">
  <h2>{icon} {title}</h2>
  <p>{msg}</p>
  <p style="font-size:.8rem;color:#9ca3af;margin-top:.75rem">This window will close automatically.</p>
</div></body></html>"""

_HTML_ON_HOLD = """<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
  body{{font-family:sans-serif;display:flex;justify-content:center;align-items:center;
       height:100vh;margin:0;background:#f3f4f6;}}
  .box{{background:white;padding:2rem 3rem;border-radius:12px;
        box-shadow:0 2px 12px rgba(0,0,0,.1);width:380px;}}
  h2{{color:#f59e0b;margin:0 0 .75rem;}}
  textarea{{width:100%;height:100px;border:1px solid #d1d5db;border-radius:6px;
            padding:.5rem;font-size:.9rem;resize:vertical;box-sizing:border-box;}}
  button{{margin-top:.75rem;width:100%;padding:.65rem;background:#f59e0b;
          color:white;border:none;border-radius:6px;font-size:1rem;cursor:pointer;}}
  button:hover{{background:#d97706;}}
  #msg{{display:none;text-align:center;color:#10b981;margin-top:.75rem;font-weight:bold;}}
</style>
</head><body><div class="box">
  <h2>⏸️ Mark as On Hold</h2>
  <p style="color:#6b7280;margin-bottom:.5rem">Please provide a reason:</p>
  <textarea id="r" placeholder="e.g. Waiting on client data, blocked by another team..."></textarea>
  <button onclick="submit()">Submit On Hold</button>
  <p id="msg">✅ Done! ClickUp updated. Closing...</p>
</div>
<script>
function submit(){{
  const reason=document.getElementById('r').value.trim();
  if(!reason){{alert('Please enter a reason before submitting.');return;}}
  fetch('/do?task_id={task_id}&status=on+hold&reason='+encodeURIComponent(reason))
    .then(r=>r.text())
    .then(()=>{{
      document.querySelector('.box').innerHTML=
        '<h2 style="color:#f59e0b">⏸️ Task placed On Hold</h2>'
        +'<p style="color:#6b7280">Reason recorded as a ClickUp comment.</p>'
        +'<p style="font-size:.8rem;color:#9ca3af;margin-top:.5rem">This window will close automatically.</p>';
      setTimeout(()=>window.close(),2500);
    }});
}}
</script></body></html>"""

_STATUS_LABELS = {
    STATUS_COMPLETE:    ("✅", "Marked as Complete",   "Task closed in ClickUp.",              "#10b981"),
    STATUS_DELAYED:     ("⏩", "Marked as Delayed",    "Status updated in ClickUp.",            "#f59e0b"),
    STATUS_ON_HOLD:     ("⏸️","Marked as On Hold",     "Status updated and reason recorded.",   "#f59e0b"),
    STATUS_IN_PROGRESS: ("🔵", "Marked as In Progress","Status updated in ClickUp.",            "#3b82f6"),
}


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.datetime.now().isoformat()})


@app.route("/routes")
def list_routes():
    """
    Debug endpoint — visit in browser to confirm which version is deployed.
    https://clickup-teams-agent.onrender.com/routes
    """
    routes = sorted([str(r) for r in app.url_map.iter_rules()])
    return jsonify({
        "deployed": True,
        "callback_base_url": CALLBACK_BASE_URL,
        "routes": routes,
        "has_do_endpoint":      "/do" in routes,
        "has_on_hold_endpoint": "/on-hold" in routes,
        "has_action_endpoint":  "/action" in routes,
        "time": datetime.datetime.now().isoformat(),
    })


@app.route("/do", methods=["GET", "POST"])
def handle_do():
    """
    Main action endpoint called by Action.OpenUrl buttons.
    Reads task_id + status from query params, calls ClickUp API, returns confirmation HTML.
    """
    task_id    = request.args.get("task_id", "") or (request.get_json(silent=True) or {}).get("task_id", "")
    new_status = request.args.get("status",  "") or (request.get_json(silent=True) or {}).get("status", "")
    reason     = request.args.get("reason",  "") or (request.get_json(silent=True) or {}).get("reason", "")

    log.info(f"[/do] task_id={task_id!r} status={new_status!r} reason={reason[:50] if reason else ''}")

    if not task_id or not new_status:
        return "Missing task_id or status", 400

    try:
        update_task_status(task_id, new_status)
        if reason:
            post_comment(task_id, f"⏸️ **Task placed On Hold via Teams.**\nReason: {reason}")
    except requests.HTTPError as e:
        log.error(f"ClickUp error: {e}")
        return _HTML_CONFIRM.format(
            color="#ef4444", icon="❌", title="ClickUp Error",
            msg=f"API returned {e.response.status_code}. Please update ClickUp manually."
        ), 502

    icon, title, msg, color = _STATUS_LABELS.get(
        new_status.lower(),
        ("✅", f"Status → '{new_status}'", "ClickUp has been updated.", "#10b981")
    )
    return _HTML_CONFIRM.format(color=color, icon=icon, title=title, msg=msg)


@app.route("/on-hold")
def on_hold_page():
    """Opens the On Hold reason input page."""
    task_id = request.args.get("task_id", "")
    if not task_id:
        return "Missing task_id", 400
    return _HTML_ON_HOLD.format(task_id=task_id)


@app.route("/action", methods=["POST"])
def handle_action_legacy():
    """Legacy POST endpoint for backwards compatibility."""
    data       = request.get_json(force=True, silent=True) or {}
    task_id    = data.get("task_id")
    new_status = data.get("status", "").strip()
    reason     = data.get("reason", "").strip()
    if not task_id or not new_status:
        return jsonify({"error": "Missing task_id or status"}), 400
    try:
        update_task_status(task_id, new_status)
        if reason:
            post_comment(task_id, f"⏸️ **On Hold via Teams.**\nReason: {reason}")
        return jsonify({"ok": True, "task_id": task_id, "new_status": new_status})
    except requests.HTTPError as e:
        return jsonify({"error": str(e)}), 502


# ─────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"

    if mode == "--audit":
        audit_clickup_list()
    elif mode == "--serve":
        log.info(f"Starting server on port {SERVER_PORT}...")
        log.info(f"Registered routes: {[str(r) for r in app.url_map.iter_rules()]}")
        log.info(f"CALLBACK_BASE_URL = {CALLBACK_BASE_URL}")
        app.run(host="0.0.0.0", port=SERVER_PORT)
    elif mode == "--both":
        check_tasks()
        log.info(f"Starting server on port {SERVER_PORT}...")
        log.info(f"Registered routes: {[str(r) for r in app.url_map.iter_rules()]}")
        log.info(f"CALLBACK_BASE_URL = {CALLBACK_BASE_URL}")
        app.run(host="0.0.0.0", port=SERVER_PORT)
    elif mode == "--check":
        check_tasks()
    else:
        print(__doc__)
        sys.exit(1)