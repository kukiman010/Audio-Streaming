import platform
from dataclasses import dataclass
from typing import List, Optional, Tuple

import sounddevice as sd


@dataclass
class AudioInputDevice:
    device_id: str
    name: str
    backend: str
    max_input_channels: int
    default_sample_rate: int


def list_microphone_devices_only() -> List[AudioInputDevice]:
    """Только физические/виртуальные входы PortAudio (микрофоны и т.д.)."""
    devices = sd.query_devices()
    host_apis = sd.query_hostapis()
    result: List[AudioInputDevice] = []
    os_name = platform.system().lower()

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


def list_windows_loopback_devices() -> Tuple[List[AudioInputDevice], Optional[str]]:
    """
    WASAPI loopback (soundcard). Возвращает (список, текст ошибки если список пуст).
    import_soundcard() поднимает COM и обходит S_FALSE (Error 0x100000001).
    """
    if platform.system().lower() != "windows":
        return [], None
    try:
        from win_com import format_soundcard_error, import_soundcard

        sc = import_soundcard()
    except Exception as e:
        return [], f"soundcard: {e}"

    out: List[AudioInputDevice] = []
    try:
        mics = sc.all_microphones(include_loopback=True)
    except Exception as e:
        return [], format_soundcard_error(e)

    for idx, m in enumerate(mics):
        if not getattr(m, "isloopback", False):
            continue
        out.append(
            AudioInputDevice(
                device_id=f"sc_lb:{idx}",
                name=m.name,
                backend="WASAPI loopback",
                max_input_channels=int(getattr(m, "channels", 2) or 2),
                default_sample_rate=48000,
            )
        )

    if not out:
        return [], (
            "В системе не найдены loopback-устройства (запись выхода). "
            "Установите soundcard, проверьте драйверы."
        )

    # Первым — loopback того же выхода, что «По умолчанию» в Windows (наушники vs HDMI и т.д.)
    try:
        ds_name = sc.default_speaker().name.lower()
    except Exception:
        ds_name = ""

    def _sort_key(d: AudioInputDevice) -> Tuple[int, str]:
        n = d.name.lower()
        if ds_name and (ds_name in n or n in ds_name):
            return (0, n)
        return (1, n)

    out.sort(key=_sort_key)
    return out, None


def list_input_devices() -> List[AudioInputDevice]:
    """Объединённый список (совместимость): Windows — loopback сверху, затем микрофоны."""
    result: List[AudioInputDevice] = []
    if platform.system().lower() == "windows":
        lb, _ = list_windows_loopback_devices()
        result.extend(lb)
    result.extend(list_microphone_devices_only())
    return result
