#!/usr/bin/env python3
import asyncio
import queue
import sys
import threading
import time
import warnings
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
from livekit import rtc

from latency_trace import (
    TOPIC_PING,
    TOPIC_PONG,
    TraceStats,
    compute_stages,
    decode_pong,
    encode_ping,
    extract_conn_type,
    extract_pub_rtt_ms,
    format_line,
)

TokenProvider = Callable[[], str]
UpdateCallback = Callable[["LiveKitState"], None]

PHASE_CONNECTING = "connecting"
PHASE_CONNECTED = "connected"
PHASE_RECONNECTING = "reconnecting"
PHASE_RECOVERING = "recovering"
PHASE_OFFLINE = "offline"

# Low-latency AudioSource buffer; frames are exactly 10 ms.
# 100 ms ≈ 10 frames — enough for short GIL/asyncio stalls without radio-like delay.
AUDIO_SOURCE_QUEUE_MS = 100
# ~200 ms of 10 ms frames — drop-oldest keeps the live edge (prefer skip over lag).
SYNC_QUEUE_MAXSIZE = 20
# record() longer than this counts as a WASAPI gap.
WASAPI_GAP_THRESHOLD_S = 0.015
STATS_PUSH_INTERVAL_S = 1.0
AUDIO_PUBLISH_BITRATE = 128_000


@dataclass
class LiveKitState:
    running: bool = False
    connected: bool = False
    phase: str = PHASE_OFFLINE
    room_name: str = ""
    identity: str = ""
    reconnect_count: int = 0
    recovery_attempt: int = 0
    max_recovery_attempts: int = 3
    last_error: str = ""
    started_at: float = 0.0
    # Loopback capture / publish diagnostics
    frames_captured: int = 0
    frames_dropped: int = 0
    wasapi_gaps_ms: float = 0.0
    queue_depth: int = 0
    queued_duration_ms: float = 0.0


def _validate_sc_loopback_index(index: int) -> None:
    """Проверка индекса sc_lb:N до старта потока; COM и объекты soundcard — в своём потоке."""
    from win_com import import_soundcard

    sc = import_soundcard()
    all_m = sc.all_microphones(include_loopback=True)
    if not (0 <= index < len(all_m)):
        raise IndexError("Нет устройства с таким индексом (soundcard).")
    if not getattr(all_m[index], "isloopback", False):
        raise ValueError("Указанный индекс не является loopback-устройством.")


def _is_soundcard_loopback_id(device_id: Optional[str]) -> bool:
    if device_id is None:
        return False
    t = str(device_id).strip().lower()
    return t.startswith("sc_lb:")


def _put_drop_oldest(sync_q: queue.Queue, frame: object, drop_counter: list) -> None:
    """Enqueue frame; if full, discard the oldest frame to keep the live edge."""
    while True:
        try:
            sync_q.put_nowait(frame)
            return
        except queue.Full:
            try:
                sync_q.get_nowait()
                drop_counter[0] += 1
            except queue.Empty:
                pass


