#!/usr/bin/env python3
import asyncio
import signal
from aiohttp import web, WSMsgType, WSCloseCode
import argparse
import json
import time
import socket
import contextlib

# ---------------- Core hub ----------------

class StreamHub:
    def __init__(self):
        self.listeners = set()            # set[asyncio.Queue]
        self.lock = asyncio.Lock()
        self.streamer = None              # web.WebSocketResponse
        self.active = False
        self.started_at = None
        self._bytes_total = 0

    async def add_listener(self) -> asyncio.Queue:
        # Маленькая очередь минимизирует задержку у каждого слушателя.
        q = asyncio.Queue(maxsize=16)
        async with self.lock:
            self.listeners.add(q)
        return q

    async def remove_listener(self, q: asyncio.Queue):
        async with self.lock:
            self.listeners.discard(q)

    async def broadcast(self, data: bytes):
        # Рассылаем всем слушателям; при переполнении дропаем старые данные, кладём свежие.
        dead = []
        for q in list(self.listeners):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                try:
                    while not q.empty():
                        q.get_nowait()
                    q.put_nowait(data)
                except Exception:
                    dead.append(q)
        if dead:
            async with self.lock:
                for q in dead:
                    self.listeners.discard(q)
        self._bytes_total += len(data)

    def listeners_count(self) -> int:
        return len(self.listeners)

    def stats(self):
        return {
            "active": self.active,
            "listeners": self.listeners_count(),
            "bytes_total": self._bytes_total,
            "started_at": self.started_at,
            "uptime_sec": (time.time() - self.started_at) if (self.active and self.started_at) else 0.0,
        }

hub = StreamHub()

# ---------------- HTTP handlers ----------------

async def index(request: web.Request):
    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Простой аудио-стрим (низкая задержка)</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 2rem; }}
