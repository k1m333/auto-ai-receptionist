# AI Receptionist (V2) — Auto Shop Voice Agent

Real-time phone receptionist powered by **Twilio Media Streams** and **Gemini Live** native audio. One Flask process serves HTTP webhooks and the WebSocket audio bridge on port `5000`, so a single ngrok tunnel is enough for local development.

**Live demo:** +1 (978) 357-2799

Try asking:
- “What are your hours?”
- “I need an oil change tomorrow afternoon.”
- “Book me with Sarah.”

---

## Features

- **Native audio streaming** — Bidirectional mulaw ↔ PCM bridge between Twilio and Gemini Live (voice: Aoede). Low-latency conversation, not turn-based STT → LLM → TTS.
- **Mock advisor scheduling** — Availability lives in `advisors.json` (Mike / Sarah / Jordan). The `check_calendar` tool finds a free slot, suggests alternatives, and removes booked times so they can’t double-book.
- **SMS confirmation** — On a successful book, Twilio sends an SMS with a Google Calendar *template* link (no Google Calendar API write). Caller ID is passed on the Stream URL so the model doesn’t need the number dictated.
- Business hours awareness (Mon–Fri 7:30–4:30, Sat 8–5, closed Sun)
- FAQ knowledge base (`faq.json`)
- Rate limiting, call logging (SQLite), `/metrics` and `/health`

---

## Architecture

```
Caller
  └─ Twilio Voice
       └─ POST /voice  →  TwiML <Connect><Stream>
            └─ wss://…/media-stream?caller=+1…
                 └─ flask-sock (same process / port as Flask)
                      └─ gemini_live_handler
                           ├─ Twilio  mulaw 8 kHz  →  PCM 16 kHz  →  Gemini Live
                           └─ Gemini  PCM 24 kHz  →  mulaw 8 kHz  →  Twilio
                           └─ tools: check_calendar, hang_up
                                └─ advisors.json + Twilio SMS invite
```

**Shared port.** `flask-sock` mounts `/media-stream` on the same Flask app that handles `/voice`. HTTP and WebSocket share port `5000`, so one public URL (ngrok or production host) covers both the voice webhook and the media stream.

**Audio transcoding.** Twilio Media Streams send base64 µ-law at 8 kHz. Gemini Live expects 16-bit PCM in at 16 kHz and returns PCM at 24 kHz. `gemini_live_handler` uses `audioop` (`audioop-lts` on Python 3.13+) to decode µ-law → linear PCM, resample, and reverse the path for playback.

---

## Setup Instructions

### 1. Prerequisites

- Python 3.11+ (3.13 needs `audioop-lts`, already in `requirements.txt`)
- A [Twilio](https://www.twilio.com/) account with a voice-capable number (SMS enabled for confirmations)
- A [Gemini API key](https://aistudio.google.com/) with Live / native-audio access
- [ngrok](https://ngrok.com/) (or another tunnel) for local Twilio webhooks

### 2. Install

```bash
python -m venv venv
# Windows
.\venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
cp .env.example .env   # then fill in real values
```

Edit `advisors.json` if you want different advisors or slots.

### 3. Run locally

```bash
python app.py
```

The app listens on `0.0.0.0:5000`.

### 4. Expose with ngrok

```bash
ngrok http 5000
```

Copy the HTTPS hostname (e.g. `spilt-jigsaw-nearly.ngrok-free.dev`) and set in `.env`:

```env
MEDIA_STREAM_URL=wss://YOUR_NGROK_HOST/media-stream
```

(Or set `NGROK_DOMAIN=YOUR_NGROK_HOST` and omit `MEDIA_STREAM_URL`.)

### 5. Point Twilio at your tunnel

In the Twilio Console → Phone Numbers → your number:

| Setting | Value |
|---|---|
| A call comes in | Webhook · `https://YOUR_NGROK_HOST/voice` · HTTP POST |

Restart `app.py` after changing `.env`, place a call, and you should see `/voice` then a `/media-stream` WebSocket connect in the logs.

---

## Environment Variables

Copy `.env.example` → `.env`. Required keys used by `app.py` / `gemini_live_handler.py`:

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | Yes | Google AI Studio key for Gemini Live WebSocket |
| `TWILIO_ACCOUNT_SID` | Yes | Twilio Account SID |
| `TWILIO_AUTH_TOKEN` | Yes | Twilio Auth Token |
| `TWILIO_PHONE_NUMBER` | Yes | E.164 number that sends booking SMS |
| `FLASK_SECRET_KEY` | Yes | Flask session secret (long random string) |
| `METRICS_KEY` | Yes* | Shared secret for `GET /metrics?key=…` |
| `MEDIA_STREAM_URL` | Recommended | Full `wss://…/media-stream` URL Twilio should open |
| `NGROK_DOMAIN` | Optional | Hostname-only fallback if `MEDIA_STREAM_URL` is unset |
| `GEMINI_LIVE_MODEL` | Optional | Overrides default Live native-audio model id |

\*If unset, `/metrics` rejects all requests.

---

## Tech Stack

- Python · Flask · flask-sock
- Twilio Voice + Media Streams + SMS
- Gemini Live (`BidiGenerateContent`, native audio)
- SQLite (call logs / idempotency)
- ngrok (local) · Render-ready (`gunicorn` in requirements)

---

### Troubleshooting

**Port 5000 is already in use (`OSError: [WinError 10048]`)**
If a previous Python process crashed or didn't close cleanly, it may still be holding onto port 5000. You can force-kill all hanging Python processes in PowerShell:
```powershell
Get-Process python* | Stop-Process -Force
```
(For macOS/Linux users, use ```killall python```)

---

## License

MIT — free to use and modify.
