"""
ClickUp Teams Agent
====================
A two-way bridge between ClickUp and Microsoft Teams.
Team members interact via Adaptive Cards — no ClickUp login needed.

Flows:
  - Due today   → PIC gets a card: "Mark Delayed" / "Mark On Hold"
  - 3 days left → PIC gets a reminder card with a "Confirm Details" form
  - Overdue     → Manager gets a card with "Mark Complete" / "Reassign"
  - Flask server receives button clicks and updates ClickUp via API

Usage:
  python agent.py          # Run the daily check once
  python agent.py --serve  # Start the Flask callback server
  python agent.py --both   # Run check + keep server alive (recommended for cron+server combo)
"""

import requests
import datetime
import json
import sys
import threading
import logging
from flask import Flask, request, jsonify
import os  # Make sure to import os at the top of agent.py

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "your_local_backup_token_if_needed")
LIST_ID     = "901416094383"
SERVER_PORT = 5000

# Public URL where this Flask server is reachable from Teams/Power Automate
# Replace with your ngrok URL during dev, or your deployed server URL in prod
# e.g. "https://abc123.ngrok-free.app" or "https://your-app.onrender.com"   
CALLBACK_BASE_URL = "https://clickup-teams-agent.onrender.com"

# Manager webhook — receives overdue alerts
MANAGER_WEBHOOK = "https://default3a7635f01d1e4df58e24716c29905a.57.environment.api.powerplatform.com:443/powerautomate/automations/direct/workflows/95ee4607778e46d69abbe4bf90d9be84/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=HwiRkpl4OdkScYKs083Xle2IR1E2XNnVIMND9z86ZPE"

# PIC name → their personal Teams webhook URL
TEAM_WEBHOOKS = {
    "Lik Ming":   "https://default3a7635f01d1e4df58e24716c29905a.57.environment.api.powerplatform.com:443/powerautomate/automations/direct/workflows/e82f10c0c2884391a946f9eec11c544c/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=dwtYWtJo53xaMOdreKZ-g5_x_xFgeoP-z2BCDpi6p4w",
    "Jia Ying":   "",
    "Daron":      "https://default3a7635f01d1e4df58e24716c29905a.57.environment.api.powerplatform.com:443/powerautomate/automations/direct/workflows/a5ce6536cade4756804b97ff840741a7/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=RwzDMYmjzhCZ6XZqv_aN8fRgmPkTXslkpCedJWu4Tlg",
    "Jee":        "",
    "Sarah":      "",
    "Fatin":      "",
    "Yung Zheng": "https://default3a7635f01d1e4df58e24716c29905a.57.environment.api.powerplatform.com:443/powerautomate/automations/direct/workflows/2a04fbf8bc2244c0834c30030b5a33d0/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=HlCXtJ0NGGZdRV-Ff95pN9_uiWYQ1RN-2c7fkLX_jAo",
}

# Active task statuses to monitor
ACTIVE_STATUSES = [
    "to do", "in progress", "delayed",
    "pending data", "analysis", "reviewing insights"
]

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
@app.route("/daily-check", methods=["GET"])
def trigger_check():
    check_tasks()
    return "Check complete", 200

def clickup_headers():
    return {"Authorization": API_TOKEN, "Content-Type": "application/json"}


def get_tasks():
    """Fetch all active tasks from the configured ClickUp list."""
    url = f"https://api.clickup.com/api/v2/list/{LIST_ID}/task"
    params = {"statuses[]": ACTIVE_STATUSES, "include_closed": "false"}
    resp = requests.get(url, headers={"Authorization": API_TOKEN}, params=params)
    resp.raise_for_status()
    return resp.json().get("tasks", [])


def update_task_status(task_id: str, new_status: str):
    """Update a task's status in ClickUp."""
    url = f"https://api.clickup.com/api/v2/task/{task_id}"
    resp = requests.put(url, headers=clickup_headers(), json={"status": new_status})
    resp.raise_for_status()
    log.info(f"✅ Task {task_id} status → '{new_status}'")
    return resp.json()


def update_task_fields(task_id: str, fields: dict):
    """
    Update standard fields on a task (due_date, start_date, etc.)
    fields example: {"due_date": 1720000000000, "start_date": 1719000000000}
    """
    url = f"https://api.clickup.com/api/v2/task/{task_id}"
    resp = requests.put(url, headers=clickup_headers(), json=fields)
    resp.raise_for_status()
    log.info(f"✅ Task {task_id} fields updated: {list(fields.keys())}")
    return resp.json()


