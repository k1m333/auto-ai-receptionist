"""
Gemini Live session bridge for Twilio Media Streams.

Audio path:
  Twilio  μ-law 8 kHz  →  PCM 16-bit 16 kHz  →  Gemini Live
  Gemini  PCM 16-bit 24 kHz  →  μ-law 8 kHz  →  Twilio

Works with:
- flask-sock sync sockets via FlaskSockAdapter (app.py /media-stream)
- websockets asyncio clients (optional websocket_server.py)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timedelta
from urllib.parse import parse_qs, quote, unquote, urlsplit

import pytz
import websockets
from dotenv import load_dotenv

try:
    import audioop  # stdlib through Python 3.12
except ModuleNotFoundError:
    import audioop_lts as audioop  # Python 3.13+

load_dotenv()

logger = logging.getLogger("ai-receptionist.gemini_live")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TIMEZONE_NAME = "America/Los_Angeles"

# Twilio telephony audio
TWILIO_RATE = 8000
# Gemini Live native audio
GEMINI_INPUT_RATE = 16000
GEMINI_OUTPUT_RATE = 24000
PCM_WIDTH = 2  # 16-bit little-endian
PCM_CHANNELS = 1

APPOINTMENT_MINUTES = 15
SUGGESTION_COUNT = 3
HANGUP_AUDIO_GRACE_SECONDS = 1.5
GEMINI_SETUP_TIMEOUT_SECONDS = 10.0
# How long silence must last before Gemini ends the caller's speech turn.
VAD_SILENCE_DURATION_MS = 800
VAD_PREFIX_PADDING_MS = 20

GEMINI_VOICE_NAME = "Aoede"
DEFAULT_CALLER_NAME = "Customer"
UNKNOWN_PHONE = "unknown"

# Explicit goodbye phrases required before hang_up is honored server-side.
GOODBYE_PHRASES = (
    "goodbye",
    "good bye",
    "bye",
    "bye bye",
    "that's all",
    "thats all",
    "that is all",
    "nothing else",
    "all set",
    "have a good day",
    "have a nice day",
    "hang up",
    "end the call",
    "end call",
)

ADVISORS_FILENAME = "advisors.json"
ADVISORS_PATH = os.path.join(os.path.dirname(__file__), ADVISORS_FILENAME)

BUSINESS_HOURS_LINES = (
    "Monday–Friday: 7:30 AM – 4:30 PM",
    "Saturday: 8:00 AM – 5:00 PM",
    "Sunday: closed",
)

CALENDAR_TITLE = "Auto Repair Appointment"
CALENDAR_TITLE_WITH_ADVISOR = "Auto Repair Appointment with {advisor}"
CALENDAR_DETAILS = "Booked via AI Receptionist"
CALENDAR_DETAILS_WITH_ADVISOR = "Booked via AI Receptionist with {advisor}"
CALENDAR_RENDER_BASE = (
    "https://calendar.google.com/calendar/render?action=TEMPLATE"
)

GREETING_WITH_PHONE = (
    "A caller just connected from {caller_phone}. "
    "Greet them briefly, ask only for their name "
    "(you already have their phone for SMS), then ask how you can help."
)
GREETING_WITHOUT_PHONE = (
    "A caller just connected but their phone number is unknown. "
    "Greet them briefly, ask for their name, then ask how you can help. "
    "Use the call's From number for SMS when available later — "
    "do not make them dictate a full phone number unless needed."
)

SYSTEM_INSTRUCTION_TEMPLATE = """
You are the AI receptionist for an auto shop. Speak briefly and naturally on a phone call.

Today's date is {today_str} ({timezone} timezone).
If the caller says "tomorrow", that means {tomorrow_str}. Always calculate relative
days (today, tomorrow, next Monday, etc.) from today's date above.

Business hours ({timezone}):
- {hours_weekday}
- {hours_saturday}
- {hours_sunday}

The shop is closed on Sundays. Never book or offer a Sunday appointment. If the
caller asks for Sunday or "tomorrow" falls on Sunday, explain the shop is closed
and offer the next open weekday/Saturday instead.

When calling check_calendar, you MUST output standard ISO 8601 timestamps with
timezone offset, using 24-hour time with two-digit hours only (00–23).
Correct examples: 2026-11-17T14:00:00-08:00 for 2 PM, 2026-11-17T09:30:00-08:00 for 9:30 AM.
Never invent malformed times like 114:00:00 or hours above 23.

You can answer common service/pricing questions and book appointments with a
service advisor (Mike, Sarah, or Jordan). When the caller wants to schedule,
collect a preferred day/time, then call check_calendar with book=false.

