# app.py - AI Receptionist for Auto Shops
# Twilio + Gemini Live (advisor slots + SMS calendar invite links)
# Single process on :5000: HTTP routes + /media-stream WebSocket (flask-sock)

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv
from flask import Flask, Response, request
from flask_sock import Sock
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse

from gemini_live_handler import run_twilio_stream_sync

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_HOST = "0.0.0.0"
APP_PORT = 5000
DB_PATH = "calls.db"
FAQ_FILENAME = "faq.json"
TIMEZONE_NAME = "America/Los_Angeles"

# Business hours (local)
WEEKDAY_OPEN_HOUR = 7
WEEKDAY_OPEN_MINUTE = 30
WEEKDAY_CLOSE_HOUR = 16
WEEKDAY_CLOSE_MINUTE = 30
SATURDAY_OPEN_HOUR = 8
SATURDAY_CLOSE_HOUR = 17  # exclusive upper bound on hour

# Rate limiting
RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW_SECONDS = 60

# Twilio / SMS
TWILIO_VOICE = "Polly.Salli"
SMS_OPT_OUT = "Reply STOP to opt out."
SMS_WITH_LINK = (
    "Your appointment{who} is confirmed for {appointment_time}. "
    "Click here to add it to your calendar: {calendar_link} "
    f"{SMS_OPT_OUT}"
)
SMS_WITHOUT_LINK = (
    "Your appointment{who} is confirmed for {appointment_time}. "
    f"{SMS_OPT_OUT}"
)

# User-facing / legal copy
RATE_LIMIT_MESSAGE = "Too many requests. Please try again later."
VOICE_ERROR_MESSAGE = (
    "We're sorry, our assistant is temporarily unavailable. Please try again shortly."
)
GEMINI_FALLBACK_MESSAGE = "I'm having trouble processing that. Could you please repeat?"
PRIVACY_HTML = """
    <h1>Privacy Policy</h1>
    <p>AI Receptionist collects your phone number solely to send verification codes and appointment confirmations. No data is shared with third parties. Messages are transactional only (2FA). Reply STOP to opt out.</p>
    <p>Last updated: June 10, 2026</p>
    """
TERMS_HTML = """
    <h1>Terms of Service</h1>
    <p>By using AI Receptionist, you agree to receive transactional SMS messages for identity verification and appointment booking. Message frequency varies. Standard rates may apply.</p>
    <p>Last updated: June 10, 2026</p>
    """
ROBOTS_TXT = "User-agent: *\nDisallow: /\n"

# ---------------------------------------------------------------------------
# Logging & app bootstrap
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("ai-receptionist")

app = Flask(__name__)
_flask_secret = os.getenv("FLASK_SECRET_KEY")
if not _flask_secret:
    # Local/dev fallback so the app still boots; set FLASK_SECRET_KEY in production.
    _flask_secret = os.urandom(24).hex()
    logger.warning(
        "FLASK_SECRET_KEY not set — using an ephemeral key (sessions won't survive restarts)"
    )
app.secret_key = _flask_secret
sock = Sock(app)

faq_cache: dict = {}
rate_limit_store: dict = defaultdict(list)

