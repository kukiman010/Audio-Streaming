"""
HTTP MP3-релей комнаты LiveKit → нативный <audio src> в браузере.

Требуется ffmpeg в PATH. Задержка выше, чем у прямого WebRTC в браузере (кодирование MP3 + HTTP),
но ниже, чем при огромной очереди PCM — см. PCM_QUEUE_MAXSIZE и libmp3lame low-delay опции.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import shutil
import subprocess
import threading
import uuid
from typing import Callable

from aiohttp import web
from livekit import rtc

logger = logging.getLogger(__name__)

FFMPEG = shutil.which("ffmpeg") or ""

WAIT_TRACK_TIMEOUT_SEC = 120.0

# Очередь PCM: большое значение накапливало секунды аудио → огромная задержка.
# Малый буфер + отбрасывание старых кадров при переполнении (как в живом эфире).
PCM_QUEUE_MAXSIZE = 32


def _pcm_try_put_live(q: queue.Queue, data: bytes) -> None:
    """Один кадр PCM; при полной очереди выбрасываем самый старый — держим задержку малой."""
    if not data:
        return
    try:
        q.put_nowait(data)
        return
    except queue.Full:
        pass
    try:
        q.get_nowait()
    except queue.Empty:
        pass
    try:
        q.put_nowait(data)
    except queue.Full:
        pass


async def _wait_first_audio_track(room: rtc.Room, timeout: float) -> rtc.Track:
    loop = asyncio.get_running_loop()
    first: list[rtc.Track | None] = [None]
    done = asyncio.Event()

    def consider(track: rtc.Track) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        if first[0] is not None:
            return
        first[0] = track
        loop.call_soon_threadsafe(done.set)

    @room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, publication, participant) -> None:
        consider(track)

    for rp in room.remote_participants.values():
        pubs = getattr(rp, "track_publications", None)
        if pubs is None:
            continue
        iterable = pubs.values() if hasattr(pubs, "values") else pubs
        for pub in iterable:
            t = getattr(pub, "track", None)
            if t is not None and t.kind == rtc.TrackKind.KIND_AUDIO:
                consider(t)
                break
        if first[0] is not None:
            break

    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    except asyncio.TimeoutError as e:
        raise web.HTTPServiceUnavailable(
            text="В комнате пока нет аудио — запустите трансляцию и нажмите воспроизведение снова."
        ) from e

    assert first[0] is not None
    return first[0]


async def _pcm_shutdown_sentinel(pcm_q: queue.Queue) -> None:
    """Закрыть очередь PCM — даже если переполнена (blocking put в потоке)."""
    try:
        pcm_q.put_nowait(None)
    except queue.Full:
        await asyncio.to_thread(pcm_q.put, None)


def _stdin_writer_thread(proc: subprocess.Popen, pcm_q: queue.Queue) -> None:
    """
    Пишет PCM в ffmpeg через обычный blocking PIPE — не asyncio.StreamWriter
    (обходит assert «Data should not be empty» в Python 3.10 asyncio Unix pipe).
    """
    assert proc.stdin is not None
    try:
        while True:
            item = pcm_q.get()
            if item is None:
                break
            if item:
                proc.stdin.write(item)
    except BrokenPipeError:
        pass
    except Exception as e:
        logger.debug("stdin_writer: %s", e)
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass


async def _pipe_track_to_mp3(
    room: rtc.Room,
    audio_track: rtc.Track,
    request: web.Request,
    response: web.StreamResponse,
) -> None:
    proc: subprocess.Popen | None = None
    writer_thread: threading.Thread | None = None
    stream = rtc.AudioStream.from_track(
        track=audio_track,
        sample_rate=48000,
        num_channels=1,
        capacity=PCM_QUEUE_MAXSIZE,
    )
    pcm_q: queue.Queue = queue.Queue(maxsize=PCM_QUEUE_MAXSIZE)

    try:
        proc = subprocess.Popen(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-fflags",
                "nobuffer",
                "-flags",
                "low_delay",
                "-f",
                "s16le",
                "-ar",
                "48000",
                "-ac",
                "1",
                "-i",
                "pipe:0",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                "96k",
                "-compression_level",
                "0",
                "-write_xing",
                "0",
                "-f",
                "mp3",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert proc.stdin is not None and proc.stdout is not None

        wt = threading.Thread(
            target=_stdin_writer_thread,
            args=(proc, pcm_q),
            name="ffmpeg-stdin",
            daemon=True,
        )
        writer_thread = wt
        wt.start()

        async def feed_livekit() -> None:
            try:
                async for event in stream:
                    data = event.frame.data
                    if not data:
                        continue
                    await asyncio.to_thread(_pcm_try_put_live, pcm_q, data)
            except Exception as e:
                logger.debug("feed_livekit: %s", e)
            finally:
                try:
                    await stream.aclose()
                except Exception:
                    pass

        async def drain_http() -> None:
            try:
                while True:
                    chunk = await asyncio.to_thread(proc.stdout.read, 4096)
                    if not chunk:
                        break
                    try:
                        await response.write(chunk)
                    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
                        break
                    tr = request.transport
                    if tr is not None and tr.is_closing():
                        break
            except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
                pass

        feed_task = asyncio.create_task(feed_livekit())
        out_task = asyncio.create_task(drain_http())

        await asyncio.wait(
            {feed_task, out_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in (feed_task, out_task):
            if not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        await _pcm_shutdown_sentinel(pcm_q)
        if writer_thread is not None:
            await asyncio.to_thread(writer_thread.join, 5.0)

    finally:
        try:
            await room.disconnect()
        except Exception:
            pass
        await _pcm_shutdown_sentinel(pcm_q)
        if writer_thread is not None and writer_thread.is_alive():
            await asyncio.to_thread(writer_thread.join, 3.0)
        if proc is not None:
            if proc.poll() is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.to_thread(proc.wait)
                except Exception:
                    pass


async def handle_listen_mp3(
    request: web.Request,
    *,
    livekit_ws_url: str,
    build_viewer_token: Callable[[str, str], str],
) -> web.StreamResponse:
    """
    Query: room=… , опционально token=… (JWT viewer).
    Без token сервер сам выпускает JWT с identity http-mp3-….
    """
    room_name = request.query.get("room", "audio-room").strip() or "audio-room"
    token_q = request.query.get("token", "").strip()
    if token_q:
        token = token_q
    else:
        identity = f"http-mp3-{uuid.uuid4().hex[:16]}"
        token = build_viewer_token(room_name, identity)

    if not FFMPEG:
        raise web.HTTPServiceUnavailable(
            text="На сервере не найден ffmpeg. Установите ffmpeg и перезапустите helper."
        )

    room = rtc.Room()
    try:
        await room.connect(livekit_ws_url, token)
        audio_track = await _wait_first_audio_track(room, WAIT_TRACK_TIMEOUT_SEC)
    except web.HTTPException:
        try:
            await room.disconnect()
        except Exception:
            pass
        raise
    except Exception as e:
        try:
            await room.disconnect()
        except Exception:
            pass
        logger.exception("livekit connect failed")
        raise web.HTTPBadGateway(text=f"LiveKit: {e}") from e

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "audio/mpeg",
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Accept-Ranges": "none",
            "X-Content-Type-Options": "nosniff",
        },
    )
    await resp.prepare(request)

    try:
        await _pipe_track_to_mp3(room, audio_track, request, resp)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("listen relay pipe failed")
    finally:
        try:
            await resp.write_eof()
        except Exception:
            pass

    return resp
