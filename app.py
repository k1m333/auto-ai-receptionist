# app.py - AI Receptionist for Auto Shops
# Built with Twilio, Groq, Google Calendar

import os
import json
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv
import pytz
from flask import Flask, request, Response, session
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client
from twilio.request_validator import RequestValidator
from groq import Groq
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.oauth2 import service_account
from collections import defaultdict
import time
import sqlite3

# Rate limiting: caller_id -> List of timestamps
rate_limit_store = defaultdict(list)

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")
pending_suggestions = {}
# Temporary in-memory store for verification codes.
# Use Redis or a database for Production.
verification_codes = {}

# ===========================
# 1. API Clients
# ===========================

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

twilio_client = Client(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN")
)
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

# ===========================
# 2. Google Calendar Helpers
# ===========================

SCOPES = ["https://www.googleapis.com/auth/calendar"]

def list_calendars(service):
    page_token = None
    while True:
        calendar_list = service.calendarList().list(pageToken=page_token).execute()
        for calendar_entry in calendar_list['items']:
            print(f"Calendar: {calendar_entry['summary']} – ID: {calendar_entry['id']}")
        page_token = calendar_list.get('nextPageToken')
        if not page_token:
            break

def get_calendar_service():
    # Try service account first
    creds_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if creds_json:
        try:
            import google.oauth2.service_account
            creds_dict = json.loads(creds_json)
            creds = google.oauth2.service_account.Credentials.from_service_account_info(
                creds_dict,
                scopes=["https://www.googleapis.com/auth/calendar"]
            )
            return build("calendar", "v3", credentials=creds)
        except Exception as e:
            print(f"Service account error: {e}")

    # Development fallback (OAuth with credentials.json)
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())
    print("✅ Using OAuth for Calendar (Local Dev)")
    return build("calendar", "v3", credentials=creds)

def check_availability(service, calendar_id, start_time, end_time):
    print(f"🔍 Checking availability for calendar: {calendar_id}")
    print(f"📅 Time range: {start_time} to {end_time}")
    
    body = {
        "timeMin": start_time,
        "timeMax": end_time,
        "items": [{"id": calendar_id}]
    }
    try:
        freebusy = service.freebusy().query(body=body).execute()
        print(f"📊 FreeBusy response: {freebusy}")
        
        busy_times = freebusy["calendars"][calendar_id].get("busy", [])
        print(f"🚫 Busy times found: {busy_times}")
        
        is_free = len(busy_times) == 0
        print(f"✅ Is free: {is_free}")
        return is_free
    except Exception as e:
        print(f"❌ check_availability error: {e}")
        import traceback
        traceback.print_exc()
        return False

def create_event(service, calendar_id, summary, start_datetime, end_datetime, description=""):
    # TODO: Add idempotency key to prevent double-booking
    # Use composite key (user_id, event_id) with unique constraint in DB
    print(f"🟢 Attempting to create event with start={start_datetime}, end={end_datetime}")
    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_datetime, "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": end_datetime, "timeZone": "America/Los_Angeles"},
    }
    try:
        created_event = service.events().insert(calendarId=calendar_id, body=event).execute()
        print(f"✅ Event created: {created_event.get('htmlLink')}")
        print(f"🔗 Event link: {created_event.get('htmlLink')}")
        print(f"📧 Event ID: {created_event.get('id')}")
        return created_event
    except Exception as e:
        print(f"❌ Calendar API error: {e}")
        import traceback
        traceback.print_exc()
        return None

# ===========================
# 3. FAQ Helpers
# ===========================

