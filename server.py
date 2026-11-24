#!/usr/bin/env python3
import argparse
import html
import os
import re
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
    # strip: случайные пробелы/перевод строк в .env ломают подпись JWT
    key, secret = api_key.strip(), api_secret.strip()
    return (
        api.AccessToken(key, secret)
        .with_identity(identity.strip())
        .with_name(identity.strip())
        .with_grants(grants)
        .to_jwt()
    )


def livekit_public_ws_url(request: web.Request) -> str:
    """Сигнальный WebSocket LiveKit: LIVEKIT_URL из env, иначе ws://<host запроса>:7880, иначе localhost."""
    env_url = os.getenv("LIVEKIT_URL", "").strip()
    if env_url:
        return env_url
    try:
        h = request.url.host
        if h and h not in ("127.0.0.1", "localhost", "::1"):
            return f"ws://{h}:7880"
    except Exception:
        pass
    return "ws://127.0.0.1:7880"


def _default_livekit_ws_url(request: web.Request) -> str:
    return livekit_public_ws_url(request)


def helper_public_base_url(request: web.Request) -> str:
    """Базовый URL этого helper (как к нему обратился клиент), без пути."""
    try:
        return str(request.url.origin()).rstrip("/")
    except Exception:
        return "http://127.0.0.1:8000"


async def client_discovery(request: web.Request) -> web.Response:
    """Для GUI: отдать рекомендуемые URL (из env на сервере + host запроса)."""
    room = os.getenv("LIVEKIT_DEFAULT_ROOM", "audio-room").strip() or "audio-room"
    return web.json_response(
        {
            "livekit_url": livekit_public_ws_url(request),
            "helper_url": helper_public_base_url(request),
            "default_room": room,
        }
    )


