#!/usr/bin/env python3
import asyncio
import argparse
import sys
import json
import shutil
import time
from aiohttp import ClientSession, ClientWebSocketResponse, WSMsgType

FFMPEG_BIN = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"

def build_ffmpeg_cmd(alsa_device: str, channels: int, rate: int, bitrate_kbps: int):
    # Минимальный, совместимый с браузерами MP3-поток
    return [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel", "error",
        "-f", "alsa",
        "-i", alsa_device,           # например, "default"
        "-ac", str(channels),
        "-ar", str(rate),
        "-codec:a", "libmp3lame",
        "-b:a", f"{bitrate_kbps}k",
        "-f", "mp3",
        "-"                         # вывод в stdout
    ]

async def read_ffmpeg_and_send(ws: ClientWebSocketResponse, proc: asyncio.subprocess.Process, chunk_size: int):
    acked = False
    last_report = time.time()
    sent_bytes = 0
    try:
        while True:
            chunk = await proc.stdout.read(chunk_size)
            if not chunk:
                await asyncio.sleep(0.01)
                if proc.returncode is not None:
                    break
                continue
            await ws.send_bytes(chunk)
            sent_bytes += len(chunk)
            # Печатаем простой локальный прогресс
            now = time.time()
            if now - last_report >= 2.0:
                print(f"[client] sent ~{sent_bytes/1024:.1f} KiB", flush=True)
                last_report = now
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[client] send loop error: {e}", file=sys.stderr)

async def ws_recv_loop(ws: ClientWebSocketResponse):
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                # на случай текстовых сообщений
                print(f"[client] server text: {msg.data}")
            elif msg.type == WSMsgType.BINARY:
                # не используем бинарные ответы
                pass
            elif msg.type == WSMsgType.CLOSE:
                break
            elif msg.type == WSMsgType.ERROR:
                print(f"[client] ws error: {ws.exception()}")
                break
            elif msg.type == WSMsgType.CONTINUATION:
                pass
            else:
                # вероятно JSON (aiohttp отдаёт JSON как TEXT, но проверим)
                try:
                    data = msg.json()
                except Exception:
                    try:
                        data = json.loads(msg.data)
                    except Exception:
                        data = None
                if data:
                    if data.get("type") == "ack":
                        print("[client] OK: подтверждение от сервера — трансляция начата")
                        print(f"[client] listeners: {data.get('listeners', 0)}")
                    elif data.get("type") == "stats":
                        print(f"[client] stats: listeners={data.get('listeners')} active={data.get('active')} uptime={int(data.get('uptime_sec',0))}s")
    except Exception as e:
        print(f"[client] recv loop error: {e}", file=sys.stderr)

async def main():
    parser = argparse.ArgumentParser(description="Simple audio stream client")
    parser.add_argument("--server", default="ws://127.0.0.1:8000/uplink", help="WebSocket URL сервера")
    parser.add_argument("--alsa", default="default", help="ALSA устройство (например, hw:0,0 или default)")
    parser.add_argument("--channels", type=int, default=2)
    parser.add_argument("--rate", type=int, default=48000)
    parser.add_argument("--bitrate", type=int, default=128, help="битрейт MP3, кбит/с")
    parser.add_argument("--chunk", type=int, default=4096, help="размер отправляемого куска в байтах")
    args = parser.parse_args()

    if not FFMPEG_BIN:
        print("Ошибка: ffmpeg не найден. Установите: sudo apt install -y ffmpeg", file=sys.stderr)
        sys.exit(1)

    ff_cmd = build_ffmpeg_cmd(args.alsa, args.channels, args.rate, args.bitrate)
    print("[client] запуск ffmpeg:", " ".join(ff_cmd))

    proc = await asyncio.create_subprocess_exec(
        *ff_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    async with ClientSession() as session:
        try:
            async with session.ws_connect(args.server, heartbeat=10.0) as ws:
                print(f"[client] подключено к {args.server}, ожидание подтверждения от сервера...")
                send_task = asyncio.create_task(read_ffmpeg_and_send(ws, proc, args.chunk))
                recv_task = asyncio.create_task(ws_recv_loop(ws))
                done, pending = await asyncio.wait(
                    [send_task, recv_task],
                    return_when=asyncio.FIRST_COMPLETED
                )
                for t in pending:
                    t.cancel()
        except Exception as e:
            print(f"[client] не удалось подключиться к серверу: {e}", file=sys.stderr)
        finally:
            with contextlib.suppress(Exception):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except Exception:
                with contextlib.suppress(Exception):
                    proc.kill()

import contextlib

if __name__ == "__main__":
    asyncio.run(main())