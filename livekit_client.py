#!/usr/bin/env python3
import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
from livekit import rtc


@dataclass
class LiveKitState:
    running: bool = False
    connected: bool = False
    room_name: str = ""
    identity: str = ""
    reconnect_count: int = 0
    last_error: str = ""
    started_at: float = 0.0


UpdateCallback = Callable[[LiveKitState], None]


def _soundcard_loopback_device_by_index(index: int):
    import soundcard as sc  # type: ignore[import-untyped]

    all_m = sc.all_microphones(include_loopback=True)
    if not (0 <= index < len(all_m)):
        raise IndexError("Нет устройства с таким индексом (soundcard).")
    dev = all_m[index]
    if not getattr(dev, "isloopback", False):
        raise ValueError("Указанный индекс не является loopback-устройством.")
    return dev


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

    async def start(
        self,
        server_url: str,
        token: str,
        device_id: Optional[str] = None,
        channels: int = 1,
        sample_rate: int = 48000,
    ) -> None:
        # `channels` is kept for callers (e.g. GUI); native capture is always mono — see MediaDevices note below.
        self.state = LiveKitState(running=True, started_at=time.time())
        self.ui_callback(self.state)
        self._stop_event = asyncio.Event()

        try:
            self.room = rtc.Room()
            await self.room.connect(server_url, token)
            self.state.connected = True
            self.state.room_name = self.room.name or ""
            self.ui_callback(self.state)
        except Exception as e:
            self.state.running = False
            self.state.last_error = f"LiveKit connect failed: {e}"
            self.ui_callback(self.state)
            return

        try:
            loop = asyncio.get_running_loop()
            if _is_soundcard_loopback_id(device_id):
                try:
                    lb_index = int(str(device_id).split(":", 1)[1].strip())
                except (ValueError, IndexError):
                    self.state.last_error = f"Некорректный идентификатор loopback: {device_id!r}"
                    await self.stop()
                    return
                try:
                    sc_mic = _soundcard_loopback_device_by_index(lb_index)
                except Exception as e:
                    self.state.last_error = f"Устройство loopback: {e}"
                    await self.stop()
                    return

                source = rtc.AudioSource(int(sample_rate), 1, loop=loop)
                self._sc_source = source
                q: asyncio.Queue = asyncio.Queue(maxsize=50)
                frame_samples = max(1, int(sample_rate) // 100)

                def _loopback_worker() -> None:
                    try:
                        ch = min(int(sc_mic.channels), 2)
                        with sc_mic.recorder(int(sample_rate), channels=ch) as rec:
                            while self._stop_event and not self._stop_event.is_set():
                                data = rec.record(frame_samples)
                                if data.ndim == 2 and data.shape[1] >= 2:
                                    mono = np.mean(data[:, :2], axis=1, dtype=np.float64)
                                elif data.ndim == 2:
                                    mono = data[:, 0].astype(np.float64)
                                else:
                                    mono = data.astype(np.float64)
                                pcm = np.clip(mono * 32767.0, -32768, 32767).astype(np.int16)
                                frame = rtc.AudioFrame(
                                    pcm.tobytes(),
                                    int(sample_rate),
                                    1,
                                    frame_samples,
                                )

                                def _enqueue(fr: rtc.AudioFrame) -> None:
                                    try:
                                        q.put_nowait(fr)
                                    except asyncio.QueueFull:
                                        pass

                                loop.call_soon_threadsafe(_enqueue, frame)
                    except Exception as e:

                        def _fail() -> None:
                            self.state.last_error = f"Захват звука экрана: {e}"
                            self.ui_callback(self.state)
                            if self._stop_event:
                                self._stop_event.set()

                        loop.call_soon_threadsafe(_fail)

                self._sc_thread = threading.Thread(
                    target=_loopback_worker, name="soundcard-loopback", daemon=True
                )
                self._sc_thread.start()

                async def _pump_soundcard() -> None:
                    assert self._sc_source is not None
                    try:
                        while self._stop_event and not self._stop_event.is_set():
                            try:
                                frame = await asyncio.wait_for(q.get(), timeout=0.3)
                            except asyncio.TimeoutError:
                                continue
                            await self._sc_source.capture_frame(frame)
                    except asyncio.CancelledError:
                        pass

                self._sc_pump_task = asyncio.create_task(_pump_soundcard())
                self.track = rtc.LocalAudioTrack.create_audio_track("screen-audio", source)
                opts = rtc.TrackPublishOptions()
                opts.source = rtc.TrackSource.SOURCE_SCREENSHARE_AUDIO
                await self.room.local_participant.publish_track(self.track, opts)
            else:
                # livekit.rtc.MediaDevices builds each frame from indata[start:end, 0] only, but still
                # passes num_channels to AudioFrame. With num_channels=2 that would require interleaved
                # stereo bytes; the buffer is only one channel → ValueError. Use mono capture for LiveKit.
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
                        self.state.last_error = f"Некорректный индекс устройства: {device_id!r}"
                        await self.stop()
                        return
                self.mic = devices.open_input(**open_kwargs)
                self.track = rtc.LocalAudioTrack.create_audio_track("input", self.mic.source)
                opts = rtc.TrackPublishOptions()
                opts.source = rtc.TrackSource.SOURCE_MICROPHONE
                await self.room.local_participant.publish_track(self.track, opts)
        except Exception as e:
            self.state.last_error = f"Publish failed: {e}"
            await self.stop()
            return

        await self._stop_event.wait()
        await self.stop()

    async def stop(self) -> None:
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
        self.state.running = False
        self.state.connected = False
        self.ui_callback(self.state)
