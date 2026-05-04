#!/usr/bin/env python3
import argparse
import html
import os
import sys
from pathlib import Path

from aiohttp import web
import livekit.api as api
from env_loader import load_env_files
from listen_relay import handle_listen_mp3

_SCRIPT_DIR = Path(__file__).resolve().parent


def build_token(api_key: str, api_secret: str, room: str, identity: str, publish: bool) -> str:
    grants = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=publish,
        can_subscribe=True,
    )
    # strip: случайные пробелы/перевод строк в .env ломают подпись JWT vs yaml на сервере
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
    base = html.escape(helper_public_base_url(request), quote=True)
    lk_hint = html.escape(_default_livekit_ws_url(request), quote=True)
    page = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Listen — LiveKit</title>
  <style>
    body {{ font-family: sans-serif; margin: 1rem; max-width: 720px; margin-left: auto; margin-right: auto; }}
    .card {{ padding: 1rem; border: 1px solid #ddd; border-radius: 12px; }}
    input, button {{ padding: 10px; margin: 6px 0; width: 100%; box-sizing: border-box; }}
    .hint {{ font-size: 0.88rem; color: #444; line-height: 1.45; margin: 0.75rem 0; }}
    audio {{ width: 100%; min-height: 54px; }}
    code {{ word-break: break-all; }}
    .ok {{ color: #15803d; }}
  </style>
</head>
<body>
  <div class="card">
    <h2>Прослушивание эфира</h2>
    <p class="hint">
      Звук идёт как обычный <strong>HTTP‑поток MP3</strong> (<code>/listen.mp3</code>) — так браузер включает
      встроенный плеер и медиа‑сессию, как у музыки по ссылке.
      На сервере helper нужны <strong>ffmpeg</strong> и <code>LIVEKIT_API_KEY</code> / <code>LIVEKIT_API_SECRET</code>.
      Ведущий публикует в LiveKit через GUI‑клиент; эта страница только слушает комнату.
    </p>
    <label>Комната (room)</label>
    <input id="room" value="audio-room" autocomplete="off" />
    <button type="button" id="play">Слушать эфир</button>
    <p class="hint ok">Плеер ниже — стандартный элемент <code>&lt;audio controls&gt;</code>. Если ошибка: запустите трансляцию и нажмите снова.</p>
    <audio id="player" controls playsinline preload="none"></audio>
    <div class="status" id="status" style="margin-top:10px;color:#555;font-size:0.9rem;white-space:pre-wrap;">offline</div>
    <p class="hint">
      Прямая ссылка: <code>{base}/listen.mp3?room=audio-room</code><br />
      LiveKit WS (для стримера): <code>{lk_hint}</code>
    </p>
  </div>
  <script>
  (function () {{
    var player = document.getElementById("player");
    var roomEl = document.getElementById("room");
    var statusEl = document.getElementById("status");
    document.getElementById("play").addEventListener("click", function () {{
      var room = (roomEl && roomEl.value) ? roomEl.value.trim() : "audio-room";
      var u = new URL("/listen.mp3", window.location.origin);
      u.searchParams.set("room", room);
      player.src = u.toString();
      statusEl.textContent = "Запрос потока…";
      player.play().catch(function (e) {{ statusEl.textContent = "play: " + e; }});
    }});
    player.addEventListener("playing", function () {{ statusEl.textContent = "Воспроизведение (MP3)"; }});
    player.addEventListener("error", function () {{
      statusEl.textContent = "Ошибка /listen.mp3 (часто 503: нет эфира в комнате или нет ffmpeg на сервере).";
    }});
  }})();
  </script>
</body>
</html>
"""
    return web.Response(text=page, content_type="text/html")


async def listen_route(request: web.Request) -> web.StreamResponse:
    """HTTP MP3 из комнаты LiveKit для нативного audio src в браузере."""
    api_key = os.getenv("LIVEKIT_API_KEY", "").strip()
    api_secret = os.getenv("LIVEKIT_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise web.HTTPServiceUnavailable(text="На сервере не заданы LIVEKIT_API_KEY / LIVEKIT_API_SECRET.")

    def build_viewer_token(room: str, identity: str) -> str:
        return build_token(api_key, api_secret, room, identity, False)

    return await handle_listen_mp3(
        request,
        livekit_ws_url=livekit_public_ws_url(request),
        build_viewer_token=build_viewer_token,
    )


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
    app.router.add_get("/listen.mp3", listen_route)
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
    yaml_secret = str(keys.get(api_key) or "").strip()
    if yaml_secret != api_secret:
        warnings.append(
            "КРИТИЧНО: LIVEKIT_API_SECRET в livekit.env не совпадает с секретом для этого API-ключа "
            f"в deploy/livekit/livekit.yaml (ключ {api_key!r}). "
            "Выровняйте значения и выполните: "
            "`docker compose -f deploy/livekit/docker-compose.yml restart livekit`."
        )
        return warnings

    try:
        import jwt
    except ImportError:
        return warnings
    try:
        probe = build_token(api_key, api_secret, "audio-room", "jwt-probe", True)
        jwt.decode(probe, api_secret, algorithms=["HS256"], options={"verify_aud": False})
    except Exception as e:
        warnings.append(f"JWT из тех же ключей локально не проходит проверку подписи: {e}")
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
        f"  Веб: HTTP MP3 (нативный плеер): {helper_url}/listen.mp3?room={room}  (нужен ffmpeg в PATH на сервере)",
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
        print("Проверка: ключи в livekit.env совпадают с deploy/livekit/livekit.yaml; JWT подпись локально валидна.", file=sys.stderr)
        print("", file=sys.stderr)

    print(
        "Если клиент всё равно показывает 401 при подключении к LiveKit:",
        file=sys.stderr,
    )
    print(
        "  • Контейнер livekit после правки livekit.yaml должен быть перезапущен "
        "(start_server.sh теперь делает restart автоматически).",
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