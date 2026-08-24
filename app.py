import os
import json
import httpx
from flask import Flask, request, jsonify
from google import genai
from google.genai import types

app = Flask(__name__)

# --- CONFIGURATION (Sanitized) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip().strip('"').strip("'")
SUPABASE_KEY = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or "").strip().strip('"').strip("'")
ORIGIN_CITY = os.getenv("ORIGIN_CITY", "Singapore").strip()

raw_supabase_url = os.getenv("SUPABASE_URL", "").strip().strip('"').strip("'")
if raw_supabase_url and not raw_supabase_url.startswith("http"):
    SUPABASE_URL = f"https://{raw_supabase_url}"
else:
    SUPABASE_URL = raw_supabase_url

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# --- HELPER FUNCTIONS ---
def send_telegram(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    with httpx.Client() as client:
        client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})

def supabase_request(method: str, endpoint: str, data=None, params=None):
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{endpoint}"
    with httpx.Client() as client:
        res = client.request(method, url, headers=headers, json=data, params=params)
        if not res.text:
            return []
        try:
            return res.json()
        except Exception:
            raise Exception(f"Supabase API Error ({res.status_code}): {res.text}")

def extract_travel_taste(user_input: str) -> dict:
    prompt = f"""
    Identify what this travel link, spot, or mention is about: "{user_input}"
    
    Extract the following details and return strictly a valid JSON object:
    - "destination": Primary Destination (City, Country or Island)
    - "vibe": Aesthetic/Vibe (e.g. beach resort, aesthetic cafe, luxury stay)
    - "activities": List of activities mentioned or suitable
    - "summary": A concise 1-sentence summary of the travel interest
    
    Format: {{"destination": "...", "vibe": "...", "activities": [...], "summary": "..."}}
    """
    
    try:
        # Attempt with live Google Search grounding first
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            )
        )
    except Exception as e:
        # Fallback to standard model if rate-limited (429)
        if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
        else:
            raise e

    raw_text = response.text.strip()
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        raw_text = "\n".join(lines).strip()
        
    return json.loads(raw_text)

def generate_curated_plan(chat_id: int) -> str:
    records = supabase_request("GET", "user_interests", params={"chat_id": f"eq.{chat_id}", "select": "*"})
    
    if not records or (isinstance(records, dict) and "error" in records) or len(records) == 0:
        return "No travel tastes saved yet! Send me a link or spot first."
    
    tastes_summary = "\n".join([f"- {r.get('summary', '')} (Vibe: {r.get('vibe', '')})" for r in records])
    
    prompt = f"""
    You are an elite travel concierge based in {ORIGIN_CITY}.
    The user's saved travel tastes are:
    {tastes_summary}
    
    1. Pick the best destination matching their taste profile.
    2. Search Google live for current travel deals, ferry/flight prices, and package promotions departing from {ORIGIN_CITY}.
    3. Build a curated getaway plan featuring:
       - 📍 Target Destination & why it fits
       - 🏷️ Real-time Live Deals & Promotions departing from {ORIGIN_CITY}
       - 🗓️ 3-Day Highlight Itinerary (Stay, Food, Activities)
       - 💡 Travel Tip (ferry duration, transit, or best booking platform)
    
    Format cleanly with Markdown and emojis.
    """
    
    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            )
        )
    except Exception as e:
        if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
        else:
            raise e

    return response.text

# --- ROUTES ---
@app.route("/", methods=["GET"])
def home():
    return "Travel Intelligence Webhook Engine is active!", 200

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json(force=True)
    if "message" not in update or "text" not in update["message"]:
        return jsonify({"status": "ignored"}), 200
    
    chat_id = update["message"]["chat"]["id"]
    user_text = update["message"]["text"]
    
    if user_text.startswith("/start"):
        send_telegram(chat_id, "👋 **Travel Intelligence Bot**\n\nSend me links, video URLs, or travel spots. I'll record your tastes and find active travel deals!\n\nType `/plan` anytime for a curated itinerary with live deals.")
        return jsonify({"status": "ok"}), 200
        
    if user_text.startswith("/plan"):
        send_telegram(chat_id, "🔍 Searching for live travel deals and curating your recommendation...")
        plan = generate_curated_plan(chat_id)
        send_telegram(chat_id, plan)
        return jsonify({"status": "ok"}), 200

    send_telegram(chat_id, "⚡ Analyzing details and recording travel taste...")
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
            f"Send more links anytime, or type `/plan` to generate an itinerary with live deals."
        )
        send_telegram(chat_id, reply)
    except Exception as e:
        print(f"DETAILED ERROR: {e}", flush=True)
        send_telegram(chat_id, f"⚠️ Error: `{str(e)}`")
        
    return jsonify({"status": "ok"}), 200

@app.route("/cron/daily-digest", methods=["GET", "POST"])
def daily_digest():
    records = supabase_request("GET", "user_interests", params={"select": "chat_id"})
    if isinstance(records, list):
        chat_ids = set(r["chat_id"] for r in records if "chat_id" in r)
        for cid in chat_ids:
            plan = generate_curated_plan(cid)
            send_telegram(cid, f"🌙 **Your Evening Travel & Live Deals Recommendation**\n\n{plan}")
    return jsonify({"status": "digest_sent"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