check_calendar responses:
- status "available": that time is free with the named advisor. Tell the caller
  who they'd see, confirm, then call check_calendar with book=true for that time.
- status "busy": that time is taken. DO NOT search again. Read the suggestions
  (time + advisor) and ask which they prefer.
- status "success": confirmed — say the advisor's name and that a calendar invite
  SMS is on the way. Do not claim you wrote to Google Calendar directly.

Hang up ONLY when the caller clearly says a goodbye phrase such as goodbye, bye,
that's all, nothing else, or all set. Then say a brief polite goodbye and call
the hang_up tool. NEVER call hang_up because of silence, a pause, an interruption,
echo, background noise, or because you finished a sentence. If unsure, stay on
the line and ask if they need anything else.

Keep replies short (one or two sentences). Greet the caller once at the start.
If you already have their phone number from the call, ask for their name only —
do not ask them to dictate their phone number for SMS.
""".strip()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv(
    "GEMINI_LIVE_MODEL",
    "gemini-2.5-flash-native-audio-preview-12-2025",
)
GEMINI_LIVE_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    f"?key={GEMINI_API_KEY}"
)

HANG_UP_DECLARATION = {
    "name": "hang_up",
    "description": (
        "End the phone call ONLY after the caller explicitly said goodbye "
        "(goodbye, bye, that's all, nothing else, all set, etc.) AND you have "
        "already spoken your goodbye. Do NOT call this on silence, pauses, "
        "interruptions, echo, or when you simply finished talking."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "reason": {
                "type": "STRING",
                "description": (
                    "Must reflect an explicit caller goodbye, e.g. caller_said_goodbye. "
                    "Never use silence, interruption, or timeout as a reason."
                ),
            }
        },
        "required": ["reason"],
    },
}

CHECK_CALENDAR_DECLARATION = {
    "name": "check_calendar",
    "description": (
        "Check advisor availability in advisors.json for a requested time. "
        "Returns status available with an advisor name, or busy with suggestions. "
        "Set book=true only after the caller confirms; SMS includes the advisor name."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "start_time": {
                "type": "STRING",
                "description": (
                    "Requested appointment start in ISO 8601 format with timezone, "
                    "e.g. 2026-11-16T14:00:00-08:00"
                ),
            },
            "book": {
                "type": "BOOLEAN",
                "description": (
                    "If true, confirm the appointment with that advisor and SMS an invite. "
                    "Defaults to false (availability check only)."
                ),
            },
            "caller_name": {
                "type": "STRING",
                "description": "Caller name for the appointment.",
            },
            "caller_phone": {
                "type": "STRING",
                "description": "Caller phone number (SMS uses Twilio Caller when available).",
            },
            "advisor": {
                "type": "STRING",
                "description": "Optional preferred advisor name (Mike, Sarah, Jordan).",
            },
        },
        "required": ["start_time"],
    },
}

_last_booked_appointment: dict | None = None


# ---------------------------------------------------------------------------
# WebSocket adapters / errors
# ---------------------------------------------------------------------------

class TwilioSocketClosed(Exception):
    """Raised when the Twilio / flask-sock connection ends."""


class FlaskSockAdapter:
    """
    Adapt flask-sock's synchronous ws.receive()/ws.send() to the async
    interface expected by handle_twilio_stream (recv/send + async iteration).
    """

    def __init__(self, ws, caller_number: str | None = None):
        self._ws = ws
        self.caller_number = caller_number
        # Back-compat alias used by handle_twilio_stream fallbacks
        self.caller_phone = caller_number

    @property
    def environ(self):
        return getattr(self._ws, "environ", None) or {}

    async def recv(self):
        data = await asyncio.to_thread(self._ws.receive)
        if data is None:
            raise TwilioSocketClosed("Twilio WebSocket closed")
        return data

    async def send(self, data):
        await asyncio.to_thread(self._ws.send, data)

    async def close(self, code=1000, reason=""):
        close_fn = getattr(self._ws, "close", None)
        if not callable(close_fn):
            return
        try:
            result = close_fn()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("Error closing flask-sock WebSocket")

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.recv()
        except TwilioSocketClosed:
            raise StopAsyncIteration from None


# ---------------------------------------------------------------------------
# System prompt / Gemini setup
# ---------------------------------------------------------------------------

def _build_system_instruction() -> str:
    """Fresh system prompt with today's date so relative times resolve correctly."""
    la_tz = pytz.timezone(TIMEZONE_NAME)
    today = datetime.now(la_tz)
    tomorrow = today + timedelta(days=1)
    return SYSTEM_INSTRUCTION_TEMPLATE.format(
        today_str=today.strftime("%A, %B %d, %Y"),
        tomorrow_str=tomorrow.strftime("%A, %B %d, %Y"),
        timezone=TIMEZONE_NAME,
        hours_weekday=BUSINESS_HOURS_LINES[0],
        hours_saturday=BUSINESS_HOURS_LINES[1],
        hours_sunday=BUSINESS_HOURS_LINES[2],
    )