async def index(request: web.Request) -> web.Response:
    default_lk = html.escape(_default_livekit_ws_url(request), quote=True)
    page = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>LiveKit Audio Viewer</title>
  <style>
    body {{ font-family: sans-serif; margin: 2rem; }}
    .card {{ max-width: 800px; margin: 0 auto; padding: 1rem; border: 1px solid #ddd; border-radius: 10px; }}
    input, button {{ padding: 8px; margin: 4px 0; width: 100%; box-sizing: border-box; }}
    .status {{ margin-top: 8px; color: #444; white-space: pre-wrap; }}
    .hint {{ font-size: 0.9rem; color: #555; margin: 0.5rem 0 1rem; line-height: 1.4; }}
  </style>
</head>
<body>
  <div class="card">
    <h2>LiveKit Audio Viewer</h2>
    <p class="hint">
      <strong>LiveKit WS URL</strong> — адрес сигнального WebSocket (порт обычно 7880). Значение по умолчанию подставляется из LIVEKIT_URL или из имени хоста этой страницы.
      С другого ПК/телефона нельзя оставлять <code>127.0.0.1</code> — укажите IP или имя машины, где запущен LiveKit.
      Страница открыта по HTTPS — нужен <code>wss://</code> и TLS на стороне LiveKit (иначе браузер блокирует «небезопасный» ws).
    </p>
    <label>LiveKit WS URL</label>
    <input id="url" value="{default_lk}" />
    <label>Room</label>
    <input id="room" value="audio-room" />
    <label>Identity</label>
    <input id="identity" value="web-viewer" />
    <button id="join">Join room</button>
    <div class="status" id="status">offline</div>
    <audio id="audio" autoplay controls></audio>
  </div>
  <script type="module">
    import {{ Room }} from "https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.esm.mjs";
    const joinBtn = document.getElementById("join");
    const status = document.getElementById("status");
    const audio = document.getElementById("audio");
    let roomRef = null;

    joinBtn.addEventListener("click", async () => {{
      try {{
        if (roomRef) {{
          await roomRef.disconnect();
          roomRef = null;
        }}
        const url = document.getElementById("url").value.trim();
        const room = document.getElementById("room").value;
        const identity = document.getElementById("identity").value;
        const tokenResp = await fetch(`/livekit/token?room=${{encodeURIComponent(room)}}&identity=${{encodeURIComponent(identity)}}&role=viewer`, {{ cache: "no-store" }});
        if (!tokenResp.ok) {{
          const errBody = await tokenResp.text();
          throw new Error("токен: HTTP " + tokenResp.status + " " + errBody);
        }}
        const tokenJson = await tokenResp.json();
        if (!tokenJson.token) {{
          throw new Error(tokenJson.error || "ответ без token");
        }}
        const lkRoom = new Room();
        function attachIfAudio(track) {{
          if (track && track.kind === "audio") {{
            track.attach(audio);
            status.textContent = "audio subscribed";
          }}
        }}
        lkRoom.on("trackSubscribed", (track) => attachIfAudio(track));
        lkRoom.on("disconnected", () => {{ status.textContent = "disconnected"; }});
        await lkRoom.connect(url, tokenJson.token);
        roomRef = lkRoom;
        status.textContent = "connected";
        // Участники уже в комнате: цепляем уже опубликованные аудио (Maps в SDK)
        lkRoom.remoteParticipants.forEach((participant) => {{
          participant.audioTrackPublications.forEach((pub) => {{
            if (pub.track) attachIfAudio(pub.track);
            pub.on("subscribed", (track) => attachIfAudio(track));
          }});
        }});
      }} catch (e) {{
        let msg = (e && e.message) ? e.message : String(e);
        if (/Failed to fetch|NetworkError|load failed/i.test(msg)) {{
          msg += "\\n\\nЧастые причины: неверный хост (127.0.0.1 с другого устройства), LiveKit не запущен, порт 7880 закрыт файрволом, или страница по HTTPS а URL начинается с ws:// (нужен wss://).";
        }}
        status.textContent = "error: " + msg;
      }}
    }});
  </script>
</body>
</html>
"""
    return web.Response(text=page, content_type="text/html")


async def healthz(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def livekit_token(request: web.Request) -> web.Response:
    api_key = os.getenv("LIVEKIT_API_KEY", "").strip()
    api_secret = os.getenv("LIVEKIT_API_SECRET", "").strip()
    if not api_key or not api_secret:
        return web.json_response({"error": "LIVEKIT_API_KEY/SECRET not configured"}, status=500)

    room = request.query.get("room", "audio-room")
    identity = request.query.get("identity", "viewer")
    role = request.query.get("role", "viewer")
    publish = role == "publisher"
    token = build_token(api_key=api_key, api_secret=api_secret, room=room, identity=identity, publish=publish)
    return web.json_response({"token": token, "room": room, "identity": identity, "role": role})


async def livekit_publisher_token(request: web.Request) -> web.Response:
    api_key = os.getenv("LIVEKIT_API_KEY", "").strip()
    api_secret = os.getenv("LIVEKIT_API_SECRET", "").strip()
    pairing_secret = os.getenv("LIVEKIT_PAIRING_SECRET", "").strip()
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
    app.router.add_get("/client/discovery", client_discovery)
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
    401 от LiveKit («invalid token» / «no permissions») почти всегда из‑за того, что контейнер
    LiveKit запущен с другим секретом. Секрет хранится только в livekit.env, поэтому вместо сверки
    файлов проверяем живой сервер: подписываем токен и отправляем его на /rtc/validate.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    warnings: list[str] = []
    api_key = os.getenv("LIVEKIT_API_KEY", "").strip()
    api_secret = os.getenv("LIVEKIT_API_SECRET", "").strip()
    if not api_key or not api_secret:
        warnings.append("LIVEKIT_API_KEY или LIVEKIT_API_SECRET пусты в livekit.env.")
        return warnings

    lk_url = os.getenv("LIVEKIT_URL", "ws://127.0.0.1:7880").strip().rstrip("/")
    http_url = re.sub(r"^ws", "http", lk_url, count=1)
    token = build_token(api_key, api_secret, "audio-room", "key-probe", False)
    probe_url = f"{http_url}/rtc/validate?" + urllib.parse.urlencode({"access_token": token})
    try:
        with urllib.request.urlopen(probe_url, timeout=3):
            pass
    except urllib.error.HTTPError as e:
        if e.code == 401:
            warnings.append(
                f"КРИТИЧНО: LiveKit ({http_url}) отклоняет токен, подписанный LIVEKIT_API_SECRET из livekit.env. "
                "Контейнер запущен со старым секретом — пересоздайте его: "
                "`docker compose --env-file livekit.env -f deploy/livekit/docker-compose.yml up -d livekit`."
            )
        else:
            warnings.append(f"LiveKit ({http_url}) ответил HTTP {e.code} на проверку токена.")
    except Exception as e:
        warnings.append(f"Не удалось проверить ключи на LiveKit ({http_url}): {e}")
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
        "  Ошибка 401 при подключении к LiveKit: см. проверку ключей ниже (livekit.env ↔ запущенный LiveKit).",
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
        print("Проверка: LiveKit принимает токены, подписанные ключами из livekit.env.", file=sys.stderr)
        print("", file=sys.stderr)

    print(
        "Если клиент всё равно показывает 401 при подключении к LiveKit:",
        file=sys.stderr,
    )
    print(
        "  • После смены секретов в livekit.env перезапустите ./start_server.sh "
        "(он пересоздаст контейнеры с новыми значениями).",
        file=sys.stderr,
    )
    print(
        "  • В момент подключения с клиента выполните: "
        "`docker compose -f deploy/livekit/docker-compose.yml logs -f livekit` — там может быть "
        "503/другая ошибка; Rust/Python SDK местами показывает вместо неё «401 no permissions».",
        file=sys.stderr,
    )
    print(
        "  • Синхронизируйте время на сервере и на ПК с gui_client (NTP); JWT зависит от exp/nbf.",
        file=sys.stderr,
    )
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