import requests
import datetime
import json

# --- CONFIGURATION ---
API_TOKEN = "pk_88306048_KSCJW7VB9VIXYCXOTASQ008WGYB7B8O0"
LIST_ID = "901416094383"

# Map the ClickUp PIC names to their individual Teams Webhook URLs
TEAM_WEBHOOKS = {
    "Lik Ming": "https://default3a7635f01d1e4df58e24716c29905a.57.environment.api.powerplatform.com:443/powerautomate/automations/direct/workflows/e82f10c0c2884391a946f9eec11c544c/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=dwtYWtJo53xaMOdreKZ-g5_x_xFgeoP-z2BCDpi6p4w",
    "Jia Ying": "",
    "Daron": "https://default3a7635f01d1e4df58e24716c29905a.57.environment.api.powerplatform.com:443/powerautomate/automations/direct/workflows/a5ce6536cade4756804b97ff840741a7/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=RwzDMYmjzhCZ6XZqv_aN8fRgmPkTXslkpCedJWu4Tlg",
    "Jee": "",
    "Sarah": "",
    "Fatin": "",
    "Yung Zheng": "https://default3a7635f01d1e4df58e24716c29905a.57.environment.api.powerplatform.com:443/powerautomate/automations/direct/workflows/2a04fbf8bc2244c0834c30030b5a33d0/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=HlCXtJ0NGGZdRV-Ff95pN9_uiWYQ1RN-2c7fkLX_jAo",
}

def send_teams_alert(pic_name, task_name, due_date_str, webhook_url):
    """Sends a formatted Adaptive Card to a specific standard Teams Webhook."""
    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": "⚠️ ClickUp Reminder: 3 Days Left",
                            "weight": "Bolder",
                            "size": "Large",
                            "color": "Attention"
                        },
                        {
                            "type": "TextBlock",
                            "text": f"**Project:** {task_name}",
                            "wrap": True,
                            "spacing": "Small"
                        },
                        {
                            "type": "TextBlock",
                            "text": f"Hi **{pic_name}**, \n\nThis is an automated reminder that your project is due on **{due_date_str}**. \n\nPlease update the ClickUp board if there are any technical or operational blockers.",
                            "wrap": True,
                            "spacing": "Medium"
                        }
                    ]
                }
            }
        ]
    }
    
    headers = {'Content-Type': 'application/json'}
    
    try:
        response = requests.post(webhook_url, data=json.dumps(payload), headers=headers)
        if response.status_code in [200, 202]:
            print(f"✅ Alert successfully delivered to {pic_name} for task: {task_name}")
        else:
            print(f"❌ Failed to send alert to {pic_name}. Status code: {response.status_code}, Error: {response.text}")
    except Exception as e:
        print(f"❌ Network error when sending to {pic_name}: {e}")

def check_tasks():
    url = f"https://api.clickup.com/api/v2/list/{LIST_ID}/task"
    headers = {"Authorization": API_TOKEN}
    
    # Check all active statuses
    params = {"statuses[]": ["to do", "in progress", "delayed", "pending data", "analysis", "reviewing insights"]}
    
    response = requests.get(url, headers=headers, params=params)
    tasks = response.json().get('tasks', [])
    today = datetime.datetime.now().date()
    
    print(f"\n🔍 Found {len(tasks)} active tasks in the List. Checking dates...\n")

    for task in tasks:
        task_name = task.get('name')
        due_date_ms = task.get('due_date')
        
        if not due_date_ms:
            continue
            
        due_date = datetime.datetime.fromtimestamp(int(due_date_ms) / 1000).date()
        days_until_due = (due_date - today).days
        
        # Debugging output to monitor progress
        print(f" 🗓️ Task: '{task_name}' | Due: {due_date} | Days left: {days_until_due}")
        
        if days_until_due == 3:
            pic_names = []
            
            for field in task.get('custom_fields', []):
                if "PIC (Core Consulting)" in field.get('name', ''):
                    val = field.get('value')
                    
                    if val is None:
                        continue
                        
                    if isinstance(val, list):
                        selected_ids = [str(v) for v in val]
                    else:
                        selected_ids = [str(val)]
                        
                    if 'type_config' in field and 'options' in field['type_config']:
                        for option in field['type_config']['options']:
                            opt_id = str(option.get('id'))
                            opt_order = str(option.get('orderindex'))
                            
                            if opt_id in selected_ids or opt_order in selected_ids:
                                name = option.get('name') or option.get('label') or option.get('title')
                                pic_names.append(name)
            
            if pic_names:
                for pic_name in pic_names:
                    # Dynamically route to the correct webhook based on the dictionary
                    target_webhook = TEAM_WEBHOOKS.get(pic_name)
                    
                    if target_webhook:
                        print(f"  👤 Routing alert to {pic_name}'s personal webhook...")
                        due_str = due_date.strftime("%Y-%m-%d")
                        send_teams_alert(pic_name, task_name, due_str, target_webhook)
                    else:
                        print(f"  ⚠️ Skipping {pic_name}: No webhook URL configured in TEAM_WEBHOOKS dictionary.")
            else:
                print(f"  ❌ IGNORING '{task_name}': No PIC assigned (or parsing failed).")

if __name__ == "__main__":
    print(f"Starting Daily ClickUp Check at {datetime.datetime.now()}...")
    check_tasks()