def load_faq():
    faq_path = os.path.join(os.path.dirname(__file__), "faq.json")
    try:
        with open(faq_path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def answer_from_faq(user_input):
    faq = load_faq()
    user_lower = user_input.lower().strip()
    for key, answer in faq.items():
        if key in user_lower:
            return answer
    return None

# ===========================
# 4. AI Response Helpers
# ===========================

def get_groq_response(prompt, context=""):
    full_prompt = f"{context}\nUser: {prompt}\nAI:"
    chat_completion = groq_client.chat.completions.create(
        messages=[{"role": "user", "content": full_prompt}],
        model="llama-3.1-8b-instant",
        temperature=0.7,
        max_tokens=150,
    )
    return chat_completion.choices[0].message.content.strip()

def extract_time_from_speech(text):
    # First, try to find an explicit time like "3 PM" or "9:00"
    match = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?', text, re.IGNORECASE)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2)) if match.group(2) else 0
        ampm = match.group(3).lower() if match.group(3) else ''
        
        # If no explicit AM/PM, infer from context
        if not ampm:
            if 'morning' in text or 'a.m.' in text or 'am' in text:
                ampm = 'am'
            elif 'afternoon' in text or 'evening' in text or 'p.m.' in text or 'pm' in text:
                ampm = 'pm'
        
        if ampm in ('p.m.', 'pm') and hour != 12:
            hour += 12
        elif ampm in ('a.m.', 'am') and hour == 12:
            hour = 0
        return hour, minute
    return None, None

def is_rate_limited(caller_id, limit=10, window_seconds=60):
    now = time.time()
    timestamps = rate_limit_store[caller_id]
    # Remove timestamps outside the window
    while timestamps and timestamps[0] < now - window_seconds:
        timestamps.pop(0)
    if len(timestamps) >= limit:
        return True
    timestamps.append(now)
    return False

# ===========================
# 5. SMS Helpers
# ===========================

def send_sms(to_number, appointment_time):
    try:
        message = twilio_client.messages.create(
            body=f"Your appointment has been confirmed for {appointment_time}. Thanks for choosing our shop!",
            from_=TWILIO_PHONE_NUMBER,
            to=to_number
        )
        print(f"SMS sent: {message.sid}")
    except Exception as e:
        print(f"Failed to send SMS: {e}")
        # Print the full error details
        if hasattr(e, 'status_code'):
            print(f"HTTP status: {e.status_code}")
        if hasattr(e, 'msg'):
            print(f"Message: {e.msg}")

# ===========================
# 6. Booking Helpers
# ===========================

def is_within_business_hours(dt):
    if dt.weekday() == 6:  # Sunday
        return False
    if dt.weekday() == 5:  # Saturday
        return 8 <= dt.hour < 17  # 8 AM to 5 PM
    return (dt.hour > 7 or (dt.hour == 7 and dt.minute >= 30)) and (dt.hour < 16 or (dt.hour == 16 and dt.minute <= 30))

def book_appointment(speech_result, session, call_sid):
    la_tz = pytz.timezone('America/Los_Angeles')
    now = datetime.now(la_tz)
    requested_hour, requested_minute = extract_time_from_speech(speech_result)
    
    # Determine target day: today if "tomorrow" not in speech, else tomorrow
    days_ahead = 1 if "tomorrow" in speech_result else 0
    
    if requested_hour is not None:
        start = now.replace(hour=requested_hour, minute=requested_minute, second=0, microsecond=0) + timedelta(days=days_ahead)
    else:
        start = now.replace(hour=13, minute=30, second=0, microsecond=0) + timedelta(days=days_ahead)
    
    if not is_within_business_hours(start):
        service = get_calendar_service()
        calendar_id = os.environ.get("CALENDAR_ID", "primary")
        
        # Start searching from the requested time (or opening time if before open)
        next_slot = start
        if next_slot.hour < 7 or (next_slot.hour == 7 and next_slot.minute < 30):
            next_slot = next_slot.replace(hour=7, minute=30, second=0, microsecond=0)
        
        max_attempts = 48  # 12 hours of 15‑min slots
        for attempt in range(max_attempts):
            # Ensure the slot is within business hours
            while not is_within_business_hours(next_slot):
                next_slot += timedelta(minutes=15)
                if next_slot.hour >= 16 and next_slot.minute > 30:
                    next_slot = next_slot.replace(hour=7, minute=30, second=0, microsecond=0) + timedelta(days=1)
            
            end_slot = next_slot + timedelta(minutes=15)
            if check_availability(service, calendar_id, next_slot.isoformat(), end_slot.isoformat()):
                break
            next_slot += timedelta(minutes=15)
        else:
            return "I'm sorry, I couldn't find any available slots. Please call us directly to book."
        pending_suggestions[call_sid] = next_slot.isoformat()
        return (f"I'm sorry, we're closed at that time. "
                f"The next available slot is {next_slot.strftime('%A at %I:%M %p')}. "
                f"Would you like me to book it for you?")
    
    end = start + timedelta(minutes=15)
    start_str = start.isoformat()
    end_str = end.isoformat()
    
    service = get_calendar_service()
    calendar_id = os.environ.get("CALENDAR_ID", "primary")

    # === IDEMPOTENCY CHECK ===
    idempotency_key = f"{call_sid}_booking"
    conn = sqlite3.connect('calls.db')
    cursor = conn.cursor()
    cursor.execute("SELECT booking_result FROM idempotency_keys WHERE idempotency_key = ?", (idempotency_key,))
    row = cursor.fetchone()
    if row:
        conn.close()
        return row[0]  # Already processed, return cached result
    # === END IDEMPOTENCY CHECK ===

    event = create_event(
        service, calendar_id,
        f"Appointment with {session.get('caller_name', 'Customer')}",
        start_str, end_str,
        f"Booked via AI assistant. Caller said: {speech_result}"
    )
    
    if event and 'id' in event:
        customer_phone = session.get("customer_phone")
        response_text = f"I've booked your appointment for {start.strftime('%A, %B %d at %I:%M %p')}. Anything else?"
        
        # === STORE IDEMPOTENCY RESULT ===
        cursor.execute(
            "INSERT OR IGNORE INTO idempotency_keys (idempotency_key, booking_result, created_at) VALUES (?, ?, ?)",
            (idempotency_key, response_text, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
        # === END STORE ===
        
        if customer_phone:
            send_sms(customer_phone, start.strftime('%A, %B %d at %I:%M %p'))
        return response_text
    else:
        return "I'm sorry, there was a problem booking your appointment. Please call us directly."

# ===========================
# 7. Main Voice Endpoint
# ===========================

GOODBYE_PHRASES = ["goodbye", "bye", "that's all", "no thanks", "nothing else", "all set", "that's it"]
AFFIRMATIVE = ["yes", "yeah", "yep", "sure", "book it", "okay", "please", "go ahead", "yes please", "yeah sure"]
BOOKING_KEYWORDS = ["appointment", "schedule", "book"] + AFFIRMATIVE

@app.route("/voice", methods=["POST"])
def voice():
    # === TWILIO SIGNATURE VERIFICATION ===
    from twilio.request_validator import RequestValidator
    
    validator = RequestValidator(os.getenv("TWILIO_AUTH_TOKEN"))
    url = request.url
    # Get the POST data as a dictionary (not raw string)
    params = request.form.to_dict()
    signature = request.headers.get('X-Twilio-Signature', '')
    
    if not validator.validate(url, params, signature):
        print(f"❌ Invalid Twilio signature from {request.remote_addr}")
        return Response("Forbidden", status=403)
    print("✅ Twilio signature verified")
    # === END VERIFICATION ===

    # === RATE LIMITING ===
    caller_id = request.form.get('Caller', 'unknown')
    if is_rate_limited(caller_id):
        resp = VoiceResponse()
        resp.say("Too many requests. Please try again later.", voice="Polly.Joanna")
        resp.hangup()
        return Response(str(resp), mimetype="text/xml")
    # === END RATE LIMITING ===

    # === CALL LOGGING (SQLite) ===
    call_sid = request.values.get('CallSid', 'unknown')
    timestamp = datetime.now().isoformat()

    conn = sqlite3.connect('calls.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS call_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_sid TEXT UNIQUE,
            caller TEXT,
            timestamp TEXT,
            outcome TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            idempotency_key TEXT PRIMARY KEY,
            booking_result TEXT,
            created_at TIMESTAMP
        )
    ''')
    cursor.execute(
        'INSERT OR IGNORE INTO call_logs (call_sid, caller, timestamp, outcome) VALUES (?, ?, ?, ?)',
        (call_sid, caller_id, timestamp, 'started')
    )
    conn.commit()
    conn.close()
    # === END CALL LOGGING ===

    speech_result = request.form.get("SpeechResult", "")
    speech_result = speech_result.lower().strip().rstrip('.').rstrip('!').rstrip('?')
    print(f"DEBUG: speech_result = '{speech_result}'")
    
    # Goodbye detection
    if speech_result and any(phrase in speech_result.lower() for phrase in GOODBYE_PHRASES):
        resp = VoiceResponse()
        resp.say("Thank you for calling. Have a great day!", voice="Polly.Joanna")
        resp.hangup()
        return Response(str(resp), mimetype="text/xml")
    
    # First call (no speech yet)
    if not speech_result and session.get("last_prompt"):
        response_text = session["last_prompt"]
    elif not speech_result:
        session.clear()
        response_text = "Hello! Thanks for calling. Could you please tell me your name and phone number?"
        session["history"] = f"AI: {response_text}"
        session["last_prompt"] = response_text
        session["awaiting_phone"] = True
    else:
        conversation_history = session.get("history", "")
        
        # Awaiting phone number
        if session.get("awaiting_phone"):
            digits = re.sub(r'\D', '', speech_result)
            if len(digits) >= 10:
                session["customer_phone"] = digits
                session["awaiting_phone"] = False
                import random
                code = str(random.randint(100000, 999999))
                call_sid = request.values.get('CallSid')
                verification_codes[call_sid] = {
                    "code": code,
                    "attempts": 0,
                    "phone": digits
                }
                # Send SMS with code
                try:
                    twilio_client.messages.create(
                        body=f"Your AI Receptionist verification code is: {code}. Please say it back to confirm your number.",
                        from_=TWILIO_PHONE_NUMBER,
                        to=digits
                    )
                    session["awaiting_verification"] = True
                    response_text = f"I've sent a 6-digit code to {digits[-4:]}. Please say the code now."
                except Exception as e:
                    print(f"SMS failed: {e}")
                    response_text = "I'm having trouble sending texts. Let's continue without verification."
                    session["awaiting_verification"] = False
            else:
                response_text = "I didn't catch that. Please say your phone number again."
        
        elif session.get("awaiting_verification"):
            spoken_digits = re.sub(r'\D', '', speech_result)
            call_sid = request.values.get('CallSid')
            stored = verification_codes.get(call_sid)
            
            if stored and spoken_digits == stored["code"]:
                # Verified successfully
                session["awaiting_verification"] = False
                del verification_codes[call_sid]
                response_text = "Code verified successfully. How can I help you today?"
            else:
                if stored:
                    stored["attempts"] += 1
                    if stored["attempts"] >= 3:
                        del verification_codes[call_sid]
                        session["awaiting_verification"] = False
                        response_text = "Too many failed attempts. Please call us directly."
                    else:
                        response_text = f"Sorry, that didn't match. You have {3 - stored['attempts']} attempts left. Please say the code sent to {stored['phone'][-4:]} again."
                else:
                    # No code found (timeout or error)
                    session["awaiting_verification"] = False
                    response_text = "Verification expired. Let's continue. How can I help?"
        
        else:
            # Normal conversation
            faq_answer = answer_from_faq(speech_result)
            if faq_answer:
                response_text = faq_answer
            elif any(word in speech_result.lower() for word in BOOKING_KEYWORDS):
                print("DEBUG: Entered booking intent")
                call_sid = request.values.get('CallSid')
                suggested = pending_suggestions.get(call_sid)
                print(f"DEBUG: suggested = {suggested}")
                print(f"DEBUG: speech_result = '{speech_result}'")
                
                if suggested and any(word in speech_result for word in AFFIRMATIVE):
                    pending_suggestions.pop(call_sid, None)
                    print("DEBUG: User accepted suggested time")
                    # === IDEMPOTENCY CHECK ===
                    idempotency_key = f"{call_sid}_suggested_booking"
                    conn = sqlite3.connect('calls.db')
                    cursor = conn.cursor()
                    cursor.execute("SELECT booking_result FROM idempotency_keys WHERE idempotency_key = ?", (idempotency_key,))
                    row = cursor.fetchone()
                    if row:
                        conn.close()
                        response_text = row[0]
                        # Skip to response (don't create duplicate event)
                    else:
                        # === END IDEMPOTENCY CHECK ===
                        try:
                            start = datetime.fromisoformat(suggested)
                            end = start + timedelta(minutes=15)
                            start_str = start.isoformat()
                            end_str = end.isoformat()
                            
                            service = get_calendar_service()
                            calendar_id = os.environ.get("CALENDAR_ID", "primary")
                            
                            event = create_event(
                                service, calendar_id,
                                f"Appointment with {session.get('caller_name', 'Customer')}",
                                start_str, end_str,
                                f"Booked via AI assistant (suggested time). Caller said: {speech_result}"
                            )
                            
                            if event and 'id' in event:
                                customer_phone = session.get("customer_phone")
                                if customer_phone:
                                    send_sms(customer_phone, start.strftime('%A, %B %d at %I:%M %p'))
                                response_text = f"Great! I've booked your appointment for {start.strftime('%A, %B %d at %I:%M %p')}. Anything else?"
                            else:
                                response_text = "I'm sorry, there was a problem booking that time. Please call us directly."
                        except Exception as e:
                            print(f"DEBUG: Exception in yes branch: {e}")
                            response_text = "I'm sorry, I had trouble booking that time. Please call us directly."
                elif "no" in speech_result:
                    pending_suggestions.pop(call_sid, None)
                    print("DEBUG: User rejected suggested time")
                    response_text = "No problem. What time would work for you?"
                else:
                    print("DEBUG: No pending suggestion, calling book_appointment")
                    response_text = book_appointment(speech_result, session, call_sid)
            else:
                prompt = f"The caller said: '{speech_result}'. Respond helpfully. Keep it brief."
                response_text = get_groq_response(prompt, conversation_history)
        
        # Update session history
        if "history" not in session:
            session["history"] = ""
        session["history"] += f"\nUser: {speech_result}\nAI: {response_text}"
        session["last_prompt"] = response_text
    
    # Build Twilio response (keep call open)
    resp = VoiceResponse()
    gather = Gather(input="speech", timeout=10, action="/voice", method="POST")
    gather.say(response_text, voice="Polly.Salli")
    resp.append(gather)
    # Don't redirect back to /voice in the same call, let gather handle it
    return Response(str(resp), mimetype="text/xml")

@app.route("/privacy", methods=["GET"])
def privacy():
    return """
    <h1>Privacy Policy</h1>
    <p>AI Receptionist collects your phone number solely to send verification codes and appointment confirmations. No data is shared with third parties. Messages are transactional only (2FA). Reply STOP to opt out.</p>
    <p>Last updated: June 10, 2026</p>
    """

@app.route("/terms", methods=["GET"])
def terms():
    return """
    <h1>Terms of Service</h1>
    <p>By using AI Receptionist, you agree to receive transactional SMS messages for identity verification and appointment booking. Message frequency varies. Standard rates may apply.</p>
    <p>Last updated: June 10, 2026</p>
    """

if __name__ == "__main__":
    app.run(port=5000, debug=False)