twilio_client = Client(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN"),
)
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db() -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS call_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_sid TEXT UNIQUE,
                caller TEXT,
                timestamp TEXT,
                outcome TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                idempotency_key TEXT PRIMARY KEY,
                booking_result TEXT,
                created_at TIMESTAMP
            )
            """
        )
        conn.commit()
        conn.close()
        logger.info("Database initialized (%s)", DB_PATH)
    except Exception:
        logger.exception("Failed to initialize database")


# ---------------------------------------------------------------------------
# Media stream URL helper
# ---------------------------------------------------------------------------

def get_media_stream_url(caller: str | None = None) -> str:
    """
    Public wss URL Twilio should open for Media Streams.

    Priority:
      1. MEDIA_STREAM_URL env (full wss://.../media-stream)
      2. NGROK_DOMAIN env (hostname only)
      3. Host from the incoming /voice request (works when Twilio hits ngrok)

    Always appends ?caller=<From> when a caller number is provided.
    """
    explicit = os.getenv("MEDIA_STREAM_URL")
    if explicit:
        base = explicit.rstrip("/")
    else:
        ngrok = os.getenv("NGROK_DOMAIN")
        if ngrok:
            host = (
                ngrok.replace("https://", "")
                .replace("http://", "")
                .replace("wss://", "")
                .replace("ws://", "")
                .rstrip("/")
            )
        else:
            host = request.headers.get("X-Forwarded-Host") or request.host
        base = f"wss://{host}/media-stream"

    if not caller or caller == "unknown":
        return base

    parts = urlsplit(base)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["caller"] = caller
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


# ---------------------------------------------------------------------------
# Core Twilio routes (logic preserved)
# ---------------------------------------------------------------------------

@app.route("/voice", methods=["POST"])
def voice():
    """Twilio voice webhook — connect the call to the Gemini Live media stream."""
    try:
        # === RATE LIMITING ===
        # Prefer From (E.164), fall back to Caller
        caller_id = request.form.get("From") or request.form.get("Caller") or "unknown"
        if is_rate_limited(caller_id):
            resp = VoiceResponse()
            resp.say(RATE_LIMIT_MESSAGE, voice=TWILIO_VOICE)
            resp.hangup()
            return Response(str(resp), mimetype="text/xml")

        # === CALL LOGGING ===
        call_sid = request.values.get("CallSid", "unknown")
        timestamp = datetime.now().isoformat()

        def log_call_async():
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR IGNORE INTO call_logs "
                    "(call_sid, caller, timestamp, outcome) VALUES (?, ?, ?, ?)",
                    (call_sid, caller_id, timestamp, "started"),
                )
                conn.commit()
                conn.close()
            except Exception:
                logger.exception("Failed to log call %s", call_sid)

        threading.Thread(target=log_call_async, daemon=True).start()

        # Twilio Stream URLs do NOT support query strings — pass From as a
        # <Parameter> so it arrives on the WebSocket start.customParameters.
        # echoCancellation is a best-effort hint (not a documented Stream attribute);
        # PSTN echo is mainly mitigated in Gemini VAD / half-duplex in the handler.
        media_url = get_media_stream_url()
        resp = VoiceResponse()
        connect = Connect()
        stream = connect.stream(url=media_url)
        stream.parameter(name="echoCancellation", value="true")
        if caller_id and caller_id != "unknown":
            stream.parameter(name="caller", value=caller_id)
        resp.append(connect)
        logger.info(
            "Sending TwiML Connect/Stream url=%s caller=%s", media_url, caller_id
        )
        return Response(str(resp), mimetype="text/xml")
    except Exception:
        logger.exception("Unhandled error in /voice")
        resp = VoiceResponse()
        resp.say(VOICE_ERROR_MESSAGE, voice=TWILIO_VOICE)
        resp.hangup()
        return Response(str(resp), mimetype="text/xml")


@sock.route("/media-stream")
def media_stream(ws):
    """
    Twilio Media Streams WebSocket on port 5000 (same process as Flask HTTP).
    flask-sock is sync; Gemini bridge is async — run_twilio_stream_sync bridges them.
    """
    # Stream URL: /media-stream?caller=+1...
    # flask-sock often leaves Flask request.args empty on the WS upgrade — prefer WSGI QUERY_STRING.
    caller = request.args.get("caller")
    if not caller:
        environ = getattr(ws, "environ", None) or {}
        qs = environ.get("QUERY_STRING") or ""
        if qs:
            caller = dict(parse_qsl(qs, keep_blank_values=True)).get("caller")
    logger.info("Twilio WebSocket connected on /media-stream (caller=%s)", caller)
    try:
        run_twilio_stream_sync(ws, caller_number=caller)
    except Exception:
        logger.exception("WebSocket /media-stream session ended with error")


# ---------------------------------------------------------------------------
# FAQ helpers (used by tests / optional tooling)
# ---------------------------------------------------------------------------

def load_faq() -> dict:
    faq_path = os.path.join(os.path.dirname(__file__), FAQ_FILENAME)
    try:
        with open(faq_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning("FAQ file not found: %s", faq_path)
        return {}
    except Exception:
        logger.exception("Failed to load FAQ")
        return {}


def answer_from_faq(user_input: str):
    if user_input in faq_cache:
        return faq_cache[user_input]

    faq = load_faq()
    user_lower = user_input.lower().strip()
    for key, answer in faq.items():
        if key in user_lower:
            faq_cache[user_input] = answer
            return answer

    faq_cache[user_input] = None
    return None


# ---------------------------------------------------------------------------
# Speech / hours / rate-limit helpers
# ---------------------------------------------------------------------------

def extract_time_from_speech(text: str):
    """Parse an explicit clock time from speech text → (hour, minute) or (None, None)."""
    match = re.search(
        r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None, None

    hour = int(match.group(1))
    minute = int(match.group(2)) if match.group(2) else 0
    ampm = match.group(3).lower() if match.group(3) else ""

    if not ampm:
        if "morning" in text or "a.m." in text or "am" in text:
            ampm = "am"
        elif "afternoon" in text or "evening" in text or "p.m." in text or "pm" in text:
            ampm = "pm"

    if ampm in ("p.m.", "pm") and hour != 12:
        hour += 12
    elif ampm in ("a.m.", "am") and hour == 12:
        hour = 0
    return hour, minute


def is_rate_limited(
    caller_id: str,
    limit: int = RATE_LIMIT_MAX,
    window_seconds: int = RATE_LIMIT_WINDOW_SECONDS,
) -> bool:
    now = time.time()
    timestamps = rate_limit_store[caller_id]
    while timestamps and timestamps[0] < now - window_seconds:
        timestamps.pop(0)
    if len(timestamps) >= limit:
        return True
    timestamps.append(now)
    return False


def is_within_business_hours(dt: datetime) -> bool:
    """True if dt falls inside shop hours (America/Los_Angeles semantics on weekday)."""
    if dt.weekday() == 6:  # Sunday
        return False
    if dt.weekday() == 5:  # Saturday
        return SATURDAY_OPEN_HOUR <= dt.hour < SATURDAY_CLOSE_HOUR
    after_open = dt.hour > WEEKDAY_OPEN_HOUR or (
        dt.hour == WEEKDAY_OPEN_HOUR and dt.minute >= WEEKDAY_OPEN_MINUTE
    )
    before_close = dt.hour < WEEKDAY_CLOSE_HOUR or (
        dt.hour == WEEKDAY_CLOSE_HOUR and dt.minute <= WEEKDAY_CLOSE_MINUTE
    )
    return after_open and before_close


# ---------------------------------------------------------------------------
# SMS
# ---------------------------------------------------------------------------

def send_sms(
    to_number,
    appointment_time,
    calendar_link=None,
    shop_name=None,
    advisor_name=None,
):
    """Send booking confirmation SMS (A2P). Safe to call from a worker thread."""
    try:
        who = f" with {advisor_name}" if advisor_name else ""
        if calendar_link:
            body = SMS_WITH_LINK.format(
                who=who,
                appointment_time=appointment_time,
                calendar_link=calendar_link,
            )
        else:
            body = SMS_WITHOUT_LINK.format(
                who=who,
                appointment_time=appointment_time,
            )

        message = twilio_client.messages.create(
            body=body,
            from_=TWILIO_PHONE_NUMBER,
            to=to_number,
        )
        logger.info("SMS sent sid=%s to=%s", message.sid, to_number)
        return message.sid
    except Exception:
        logger.exception("Failed to send SMS to %s", to_number)
        return None


# ---------------------------------------------------------------------------
# Ops / legal routes
# ---------------------------------------------------------------------------

@app.route("/privacy", methods=["GET"])
def privacy():
    return PRIVACY_HTML


@app.route("/terms", methods=["GET"])
def terms():
    return TERMS_HTML


@app.route("/metrics", methods=["GET"])
def metrics():
    # Auth with the same secret used for Flask sessions (no separate METRICS_KEY).
    if not request.args.get("key") or request.args.get("key") != app.secret_key:
        return {"error": "Unauthorized"}, 401
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM call_logs")
        total_calls = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM idempotency_keys")
        total_bookings = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT COUNT(*) FROM idempotency_keys
            WHERE created_at >= datetime('now', '-7 days')
            """
        )
        bookings_7d = cursor.fetchone()[0]
        conn.close()
        return {
            "total_calls": total_calls,
            "total_bookings": total_bookings,
            "bookings_last_7_days": bookings_7d,
            "status": "ok",
        }
    except Exception:
        logger.exception("Metrics query failed")
        return {"error": "internal_error", "status": "error"}, 500


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.route("/robots.txt")
def robots():
    return ROBOTS_TXT


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    """Last-resort guard so uncaught exceptions don't take down the worker silently."""
    logger.exception("Unhandled Flask exception: %s", exc)
    return {"error": "internal_error"}, 500


init_db()

if __name__ == "__main__":
    # One process: HTTP (/voice, /health, ...) + WebSocket (/media-stream)
    # Expose with a single tunnel: ngrok http 5000
    try:
        app.run(host=APP_HOST, port=APP_PORT, debug=False)
    except Exception:
        logger.exception("Flask failed to start")
        raise