def _build_setup_message() -> dict:
    # Equivalent to LiveConnectConfig over the raw WebSocket protocol.
    return {
        "setup": {
            "model": f"models/{GEMINI_MODEL}",
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": GEMINI_VOICE_NAME}
                    }
                },
            },
            # Reduce false barge-in from handset echo of the AI's own voice.
            "realtimeInputConfig": {
                "automaticActivityDetection": {
                    "disabled": False,
                    "startOfSpeechSensitivity": "START_SENSITIVITY_LOW",
                    "endOfSpeechSensitivity": "END_SENSITIVITY_LOW",
                    "prefixPaddingMs": VAD_PREFIX_PADDING_MS,
                    "silenceDurationMs": VAD_SILENCE_DURATION_MS,
                },
                # Echo through the phone speaker must not cut off the AI mid-sentence.
                "activityHandling": "NO_INTERRUPTION",
            },
            "systemInstruction": {"parts": [{"text": _build_system_instruction()}]},
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            "tools": [
                {
                    "functionDeclarations": [
                        CHECK_CALENDAR_DECLARATION,
                        HANG_UP_DECLARATION,
                    ]
                }
            ],
        }
    }


def _transcript_text(payload: dict | None) -> str:
    if not payload:
        return ""
    return (payload.get("text") or payload.get("transcript") or "").strip()


def _looks_like_goodbye(*texts: str | None) -> bool:
    """True only if an explicit goodbye phrase appears in the combined text."""
    haystack = " ".join(t for t in texts if t).lower()
    if not haystack:
        return False
    return any(phrase in haystack for phrase in GOODBYE_PHRASES)


# ---------------------------------------------------------------------------
# Advisor schedule (advisors.json)
# ---------------------------------------------------------------------------

def _parse_start_time(start_time: str) -> datetime:
    la_tz = pytz.timezone(TIMEZONE_NAME)
    cleaned = start_time.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        return la_tz.localize(dt)
    return dt.astimezone(la_tz)


def _slot_key(dt: datetime) -> str:
    """Compare slots by local wall-clock minute (ignore offset formatting)."""
    la_tz = pytz.timezone(TIMEZONE_NAME)
    local = dt.astimezone(la_tz) if dt.tzinfo else la_tz.localize(dt)
    return local.strftime("%Y-%m-%dT%H:%M")


