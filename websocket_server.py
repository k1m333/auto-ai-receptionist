import asyncio
import json
import websockets
import os
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY not set in environment")

GEMINI_LIVE_URL = f"wss://generativelanguage.googleapis.com/ws/live/v1beta/models/gemini-3.1-flash-live-preview:live?key={GEMINI_API_KEY}"

async def handle_twilio_stream(websocket, path):
    """Handle Twilio Media Streams and bridge to Gemini Live."""
    try:
        print("🔗 Twilio WebSocket connected")

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

            # Main loop: forward audio both ways
            while True:
                try:
                    # Receive from Twilio
                    message = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                    data = json.loads(message)
                    event = data.get("event")

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

                    # Receive from Gemini and send to Twilio
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
                        # No Gemini audio right now, keep listening
                        pass

                except asyncio.TimeoutError:
                    # No Twilio message, continue
                    continue
                except websockets.exceptions.ConnectionClosed:
                    print("🔌 WebSocket connection closed by Twilio")
                    break

    except Exception as e:
        print(f"❌ WebSocket error: {e}")

async def main():
    port = int(os.environ.get("WEBSOCKET_PORT", 8765))
    print(f"🚀 WebSocket server starting on port {port}...")
    async with websockets.serve(handle_twilio_stream, "0.0.0.0", port):
        await asyncio.Future()  # Run forever

if __name__ == "__main__":
    asyncio.run(main())