def update_custom_field(task_id: str, field_id: str, value):
    """Update a custom field value on a task."""
    url = f"https://api.clickup.com/api/v2/task/{task_id}/field/{field_id}"
    resp = requests.post(url, headers=clickup_headers(), json={"value": value})
    resp.raise_for_status()
    log.info(f"✅ Task {task_id} custom field {field_id} updated")
    return resp.json()


def get_pic_names(task: dict) -> list[str]:
    """Extract PIC names from a task's custom fields."""
    pic_names = []
    for field in task.get("custom_fields", []):
        if "PIC (Core Consulting)" not in field.get("name", ""):
            continue
        val = field.get("value")
        if val is None:
            continue
        selected_ids = [str(v) for v in val] if isinstance(val, list) else [str(val)]
        options = field.get("type_config", {}).get("options", [])
        for opt in options:
            if str(opt.get("id")) in selected_ids or str(opt.get("orderindex")) in selected_ids:
                name = opt.get("name") or opt.get("label") or opt.get("title")
                if name:
                    pic_names.append(name)
    return pic_names

# ─────────────────────────────────────────────
# ADAPTIVE CARD BUILDERS
# ─────────────────────────────────────────────

def _wrap_card(card_content: dict) -> dict:
    """Wrap an Adaptive Card body in the Teams message envelope."""
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


def build_due_today_card(task_id: str, task_name: str, pic_name: str, due_date_str: str) -> dict:
    """
    Card sent to PIC when a task is due today.
    Buttons: Mark Delayed | Mark On Hold | Already Done
    """
    callback_url = f"{CALLBACK_BASE_URL}/action"
    return _wrap_card({
        "body": [
            {
                "type": "TextBlock",
                "text": "🔔 Task Due Today",
                "weight": "Bolder",
                "size": "Large",
                "color": "Warning",
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Project", "value": task_name},
                    {"title": "Due",     "value": due_date_str},
                    {"title": "PIC",     "value": pic_name},
                ]
            },
            {
                "type": "TextBlock",
                "text": f"Hi **{pic_name}**, this task is due today. Please update the status below — no ClickUp login needed.",
                "wrap": True,
                "spacing": "Medium",
            }
        ],
        "actions": [
            {
                "type": "Action.Http",
                "title": "Mark as Delayed",
                "method": "POST",
                "url": callback_url,
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "body": json.dumps({"task_id": task_id, "action": "update_status", "status": "delayed"}),
            },
            {
                "type": "Action.Http",
                "title": "Mark as On Hold",
                "method": "POST",
                "url": callback_url,
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "body": json.dumps({"task_id": task_id, "action": "update_status", "status": "on hold"}),
            },
            {
                "type": "Action.Http",
                "title": "✅ Already Done",
                "method": "POST",
                "url": callback_url,
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "body": json.dumps({"task_id": task_id, "action": "update_status", "status": "complete"}),
            },
        ]
    })


