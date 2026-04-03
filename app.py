import json
import logging
import os
from datetime import datetime, timedelta

import requests
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, url_for

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yml")


def load_config():
    with open(CONFIG_FILE, "r") as f:
        return yaml.safe_load(f)


def load_state():
    os.makedirs(DATA_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        if state.get("date") != today:
            state = {"date": today, "confirmed": {}, "snoozed": {}}
            save_state(state)
        if "snoozed" not in state:
            state["snoozed"] = {}
    except (FileNotFoundError, json.JSONDecodeError):
        state = {"date": today, "confirmed": {}, "snoozed": {}}
        save_state(state)
    return state


def save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_effective_due_time(reminder, state):
    now = datetime.now()
    reminder_id = str(reminder["id"])
    snoozed_until = state.get("snoozed", {}).get(reminder_id)
    if snoozed_until:
        return datetime.strptime(snoozed_until, "%H:%M").replace(
            year=now.year, month=now.month, day=now.day
        )
    return datetime.strptime(reminder["time"], "%H:%M").replace(
        year=now.year, month=now.month, day=now.day
    )


def get_reminder_status(reminder, state):
    reminder_id = str(reminder["id"])
    if state["confirmed"].get(reminder_id):
        return "confirmed"
    now = datetime.now()
    due_time = get_effective_due_time(reminder, state)
    if now >= due_time:
        return "due"
    return "pending"


def get_reminders_with_status():
    config = load_config()
    state = load_state()
    reminders = []
    for r in config["reminders"]:
        reminder_id = str(r["id"])
        status = get_reminder_status(r, state)
        snoozed_until = state.get("snoozed", {}).get(reminder_id)
        reminders.append(
            {
                "id": reminder_id,
                "text": r["text"],
                "time": r["time"],
                "icon": r["icon"],
                "status": status,
                "snoozed_until": snoozed_until,
            }
        )
    return reminders


def confirm_reminder(reminder_id):
    config = load_config()
    valid_ids = {str(r["id"]) for r in config["reminders"]}
    if reminder_id not in valid_ids:
        return None
    state = load_state()
    state["confirmed"][reminder_id] = True
    state.get("snoozed", {}).pop(reminder_id, None)
    save_state(state)
    for r in config["reminders"]:
        if str(r["id"]) == reminder_id:
            return {
                "id": str(r["id"]),
                "text": r["text"],
                "time": r["time"],
                "icon": r["icon"],
                "status": "confirmed",
            }
    return None


def unconfirm_reminder(reminder_id):
    config = load_config()
    valid_ids = {str(r["id"]) for r in config["reminders"]}
    if reminder_id not in valid_ids:
        return None
    state = load_state()
    state["confirmed"].pop(reminder_id, None)
    save_state(state)
    for r in config["reminders"]:
        if str(r["id"]) == reminder_id:
            status = get_reminder_status(r, state)
            return {
                "id": str(r["id"]),
                "text": r["text"],
                "time": r["time"],
                "icon": r["icon"],
                "status": status,
                "snoozed_until": state.get("snoozed", {}).get(str(r["id"])),
            }
    return None


def snooze_reminder(reminder_id):
    config = load_config()
    valid_ids = {str(r["id"]) for r in config["reminders"]}
    if reminder_id not in valid_ids:
        return None
    state = load_state()
    reminder = next(r for r in config["reminders"] if str(r["id"]) == reminder_id)
    effective_due = get_effective_due_time(reminder, state)
    snoozed_until = (effective_due + timedelta(hours=1)).strftime("%H:%M")
    state.setdefault("snoozed", {})[reminder_id] = snoozed_until
    save_state(state)
    for r in config["reminders"]:
        if str(r["id"]) == reminder_id:
            return {
                "id": str(r["id"]),
                "text": r["text"],
                "time": r["time"],
                "icon": r["icon"],
                "status": "pending",
                "snoozed_until": snoozed_until,
            }
    return None


def snooze_all_due_reminders():
    reminders = get_reminders_with_status()
    snoozed = []
    for r in reminders:
        if r["status"] == "due":
            result = snooze_reminder(r["id"])
            snoozed.append((r["text"], result["snoozed_until"]))
    return snoozed


def send_signal_message(message):
    api_url = os.environ["SIGNAL_API_URL"]
    sender = os.environ["SIGNAL_SENDER"]
    recipient = os.environ["SIGNAL_RECIPIENT"]
    resp = requests.post(
        f"{api_url}/v2/send",
        json={
            "message": message,
            "text_mode": "styled",
            "number": sender,
            "recipients": [recipient],
        },
    )
    resp.raise_for_status()
    logger.info("Signal message sent.")


def check_due_reminders():
    reminders = get_reminders_with_status()
    due = [r for r in reminders if r["status"] == "due"]
    if due:
        app_domain = os.environ.get("APP_DOMAIN", "http://localhost:8080")
        lines = [
            f"- {r['text']} - {app_domain}/confirm/{r['id']}"
            for r in due
        ]
        message = "Have you completed:\n" + "\n".join(lines)
        send_signal_message(message)


def confirm_all_due_reminders():
    reminders = get_reminders_with_status()
    confirmed = []
    for r in reminders:
        if r["status"] == "due":
            confirm_reminder(r["id"])
            confirmed.append(r["text"])
    return confirmed


def check_signal_messages():
    api_url = os.environ["SIGNAL_API_URL"]
    sender = os.environ["SIGNAL_SENDER"]
    try:
        resp = requests.get(f"{api_url}/v1/receive/{sender}")
        resp.raise_for_status()
        messages = resp.json()
    except Exception:
        logger.exception("Failed to check Signal messages")
        return

    for msg in messages:
        envelope = msg.get("envelope", {})
        # Handle both direct messages (dataMessage) and synced sent messages (syncMessage)
        data_body = (envelope.get("dataMessage") or {}).get("message") or ""
        sync_body = ((envelope.get("syncMessage") or {}).get("sentMessage") or {}).get("message") or ""
        body = (data_body or sync_body).strip().lower()
        if not body:
            continue
        if "confirm" in body:
            confirmed = confirm_all_due_reminders()
            if confirmed:
                reply = "Confirmed:\n" + "\n".join(
                    f"- {t}" for t in confirmed
                )
            else:
                reply = "No due reminders to confirm."
            send_signal_message(reply)
        elif "snooze" in body:
            snoozed = snooze_all_due_reminders()
            if snoozed:
                reply = "Snoozed:\n" + "\n".join(
                    f"- {text} (due {time})" for text, time in snoozed
                )
            else:
                reply = "No due reminders to snooze."
            send_signal_message(reply)


def reset_reminders():
    today = datetime.now().strftime("%Y-%m-%d")
    save_state({"date": today, "confirmed": {}})
    logger.info("All reminders reset for %s", today)


# --- Routes ---


@app.route("/")
def index():
    reminders = get_reminders_with_status()
    return render_template("index.html", reminders=reminders)


@app.route("/confirm/<reminder_id>")
def confirm_direct(reminder_id):
    result = confirm_reminder(reminder_id)
    if result is None:
        return "Reminder not found", 404
    return redirect(url_for("index"))


@app.route("/api/reminders")
def api_get_reminders():
    return jsonify(get_reminders_with_status())


@app.route("/api/reminders/<reminder_id>/confirm", methods=["POST"])
def api_confirm_reminder(reminder_id):
    result = confirm_reminder(reminder_id)
    if result is None:
        return jsonify({"error": "Reminder not found"}), 404
    return jsonify(result)


@app.route("/api/reminders/<reminder_id>/unconfirm", methods=["POST"])
def api_unconfirm_reminder(reminder_id):
    result = unconfirm_reminder(reminder_id)
    if result is None:
        return jsonify({"error": "Reminder not found"}), 404
    return jsonify(result)


@app.route("/snooze/<reminder_id>")
def snooze_direct(reminder_id):
    result = snooze_reminder(reminder_id)
    if result is None:
        return "Reminder not found", 404
    return redirect(url_for("index"))


@app.route("/api/reminders/<reminder_id>/snooze", methods=["POST"])
def api_snooze_reminder(reminder_id):
    result = snooze_reminder(reminder_id)
    if result is None:
        return jsonify({"error": "Reminder not found"}), 404
    return jsonify(result)


# --- Scheduler ---

scheduler = BackgroundScheduler()
scheduler.add_job(reset_reminders, "cron", hour=0, minute=0, id="midnight_reset")
scheduler.add_job(check_due_reminders, "cron", hour="*", id="hourly_check")
scheduler.add_job(check_signal_messages, "interval", minutes=1, id="signal_check")

if __name__ == "__main__":
    scheduler.start()
    try:
        app.run(host="0.0.0.0", port=8080, debug=False)
    finally:
        scheduler.shutdown()
