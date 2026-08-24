import asyncio
import json
import websockets
import os
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_LIVE_URL = "wss://generativelanguage.googleapis.com/ws/live/v1beta/models/gemini-3.1-flash-live-preview:live?key=" + GEMINI_API_KEY

async def handle_twilio_stream(websocket, path):
    """Bridge Twilio Media Streams to Gemini Live."""
    try:
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

            # Continuously forward audio between Twilio and Gemini
            while True:
                message = await websocket.recv()
                data = json.loads(message)

                # Forward audio from Twilio to Gemini
                if data.get("event") == "media":
                    audio_payload = data["media"]["payload"]
                    await gemini_ws.send(json.dumps({
                        "realtime_input": {
                            "media_chunks": [{
                                "data": audio_payload,
                                "mime_type": "audio/pcm"
                            }]
                        }
                    }))

                # Forward audio from Gemini to Twilio
                gemini_response = await gemini_ws.recv()
                gemini_data = json.loads(gemini_response)
                if "audio" in gemini_data:
                    await websocket.send(json.dumps({
                        "event": "media",
                        "media": {
                            "payload": gemini_data["audio"]
                        }
                    }))

    except Exception as e:
        print(f"❌ WebSocket error: {e}")