.card {{ max-width: 720px; margin: auto; padding: 1.5rem; border: 1px solid #ddd; border-radius: 12px; }}
h1 {{ margin-top: 0; }}
.meta {{ color: #555; margin: 0.5rem 0 1rem; }}
.badge {{ display: inline-block; background: #eef; color: #224; padding: 0.25rem 0.5rem; border-radius: 8px; margin-right: 0.5rem; }}
audio {{ width: 100%; margin-top: 1rem; }}
</style>
</head>
<body>
<div class="card">
  <h1>Аудио‑стрим</h1>
  <p class="meta">Если автозапуск запрещён в браузере — нажмите Play.</p>
  <div>
    <span class="badge" id="bActive">Статус: offline</span>
    <span class="badge" id="bListeners">Слушателей: 0</span>
    <span class="badge" id="bUptime">Аптайм: 0 c</span>
  </div>
  <audio id="player" controls autoplay preload="none" playsinline controlslist="noplaybackrate nodownload" src="/listen.mp3"></audio>
  <p style="color:#666; font-size: 0.9rem;">
    Формат: MP3 (audio/mpeg), HTTP chunked. Кэш отключён, воспроизводится только текущая трансляция.
  </p>
</div>
<script>
async function refresh() {{
  try {{
    const r = await fetch('/stats', {{cache:'no-store'}});
    const s = await r.json();
    document.getElementById('bActive').textContent = 'Статус: ' + (s.active ? 'online' : 'offline');
    document.getElementById('bListeners').textContent = 'Слушателей: ' + s.listeners;
    document.getElementById('bUptime').textContent = 'Аптайм: ' + Math.floor(s.uptime_sec) + ' c';
  }} catch(e) {{}}
}}
setInterval(refresh, 1000);
refresh();

// Фиксация на live-краю: не позволяем «уходить назад» в рамках текущей сессии
const audio = document.getElementById('player');
function snapToLive() {{
  try {{
    const r = audio.seekable;
    if (r && r.length) {{
      const liveEdge = r.end(r.length - 1);
      if (liveEdge - audio.currentTime > 0.35) {{
        audio.currentTime = Math.max(0, liveEdge - 0.1);
      }}
    }}
  }} catch(e) {{}}
}}
audio.addEventListener('seeking', snapToLive);
audio.addEventListener('loadedmetadata', snapToLive);
</script>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")

async def stats(request: web.Request):
    return web.json_response(hub.stats(), headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0, private",
        "Pragma": "no-cache",
        "Expires": "0",
    })

async def listen_mp3(request: web.Request):
    # Стримим текущие mp3-данные "как есть". Кэш и Range отключаем намеренно.
    resp = web.StreamResponse(
        status=200,
        reason='OK',
        headers={
            'Content-Type': 'audio/mpeg',
            'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0, private',
            'Pragma': 'no-cache',
            'Expires': '0',
            'Accept-Ranges': 'none',           # запрет Range, чтобы не было таймшифта с сервера
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',         # если вдруг есть nginx перед приложением
            'icy-name': 'SimplePythonStream',
            'icy-genre': 'Live',
        }
    )
    await resp.prepare(request)

    # Включаем TCP_NODELAY для уменьшения задержки мелких пакетов.
    try:
        tr = request.transport
        if tr:
            sock = tr.get_extra_info('socket')
            import socket as _socket
            if sock and hasattr(_socket, "TCP_NODELAY"):
                sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
    except Exception:
        pass

    q = await hub.add_listener()
    try:
        while True:
            chunk = await q.get()
            # write уже ждёт освобождения буфера транспорта при необходимости (вместо drain)
            await resp.write(chunk)
            # по желанию можно иногда уступать цикл:
            # if resp.transport and resp.transport.get_write_buffer_size() > 256*1024:
            #     await asyncio.sleep(0)
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        await hub.remove_listener(q)
        with contextlib.suppress(Exception):
            await resp.write_eof()
    return resp

# ---------------- WebSocket uplink (from encoder client) ----------------

async def uplink(request: web.Request):
    # Один активный стример
    if hub.streamer is not None and not hub.streamer.closed:
        return web.Response(status=423, text="Streamer already connected")

    ws = web.WebSocketResponse(heartbeat=10.0, compress=False)
    await ws.prepare(request)

    # Включаем TCP_NODELAY на uplink-соединении
    try:
        tr = request.transport
        if tr:
            sock = tr.get_extra_info('socket')
            if sock and hasattr(socket, "TCP_NODELAY"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass

    hub.streamer = ws
    hub.active = False
    hub.started_at = None
    ack_sent = False
    print("[server] streamer connected")

    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                data = msg.data
                if not data:
                    continue
                # Ретрансляция всем слушателям
                await hub.broadcast(data)

                if not ack_sent:
                    hub.active = True
                    hub.started_at = time.time()
                    await ws.send_json({
                        "type": "ack",
                        "status": "streaming_started",
                        "listeners": hub.listeners_count(),
                        "message": "Сервер принимает аудио и транслирует /listen.mp3",
                    })
                    ack_sent = True

            elif msg.type == WSMsgType.TEXT:
                if msg.data == "ping":
                    await ws.send_str("pong")

            elif msg.type == WSMsgType.ERROR:
                print(f"[server] ws error: {ws.exception()}")
                break

        return ws

    except Exception as e:
        print(f"[server] exception: {e}")

    finally:
        # Стример отключился
        try:
            await ws.close(code=WSCloseCode.GOING_AWAY, message=b"bye")
        except Exception:
            pass
        if hub.streamer is ws:
            hub.streamer = None
        hub.active = False
        print("[server] streamer disconnected")

    return ws

# ---------------- Periodic stats push to streamer ----------------

async def streamer_stats_push():
    # Периодически отправляем стримеру статистику, если подключён
    while True:
        await asyncio.sleep(1.5)
        ws = hub.streamer
        if ws and not ws.closed:
            try:
                await ws.send_json({
                    "type": "stats",
                    "listeners": hub.listeners_count(),
                    "bytes_total": hub._bytes_total,
                    "active": hub.active,
                    "uptime_sec": (time.time() - hub.started_at) if (hub.active and hub.started_at) else 0.0,
                })
            except Exception:
                pass

async def on_start(app):
    app['stats_task'] = asyncio.create_task(streamer_stats_push())

async def on_cleanup(app):
    task = app.get('stats_task')
    if task:
        task.cancel()
        with contextlib.suppress(Exception):
            await task

# ---------------- App factory / main ----------------

def make_app():
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/stats', stats)
    app.router.add_get('/listen.mp3', listen_mp3)
    app.router.add_get('/uplink', uplink)
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_cleanup)
    return app

def main():
    parser = argparse.ArgumentParser(description="Simple low-latency audio stream server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    app = make_app()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, loop.stop)

    web.run_app(app, host=args.host, port=args.port)

if __name__ == "__main__":
    main()