def build_three_day_reminder_card(task_id: str, task_name: str, pic_name: str, due_date_str: str) -> dict:
    """
    Card sent to PIC 3 days before due date.
    Includes a form to confirm/update: start date, task type, account name.
    Button: Confirm & Update ClickUp
    """
    callback_url = f"{CALLBACK_BASE_URL}/confirm_details"
    return _wrap_card({
        "body": [
            {
                "type": "TextBlock",
                "text": "⚠️ 3 Days Until Due",
                "weight": "Bolder",
                "size": "Large",
                "color": "Attention",
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Project", "value": task_name},
                    {"title": "Due",     "value": due_date_str},
                    {"title": "PIC",     "value": pic_name},
                ]
            },
            {
                "type": "TextBlock",
                "text": f"Hi **{pic_name}**, please confirm the project details below. This will update ClickUp automatically.",
                "wrap": True,
                "spacing": "Medium",
            },
            {
                "type": "TextBlock",
                "text": "Start Date (YYYY-MM-DD)",
                "weight": "Bolder",
                "spacing": "Medium",
            },
            {
                "type": "Input.Text",
                "id": "start_date",
                "placeholder": "e.g. 2025-06-01",
            },
            {
                "type": "TextBlock",
                "text": "Task Type",
                "weight": "Bolder",
                "spacing": "Small",
            },
            {
                "type": "Input.ChoiceSet",
                "id": "task_type",
                "style": "compact",
                "choices": [
                    {"title": "Analysis",    "value": "analysis"},
                    {"title": "Consulting",  "value": "consulting"},
                    {"title": "Research",    "value": "research"},
                    {"title": "Operations",  "value": "operations"},
                    {"title": "Other",       "value": "other"},
                ]
            },
            {
                "type": "TextBlock",
                "text": "Account / Client Name",
                "weight": "Bolder",
                "spacing": "Small",
            },
            {
                "type": "Input.Text",
                "id": "account_name",
                "placeholder": "e.g. Acme Corp",
            },
            {
                "type": "TextBlock",
                "text": "Any blockers? (optional)",
                "weight": "Bolder",
                "spacing": "Small",
            },
            {
                "type": "Input.Text",
                "id": "blockers",
                "placeholder": "Describe any blockers...",
                "isMultiline": True,
            },
        ],
        "actions": [
            {
                "type": "Action.Http",
                "title": "Confirm & Update ClickUp",
                "method": "POST",
                "url": callback_url,
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "body": json.dumps({
                    "task_id":      task_id,
                    "action":       "confirm_details",
                    "start_date":   "{{start_date.value}}",
                    "task_type":    "{{task_type.value}}",
                    "account_name": "{{account_name.value}}",
                    "blockers":     "{{blockers.value}}",
                }),
            }
        ]
    })


def build_overdue_manager_card(task_id: str, task_name: str, pic_names: list, due_date_str: str, days_overdue: int) -> dict:
    """
    Card sent to manager when a task is overdue.
    Buttons: Mark Complete | Mark Delayed | Reassign (opens form)
    """
    callback_url = f"{CALLBACK_BASE_URL}/action"
    pic_display = ", ".join(pic_names) if pic_names else "Unassigned"

    reassign_choices = [
        {"title": name, "value": name}
        for name in TEAM_WEBHOOKS.keys()
    ]

    return _wrap_card({
        "body": [
            {
                "type": "TextBlock",
                "text": f"🚨 Task Overdue by {days_overdue} day{'s' if days_overdue != 1 else ''}",
                "weight": "Bolder",
                "size": "Large",
                "color": "Attention",
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Project",     "value": task_name},
                    {"title": "Was Due",     "value": due_date_str},
                    {"title": "PIC",         "value": pic_display},
                    {"title": "Days Overdue","value": str(days_overdue)},
                ]
            },
            {
                "type": "TextBlock",
                "text": "Please take one of the actions below to resolve this task:",
                "wrap": True,
                "spacing": "Medium",
            },
            {
                "type": "TextBlock",
                "text": "Or reassign to:",
                "weight": "Bolder",
                "spacing": "Medium",
            },
            {
                "type": "Input.ChoiceSet",
                "id": "reassign_to",
                "style": "compact",
                "placeholder": "Select team member",
                "choices": reassign_choices,
            },
        ],
        "actions": [
            {
                "type": "Action.Http",
                "title": "✅ Mark as Complete",
                "method": "POST",
                "url": callback_url,
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "body": json.dumps({"task_id": task_id, "action": "update_status", "status": "complete"}),
            },
            {
                "type": "Action.Http",
                "title": "Mark as Delayed",
                "method": "POST",
                "url": callback_url,
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "body": json.dumps({"task_id": task_id, "action": "update_status", "status": "delayed"}),
            },
            {
                "type": "Action.Http",
                "title": "Reassign",
                "method": "POST",
                "url": f"{CALLBACK_BASE_URL}/reassign",
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "body": json.dumps({
                    "task_id":     task_id,
                    "action":      "reassign",
                    "reassign_to": "{{reassign_to.value}}",
                }),
            },
        ]
    })

# ─────────────────────────────────────────────
# TEAMS SENDER
# ─────────────────────────────────────────────

