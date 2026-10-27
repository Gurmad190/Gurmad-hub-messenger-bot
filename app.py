"""
Gurmad Messenger Bot - starter version (NOT live yet).

This is the Messenger version of the Gurmad bot, NOT the WhatsApp bot.
Messenger and WhatsApp are separate Meta products with separate
webhooks, tokens, and test flows, even though they share a Meta app.

What this is:
  A small Flask server for the Facebook Messenger API (Send API).
  It answers FAQs, shows a numbered menu, collects inquiries, and
  hands over to a human. If no AI key is set, it uses the FAQ
  knowledge base only, so it never invents prices or dates.

Profiles:
  BOT_PROFILE=hub     -> Gurmad Educational Hub (the chosen Page)
  BOT_PROFILE=gurmad  -> Gurmad Technology demo
  BOT_PROFILE=school  -> Reusable template for a school in Garowe
                        (fill in knowledge/school.json first)

To go live you still need, with Yasin's approval:
  1. A Facebook Page to attach the bot to (create one, or use a
     Page you already manage)
  2. A Meta app with the Messenger product, subscribed to that Page
  3. A Page Access Token (never share it, never put it in code)
  4. This server on a public HTTPS URL, with the webhook verified
  5. App Review / permissions if Meta requires them for your use case

No secrets are stored in this file. Put them in a .env file.
"""

import json
import os
import re
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "gurmad-messenger-verify-change-me")
PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
BOT_PROFILE = os.environ.get("BOT_PROFILE", "gurmad").strip().lower()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data", "inquiries.jsonl")

sessions = {}

SOMALI_WORDS = {
    "salaam", "salaamu", "sidee", "qiimaha", "qiimo", "koorso",
    "koorsooyin", "isdiiwaan", "diiwaan", "galin", "halka",
    "xagee", "saacad", "saacadaha", "adeeg", "adeegyo",
    "caawimo", "qof", "shaqaale", "lacag", "kharash",
    "dugsiga", "iskuul", "arday", "waalid",
}