class LiveKitStreamClient:
    def __init__(self, ui_callback: UpdateCallback):
        self.ui_callback = ui_callback
        self.room: Optional[rtc.Room] = None
        self.mic = None
        self.track: Optional[rtc.LocalAudioTrack] = None
        self.state = LiveKitState()
        self._stop_event: Optional[asyncio.Event] = None
        self._sc_thread: Optional[threading.Thread] = None
        self._sc_pump_task: Optional[asyncio.Task] = None
        self._sc_stats_task: Optional[asyncio.Task] = None
        self._sc_source: Optional[rtc.AudioSource] = None
        self._sc_sync_queue: Optional[queue.Queue] = None
        self._pump_enabled: Optional[asyncio.Event] = None
        self._user_stop = False
        self._token_provider: Optional[TokenProvider] = None
        self._max_recovery_attempts = 3
        self._server_url = ""
        self._device_id: Optional[str] = None
        self._sample_rate = 48000
        self._lb_index: Optional[int] = None
        self._recovery_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Shared with capture thread (list cells for atomic-ish updates without locks).
        self._frames_captured = [0]
        self._frames_dropped = [0]
        self._wasapi_gaps_ms = [0.0]
        # Трассировка задержки: см. docs/superpowers/specs/2026-11-25-latency-trace-design.md
        self.trace_callback: Optional[Callable[[str], None]] = None
        self._tracing = False
        self._trace_task: Optional[asyncio.Task] = None
        self._trace_seq = 0
        self._trace_pending: dict = {}  # id -> (t_sent_ms, pub_rtt_ms)
        self._trace_answered: dict = {}  # identity -> id последнего ответа
        self._trace_stats = TraceStats()

    @property
    def tracing(self) -> bool:
        return self._tracing

    def _set_phase(self, phase: str) -> None:
        self.state.phase = phase
        self.state.connected = phase == PHASE_CONNECTED
        if phase != PHASE_CONNECTED and self._tracing:
            asyncio.ensure_future(self.stop_trace())
        self.ui_callback(self.state)

    def _pause_pump(self) -> None:
        if self._pump_enabled is not None:
            self._pump_enabled.clear()

    def _resume_pump(self) -> None:
        if self._pump_enabled is not None:
            self._pump_enabled.set()

    def _sync_diag_to_state(self) -> None:
        self.state.frames_captured = self._frames_captured[0]
        self.state.frames_dropped = self._frames_dropped[0]
        self.state.wasapi_gaps_ms = round(self._wasapi_gaps_ms[0], 1)
        q = self._sc_sync_queue
        self.state.queue_depth = q.qsize() if q is not None else 0
        src = self._sc_source
        if src is not None:
            try:
                self.state.queued_duration_ms = round(float(src.queued_duration) * 1000.0, 1)
            except Exception:
                self.state.queued_duration_ms = 0.0
        else:
            self.state.queued_duration_ms = 0.0

    def _register_room_handlers(self, room: rtc.Room) -> None:
        room.on("connection_state_changed", self._on_connection_state_changed)
        room.on("reconnecting", self._on_reconnecting)
        room.on("reconnected", self._on_reconnected)
        room.on("disconnected", self._on_disconnected)
        room.on("data_received", self._on_data_received)

    def _on_connection_state_changed(self, connection_state: rtc.ConnectionState.ValueType) -> None:
        if connection_state == rtc.ConnectionState.CONN_CONNECTED:
            if self.state.phase not in (PHASE_RECOVERING, PHASE_CONNECTING):
                self._set_phase(PHASE_CONNECTED)
                # Soft reconnect never pauses the pump; resume is for hard recovery.
                self._resume_pump()
        elif connection_state == rtc.ConnectionState.CONN_RECONNECTING:
            # Soft reconnect: keep pumping. Pausing fills sync_q → drop-oldest → audible holes.
            self._set_phase(PHASE_RECONNECTING)

    def _on_reconnecting(self) -> None:
        self.state.reconnect_count += 1
        self._set_phase(PHASE_RECONNECTING)

    def _on_reconnected(self) -> None:
        self.state.recovery_attempt = 0
        self._set_phase(PHASE_CONNECTED)
        self._resume_pump()

    def _on_disconnected(self, _reason: rtc.DisconnectReason.ValueType) -> None:
        if self._user_stop or not self.state.running:
            return
        if self._recovery_task is not None and not self._recovery_task.done():
            return
        self._recovery_task = asyncio.create_task(self._handle_unexpected_disconnect())

    async def _handle_unexpected_disconnect(self) -> None:
        await asyncio.sleep(3)
        if self._user_stop or not self.state.running:
            return
        if self.room is not None and self.room.connection_state == rtc.ConnectionState.CONN_CONNECTED:
            return
        await self._hard_reconnect()

    async def _hard_reconnect(self) -> None:
        if not self._token_provider:
            self.state.last_error = "Связь потеряна (token_provider не задан)"
            await self.stop()
            return

        loop = asyncio.get_running_loop()
        for attempt in range(1, self._max_recovery_attempts + 1):
            if self._user_stop or not self.state.running:
                return
            self.state.recovery_attempt = attempt
            self._set_phase(PHASE_RECOVERING)
            # Hard recovery tears down the room/track — pause only here, not on soft reconnect.
            self._pause_pump()

            try:
                if self.room is not None:
                    try:
                        await self.room.disconnect()
                    except Exception:
                        pass
                token = await loop.run_in_executor(None, self._token_provider)
                await self.room.connect(self._server_url, token)
                await self._publish_audio_track()
                self.state.recovery_attempt = 0
                self.state.room_name = self.room.name or "" if self.room else ""
                self._set_phase(PHASE_CONNECTED)
                self._resume_pump()
                return
            except Exception as e:
                self.state.last_error = (
                    f"Восстановление ({attempt}/{self._max_recovery_attempts}): {e}"
                )
                self.ui_callback(self.state)
                if attempt < self._max_recovery_attempts:
                    await asyncio.sleep(2)

        self.state.last_error = "Связь с сервером потеряна"
        await self.stop()

    async def start(
        self,
        server_url: str,
        token: str,
        device_id: Optional[str] = None,
        channels: int = 1,
        sample_rate: int = 48000,
        token_provider: Optional[TokenProvider] = None,
        max_recovery_attempts: int = 3,
    ) -> None:
        # `channels` is kept for callers (e.g. GUI); native capture is always mono — see MediaDevices note below.
        self._user_stop = False
        self._token_provider = token_provider
        self._max_recovery_attempts = max(1, int(max_recovery_attempts))
        self._server_url = server_url
        self._device_id = device_id
        self._sample_rate = int(sample_rate)
        self._lb_index = None
        self._recovery_task = None
        self._pump_enabled = asyncio.Event()
        self._pump_enabled.set()
        self._loop = asyncio.get_running_loop()
        self._frames_captured[0] = 0
        self._frames_dropped[0] = 0
        self._wasapi_gaps_ms[0] = 0.0

        self.state = LiveKitState(
            running=True,
            phase=PHASE_CONNECTING,
            started_at=time.time(),
            max_recovery_attempts=self._max_recovery_attempts,
        )
        self.ui_callback(self.state)
        self._stop_event = asyncio.Event()

        try:
            self.room = rtc.Room()
            self._register_room_handlers(self.room)
            await self.room.connect(server_url, token)
            self.state.room_name = self.room.name or ""
            self._set_phase(PHASE_CONNECTED)
        except Exception as e:
            self.state.running = False
            self.state.phase = PHASE_OFFLINE
            self.state.connected = False
            self.state.last_error = f"LiveKit connect failed: {e}"
            self.ui_callback(self.state)
            return

        try:
            await self._setup_audio_capture(device_id, sample_rate)
            await self._publish_audio_track()
        except Exception as e:
            self.state.last_error = f"Publish failed: {e}"
            await self.stop()
            return

        await self._stop_event.wait()
        await self.stop()

    async def _setup_audio_capture(
        self, device_id: Optional[str], sample_rate: int
    ) -> None:
        loop = self._loop or asyncio.get_running_loop()
        if _is_soundcard_loopback_id(device_id):
            try:
                lb_index = int(str(device_id).split(":", 1)[1].strip())
            except (ValueError, IndexError):
                raise ValueError(f"Некорректный идентификатор loopback: {device_id!r}") from None
            _validate_sc_loopback_index(lb_index)
            self._lb_index = lb_index

            source = rtc.AudioSource(
                int(sample_rate),
                1,
                queue_size_ms=AUDIO_SOURCE_QUEUE_MS,
                loop=loop,
            )
            self._sc_source = source
            sync_q: queue.Queue = queue.Queue(maxsize=SYNC_QUEUE_MAXSIZE)
            self._sc_sync_queue = sync_q
            frame_samples = max(1, int(sample_rate) // 100)
            frames_captured = self._frames_captured
            frames_dropped = self._frames_dropped
            wasapi_gaps_ms = self._wasapi_gaps_ms

            def _loopback_worker() -> None:
                from win_com import format_soundcard_error, import_soundcard

                try:
                    if sys.platform == "win32":
                        import ctypes

                        # Меньше пропусков буфера WASAPI при нагрузке на CPU/GIL.
                        ctypes.windll.kernel32.SetThreadPriority(
                            ctypes.windll.kernel32.GetCurrentThread(), 1
                        )
                    sc = import_soundcard()
                    from soundcard import SoundcardRuntimeWarning  # type: ignore[import-untyped]

                    all_m = sc.all_microphones(include_loopback=True)
                    sc_mic = all_m[lb_index]
                    ch = min(int(sc_mic.channels), 2)
                    with sc_mic.recorder(
                        int(sample_rate), channels=ch, blocksize=frame_samples
                    ) as rec:
                        while self._stop_event and not self._stop_event.is_set():
                            t0 = time.perf_counter()
                            with warnings.catch_warnings(record=True) as caught:
                                warnings.simplefilter("always", SoundcardRuntimeWarning)
                                data = rec.record(frame_samples)
                            for w in caught:
                                if "data discontinuity" in str(w.message).lower():
                                    wasapi_gaps_ms[0] += (
                                        frame_samples / float(sample_rate) * 1000.0
                                    )
                            elapsed = time.perf_counter() - t0
                            if elapsed > WASAPI_GAP_THRESHOLD_S:
                                wasapi_gaps_ms[0] += (elapsed - WASAPI_GAP_THRESHOLD_S) * 1000.0

                            if data.shape[0] < frame_samples:
                                # Pad short blocks instead of skipping (would create a hole).
                                pad = frame_samples - data.shape[0]
                                if data.ndim == 2:
                                    pad_arr = np.zeros((pad, data.shape[1]), dtype=data.dtype)
                                else:
                                    pad_arr = np.zeros(pad, dtype=data.dtype)
                                data = np.concatenate([data, pad_arr], axis=0)
                                wasapi_gaps_ms[0] += pad / float(sample_rate) * 1000.0

                            if data.ndim == 2:
                                if data.shape[1] >= 2:
                                    mono = (data[:, 0] + data[:, 1]) * 0.5
                                else:
                                    mono = data[:, 0]
                            else:
                                mono = data
                            pcm = (np.clip(mono, -1.0, 1.0) * 32767.0).astype(np.int16)
                            frame = rtc.AudioFrame(
                                pcm.tobytes(),
                                int(sample_rate),
                                1,
                                frame_samples,
                            )
                            _put_drop_oldest(sync_q, frame, frames_dropped)
                            frames_captured[0] += 1
                except Exception as e:
                    # e очищается при выходе из except (Python 3.11+); сохраняем текст.
                    err_text = format_soundcard_error(e)

                    def _fail() -> None:
                        self.state.last_error = f"Захват звука экрана: {err_text}"
                        self.ui_callback(self.state)
                        if self._stop_event:
                            self._stop_event.set()

                    loop.call_soon_threadsafe(_fail)

            self._sc_thread = threading.Thread(
                target=_loopback_worker, name="soundcard-loopback", daemon=True
            )
            self._sc_thread.start()
            self._sc_pump_task = asyncio.create_task(self._pump_soundcard())
            self._sc_stats_task = asyncio.create_task(self._stats_loop())
            self.track = rtc.LocalAudioTrack.create_audio_track("screen-audio", source)
        else:
            devices = rtc.MediaDevices(
                loop=loop,
                input_sample_rate=int(sample_rate),
                output_sample_rate=int(sample_rate),
                num_channels=1,
            )
            open_kwargs: dict = {}
            if device_id not in (None, ""):
                try:
                    open_kwargs["input_device"] = int(device_id)
                except ValueError:
                    raise ValueError(f"Некорректный индекс устройства: {device_id!r}") from None
            self.mic = devices.open_input(**open_kwargs)
            self.track = rtc.LocalAudioTrack.create_audio_track("input", self.mic.source)

    async def _stats_loop(self) -> None:
        try:
            while self._stop_event and not self._stop_event.is_set():
                self._sync_diag_to_state()
                self.ui_callback(self.state)
                await asyncio.sleep(STATS_PUSH_INTERVAL_S)
        except asyncio.CancelledError:
            pass

    # ---- трассировка задержки ----

    def _emit_trace(self, text: str) -> None:
        if self.trace_callback is not None:
            self.trace_callback(text)

    def _on_data_received(self, packet) -> None:
        """Ответ слушателя на ping; всё лишнее (мусор, старый id, чужой topic) тихо игнорируется."""
        if not self._tracing or packet.topic != TOPIC_PONG or packet.participant is None:
            return
        identity = packet.participant.identity
        if self.room is None or identity not in self.room.remote_participants:
            return
        pong = decode_pong(packet.data)
        sent = self._trace_pending.get(pong.id) if pong is not None else None
        if sent is None:
            return
        t_sent, pub_rtt = sent
        loop_ms = time.monotonic() * 1000.0 - t_sent
        stages = compute_stages(pub_rtt, loop_ms, pong.jitter_buffer_ms)
        self._trace_answered[identity] = max(pong.id, self._trace_answered.get(identity, 0))
        self._trace_stats.add(identity, stages)
        self._emit_trace(format_line(identity, stages))

    async def start_trace(self) -> None:
        if self._tracing or self.room is None or self.state.phase != PHASE_CONNECTED:
            return
        self._trace_seq = 0
        self._trace_pending = {}
        self._trace_answered = {}
        self._trace_stats = TraceStats()
        self._tracing = True
        try:
            stats = await self.room.get_rtc_stats()
            conn = extract_conn_type(list(stats.publisher_stats))
        except Exception:
            conn = "н/д"
        self._emit_trace(f"[замер] старт. Соединение стримера: {conn}")
        self._trace_task = asyncio.create_task(self._trace_loop())

    async def stop_trace(self) -> None:
        if not self._tracing:
            return
        self._tracing = False
        task, self._trace_task = self._trace_task, None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for line in self._trace_stats.summary_lines():
            self._emit_trace(line)
        self._emit_trace("[замер] остановлено")

    async def _trace_loop(self) -> None:
        try:
            while self._tracing and self.room is not None:
                try:
                    stats = await self.room.get_rtc_stats()
                    pub_rtt = extract_pub_rtt_ms(list(stats.publisher_stats))
                    self._trace_seq += 1
                    ping_id = self._trace_seq
                    now = time.monotonic() * 1000.0
                    self._trace_pending[ping_id] = (now, pub_rtt)
                    self._trace_pending = {
                        k: v for k, v in self._trace_pending.items() if now - v[0] < 5000.0
                    }
                    listeners = list(self.room.remote_participants.keys())
                    if not listeners:
                        self._emit_trace("[замер] слушателей нет")
                    elif ping_id >= 2:
                        for identity in listeners:
                            if self._trace_answered.get(identity, 0) < ping_id - 1:
                                self._emit_trace(format_line(identity, None))
                    await self.room.local_participant.publish_data(
                        encode_ping(ping_id, now, pub_rtt), reliable=False, topic=TOPIC_PING
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._emit_trace(f"[замер] ошибка: {e}")
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass

    async def _pump_soundcard(self) -> None:
        assert self._sc_source is not None
        sync_q = self._sc_sync_queue
        try:
            while self._stop_event and not self._stop_event.is_set():
                if self._pump_enabled is not None and not self._pump_enabled.is_set():
                    # Do not drain: capture keeps filling; drop-oldest preserves live edge.
                    await asyncio.sleep(0.05)
                    continue
                if sync_q is None:
                    await asyncio.sleep(0.05)
                    continue
                try:
                    frame = sync_q.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.005)
                    continue
                if self._pump_enabled is not None and not self._pump_enabled.is_set():
                    continue
                await self._sc_source.capture_frame(frame)
        except asyncio.CancelledError:
            pass

    def _build_publish_options(self) -> rtc.TrackPublishOptions:
        opts = rtc.TrackPublishOptions()
        opts.dtx = False
        opts.red = True
        opts.audio_encoding.max_bitrate = AUDIO_PUBLISH_BITRATE
        if self._lb_index is not None:
            opts.source = rtc.TrackSource.SOURCE_SCREENSHARE_AUDIO
        else:
            opts.source = rtc.TrackSource.SOURCE_MICROPHONE
        return opts

    async def _publish_audio_track(self) -> None:
        if self.room is None or self.track is None:
            raise RuntimeError("Комната или трек не инициализированы")
        opts = self._build_publish_options()
        try:
            await self.room.local_participant.publish_track(self.track, opts)
        except Exception:
            if self._lb_index is not None:
                await self._recreate_loopback_track()
                await self.room.local_participant.publish_track(self.track, opts)
            else:
                raise

    async def _recreate_loopback_track(self) -> None:
        loop = self._loop or asyncio.get_running_loop()
        old = self._sc_source
        self._sc_source = rtc.AudioSource(
            self._sample_rate,
            1,
            queue_size_ms=AUDIO_SOURCE_QUEUE_MS,
            loop=loop,
        )
        self.track = rtc.LocalAudioTrack.create_audio_track("screen-audio", self._sc_source)
        if old is not None:
            try:
                await old.aclose()
            except Exception:
                pass

    async def stop(self) -> None:
        self._user_stop = True
        await self.stop_trace()

        if self._recovery_task is not None and not self._recovery_task.done():
            self._recovery_task.cancel()
            try:
                await self._recovery_task
            except asyncio.CancelledError:
                pass
            self._recovery_task = None

        if self._stop_event and not self._stop_event.is_set():
            self._stop_event.set()

        if self._sc_stats_task is not None:
            t = self._sc_stats_task
            self._sc_stats_task = None
            if not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        if self._sc_pump_task is not None:
            t = self._sc_pump_task
            self._sc_pump_task = None
            if not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        if self._sc_thread is not None:
            th = self._sc_thread
            self._sc_thread = None
            if th.is_alive():
                th.join(timeout=5.0)

        self._sc_sync_queue = None

        if self._sc_source is not None:
            try:
                await self._sc_source.aclose()
            except Exception:
                pass
            self._sc_source = None

        if self.mic is not None:
            try:
                await self.mic.aclose()
            except Exception:
                pass
            self.mic = None

        if self.room is not None:
            try:
                await self.room.disconnect()
            except Exception:
                pass
            self.room = None

        self.track = None
        self._pump_enabled = None
        self._sync_diag_to_state()
        self.state.running = False
        self.state.connected = False
        self.state.phase = PHASE_OFFLINE
        self.ui_callback(self.state)
