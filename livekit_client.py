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

TokenProvider = Callable[[], str]
UpdateCallback = Callable[["LiveKitState"], None]

PHASE_CONNECTING = "connecting"
PHASE_CONNECTED = "connected"
PHASE_RECONNECTING = "reconnecting"
PHASE_RECOVERING = "recovering"
PHASE_OFFLINE = "offline"


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

    def _set_phase(self, phase: str) -> None:
        self.state.phase = phase
        self.state.connected = phase == PHASE_CONNECTED
        self.ui_callback(self.state)

    def _pause_pump(self) -> None:
        if self._pump_enabled is not None:
            self._pump_enabled.clear()

    def _resume_pump(self) -> None:
        if self._pump_enabled is not None:
            self._pump_enabled.set()

    def _drain_sc_queue(self) -> None:
        q = self._sc_sync_queue
        if q is None:
            return
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                break

    def _register_room_handlers(self, room: rtc.Room) -> None:
        room.on("connection_state_changed", self._on_connection_state_changed)
        room.on("reconnecting", self._on_reconnecting)
        room.on("reconnected", self._on_reconnected)
        room.on("disconnected", self._on_disconnected)

    def _on_connection_state_changed(self, connection_state: rtc.ConnectionState.ValueType) -> None:
        if connection_state == rtc.ConnectionState.CONN_CONNECTED:
            if self.state.phase not in (PHASE_RECOVERING, PHASE_CONNECTING):
                self._set_phase(PHASE_CONNECTED)
                self._resume_pump()
        elif connection_state == rtc.ConnectionState.CONN_RECONNECTING:
            self._set_phase(PHASE_RECONNECTING)
            self._pause_pump()
            self._drain_sc_queue()

    def _on_reconnecting(self) -> None:
        self.state.reconnect_count += 1
        self._set_phase(PHASE_RECONNECTING)
        self._pause_pump()
        self._drain_sc_queue()

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
            self._pause_pump()
            self._drain_sc_queue()

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

            source = rtc.AudioSource(int(sample_rate), 1, loop=loop)
            self._sc_source = source
            sync_q: queue.Queue = queue.Queue(maxsize=200)
            self._sc_sync_queue = sync_q
            frame_samples = max(1, int(sample_rate) // 100)

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

                    warnings.filterwarnings(
                        "ignore",
                        message="data discontinuity in recording",
                        category=SoundcardRuntimeWarning,
                    )
                    all_m = sc.all_microphones(include_loopback=True)
                    sc_mic = all_m[lb_index]
                    ch = min(int(sc_mic.channels), 2)
                    with sc_mic.recorder(
                        int(sample_rate), channels=ch, blocksize=frame_samples
                    ) as rec:
                        while self._stop_event and not self._stop_event.is_set():
                            data = rec.record(frame_samples)
                            if data.shape[0] < frame_samples:
                                continue
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
                            try:
                                sync_q.put_nowait(frame)
                            except queue.Full:
                                pass
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

    async def _pump_soundcard(self) -> None:
        assert self._sc_source is not None
        sync_q = self._sc_sync_queue
        try:
            while self._stop_event and not self._stop_event.is_set():
                if self._pump_enabled is not None and not self._pump_enabled.is_set():
                    self._drain_sc_queue()
                    await asyncio.sleep(0.05)
                    continue
                if sync_q is None:
                    await asyncio.sleep(0.05)
                    continue
                drained = False
                for _ in range(32):
                    try:
                        frame = sync_q.get_nowait()
                    except queue.Empty:
                        break
                    drained = True
                    if self._pump_enabled is not None and not self._pump_enabled.is_set():
                        continue
                    await self._sc_source.capture_frame(frame)
                if not drained:
                    await asyncio.sleep(0.005)
        except asyncio.CancelledError:
            pass

    async def _publish_audio_track(self) -> None:
        if self.room is None or self.track is None:
            raise RuntimeError("Комната или трек не инициализированы")
        opts = rtc.TrackPublishOptions()
        opts.source = rtc.TrackSource.SOURCE_MICROPHONE
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
        self._sc_source = rtc.AudioSource(self._sample_rate, 1, loop=loop)
        self.track = rtc.LocalAudioTrack.create_audio_track("screen-audio", self._sc_source)
        if old is not None:
            try:
                await old.aclose()
            except Exception:
                pass

    async def stop(self) -> None:
        self._user_stop = True

        if self._recovery_task is not None and not self._recovery_task.done():
            self._recovery_task.cancel()
            try:
                await self._recovery_task
            except asyncio.CancelledError:
                pass
            self._recovery_task = None

        if self._stop_event and not self._stop_event.is_set():
            self._stop_event.set()

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
        self.state.running = False
        self.state.connected = False
        self.state.phase = PHASE_OFFLINE
        self.ui_callback(self.state)
