# AI Receptionist – Project Context

## Tech Stack
- Python / Flask, Twilio, Groq, Google Calendar API
- Render (deployment), SQLite (logging)

## Core Features
- Voice call handling (Twilio webhook)
- FAQ answers (`faq.json`)
- Business hours check (Mon–Fri 7:30‑16:30, Sat 8‑17, closed Sun)
- Next available slot suggestion
- Calendar availability & booking (Google Calendar API)
- SMS verification (A2P approved)
- Rate limiting (10 calls / 60 sec, in‑memory)
- Twilio signature verification
- Call logging (SQLite)
- Idempotency (phone + start_time)

## Patterns & Conventions
- **Idempotency key:** `{phone}_{start_time_str}` – prevents duplicate bookings.
- **Rate limiting:** In‑memory dict per `Caller` ID.
- **Error handling:** Logs errors to console and returns user‑friendly messages.
- **Testing:** Use Twilio phone calls for E2E testing (SMS verification required).

## Known Issues
- None.