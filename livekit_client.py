#!/usr/bin/env python3
import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Optional

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


class LiveKitStreamClient:
    def __init__(self, ui_callback: UpdateCallback):
        self.ui_callback = ui_callback
        self.room: Optional[rtc.Room] = None
        self.mic = None
        self.track: Optional[rtc.LocalAudioTrack] = None
        self.state = LiveKitState()
        self._stop_event: Optional[asyncio.Event] = None

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
