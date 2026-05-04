import platform
from dataclasses import dataclass
from typing import List

import sounddevice as sd


@dataclass
class AudioInputDevice:
    device_id: str
    name: str
    backend: str
    max_input_channels: int
    default_sample_rate: int


def _list_windows_loopback_soundcard() -> List[AudioInputDevice]:
    """WASAPI loopback (звук с экрана / колонок / выбранного выхода). Только Windows + soundcard."""
    try:
        import soundcard as sc  # type: ignore[import-untyped]
    except Exception:
        return []
    out: List[AudioInputDevice] = []
    for idx, m in enumerate(sc.all_microphones(include_loopback=True)):
        if not getattr(m, "isloopback", False):
            continue
        out.append(
            AudioInputDevice(
                device_id=f"sc_lb:{idx}",
                name=f"Звук с экрана: {m.name}",
                backend="WASAPI loopback",
                max_input_channels=int(getattr(m, "channels", 2) or 2),
                default_sample_rate=48000,
            )
        )
    return out


def list_input_devices() -> List[AudioInputDevice]:
    devices = sd.query_devices()
    host_apis = sd.query_hostapis()
    result: List[AudioInputDevice] = []
    os_name = platform.system().lower()

    if "windows" in os_name:
        result.extend(_list_windows_loopback_soundcard())

    for idx, dev in enumerate(devices):
        max_input = int(dev.get("max_input_channels", 0))
        if max_input <= 0:
            continue
        hostapi_idx = int(dev.get("hostapi", 0))
        hostapi_name = host_apis[hostapi_idx]["name"] if hostapi_idx < len(host_apis) else "Unknown"
        backend = hostapi_name
        if "windows" in os_name and "wasapi" in hostapi_name.lower():
            backend = "WASAPI"
        elif "linux" in os_name and "alsa" in hostapi_name.lower():
            backend = "ALSA"
        elif "linux" in os_name and "pulse" in hostapi_name.lower():
            backend = "PulseAudio"

        result.append(
            AudioInputDevice(
                device_id=str(idx),
                name=str(dev.get("name", f"Input {idx}")),
                backend=backend,
                max_input_channels=max_input,
                default_sample_rate=int(float(dev.get("default_samplerate", 48000))),
            )
        )

    return result
