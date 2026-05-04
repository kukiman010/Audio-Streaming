#!/usr/bin/env python3
import argparse
import html
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
    .stream-audio {{ width: 100%; margin-top: 0.5rem; min-height: 48px; }}
    #tapToPlay {{ display: none; margin-top: 12px; padding: 12px; background: #fff3cd; border: 1px solid #ffc107; border-radius: 8px; }}
    #tapToPlay.show {{ display: block; }}
    #tapToPlayBtn {{ font-size: 1.05rem; font-weight: 600; }}
  </style>
</head>
<body>
  <div class="card">
    <h2>LiveKit Audio Viewer</h2>
    <p class="hint">
      <strong>LiveKit WS URL</strong> — адрес сигнального WebSocket (порт обычно 7880). Значение по умолчанию подставляется из LIVEKIT_URL или из имени хоста этой страницы.
      С другого ПК/телефона нельзя оставлять <code>127.0.0.1</code> — укажите IP или имя машины, где запущен LiveKit.
      Страница открыта по HTTPS — нужен <code>wss://</code> и TLS на стороне LiveKit (иначе браузер блокирует «небезопасный» ws).
      Звук — WebRTC (<code>MediaStream</code> в LiveKit), не прогрессивный MP3 по URL: внешне у <code>&lt;audio controls&gt;</code> привычный плеер внизу элемента; на телефоне для шторки уведомлений обновляется <strong>Media Session</strong>. На iOS Safari звук при заблокированном экране иногда ограничен самой системой — держите вкладку активной или используйте «Включить звук», если эфир начался после входа. Нужен актуальный <code>server.py</code> на машине. Соединение с комнатой не рвётся при уходе со страницы (в SDK отключено авто‑отключение).
    </p>
    <label>LiveKit WS URL</label>
    <input id="url" value="{default_lk}" />
    <label>Room</label>
    <input id="room" value="audio-room" />
    <label>Identity</label>
    <input id="identity" value="web-viewer" />
    <button id="join">Join room</button>
    <div class="status" id="status">offline</div>
    <div id="tapToPlay" aria-live="polite">
      <button type="button" id="tapToPlayBtn">Включить звук</button>
      <p class="hint" style="margin: 8px 0 0;">Если эфир начался после входа в комнату, браузер на телефоне мог заблокировать автозвук — нажмите кнопку выше.</p>
    </div>
    <audio id="player" class="stream-audio" controls playsinline webkit-playsinline preload="metadata"></audio>
  </div>
  <script type="module">
    import {{ Room }} from "https://cdn.jsdelivr.net/npm/livekit-client@2/dist/livekit-client.esm.mjs";
    const joinBtn = document.getElementById("join");
    const status = document.getElementById("status");
    const player = document.getElementById("player");
    const tapToPlayEl = document.getElementById("tapToPlay");
    const tapToPlayBtn = document.getElementById("tapToPlayBtn");
    let roomRef = null;
    let mediaSessionKeepAlive = null;
    /** Минимальный WAV — синхронное воспроизведение в том же тике, что и tap (iOS / автозапуск). */
    const SILENT_WAV_DATA_URI =
      "data:audio/wav;base64,UklGRigAAABXQVZFZm10IBIAAAABAAEARKwAAIhYAQACABAAAABkYXRhAgAAAAEA";

    try {{
      player.setAttribute("playsinline", "");
      player.playsInline = true;
    }} catch (_) {{}}

    function showTapToPlay() {{
      tapToPlayEl.classList.add("show");
    }}
    function hideTapToPlay() {{
      tapToPlayEl.classList.remove("show");
    }}

    /**
     * Должно вызываться синхронно из обработчика клика (до первого await),
     * иначе Safari/Android снимают «user activation» и блокируют звук при поздней подписке на трек.
     */
    function primeAudioElementInGesture() {{
      try {{
        player.removeAttribute("src");
        player.srcObject = null;
        player.src = SILENT_WAV_DATA_URI;
        player.loop = true;
        player.muted = true;
        player.volume = 0;
        const pr = player.play();
        if (pr && typeof pr.then === "function") pr.catch(() => {{}});
      }} catch (_) {{}}
      try {{
        const AC = window.AudioContext || window.webkitAudioContext;
        if (AC && !window.__lkPrimedAudioCtx) {{
          const ctx = new AC({{ latencyHint: "interactive" }});
          window.__lkPrimedAudioCtx = ctx;
          if (ctx.state === "suspended") void ctx.resume();
          const osc = ctx.createOscillator();
          const gain = ctx.createGain();
          gain.gain.value = 0;
          osc.connect(gain);
          gain.connect(ctx.destination);
          osc.start();
          setTimeout(() => {{ try {{ osc.stop(); }} catch (_) {{}} }}, 50);
        }} else if (window.__lkPrimedAudioCtx && window.__lkPrimedAudioCtx.state === "suspended") {{
          void window.__lkPrimedAudioCtx.resume();
        }}
      }} catch (_) {{}}
    }}

    function preparePlayerForLiveTrack() {{
      try {{
        player.loop = false;
        player.muted = false;
        player.volume = 1;
        player.removeAttribute("src");
        player.src = "";
      }} catch (_) {{}}
    }}

    async function unlockPlaybackAfterAttach(room) {{
      const r = room || roomRef;
      if (!r) return;
      try {{
        await r.startAudio();
      }} catch (_) {{}}
      try {{
        await player.play();
        hideTapToPlay();
        ensureMediaSessionMetadata();
        syncMediaSessionPlaying();
      }} catch (_) {{
        showTapToPlay();
      }}
    }}

    tapToPlayBtn.addEventListener("click", async () => {{
      try {{
        if (roomRef) await roomRef.startAudio();
      }} catch (_) {{}}
      try {{
        preparePlayerForLiveTrack();
        await player.play();
        hideTapToPlay();
        ensureMediaSessionMetadata();
        syncMediaSessionPlaying();
      }} catch (_) {{
        showTapToPlay();
      }}
    }});

    window.addEventListener("focus", () => {{
      if (!roomRef || !player.srcObject) return;
      if (player.paused) {{
        roomRef.startAudio().catch(() => {{}});
        player.play().catch(() => {{ showTapToPlay(); }});
      }}
    }});

    function wireMediaSessionControls() {{
      if (!("mediaSession" in navigator)) return;
      try {{
        navigator.mediaSession.setActionHandler("play", () => {{
          player.play().catch(() => {{}});
          syncMediaSessionPlaying();
        }});
        navigator.mediaSession.setActionHandler("pause", () => {{
          player.pause();
          syncMediaSessionPlaying();
        }});
        navigator.mediaSession.setActionHandler("stop", () => {{
          player.pause();
          syncMediaSessionPlaying();
        }});
      }} catch (_) {{}}
    }}

    function syncMediaSessionPlaying() {{
      if (!("mediaSession" in navigator)) return;
      try {{
        navigator.mediaSession.playbackState = player.paused ? "paused" : "playing";
      }} catch (_) {{}}
    }}

    function ensureMediaSessionMetadata() {{
      if (!("mediaSession" in navigator)) return;
      try {{
        navigator.mediaSession.metadata = new MediaMetadata({{
          title: "Прямая трансляция",
          artist: "LiveKit",
          album: document.getElementById("room").value || "audio-room",
        }});
      }} catch (_) {{}}
    }}

    function startMediaSessionKeepAlive() {{
      if (mediaSessionKeepAlive) clearInterval(mediaSessionKeepAlive);
      mediaSessionKeepAlive = setInterval(() => {{
        if (!roomRef || player.paused) return;
        ensureMediaSessionMetadata();
        syncMediaSessionPlaying();
      }}, 15000);
    }}

    function stopMediaSessionKeepAlive() {{
      if (mediaSessionKeepAlive) {{
        clearInterval(mediaSessionKeepAlive);
        mediaSessionKeepAlive = null;
      }}
    }}

    wireMediaSessionControls();

    player.addEventListener("playing", () => {{
      ensureMediaSessionMetadata();
      syncMediaSessionPlaying();
    }});
    player.addEventListener("play", () => {{
      syncMediaSessionPlaying();
      ensureMediaSessionMetadata();
    }});
    player.addEventListener("pause", syncMediaSessionPlaying);

    document.addEventListener("visibilitychange", () => {{
      if (!roomRef) return;
      if (document.hidden) {{
        if (!player.paused) {{
          ensureMediaSessionMetadata();
          syncMediaSessionPlaying();
        }}
        return;
      }}
      ensureMediaSessionMetadata();
      syncMediaSessionPlaying();
      roomRef.startAudio().catch(() => {{}});
      player.play().catch(() => {{ showTapToPlay(); }});
    }});

    joinBtn.addEventListener("click", async () => {{
      /** Сразу при нажатии (до await), иначе мобильный Safari не считает жестом unlock для позднего трека. */
      primeAudioElementInGesture();
      hideTapToPlay();
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
        const lkRoom = new Room({{
          disconnectOnPageLeave: false,
          adaptiveStream: false,
          dynacast: false,
          webAudioMix: false,
        }});
        roomRef = lkRoom;
        function attachIfAudio(track) {{
          if (track && track.kind === "audio") {{
            preparePlayerForLiveTrack();
            track.attach(player);
            status.textContent = "audio subscribed";
            ensureMediaSessionMetadata();
            syncMediaSessionPlaying();
            void unlockPlaybackAfterAttach(lkRoom);
          }}
        }}
        lkRoom.on("trackSubscribed", (track) => attachIfAudio(track));
        lkRoom.on("disconnected", () => {{
          stopMediaSessionKeepAlive();
          status.textContent = "disconnected";
          hideTapToPlay();
        }});
        await lkRoom.connect(url, tokenJson.token);
        status.textContent = "connected";
        // Разблокировка звука в рамках жеста (клик Join): важно для iOS / политик автозапуска
        try {{
          await lkRoom.startAudio();
        }} catch (_) {{}}
        // Участники уже в комнате: цепляем уже опубликованные аудио (Maps в SDK)
        lkRoom.remoteParticipants.forEach((participant) => {{
          participant.audioTrackPublications.forEach((pub) => {{
            if (pub.track) attachIfAudio(pub.track);
            pub.on("subscribed", (track) => attachIfAudio(track));
          }});
        }});
        try {{
          await lkRoom.startAudio();
        }} catch (_) {{}}
        startMediaSessionKeepAlive();
        ensureMediaSessionMetadata();
        syncMediaSessionPlaying();
      }} catch (e) {{
        try {{
          if (roomRef) await roomRef.disconnect();
        }} catch (_) {{}}
        roomRef = null;
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