def send_to_teams(webhook_url: str, payload: dict, label: str = ""):
    """POST an Adaptive Card payload to a Teams webhook."""
    headers = {"Content-Type": "application/json"}
    try:
        resp = requests.post(webhook_url, data=json.dumps(payload), headers=headers, timeout=10)
        if resp.status_code in [200, 202]:
            log.info(f"✅ Card delivered{' → ' + label if label else ''}")
        else:
            log.error(f"❌ Card failed{' → ' + label if label else ''}: {resp.status_code} {resp.text[:200]}")
    except requests.exceptions.RequestException as e:
        log.error(f"❌ Network error sending card{' → ' + label if label else ''}: {e}")

# ─────────────────────────────────────────────
# DAILY CHECK
# ─────────────────────────────────────────────

def check_tasks():
    """Main daily check — fetches tasks and routes cards based on due date."""
    log.info(f"Starting daily ClickUp check...")
    tasks = get_tasks()
    today = datetime.datetime.now().date()
    log.info(f"Found {len(tasks)} active tasks. Evaluating due dates...\n")

    counts = {"due_today": 0, "three_day": 0, "overdue": 0, "skipped": 0}

    for task in tasks:
        task_id   = task.get("id")
        task_name = task.get("name", "(unnamed)")
        due_ms    = task.get("due_date")

        if not due_ms:
            log.debug(f"  SKIP '{task_name}': no due date")
            counts["skipped"] += 1
            continue

        due_date      = datetime.datetime.fromtimestamp(int(due_ms) / 1000).date()
        days_until_due = (due_date - today).days
        due_str       = due_date.strftime("%d %b %Y")

        log.info(f"  📅 '{task_name}' | Due: {due_date} | Days: {days_until_due:+d}")

        # ── Due today ──────────────────────────────
        if days_until_due == 0:
            pic_names = get_pic_names(task)
            if not pic_names:
                log.warning(f"    ⚠️  No PIC found for '{task_name}' — skipping due-today card")
                continue
            for pic_name in pic_names:
                webhook = TEAM_WEBHOOKS.get(pic_name)
                if webhook:
                    card = build_due_today_card(task_id, task_name, pic_name, due_str)
                    send_to_teams(webhook, card, label=f"{pic_name} / due today")
                    counts["due_today"] += 1
                else:
                    log.warning(f"    ⚠️  No webhook for {pic_name}")

        # ── 3 days left ────────────────────────────
        elif days_until_due == 3:
            pic_names = get_pic_names(task)
            if not pic_names:
                log.warning(f"    ⚠️  No PIC found for '{task_name}' — skipping 3-day reminder")
                continue
            for pic_name in pic_names:
                webhook = TEAM_WEBHOOKS.get(pic_name)
                if webhook:
                    card = build_three_day_reminder_card(task_id, task_name, pic_name, due_str)
                    send_to_teams(webhook, card, label=f"{pic_name} / 3 days")
                    counts["three_day"] += 1
                else:
                    log.warning(f"    ⚠️  No webhook for {pic_name}")

        # ── Overdue ────────────────────────────────
        elif days_until_due < 0:
            days_overdue = abs(days_until_due)
            pic_names    = get_pic_names(task)
            if MANAGER_WEBHOOK:
                card = build_overdue_manager_card(task_id, task_name, pic_names, due_str, days_overdue)
                send_to_teams(MANAGER_WEBHOOK, card, label=f"manager / '{task_name}' overdue")
                counts["overdue"] += 1
            else:
                log.warning(f"    ⚠️  MANAGER_WEBHOOK not set — skipping overdue card for '{task_name}'")

    log.info(
        f"\nSummary: {counts['due_today']} due-today, "
        f"{counts['three_day']} 3-day reminders, "
        f"{counts['overdue']} overdue alerts, "
        f"{counts['skipped']} skipped (no due date)"
    )

# ─────────────────────────────────────────────
# FLASK CALLBACK SERVER
# ─────────────────────────────────────────────

app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok", "time": datetime.datetime.now().isoformat()})


@app.route("/action", methods=["POST"])
def handle_action():
    """
    Handles button clicks from Teams cards.
    Expected body: { "task_id": "...", "action": "update_status", "status": "..." }
    """
    data = request.get_json(force=True, silent=True) or {}
    log.info(f"[/action] received: {data}")

    task_id = data.get("task_id")
    action  = data.get("action")

    if not task_id or not action:
        return jsonify({"error": "Missing task_id or action"}), 400

    if action == "update_status":
        new_status = data.get("status")
        if not new_status:
            return jsonify({"error": "Missing status"}), 400
        try:
            update_task_status(task_id, new_status)
            return jsonify({"ok": True, "task_id": task_id, "new_status": new_status})
        except requests.HTTPError as e:
            log.error(f"ClickUp API error: {e}")
            return jsonify({"error": str(e)}), 502

    return jsonify({"error": f"Unknown action: {action}"}), 400