def load_knowledge():
    profile = BOT_PROFILE if BOT_PROFILE in ("gurmad", "school", "hub") else "gurmad"
    path = os.path.join(BASE_DIR, "knowledge", f"{profile}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


KNOWLEDGE = load_knowledge()


def detect_language(text):
    words = set(re.findall(r"[a-z]+", text.lower()))
    return "so" if words & SOMALI_WORDS else "en"


def save_inquiry(sender, data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    record = {
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "profile": BOT_PROFILE,
        "channel": "messenger",
        "sender_psid": sender,
        "name": data.get("name", ""),
        "topic": data.get("topic", ""),
        "details": data.get("details", ""),
        "human_handover": bool(data.get("human_handover")),
    }
    with open(DATA_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def send_messenger(recipient_id, text):
    """Send a text message with Menu / Talk to human quick replies.

    In demo mode (no token) just log it, so local tests never send
    anything to a real person.
    """
    if not PAGE_ACCESS_TOKEN:
        print(f"[DEMO - not sent to {recipient_id}] {text}")
        return {"demo": True}
    url = "https://graph.facebook.com/v19.0/me/messages"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {
            "text": text[:2000],
            "quick_replies": [
                {"content_type": "text", "title": "Menu", "payload": "MENU"},
                {"content_type": "text", "title": "Talk to human", "payload": "HUMAN"},
            ],
        },
    }
    resp = requests.post(
        url,
        params={"access_token": PAGE_ACCESS_TOKEN},
        json=payload,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def menu_text(lang):
    items = KNOWLEDGE.get("menu", [])
    lines = []
    if lang == "so":
        lines.append(f"Ku soo dhawoow {KNOWLEDGE.get('business_name', '')}! Dooro:")
    else:
        lines.append(f"Welcome to {KNOWLEDGE.get('business_name', '')}! Choose:")
    for i, item in enumerate(items, start=1):
        lines.append(f"{i}. {item['label']}")
    lines.append("")
    if lang == "so":
        lines.append("Jawaab lambarka, tusaale: 1. Qof la hadal: qor 'human'.")
    else:
        lines.append("Reply with a number, e.g. 1. To talk to a person, type 'human'.")
    return "\n".join(lines)


def faq_answer(text, lang):
    low = text.lower()
    for faq in KNOWLEDGE.get("faqs", []):
        if any(k in low for k in faq.get("keywords", [])):
            return faq.get("answer_so") if lang == "so" and faq.get("answer_so") else faq.get("answer_en", "")
    return None


def ai_answer(text, lang):
    if not OPENAI_API_KEY:
        return None
    try:
        import openai  # type: ignore
    except ImportError:
        return None
    lang_line = "Reply in Somali." if lang == "so" else "Reply in English."
    system = (
        f"You are the Messenger assistant for {KNOWLEDGE.get('business_name', '')} "
        f"in {KNOWLEDGE.get('location', '')}. {lang_line} "
        "Answer only from this information: "
        f"{json.dumps(KNOWLEDGE, ensure_ascii=False)[:6000]} "
        "Never invent a price, date, phone number, or fee. If the answer is "
        "not in the information, say you do not know and offer a human. "
        "Keep replies short, suitable for Messenger."
    )
    try:
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            max_tokens=300,
            temperature=0.3,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        print(f"[AI error] {exc}")
        return None


def handle_message(sender, text):
    text = (text or "").strip()
    low = text.lower()
    lang = detect_language(text)
    state = sessions.get(sender, {"step": "menu"})

    if low in ("menu", "get_started", "hi", "hello", "salaam", "salaamu caleykum", "start", "0"):
        sessions[sender] = {"step": "menu"}
        return menu_text(lang)

    if low in ("human", "qof", "shaqaale", "agent", "talk to human", "la hadal qof"):
        save_inquiry(sender, {"topic": "human_handover", "human_handover": True})
        sessions[sender] = {"step": "menu"}
        if lang == "so":
            return "Waad ku mahadsan tahay. Qof ka tirsan kooxda ayaa kula soo xiriiri doona. Fadlan magacaaga iyo waxaad u baahan tahay noo soo dir."
        return "Thank you. Someone from the team will reply to you here. Please send your name and what you need."

    if state.get("step") == "awaiting_name":
        state["name"] = text
        state["step"] = "awaiting_details"
        sessions[sender] = state
        if lang == "so":
            return "Mahadsanid. Hadda fadlan sharax waxaad u baahan tahay (koorso, adeeg, ama su'aal)."
        return "Thanks. Now please describe what you need (course, service, or question)."
    if state.get("step") == "awaiting_details":
        state["details"] = text
        save_inquiry(sender, state)
        sessions[sender] = {"step": "menu"}
        if lang == "so":
            return "Waad ku mahadsan tahay! Codsigaaga waa la diiwaangeliyay, kooxda ayaa kula soo xiriiri doonta."
        return "Thank you! Your request has been saved, and the team will get back to you."

    menu = KNOWLEDGE.get("menu", [])
    if low.isdigit() and 1 <= int(low) <= len(menu):
        choice = menu[int(low) - 1]
        action = choice.get("action", "")
        if action == "human":
            sessions[sender] = {"step": "awaiting_name"}
            if lang == "so":
                return "Fadlan magacaaga soo dir si qofku kula soo xiriiro."
            return "Please send your name so a person can get back to you."
        if action == "inquiry":
            sessions[sender] = {"step": "awaiting_name", "topic": choice.get("label", "")}
            if lang == "so":
                return "Fadlan magacaaga soo dir."
            return "Please send your name."
        answer = choice.get("answer_so") if lang == "so" and choice.get("answer_so") else choice.get("answer_en", "")
        return answer or menu_text(lang)

    answer = faq_answer(text, lang)
    if answer:
        return answer
    answer = ai_answer(text, lang)
    if answer:
        return answer

    if lang == "so":
        return "Waan ka xumahay, jawaabtaas ma hayo hadda.\n\n" + menu_text("so")
    return "Sorry, I don't have that answer right now.\n\n" + menu_text("en")


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "channel": "messenger", "profile": BOT_PROFILE, "demo_mode": not bool(PAGE_ACCESS_TOKEN)})


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Verification failed", 403


@app.route("/webhook", methods=["POST"])
def receive_webhook():
    data = request.get_json(silent=True) or {}
    # Messenger sends object=page for Page messages. Ignore anything else.
    if data.get("object") != "page":
        return jsonify({"status": "ignored"}), 200
    try:
        for entry in data.get("entry", []):
            for event in entry.get("messaging", []):
                sender = event.get("sender", {}).get("id", "")
                message = event.get("message", {})
                # Never reply to our own echo messages.
                if message.get("is_echo"):
                    continue
                if event.get("postback"):
                    text = event["postback"].get("payload", "")
                elif message.get("quick_reply"):
                    text = message["quick_reply"].get("payload", "")
                else:
                    text = message.get("text", "")
                if sender and text:
                    reply = handle_message(sender, text)
                    send_messenger(sender, reply)
    except Exception as exc:
        print(f"[webhook error] {exc}")
    return jsonify({"status": "received"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print(f"Messenger bot profile: {BOT_PROFILE} | Demo mode: {not bool(PAGE_ACCESS_TOKEN)}")
    app.run(host="0.0.0.0", port=port)
