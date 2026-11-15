"""
Optional standalone Twilio Media Streams WebSocket server.

Local / free-ngrok development should NOT use this file — run `python app.py`
instead. Flask + flask-sock serve both /voice and /media-stream on port 5000
so a single `ngrok http 5000` tunnel reaches everything.

Keep this for dual-process deploys (e.g. Render websocket-server service) where
HTTP and WebSockets are split across hosts.
"""

import asyncio
import os

import websockets
from dotenv import load_dotenv

from gemini_live_handler import handle_twilio_stream

load_dotenv()


async def twilio_media_handler(websocket):
    """
    Accept Twilio connections on /media-stream.

    Twilio sends JSON events:
      - connected / start / media / stop
    media.payload is base64-encoded mulaw (audio/pcmu) at 8 kHz.
    """
    path = getattr(getattr(websocket, "request", None), "path", "") or ""
    if path and not path.startswith("/media-stream"):
        print(f"⚠️ Rejecting unexpected path: {path}")
        await websocket.close(code=1008, reason="Expected /media-stream")
        return

    await handle_twilio_stream(websocket)


async def main():
    port = int(os.environ.get("PORT", os.environ.get("WEBSOCKET_PORT", 8765)))
    host = os.environ.get("WEBSOCKET_HOST", "0.0.0.0")
    print(
        "⚠️  Standalone websocket_server.py — for local/ngrok use `python app.py` "
        "so HTTP + WS share one port."
    )
    print(f"🚀 Twilio Media Stream WebSocket listening on ws://{host}:{port}/media-stream")

    async with websockets.serve(
        twilio_media_handler,
        host,
        port,
        ping_interval=20,
        ping_timeout=20,
        max_size=8 * 1024 * 1024,
    ):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
