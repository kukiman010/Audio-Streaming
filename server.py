#!/usr/bin/env python3
import asyncio
import signal
from aiohttp import web, WSMsgType, WSCloseCode
import argparse
import json
import time

class StreamHub:
    def __init__(self):
        self.listeners = set()            # set[asyncio.Queue]
        self.lock = asyncio.Lock()
        self.streamer = None              # web.WebSocketResponse
        self.active = False
        self.started_at = None
        self._bytes_total = 0

    async def add_listener(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=256)    # небольшая очередь, чтобы ограничить задержку
        async with self.lock:
            self.listeners.add(q)
        return q

    async def remove_listener(self, q: asyncio.Queue):
        async with self.lock:
            self.listeners.discard(q)

    async def broadcast(self, data: bytes):
        # Рассылаем всем слушателям; если очередь переполнена — очищаем и кладём свежие данные,
        # чтобы держать задержку минимальной.
        dead = []
        for q in list(self.listeners):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                try:
                    # быстрый дроп накопившегося
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

async def index(request: web.Request):
    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Простой аудио-стрим</title>
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
  <p class="meta">Откройте эту страницу и нажмите Play, если автозапуск запрещён в браузере.</p>
  <div>
    <span class="badge" id="bActive">Статус: offline</span>
    <span class="badge" id="bListeners">Слушателей: 0</span>
    <span class="badge" id="bUptime">Аптайм: 0 c</span>
  </div>
  <audio id="player" controls autoplay src="/listen.mp3"></audio>
  <p style="color:#666; font-size: 0.9rem;">
    Совместимо с современными браузерами. Формат: MP3 (audio/mpeg), потоковая передача по HTTP chunked.
  </p>
</div>
<script>
async function refresh() {{
  try {{
    const r = await fetch('/stats');
    const s = await r.json();
    document.getElementById('bActive').textContent = 'Статус: ' + (s.active ? 'online' : 'offline');
    document.getElementById('bListeners').textContent = 'Слушателей: ' + s.listeners;
    document.getElementById('bUptime').textContent = 'Аптайм: ' + Math.floor(s.uptime_sec) + ' c';
  }} catch(e) {{}}
}}
setInterval(refresh, 1000);
refresh();
</script>
</body>
</html>"""
    # return web.Response(text=html, content_type="text/html; charset=utf-8")
    # return web.Response(text=html, contenttype="text/html")
    return web.Response(text=html, content_type="text/html")


async def stats(request: web.Request):
    return web.json_response(hub.stats())

async def listen_mp3(request: web.Request):
    # Возвращаем поток audio/mpeg. Слушатель получает данные "как есть".
    resp = web.StreamResponse(
        status=200,
        reason='OK',
        headers={
            'Content-Type': 'audio/mpeg',
            'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
            'Connection': 'keep-alive',
            # Опционально можно указать ICY-заголовки для некоторых плееров:
            'icy-name': 'SimplePythonStream',
            'icy-genre': 'Live',
        }
    )
    await resp.prepare(request)
    q = await hub.add_listener()
    try:
        # Если стрима нет — просто ждём появления данных.
        while True:
            chunk = await q.get()
            await resp.write(chunk)
            await resp.drain()
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        await hub.remove_listener(q)
        with contextlib.suppress(Exception):
            await resp.write_eof()
    return resp

async def uplink(request: web.Request):
    # Один активный стример
    if hub.streamer is not None and not hub.streamer.closed:
        return web.Response(status=423, text="Streamer already connected")
    ws = web.WebSocketResponse(heartbeat=10.0)  # встроенные пинги
    await ws.prepare(request)
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
                # Приняли аудиоданные — ретрансляция
                await hub.broadcast(data)
                if not ack_sent:
                    hub.active = True
                    hub.started_at = time.time()
                    ack = {
                        "type": "ack",
                        "status": "streaming_started",
                        "listeners": hub.listeners_count(),
                        "message": "Сервер принимает аудио и транслирует /listen.mp3"
                    }
                    await ws.send_json(ack)
                    ack_sent = True
            elif msg.type == WSMsgType.TEXT:
                # опционально поддержим ping от клиента
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

import contextlib

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
    parser = argparse.ArgumentParser(description="Simple audio stream server")
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
