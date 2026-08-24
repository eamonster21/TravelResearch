import os
import json
import httpx
from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)

# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
ORIGIN_CITY = os.getenv("ORIGIN_CITY", "Singapore")

openai_client = OpenAI(api_key=OPENAI_API_KEY)

# --- HELPER FUNCTIONS ---
def send_telegram(chat_id: int, text: str):
    """Sends a formatted message to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    httpx.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})

def supabase_request(method: str, endpoint: str, data=None, params=None):
    """Simple REST wrapper for Supabase to avoid SDK setup issues."""
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{endpoint}"
    with httpx.Client() as client:
        res = client.request(method, url, headers=headers, json=data, params=params)
        return res.json()

def extract_travel_taste(user_input: str) -> dict:
    """Uses GPT-4o-mini to extract travel tastes and destinations from links or text."""
    prompt = f"""
    Analyze this travel request, link description, or input: "{user_input}"
    
    Extract:
    1. Primary Destination(s) (City, Country).
    2. Vibe/Style (e.g., minimalist cafes, boutique stay, nature, food markets).
    3. Key activities mentioned.
    4. A concise 1-sentence summary of the user's acquired taste.
    
    Return strictly JSON with keys: "destination", "vibe", "activities", "summary".
    """
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"}
    )
    return json.loads(response.choices[0].message.content)

def generate_curated_plan(chat_id: int) -> str:
    """Reads saved tastes from Supabase and generates a customized itinerary."""
    records = supabase_request("GET", "user_interests", params={"chat_id": f"eq.{chat_id}", "select": "*"})
    
    if not records or "error" in records or len(records) == 0:
        return "No travel tastes saved yet! Paste some TikTok/Instagram links or tell me what you're interested in first."
    
    tastes_summary = "\n".join([f"- {r.get('summary', '')} (Vibe: {r.get('vibe', '')})" for r in records])
    
    prompt = f"""
    You are an elite travel concierge based in {ORIGIN_CITY}.
    Here is the user's acquired travel profile and saved interests:
    {tastes_summary}
    
    Create a curated travel plan suggestion for their next getaway.
    Include:
    - Target Destination matching their aesthetic
    - Why it fits their preferences
    - A 3-day highlight itinerary (Stay, Food, Activities)
    - Practical travel tip departing from {ORIGIN_CITY}
    
    Keep it engaging, concise, and formatted with Markdown and emojis.
    """
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content

# --- ROUTES & HANDLERS ---
@app.route("/", methods=["GET"])
def home():
    return "Travel Intelligence Webhook Engine is active!", 200

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    """Wakes up on-demand whenever a Telegram message is received."""
    update = request.get_json(force=True)
    if "message" not in update or "text" not in update["message"]:
        return jsonify({"status": "ignored"}), 200
    
    chat_id = update["message"]["chat"]["id"]
    user_text = update["message"]["text"]
    
    if user_text.startswith("/start"):
        send_telegram(chat_id, "👋 **Travel Intelligence Bot**\n\nSend me links (TikTok, IG, web) or text ideas (e.g. *'Research boutique stays in Bali'*). I'll record your tastes and curate custom travel plans!\n\nType `/plan` anytime for a fresh itinerary.")
        return jsonify({"status": "ok"}), 200
        
    if user_text.startswith("/plan"):
        send_telegram(chat_id, "✨ Curating a travel recommendation based on your profile...")
        plan = generate_curated_plan(chat_id)
        send_telegram(chat_id, plan)
        return jsonify({"status": "ok"}), 200

    # Process links / input on demand
    send_telegram(chat_id, "⚡ Analyzing input and updating your travel taste profile...")
    try:
        parsed = extract_travel_taste(user_text)
        
        supabase_request("POST", "user_interests", data={
            "chat_id": chat_id,
            "destination": parsed.get("destination", "General"),
            "vibe": str(parsed.get("vibe", "")),
            "activities": json.dumps(parsed.get("activities", [])),
            "summary": parsed.get("summary", user_text)
        })
        
        reply = (
            f"✅ **Taste Recorded!**\n\n"
            f"📍 **Destination:** {parsed.get('destination')}\n"
            f"✨ **Vibe:** {parsed.get('vibe')}\n"
            f"📝 **Summary:** {parsed.get('summary')}\n\n"
            f"Send more links anytime, or type `/plan` to get your custom itinerary."
        )
        send_telegram(chat_id, reply)
    except Exception as e:
        print(f"DETAILED ERROR: {e}", flush=True)
        send_telegram(chat_id, f"⚠️ Debug Error Details:\n`{str(e)}`")
        
    return jsonify({"status": "ok"}), 200

@app.route("/cron/daily-digest", methods=["GET", "POST"])
def daily_digest():
    """Triggered by an external daily timer (e.g., cron-job.org at 8 PM)."""
    records = supabase_request("GET", "user_interests", params={"select": "chat_id"})
    if isinstance(records, list):
        chat_ids = set(r["chat_id"] for r in records if "chat_id" in r)
        for cid in chat_ids:
            plan = generate_curated_plan(cid)
            send_telegram(cid, f"🌙 **Your Evening Travel Recommendation**\n\n{plan}")
    return jsonify({"status": "digest_sent"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
