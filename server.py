#!/usr/bin/env python3
import argparse
import os
import sys
from pathlib import Path

from aiohttp import web
import livekit.api as api
from env_loader import load_env_files

_SCRIPT_DIR = Path(__file__).resolve().parent


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


def _load_project_env() -> None:
    load_env_files((str(_SCRIPT_DIR / "livekit.env"), str(_SCRIPT_DIR / ".env")))
    load_env_files(("livekit.env", ".env"), override_existing=True)


def _client_facing_helper_url(bind_port: int) -> str:
    explicit = os.getenv("HELPER_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    return f"http://127.0.0.1:{bind_port}"


def collect_livekit_key_mismatch_warnings() -> list[str]:
    """
    401 от LiveKit («invalid token» / «no permissions») почти всегда из‑за того, что
    LIVEKIT_API_KEY / LIVEKIT_API_SECRET в livekit.env не совпадают с keys: в deploy/livekit/livekit.yaml.
    """
    warnings: list[str] = []
    yaml_path = _SCRIPT_DIR / "deploy" / "livekit" / "livekit.yaml"
    api_key = os.getenv("LIVEKIT_API_KEY", "").strip()
    api_secret = os.getenv("LIVEKIT_API_SECRET", "").strip()
    if not api_key or not api_secret:
        warnings.append("LIVEKIT_API_KEY или LIVEKIT_API_SECRET пусты в окружении.")
        return warnings
    if not yaml_path.is_file():
        warnings.append(f"Нет файла {yaml_path} — проверка ключей пропущена.")
        return warnings
    try:
        import yaml
    except ImportError:
        warnings.append("Установите PyYAML (pip install PyYAML), чтобы проверять ключи против livekit.yaml.")
        return warnings
    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except Exception as e:
        warnings.append(f"Не удалось прочитать livekit.yaml: {e}")
        return warnings
    keys = (data or {}).get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, dict):
        warnings.append("В livekit.yaml нет секции keys — проверьте конфиг Docker LiveKit.")
        return warnings
    if api_key not in keys:
        warnings.append(
            f"LIVEKIT_API_KEY={api_key!r} отсутствует в keys в livekit.yaml "
            f"(есть ключи: {list(keys.keys())}). Токены будут отклонены с 401."
        )
        return warnings
    yaml_secret = keys.get(api_key)
    if yaml_secret != api_secret:
        warnings.append(
            "КРИТИЧНО: LIVEKIT_API_SECRET в livekit.env не совпадает с секретом для этого API-ключа "
            f"в deploy/livekit/livekit.yaml (ключ {api_key!r}). "
            "Выровняйте значения и перезапустите контейнер LiveKit: "
            "`docker compose -f deploy/livekit/docker-compose.yml up -d`."
        )
    return warnings


def print_client_connection_banner(*, bind_host: str, bind_port: int) -> None:
    lk_url = os.getenv("LIVEKIT_URL", "ws://127.0.0.1:7880").strip()
    helper_url = _client_facing_helper_url(bind_port)
    pairing = os.getenv("LIVEKIT_PAIRING_SECRET", "").strip()
    api_key_name = os.getenv("LIVEKIT_API_KEY", "").strip()
    room = "audio-room"
    publisher_id = os.getenv("LIVEKIT_PUBLISHER_IDENTITY", "publisher-local").strip() or "publisher-local"
    viewer_id = "web-viewer"

    lines = [
        "",
        "=" * 72,
        "  Параметры для GUI-клиента и веб-просмотра (скопируйте в клиент на своей машине)",
        "=" * 72,
        f"  LiveKit URL (ws/wss):     {lk_url}",
        f"  Helper URL (http):        {helper_url}",
        f"  API key (имя в JWT):      {api_key_name or '(не задан)'}",
        f"  Комната (room):           {room}   (любое совпадающее имя у publisher и viewer)",
        f"  Identity (publisher):    {publisher_id}",
        f"  Identity (web viewer):    {viewer_id}",
    ]
    if pairing:
        lines.append(f"  Pairing secret:           {pairing}")
    else:
        lines.append("  Pairing secret:           (не задан — задайте LIVEKIT_PAIRING_SECRET в livekit.env)")
    if "127.0.0.1" in lk_url or "localhost" in lk_url.lower():
        lines.append("")
        lines.append(
            "  >>> Удалённый клиент: в livekit.env на сервере задайте LIVEKIT_URL и HELPER_URL с публичным IP/доменом,"
        )
        lines.append(
            "      чтобы этот блок совпадал с тем, что вводите в GUI (иначе легко перепутать адреса)."
        )
    lines += [
        "",
        f"  Helper слушает:           http://{bind_host}:{bind_port}/",
        "  Ошибка 401 при подключении к LiveKit: см. проверку ключей выше (livekit.env ↔ livekit.yaml).",
        "=" * 72,
        "",
    ]
    print("\n".join(lines), file=sys.stderr)

    key_issues = collect_livekit_key_mismatch_warnings()
    if key_issues:
        print("!!! ПРОБЛЕМА С КЛЮЧАМИ (типичная причина HTTP 401 от LiveKit):", file=sys.stderr)
        for msg in key_issues:
            print(f"    • {msg}", file=sys.stderr)
        print("", file=sys.stderr)
    else:
        print("Проверка: LIVEKIT_API_KEY / LIVEKIT_API_SECRET совпадают с deploy/livekit/livekit.yaml.", file=sys.stderr)
        print("", file=sys.stderr)


def main() -> None:
    _load_project_env()
    parser = argparse.ArgumentParser(description="LiveKit helper service for web viewer + token issuing")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    print_client_connection_banner(bind_host=args.host, bind_port=args.port)
    web.run_app(make_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()