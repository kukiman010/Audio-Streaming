#!/usr/bin/env python3
import argparse
import os
from aiohttp import web
import livekit.api as api
from env_loader import load_env_files


def build_token(api_key: str, api_secret: str, room: str, identity: str, publish: bool) -> str:
    grants = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=publish,
        can_subscribe=True,
    )
    return (
        api.AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(grants)
        .to_jwt()
    )


async def index(_: web.Request) -> web.Response:
    html = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>LiveKit Audio Viewer</title>
  <style>
    body { font-family: sans-serif; margin: 2rem; }
    .card { max-width: 800px; margin: 0 auto; padding: 1rem; border: 1px solid #ddd; border-radius: 10px; }
    input, button { padding: 8px; margin: 4px 0; width: 100%; box-sizing: border-box; }
    .status { margin-top: 8px; color: #444; }
  </style>
</head>
<body>
  <div class="card">
    <h2>LiveKit Audio Viewer</h2>
    <label>LiveKit WS URL</label>
    <input id="url" value="ws://127.0.0.1:7880" />
    <label>Room</label>
    <input id="room" value="audio-room" />
    <label>Identity</label>
    <input id="identity" value="web-viewer" />
    <button id="join">Join room</button>
    <div class="status" id="status">offline</div>
    <audio id="audio" autoplay controls></audio>
  </div>
  <script type="module">
    import { Room } from "https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.esm.mjs";
    const joinBtn = document.getElementById("join");
    const status = document.getElementById("status");
    const audio = document.getElementById("audio");
    let roomRef = null;

    joinBtn.addEventListener("click", async () => {
      try {
        if (roomRef) {
          await roomRef.disconnect();
          roomRef = null;
        }
        const url = document.getElementById("url").value;
        const room = document.getElementById("room").value;
        const identity = document.getElementById("identity").value;
        const tokenResp = await fetch(`/livekit/token?room=${encodeURIComponent(room)}&identity=${encodeURIComponent(identity)}&role=viewer`, { cache: "no-store" });
        const tokenJson = await tokenResp.json();
        const lkRoom = new Room();
        lkRoom.on("trackSubscribed", (track) => {
          if (track.kind === "audio") {
            track.attach(audio);
            status.textContent = "audio subscribed";
          }
        });
        lkRoom.on("disconnected", () => { status.textContent = "disconnected"; });
        await lkRoom.connect(url, tokenJson.token);
        roomRef = lkRoom;
        status.textContent = "connected";
      } catch (e) {
        status.textContent = `error: ${e}`;
      }
    });
  </script>
</body>
</html>
"""
    return web.Response(text=html, content_type="text/html")


async def healthz(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def livekit_token(request: web.Request) -> web.Response:
    api_key = os.getenv("LIVEKIT_API_KEY", "")
    api_secret = os.getenv("LIVEKIT_API_SECRET", "")
    if not api_key or not api_secret:
        return web.json_response({"error": "LIVEKIT_API_KEY/SECRET not configured"}, status=500)

    room = request.query.get("room", "audio-room")
    identity = request.query.get("identity", "viewer")
    role = request.query.get("role", "viewer")
    publish = role == "publisher"
    token = build_token(api_key=api_key, api_secret=api_secret, room=room, identity=identity, publish=publish)
    return web.json_response({"token": token, "room": room, "identity": identity, "role": role})


async def livekit_publisher_token(request: web.Request) -> web.Response:
    api_key = os.getenv("LIVEKIT_API_KEY", "")
    api_secret = os.getenv("LIVEKIT_API_SECRET", "")
    pairing_secret = os.getenv("LIVEKIT_PAIRING_SECRET", "")
    if not api_key or not api_secret or not pairing_secret:
        return web.json_response({"error": "server secrets not configured"}, status=500)

    room = request.query.get("room", "audio-room")
    identity = request.query.get("identity", "publisher")
    provided = request.query.get("pairing_secret", "")
    if provided != pairing_secret:
        return web.json_response({"error": "invalid pairing secret"}, status=403)

    token = build_token(api_key=api_key, api_secret=api_secret, room=room, identity=identity, publish=True)
    return web.json_response({"token": token, "room": room, "identity": identity, "role": "publisher"})


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/livekit/token", livekit_token)
    app.router.add_get("/livekit/publisher_token", livekit_publisher_token)
    return app


def main() -> None:
    load_env_files(("livekit.env", ".env"))
    parser = argparse.ArgumentParser(description="LiveKit helper service for web viewer + token issuing")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    web.run_app(make_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()