def load_advisors() -> list[dict]:
    try:
        with open(ADVISORS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("advisors") or []
    except FileNotFoundError:
        logger.error("Advisors file missing: %s", ADVISORS_PATH)
        return []
    except Exception:
        logger.exception("Failed to load advisors from %s", ADVISORS_PATH)
        return []


def save_advisors(advisors: list[dict]) -> None:
    try:
        with open(ADVISORS_PATH, "w", encoding="utf-8") as f:
            json.dump({"advisors": advisors}, f, indent=2)
            f.write("\n")
    except Exception:
        logger.exception("Failed to save advisors to %s", ADVISORS_PATH)


def _all_open_slots(advisors: list[dict]) -> list[tuple[datetime, dict, str]]:
    """Flatten (datetime, advisor, raw_slot_str) sorted by time."""
    rows = []
    for advisor in advisors:
        for raw in advisor.get("available_slots") or []:
            try:
                dt = _parse_start_time(raw)
            except ValueError:
                logger.warning("Skipping invalid advisor slot: %s", raw)
                continue
            rows.append((dt, advisor, raw))
    rows.sort(key=lambda r: r[0])
    return rows


def find_advisor_for_time(
    start: datetime, advisors: list[dict], preferred: str | None = None
) -> dict | None:
    """Return the first advisor who has this exact slot (optional name filter)."""
    want = _slot_key(start)
    preferred_l = preferred.strip().lower() if preferred else None
    for advisor in advisors:
        if preferred_l and advisor.get("name", "").lower() != preferred_l:
            continue
        for raw in advisor.get("available_slots") or []:
            try:
                if _slot_key(_parse_start_time(raw)) == want:
                    return advisor
            except ValueError:
                continue
    if preferred_l:
        return find_advisor_for_time(start, advisors, preferred=None)
    return None


def suggest_next_slots(
    start: datetime, advisors: list[dict], count: int = SUGGESTION_COUNT
) -> list[dict]:
    """Next open slots after `start` from any advisor."""
    suggestions = []
    for dt, advisor, _raw in _all_open_slots(advisors):
        if dt <= start:
            continue
        suggestions.append(
            {
                "slot": dt.isoformat(),
                "advisor": advisor.get("name"),
                "specialty": advisor.get("specialty"),
                "display": (
                    f"{dt.strftime('%A at %I:%M %p')} with {advisor.get('name')}"
                ),
            }
        )
        if len(suggestions) >= count:
            break
    return suggestions


def claim_advisor_slot(start: datetime, advisor_name: str) -> bool:
    """Remove a booked slot from advisors.json so it can't be double-booked."""
    advisors = load_advisors()
    want = _slot_key(start)
    changed = False
    for advisor in advisors:
        if advisor.get("name") != advisor_name:
            continue
        kept = []
        for raw in advisor.get("available_slots") or []:
            try:
                if _slot_key(_parse_start_time(raw)) == want:
                    changed = True
                    continue
            except ValueError:
                pass
            kept.append(raw)
        advisor["available_slots"] = kept
        break
    if changed:
        save_advisors(advisors)
    return changed


def generate_calendar_link(
    start_time_str: str, end_time_str: str, advisor_name: str | None = None
) -> str:
    """Build a Google Calendar TEMPLATE invite URL (no API credentials)."""
    start_utc = _parse_start_time(start_time_str).astimezone(pytz.UTC)
    end_utc = _parse_start_time(end_time_str).astimezone(pytz.UTC)
    start_fmt = start_utc.strftime("%Y%m%dT%H%M%SZ")
    end_fmt = end_utc.strftime("%Y%m%dT%H%M%SZ")
    title = (
        CALENDAR_TITLE_WITH_ADVISOR.format(advisor=advisor_name)
        if advisor_name
        else CALENDAR_TITLE
    )
    details = (
        CALENDAR_DETAILS_WITH_ADVISOR.format(advisor=advisor_name)
        if advisor_name
        else CALENDAR_DETAILS
    )
    return (
        f"{CALENDAR_RENDER_BASE}"
        f"&text={quote(title)}"
        f"&dates={start_fmt}/{end_fmt}"
        f"&details={quote(details)}"
    )


def check_calendar(
    start_time: str,
    book: bool = False,
    caller_name: str = DEFAULT_CALLER_NAME,
    caller_phone: str = UNKNOWN_PHONE,
    advisor: str | None = None,
) -> dict:
    """
    Advisor schedule tool backed by advisors.json.

    book=false → available + advisor name, or busy + next suggestions
    book=true  → claim slot, return success + SMS fields including advisor
    """
    global _last_booked_appointment

    try:
        start = _parse_start_time(start_time)
    except ValueError as exc:
        logger.warning("Invalid start_time from model: %s", start_time)
        return {"ok": False, "status": "error", "error": f"Invalid start_time: {exc}"}

    try:
        end = start + timedelta(minutes=APPOINTMENT_MINUTES)
        advisors = load_advisors()
        match = find_advisor_for_time(start, advisors, preferred=advisor)

        if not book:
            if match:
                return {
                    "ok": True,
                    "status": "available",
                    "advisor": match["name"],
                    "specialty": match.get("specialty"),
                    "slot": start.isoformat(),
                    "message": (
                        f"{start.strftime('%A, %B %d at %I:%M %p')} is available "
                        f"with {match['name']} ({match.get('specialty', 'advisor')})."
                    ),
                }

            suggestions = suggest_next_slots(start, advisors)
            return {
                "ok": True,
                "status": "busy",
                "requested_time": start.isoformat(),
                "suggestions": suggestions,
                "message": (
                    "That time is not available."
                    + (
                        " Options: "
                        + "; ".join(s["display"] for s in suggestions)
                        + "."
                        if suggestions
                        else " No later advisor slots found."
                    )
                    + " Ask which option they prefer — do not check again."
                ),
            }

        if not match:
            suggestions = suggest_next_slots(start, advisors)
            return {
                "ok": True,
                "status": "busy",
                "requested_time": start.isoformat(),
                "suggestions": suggestions,
                "message": "Cannot book that time. Offer the suggestions.",
            }

        advisor_name = match["name"]
        claim_advisor_slot(start, advisor_name)
        appointment_display = start.strftime("%A, %B %d at %I:%M %p")
        calendar_link = generate_calendar_link(
            start.isoformat(), end.isoformat(), advisor_name=advisor_name
        )
        _last_booked_appointment = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "advisor": advisor_name,
            "caller_name": caller_name,
            "caller_phone": caller_phone,
            "appointment_display": appointment_display,
            "calendar_link": calendar_link,
        }
        logger.info("Booked %s with %s", appointment_display, advisor_name)

        return {
            "ok": True,
            "status": "success",
            "message": f"Appointment confirmed with {advisor_name}.",
            "advisor": advisor_name,
            "slot": start.isoformat(),
            "appointment_display": appointment_display,
            "calendar_link": calendar_link,
            "caller_phone": caller_phone,
        }
    except Exception as exc:
        logger.exception("check_calendar failed")
        return {"ok": False, "status": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# Audio transcoding
# ---------------------------------------------------------------------------

def twilio_mulaw_to_gemini_pcm(payload_b64: str, ratecv_state=None):
    """Twilio μ-law 8 kHz → PCM 16 kHz for Gemini."""
    try:
        mulaw = base64.b64decode(payload_b64)
        pcm_8k = audioop.ulaw2lin(mulaw, PCM_WIDTH)
        pcm_16k, ratecv_state = audioop.ratecv(
            pcm_8k,
            PCM_WIDTH,
            PCM_CHANNELS,
            TWILIO_RATE,
            GEMINI_INPUT_RATE,
            ratecv_state,
        )
        return base64.b64encode(pcm_16k).decode("ascii"), ratecv_state
    except Exception:
        logger.exception("Failed Twilio→Gemini audio transcode")
        return None, ratecv_state


def gemini_pcm_to_twilio_mulaw(payload_b64: str, ratecv_state=None):
    """Gemini PCM 24 kHz → Twilio μ-law 8 kHz."""
    try:
        pcm_24k = base64.b64decode(payload_b64)
        pcm_8k, ratecv_state = audioop.ratecv(
            pcm_24k,
            PCM_WIDTH,
            PCM_CHANNELS,
            GEMINI_OUTPUT_RATE,
            TWILIO_RATE,
            ratecv_state,
        )
        mulaw = audioop.lin2ulaw(pcm_8k, PCM_WIDTH)
        return base64.b64encode(mulaw).decode("ascii"), ratecv_state
    except Exception:
        logger.exception("Failed Gemini→Twilio audio transcode")
        return None, ratecv_state


def _extract_audio_payload(gemini_data: dict) -> str | None:
    """Pull base64 PCM audio from a Gemini Live server message."""
    server_content = gemini_data.get("serverContent") or {}
    model_turn = server_content.get("modelTurn") or {}
    for part in model_turn.get("parts") or []:
        inline = part.get("inlineData") or part.get("inline_data")
        if not inline:
            continue
        mime = (inline.get("mimeType") or inline.get("mime_type") or "").lower()
        data = inline.get("data")
        if data and ("audio" in mime or "pcm" in mime or not mime):
            return data

    if "audio" in gemini_data:
        return gemini_data["audio"]
    return None


# ---------------------------------------------------------------------------
# Caller phone helpers
# ---------------------------------------------------------------------------

async def _close_twilio_socket(websocket) -> None:
    """Drop the Twilio Media Stream so Connect/Stream ends the call."""
    close = getattr(websocket, "close", None)
    if not callable(close):
        return
    try:
        result = close()
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        logger.exception("Error while closing Twilio WebSocket")


def _normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    phone = unquote(str(value)).strip()
    if not phone or phone.lower() == UNKNOWN_PHONE:
        return None
    return phone


def _format_e164(value: str | None) -> str | None:
    """Normalize a phone string to E.164 (+digits). Assumes US if 10 digits."""
    phone = _normalize_phone(value)
    if not phone:
        return None
    cleaned = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    if cleaned.startswith("+"):
        digits = cleaned[1:]
        return f"+{digits}" if digits.isdigit() and digits else None
    digits = "".join(ch for ch in cleaned if ch.isdigit())
    if not digits:
        return None
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}"


