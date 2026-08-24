import asyncio
import json
import websockets
import os
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_LIVE_URL = f"wss://generativelanguage.googleapis.com/ws/live/v1beta/models/gemini-3.1-flash-live-preview:live?key={GEMINI_API_KEY}"

async def handle_twilio_stream(websocket):
    """Handle Twilio Media Streams."""
    try:
        # Connect to Gemini Live
        async with websockets.connect(GEMINI_LIVE_URL) as gemini_ws:
            print("✅ Connected to Gemini Live")

            # Send setup message to Gemini
            setup = {
                "setup": {
                    "model": "gemini-3.1-flash-live-preview",
                    "generation_config": {
                        "response_modalities": ["AUDIO"]
                    }
                }
            }
            await gemini_ws.send(json.dumps(setup))
            print("📤 Sent Gemini setup")

            # Handle Twilio events
            while True:
                message = await websocket.recv()
                data = json.loads(message)
                event = data.get("event")

                # Handle Twilio Media Stream events
                if event == "start":
                    print("📞 Call started")
                elif event == "media":
                    # Forward audio to Gemini
                    audio_payload = data["media"]["payload"]
                    await gemini_ws.send(json.dumps({
                        "realtime_input": {
                            "media_chunks": [{
                                "data": audio_payload,
                                "mime_type": "audio/pcm"
                            }]
                        }
                    }))
                elif event == "stop":
                    print("📞 Call ended")
                    break

                # Handle Gemini responses
                try:
                    gemini_response = await asyncio.wait_for(gemini_ws.recv(), timeout=0.1)
                    gemini_data = json.loads(gemini_response)
                    if "audio" in gemini_data:
                        await websocket.send(json.dumps({
                            "event": "media",
                            "media": {
                                "payload": gemini_data["audio"]
                            }
                        }))
                except asyncio.TimeoutError:
                    continue

    except Exception as e:
        print(f"❌ WebSocket error: {e}")