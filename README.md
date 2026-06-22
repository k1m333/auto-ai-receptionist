# AI Receptionist for Auto Shops

An AI-powered phone assistant that handles 24/7 calls, answers FAQs, checks business hours, and books appointments directly into Google Calendar. Built with Twilio, Groq, and Google Calendar API.

## Live Demo

Call: +1 (978) 357-2799

Try it yourself — say something like:
- "What are your hours?"
- "Book an appointment for tomorrow at 10 AM."
- "Do you offer oil changes?"

## Features

- Voice call handling with natural conversation
- FAQ answering from a knowledge base
- Business hours (Mon-Fri 7:30-16:30, Sat 8-17, closed Sun)
- Google Calendar booking with availability checks
- SMS verification (A2P 10DLC approved)
- Rate limiting (10 calls per 60 seconds)
- Idempotency to prevent duplicate bookings
- Call logging with SQLite
- Metrics endpoint for monitoring
- SMS calendar invites via Google Calendar link
- Twilio signature verification for security
- Hybrid caller ID detection

## Tech Stack

- Python 3.11 + Flask
- Twilio (voice + SMS)
- Groq (Llama 3.1 8B)
- Google Calendar API
- SQLite
- Render (deployment)

## Architecture

Caller -> Twilio -> Flask App (Render) -> Groq (LLM)
                                    -> Google Calendar API
                                    -> SQLite (logging)
                                    -> SMS (Twilio)

## Environment Variables

Create a .env file with:

TWILIO_ACCOUNT_SID=your_sid
TWILIO_AUTH_TOKEN=your_token
TWILIO_PHONE_NUMBER=+19783572799
GROQ_API_KEY=your_groq_key
GOOGLE_APPLICATION_CREDENTIALS_JSON={"type":"service_account",...}
CALENDAR_ID=your_email@gmail.com
FLASK_SECRET_KEY=your_secret_key
METRICS_KEY=your_metrics_key (optional)

## Deployment

Deployed on Render.

1. Push code to GitHub
2. Connect repository to Render
3. Set environment variables in Render dashboard
4. Deploy

## Monitoring

Metrics endpoint (protected with METRICS_KEY):
https://auto-ai-receptionist.onrender.com/metrics?key=your_key

Returns:
{
  "total_calls": 42,
  "total_bookings": 15,
  "bookings_last_7_days": 12,
  "status": "ok"
}

## Testing

Call the Twilio number and complete a full booking:
1. Say your phone number
2. Receive SMS verification code
3. Say the code back
4. Book an appointment (e.g., "tomorrow at 10 AM")
5. Receive SMS confirmation with calendar invite link

## License

MIT — free to use and modify.

Built as a portfolio project to demonstrate AI + voice + calendar integration in production.