def _extract_caller_from_query(path_or_url: str | None) -> str | None:
    """Parse ?caller= from a WebSocket path / URL (Twilio Stream URLs ignore these)."""
    if not path_or_url:
        return None
    query = urlsplit(path_or_url).query
    if not query and "?" in path_or_url:
        query = path_or_url.split("?", 1)[1]
    params = parse_qs(query)
    values = params.get("caller") or params.get("From") or []
    return _format_e164(values[0] if values else None)


def _extract_caller_from_environ(websocket) -> str | None:
    """Parse caller from flask-sock / WSGI QUERY_STRING when request.args is empty."""
    environ = getattr(websocket, "environ", None)
    if not environ:
        raw_ws = getattr(websocket, "_ws", None)
        environ = getattr(raw_ws, "environ", None) if raw_ws is not None else None
    if not environ:
        return None
    qs = environ.get("QUERY_STRING") or ""
    if not qs:
        return None
    return _extract_caller_from_query(f"?{qs}")


def _extract_caller_from_start(start_payload: dict) -> str | None:
    """
    Resolve caller from a Twilio Media Streams `start` event.

    Preferred path: start.customParameters.caller (from TwiML <Parameter>).
    Also accepts start.caller / start.from if present.
    """
    if not start_payload:
        return None
    custom = (
        start_payload.get("customParameters")
        or start_payload.get("custom_parameters")
        or {}
    )
    caller = (
        start_payload.get("caller")
        or custom.get("caller")
        or custom.get("Caller")
        or custom.get("from_number")
        or custom.get("From")
        or start_payload.get("from")
        or start_payload.get("From")
    )
    return _format_e164(caller)