@app.route("/confirm_details", methods=["POST"])
def handle_confirm_details():
    """
    Handles the 3-day reminder form submission.
    Expected body: { task_id, start_date, task_type, account_name, blockers }
    Updates ClickUp fields accordingly.
    """
    data = request.get_json(force=True, silent=True) or {}
    log.info(f"[/confirm_details] received: {data}")

    task_id      = data.get("task_id")
    start_date   = data.get("start_date", "").strip()
    task_type    = data.get("task_type", "").strip()
    account_name = data.get("account_name", "").strip()
    blockers     = data.get("blockers", "").strip()

    if not task_id:
        return jsonify({"error": "Missing task_id"}), 400

    updates = {}

    # Convert start_date string → millisecond timestamp for ClickUp
    if start_date:
        try:
            dt = datetime.datetime.strptime(start_date, "%Y-%m-%d")
            updates["start_date"] = int(dt.timestamp() * 1000)
        except ValueError:
            log.warning(f"Invalid start_date format: '{start_date}' — skipping")

    # Add a comment to the task with the confirmed details
    comment_lines = ["✅ Details confirmed via Teams:"]
    if task_type:    comment_lines.append(f"• Task type: {task_type}")
    if account_name: comment_lines.append(f"• Account: {account_name}")
    if blockers:     comment_lines.append(f"• Blockers: {blockers}")
    if start_date:   comment_lines.append(f"• Start date: {start_date}")

    try:
        if updates:
            update_task_fields(task_id, updates)

        # Post a comment with all confirmed details
        comment_url = f"https://api.clickup.com/api/v2/task/{task_id}/comment"
        requests.post(
            comment_url,
            headers=clickup_headers(),
            json={"comment_text": "\n".join(comment_lines)},
        )
        log.info(f"✅ Details confirmed and comment posted for task {task_id}")

        return jsonify({
            "ok":           True,
            "task_id":      task_id,
            "fields_updated": list(updates.keys()),
            "comment_posted": True,
        })

    except requests.HTTPError as e:
        log.error(f"ClickUp API error in /confirm_details: {e}")
        return jsonify({"error": str(e)}), 502


@app.route("/reassign", methods=["POST"])
def handle_reassign():
    """
    Handles reassignment from the manager overdue card.
    Posts a comment on the task tagging the new assignee.
    (Full reassign via API requires a ClickUp user ID — this uses a comment as a workaround.)
    """
    data = request.get_json(force=True, silent=True) or {}
    log.info(f"[/reassign] received: {data}")

    task_id     = data.get("task_id")
    reassign_to = data.get("reassign_to", "").strip()

    if not task_id or not reassign_to:
        return jsonify({"error": "Missing task_id or reassign_to"}), 400

    comment_text = f"🔄 Manager action (via Teams): Task reassigned to **{reassign_to}**. Please update ClickUp accordingly."

    try:
        comment_url = f"https://api.clickup.com/api/v2/task/{task_id}/comment"
        requests.post(
            comment_url,
            headers=clickup_headers(),
            json={"comment_text": comment_text},
        )
        log.info(f"✅ Reassignment comment posted on task {task_id} → {reassign_to}")
        return jsonify({"ok": True, "task_id": task_id, "reassigned_to": reassign_to})

    except requests.HTTPError as e:
        log.error(f"ClickUp API error in /reassign: {e}")
        return jsonify({"error": str(e)}), 502


# ─────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"

    if mode == "--serve":
        log.info(f"Starting callback server on port {SERVER_PORT}...")
        app.run(host="0.0.0.0", port=SERVER_PORT)

    elif mode == "--both":
        # Run the daily check first, then keep the Flask server alive
        log.info("Running daily check then starting server...")
        check_tasks()
        log.info(f"Starting callback server on port {SERVER_PORT}...")
        app.run(host="0.0.0.0", port=SERVER_PORT)

    elif mode == "--check":
        check_tasks()

    else:
        print(__doc__)
        sys.exit(1)