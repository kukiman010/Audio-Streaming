"""
HTTP MP3-релей комнаты LiveKit → нативный <audio src> в браузере.

Требуется ffmpeg в PATH. Один HTTP-клиент = одно подключение к LiveKit + один процесс ffmpeg.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from typing import Callable

from aiohttp import web
from livekit import rtc

logger = logging.getLogger(__name__)

FFMPEG = shutil.which("ffmpeg") or ""

WAIT_TRACK_TIMEOUT_SEC = 120.0


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


async def _pipe_track_to_mp3(
    room: rtc.Room,
    audio_track: rtc.Track,
    request: web.Request,
    response: web.StreamResponse,
) -> None:
    proc: asyncio.subprocess.Process | None = None
    stream = rtc.AudioStream.from_track(track=audio_track, sample_rate=48000, num_channels=1)

    async def drain_stderr(p: asyncio.subprocess.Process) -> None:
        if p.stderr is None:
            return
        try:
            err = await p.stderr.read()
            if err:
                logger.warning("ffmpeg stderr: %s", err.decode(errors="replace")[:2000])
        except Exception:
            pass

    try:
        proc = await asyncio.create_subprocess_exec(
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
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
            "-f",
            "mp3",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def feed_stdin() -> None:
            assert proc is not None and proc.stdin is not None
            try:
                async for event in stream:
                    data = event.frame.data
                    if data:
                        proc.stdin.write(data)
                        await proc.stdin.drain()
            except Exception as e:
                logger.debug("feed_stdin: %s", e)
            finally:
                try:
                    if proc.stdin and not proc.stdin.is_closing():
                        proc.stdin.close()
                        await proc.stdin.wait_closed()
                except Exception:
                    pass
                try:
                    await stream.aclose()
                except Exception:
                    pass

        async def drain_stdout() -> None:
            assert proc is not None and proc.stdout is not None
            try:
                while True:
                    chunk = await proc.stdout.read(8192)
                    if not chunk:
                        break
                    await response.write(chunk)
                    tr = request.transport
                    if tr is not None and tr.is_closing():
                        break
            except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
                pass

        feed_task = asyncio.create_task(feed_stdin())
        err_task = asyncio.create_task(drain_stderr(proc))
        out_task = asyncio.create_task(drain_stdout())

        await asyncio.wait(
            [feed_task, out_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in (feed_task, out_task):
            if not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        err_task.cancel()
        try:
            await err_task
        except asyncio.CancelledError:
            pass

    finally:
        try:
            await room.disconnect()
        except Exception:
            pass
        if proc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
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
    except Exception as e:
        logger.exception("listen relay pipe failed")
        try:
            await resp.write(f"\n".encode())
        except Exception:
            pass
    finally:
        try:
            await resp.write_eof()
        except Exception:
            pass

    return resp