# ---------------------------------------------------------------------------
# Tools + SMS
# ---------------------------------------------------------------------------

async def _send_booking_sms_async(
    to_number: str,
    appointment_display: str,
    calendar_link=None,
    advisor_name: str | None = None,
):
    """Fire Twilio SMS off the event loop so Gemini audio is not blocked."""
    from app import send_sms

    try:
        await asyncio.to_thread(
            send_sms,
            to_number,
            appointment_display,
            calendar_link,
            None,
            advisor_name,
        )
    except Exception:
        logger.exception("Booking SMS task failed for %s", to_number)


async def _handle_tool_call(
    gemini_ws,
    tool_call: dict,
    websocket,
    caller_phone: str | None = None,
    last_user_transcript: str | None = None,
) -> bool:
    """
    Execute Gemini tool calls. Returns True if hang_up was honored
    (Twilio socket should be closed after any in-flight goodbye audio).

    `caller_phone` must be the E.164 number from the Twilio start event
    (customParameters), not something Gemini invents.

    hang_up is rejected unless an explicit goodbye phrase appears in the
    tool reason and/or the latest user input transcription.
    """
    function_responses = []
    should_hang_up = False
    stream_phone = (
        _format_e164(caller_phone)
        if caller_phone and caller_phone != UNKNOWN_PHONE
        else None
    )

    try:
        for fc in tool_call.get("functionCalls") or tool_call.get("function_calls") or []:
            name = fc.get("name")
            call_id = fc.get("id")
            args = fc.get("args") or {}
            logger.info("Gemini tool call: %s(%s)", name, args)

            if name == "check_calendar":
                book = bool(args.get("book", False))
                # Inject Twilio start-event caller into check_calendar.caller_phone
                phone = stream_phone or UNKNOWN_PHONE
                try:
                    result = await asyncio.to_thread(
                        check_calendar,
                        start_time=args.get("start_time"),
                        book=book,
                        caller_name=args.get("caller_name", DEFAULT_CALLER_NAME),
                        caller_phone=phone,
                        advisor=args.get("advisor"),
                    )
                except Exception as exc:
                    logger.exception("check_calendar tool raised")
                    result = {"ok": False, "status": "error", "error": str(exc)}

                if book and result.get("status") == "success":
                    sms_to = stream_phone or _format_e164(
                        result.get("caller_phone")
                    )
                    display = result.get("appointment_display")
                    link = result.get("calendar_link")
                    advisor_name = result.get("advisor")
                    if sms_to and sms_to != UNKNOWN_PHONE and display and link:
                        asyncio.create_task(
                            _send_booking_sms_async(
                                sms_to, display, link, advisor_name=advisor_name
                            )
                        )
                        result = {**result, "sms_queued": True, "sms_to": sms_to}
                    else:
                        logger.warning(
                            "Booking success but missing phone/link for SMS "
                            "(phone=%s display=%s link=%s)",
                            sms_to,
                            bool(display),
                            bool(link),
                        )
                        result = {**result, "sms_queued": False}

            elif name == "hang_up":
                reason = args.get("reason") or ""
                # Only trust the caller's transcribed words — the model can invent
                # a "goodbye" reason after a false interruption.
                if _looks_like_goodbye(last_user_transcript):
                    should_hang_up = True
                    result = {
                        "ok": True,
                        "hanging_up": True,
                        "reason": reason or "caller_said_goodbye",
                    }
                else:
                    logger.warning(
                        "Ignoring hang_up without explicit caller goodbye "
                        "(reason=%r transcript=%r)",
                        reason,
                        last_user_transcript,
                    )
                    result = {
                        "ok": False,
                        "hanging_up": False,
                        "error": (
                            "hang_up rejected: no goodbye heard from caller. "
                            "Stay on the line and continue helping."
                        ),
                    }
            else:
                result = {"ok": False, "error": f"Unknown tool: {name}"}

            function_responses.append(
                {"id": call_id, "name": name, "response": result}
            )

        await gemini_ws.send(
            json.dumps({"toolResponse": {"functionResponses": function_responses}})
        )
    except Exception:
        logger.exception("Failed while handling Gemini tool call")
        return False

    if should_hang_up:
        await asyncio.sleep(HANGUP_AUDIO_GRACE_SECONDS)
        logger.info("hang_up tool — closing Twilio WebSocket")
        await _close_twilio_socket(websocket)

    return should_hang_up


# ---------------------------------------------------------------------------
# Main bridge (core streaming logic preserved)
# ---------------------------------------------------------------------------

async def handle_twilio_stream(websocket, caller_number: str | None = None):
    """
    Bridge one Twilio Media Stream WebSocket to a Gemini Live session.

    `websocket` may be a websockets connection OR a FlaskSockAdapter.
    Caller ID is taken from the Twilio `start` event (customParameters /
    start.caller), not from the Stream URL query string (unsupported by Twilio).
    """
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY not set — closing Twilio stream")
        await _close_twilio_socket(websocket)
        return

    # Mutable session state shared by both bridge tasks (avoids stale closures).
    call_state: dict = {
        "stream_sid": None,
        "caller_phone": _format_e164(caller_number)
        or _format_e164(getattr(websocket, "caller_number", None))
        or _format_e164(getattr(websocket, "caller_phone", None)),
        # Half-duplex: while the AI is talking, don't feed mic audio to Gemini
        # (handset speaker echo otherwise looks like a barge-in).
        "model_speaking": False,
        "last_user_transcript": "",
    }

    inbound_rate_state = None
    outbound_rate_state = None
    logger.info(
        "Twilio WebSocket connected (pre-start caller=%s)",
        call_state["caller_phone"],
    )

    try:
        async with websockets.connect(
            GEMINI_LIVE_URL,
            additional_headers={"Content-Type": "application/json"},
        ) as gemini_ws:
            await gemini_ws.send(json.dumps(_build_setup_message()))
            logger.info(
                "Sent Gemini Live setup (pcm %sk in / %sk out + tools)",
                GEMINI_INPUT_RATE // 1000,
                GEMINI_OUTPUT_RATE // 1000,
            )

            setup_raw = await asyncio.wait_for(
                gemini_ws.recv(), timeout=GEMINI_SETUP_TIMEOUT_SECONDS
            )
            setup_msg = json.loads(setup_raw)
            if "setupComplete" not in setup_msg and "setup_complete" not in setup_msg:
                logger.warning("Unexpected Gemini setup response: %s", setup_msg)
            else:
                logger.info("Gemini Live setup complete")

            # Soft greeting; SMS uses caller_phone from the start event below.
            greet = (
                GREETING_WITH_PHONE.format(caller_phone=call_state["caller_phone"])
                if call_state["caller_phone"]
                else GREETING_WITHOUT_PHONE
            )
            await gemini_ws.send(
                json.dumps(
                    {
                        "clientContent": {
                            "turns": [
                                {"role": "user", "parts": [{"text": greet}]}
                            ],
                            "turnComplete": True,
                        }
                    }
                )
            )

            async def twilio_to_gemini():
                nonlocal inbound_rate_state
                async for raw in websocket:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("Non-JSON Twilio frame skipped")
                        continue

                    event = data.get("event")
                    if event == "connected":
                        logger.info("Twilio stream connected")
                    elif event == "start":
                        start_info = data.get("start") or {}
                        call_state["stream_sid"] = data.get(
                            "streamSid"
                        ) or start_info.get("streamSid")

                        # Primary source: TwiML <Parameter name="caller"> →
                        # start.customParameters.caller (also checks start.caller).
                        extracted = _extract_caller_from_start(start_info)
                        if extracted:
                            call_state["caller_phone"] = extracted
                            logger.info(
                                "Extracted caller from Twilio start event: %s",
                                call_state["caller_phone"],
                            )
                        else:
                            logger.warning(
                                "Twilio start event had no caller "
                                "(customParameters=%s)",
                                start_info.get("customParameters")
                                or start_info.get("custom_parameters"),
                            )

                        logger.info(
                            "Call started streamSid=%s caller=%s",
                            call_state["stream_sid"],
                            call_state["caller_phone"],
                        )
                    elif event == "media":
                        # Half-duplex mute: drop inbound while Gemini is speaking
                        # so speaker echo cannot trip VAD / interrupt the reply.
                        if call_state["model_speaking"]:
                            continue
                        payload = data.get("media", {}).get("payload")
                        if not payload:
                            continue
                        pcm_b64, inbound_rate_state = twilio_mulaw_to_gemini_pcm(
                            payload, inbound_rate_state
                        )
                        if not pcm_b64:
                            continue
                        await gemini_ws.send(
                            json.dumps(
                                {
                                    "realtimeInput": {
                                        "audio": {
                                            "mimeType": (
                                                f"audio/pcm;rate={GEMINI_INPUT_RATE}"
                                            ),
                                            "data": pcm_b64,
                                        }
                                    }
                                }
                            )
                        )
                    elif event == "stop":
                        logger.info("Call ended (Twilio stop)")
                        break

            async def gemini_to_twilio():
                nonlocal outbound_rate_state
                async for raw in gemini_ws:
                    try:
                        gemini_data = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("Non-JSON Gemini frame skipped")
                        continue

                    # Track caller transcript for hang_up goodbye gating.
                    sc_early = gemini_data.get("serverContent") or {}
                    user_tx = _transcript_text(
                        sc_early.get("inputTranscription")
                        or sc_early.get("input_transcription")
                    )
                    if user_tx:
                        call_state["last_user_transcript"] = user_tx

                    tool_call = gemini_data.get("toolCall") or gemini_data.get(
                        "tool_call"
                    )
                    if tool_call:
                        hung_up = await _handle_tool_call(
                            gemini_ws,
                            tool_call,
                            websocket,
                            call_state["caller_phone"],
                            last_user_transcript=call_state["last_user_transcript"],
                        )
                        if hung_up:
                            break
                        continue

                    server_content = gemini_data.get("serverContent") or {}
                    stream_sid = call_state["stream_sid"]

                    if server_content.get("turnComplete") or server_content.get(
                        "turn_complete"
                    ):
                        call_state["model_speaking"] = False

                    if server_content.get("interrupted"):
                        # With NO_INTERRUPTION this should be rare; still clear
                        # Twilio buffer but do not treat as a hang-up signal.
                        call_state["model_speaking"] = False
                        outbound_rate_state = None
                        logger.info(
                            "Gemini interrupted event (echo/barge-in) — clearing "
                            "playback, staying on the call"
                        )
                        if stream_sid:
                            try:
                                await websocket.send(
                                    json.dumps(
                                        {"event": "clear", "streamSid": stream_sid}
                                    )
                                )
                            except Exception:
                                logger.exception("Failed to send Twilio clear")
                        continue

                    audio_b64 = _extract_audio_payload(gemini_data)
                    if not audio_b64 or not stream_sid:
                        continue

                    call_state["model_speaking"] = True
                    mulaw_b64, outbound_rate_state = gemini_pcm_to_twilio_mulaw(
                        audio_b64, outbound_rate_state
                    )
                    if not mulaw_b64:
                        continue
                    try:
                        await websocket.send(
                            json.dumps(
                                {
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {"payload": mulaw_b64},
                                }
                            )
                        )
                    except Exception:
                        logger.exception("Failed to send media to Twilio")
                        break

            twilio_task = asyncio.create_task(twilio_to_gemini())
            gemini_task = asyncio.create_task(gemini_to_twilio())
            done, pending = await asyncio.wait(
                {twilio_task, gemini_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc and not isinstance(
                    exc, (TwilioSocketClosed, websockets.exceptions.ConnectionClosed)
                ):
                    logger.exception("Bridge task failed: %s", exc)

    except (websockets.exceptions.ConnectionClosed, TwilioSocketClosed) as exc:
        logger.info("WebSocket closed: %s", exc)
    except asyncio.TimeoutError:
        logger.error("Timed out waiting for Gemini Live setup")
    except Exception:
        logger.exception("Gemini Live bridge error")


def run_twilio_stream_sync(ws, caller_number: str | None = None):
    """
    Entry point for flask-sock (synchronous) routes.
    Caller is expected from the Twilio start event; optional seed is fine.
    """
    phone = _format_e164(caller_number)
    adapter = FlaskSockAdapter(ws, caller_number=phone)
    try:
        asyncio.run(handle_twilio_stream(adapter, caller_number=phone))
    except Exception:
        logger.exception("run_twilio_stream_sync failed")
