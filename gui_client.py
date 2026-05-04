#!/usr/bin/env python3
import asyncio
import ipaddress
import json
import shutil
import subprocess
import sys
import threading
import time
import os
import re
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from dataclasses import dataclass
from typing import List, Tuple, Optional, Callable

import platform

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, scrolledtext
from audio_devices import (
    AudioInputDevice,
    list_input_devices,
    list_microphone_devices_only,
    list_windows_loopback_devices,
)
from livekit_client import LiveKitStreamClient, LiveKitState
from env_loader import load_env_files

try:
    from aiohttp import ClientSession, WSMsgType, ClientConnectorError, WSServerHandshakeError
except Exception as e:
    print("Не установлен aiohttp. Установите: pip install aiohttp", file=sys.stderr)
    raise

FFMPEG_BIN = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
ARECORD_BIN = shutil.which("arecord") or "/usr/bin/arecord"
PACTL_BIN = shutil.which("pactl") or "/usr/bin/pactl"

# Префикс для своих виртуальных устройств PulseAudio (как в audio_recorder.py)
PREFIX = "MYAPP_"
# Сначала каталог скрипта (Windows/запуск не из корня проекта), затем cwd с приоритетом переопределения.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_env_files(
    (os.path.join(_SCRIPT_DIR, "livekit.env"), os.path.join(_SCRIPT_DIR, ".env")),
)
load_env_files(("livekit.env", ".env"), override_existing=True)
ENABLE_LEGACY_TRANSPORT = os.getenv("ENABLE_LEGACY_TRANSPORT", "0") == "1"
# Порты по умолчанию, если в поле «Сервер» указан только IP/hostname
DEFAULT_LIVEKIT_PORT = int(os.getenv("DEFAULT_LIVEKIT_PORT", "7880"))
DEFAULT_HELPER_PORT = int(os.getenv("DEFAULT_HELPER_PORT", "8000"))
# Legacy MP3 WebSocket (транспорт «не LiveKit») — если задан только хост
DEFAULT_LEGACY_WS_PORT = int(os.getenv("DEFAULT_LEGACY_WS_PORT", "8765"))


def _host_for_url(host: str) -> str:
    """IPv6 в URL должен быть в квадратных скобках."""
    try:
        ip = ipaddress.ip_address(host)
        if isinstance(ip, ipaddress.IPv6Address):
            return f"[{ip.compressed}]"
    except ValueError:
        pass
    return host


def _default_gui_server_field() -> str:
    h = os.getenv("GUI_DEFAULT_SERVER", "").strip()
    if h:
        return h
    for u in (os.getenv("HELPER_URL", ""), os.getenv("LIVEKIT_URL", "")):
        u = u.strip()
        if not u:
            continue
        try:
            p = urlparse(u)
            if p.hostname:
                return p.hostname
        except Exception:
            pass
    return "127.0.0.1"


def fetch_client_discovery(helper_url: str) -> Optional[dict]:
    url = f"{helper_url.rstrip('/')}/client/discovery"
    try:
        with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=5.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def resolve_livekit_endpoints(user_text: str) -> Tuple[str, str]:
    """
    Разбор поля «Сервер» в (LiveKit ws/wss URL, helper http(s) URL).
    Допустимо: только IP/hostname, http(s)://… (helper), ws(s)://… (LiveKit).
    """
    t = user_text.strip()
    if not t:
        raise ValueError("Укажите сервер (IP, hostname или URL).")
    if t.startswith("ttp://"):
        t = "http://" + t[6:]
    low = t.lower()
    if low.startswith("http://") or low.startswith("https://"):
        p = urlparse(t)
        h = p.hostname
        if not h:
            raise ValueError("Некорректный HTTP(S) URL.")
        helper = t.split("?", 1)[0].rstrip("/")
        lk_scheme = "wss" if p.scheme == "https" else "ws"
        livekit = f"{lk_scheme}://{h}:{DEFAULT_LIVEKIT_PORT}"
        return (livekit, helper)
    if low.startswith("ws://") or low.startswith("wss://"):
        p = urlparse(t)
        h = p.hostname
        if not h:
            raise ValueError("Некорректный WebSocket URL.")
        livekit = t.split("?", 1)[0].rstrip("/")
        if p.scheme == "wss":
            helper = f"https://{h}:{DEFAULT_HELPER_PORT}"
        else:
            helper = f"http://{h}:{DEFAULT_HELPER_PORT}"
        return (livekit, helper)
    if "://" in t:
        raise ValueError("Ожидался IP/hostname или URL с http(s) или ws(s).")
    # host или host:port (IPv4; IPv6 — в квадратных скобках)
    if t.count(":") == 1 and not t.startswith("["):
        a, b = t.rsplit(":", 1)
        if b.isdigit():
            port = int(b)
            host = _host_for_url(a.strip("[]"))
            if port == DEFAULT_LIVEKIT_PORT:
                return (
                    f"ws://{host}:{port}",
                    f"http://{host}:{DEFAULT_HELPER_PORT}",
                )
            return (
                f"ws://{host}:{DEFAULT_LIVEKIT_PORT}",
                f"http://{host}:{port}",
            )
    host = _host_for_url(t.strip("[]"))
    return (
        f"ws://{host}:{DEFAULT_LIVEKIT_PORT}",
        f"http://{host}:{DEFAULT_HELPER_PORT}",
    )


def resolve_legacy_websocket_url(user_text: str) -> str:
    """URL WebSocket для legacy-транспорта."""
    t = user_text.strip()
    if not t:
        raise ValueError("Укажите сервер (IP, hostname или ws://…).")
    low = t.lower()
    if low.startswith("ws://") or low.startswith("wss://"):
        return t.split("?", 1)[0].rstrip("/")
    if "://" in t:
        raise ValueError("Для legacy укажите ws://… или IP/hostname.")
    if t.count(":") == 1 and not t.startswith("["):
        a, b = t.rsplit(":", 1)
        if b.isdigit():
            return f"ws://{_host_for_url(a.strip('[]'))}:{b}"
    host = _host_for_url(t.strip("[]"))
    return f"ws://{host}:{DEFAULT_LEGACY_WS_PORT}"


def livekit_urls_with_discovery(user_text: str) -> Tuple[str, str]:
    """Как resolve_livekit_endpoints, затем при успехе — подстановка с helper /client/discovery."""
    lk, helper = resolve_livekit_endpoints(user_text)
    data = fetch_client_discovery(helper)
    if data:
        lk = (data.get("livekit_url") or lk).strip() or lk
        helper = (data.get("helper_url") or helper).strip() or helper
    return (lk, helper)
DEFAULT_LIVEKIT_ROOM = os.getenv("LIVEKIT_DEFAULT_ROOM", "audio-room")
DEFAULT_LIVEKIT_IDENTITY = os.getenv("LIVEKIT_PUBLISHER_IDENTITY", "publisher-local")

# ---------------- Утилиты обнаружения устройств ----------------

def list_alsa_devices() -> List[Tuple[str, str]]:
    """
    Возвращает список устройств ALSA как [(id, label)], где id можно передать в ffmpeg.
    """
    result: List[Tuple[str, str]] = [("default", "default (ALSA по умолчанию)")]
    try:
        out = subprocess.check_output([ARECORD_BIN, "-l"], stderr=subprocess.STDOUT, text=True, errors="replace")
        lines = out.splitlines()
        for line in lines:
            line = line.strip()
            if line.startswith("card ") and ", device " in line:
                try:
                    prefix, rest = line.split(":", 1)
                    _, card_num_str = prefix.split("card", 1)
                    card_num = int(card_num_str.strip())
                    dev_part = rest.split(", device", 1)[1]
                    dev_num = int(dev_part.split(":")[0].strip())
                    card_name = rest.split(", device", 1)[0].strip()
                    dev_name = dev_part.split(":", 1)[1].strip()
                    alsa_id = f"hw:{card_num},{dev_num}"
                    label = f"{alsa_id} — {card_name} / {dev_name}"
                    result.append((alsa_id, label))
                except Exception:
                    continue
    except Exception:
        pass
    # dedup, keep order
    seen = set()
    uniq: List[Tuple[str, str]] = []
    for i, l in result:
        if i not in seen:
            uniq.append((i, l))
            seen.add(i)
    return uniq

def has_pactl() -> bool:
    return os.path.exists(PACTL_BIN)

def list_pulse_sources() -> List[Tuple[str, str]]:
    """
    Возвращает список источников PulseAudio [(name, label)] из 'pactl list short sources'.
    Чаще всего для записи нужен монитор sink'а: <sink>.monitor.
    """
    res: List[Tuple[str, str]] = []
    if not has_pactl():
        return res
    try:
        out = subprocess.check_output([PACTL_BIN, "list", "short", "sources"], encoding="utf-8", stderr=subprocess.STDOUT)
        for line in out.strip().splitlines():
            cols = line.split("\t")
            if len(cols) >= 2:
                name = cols[1]
                res.append((name, name))
    except Exception:
        pass
    return res

def get_null_sinks() -> List[str]:
    """
    Возвращает список имён виртуальных sink'ов PulseAudio нашей программы (по префиксу).
    """
    if not has_pactl():
        return []
    try:
        out = subprocess.check_output([PACTL_BIN, "list", "short", "sinks"], encoding="utf-8", stderr=subprocess.STDOUT)
    except Exception:
        return []
    pattern = re.compile(f"^{re.escape(PREFIX)}")
    sinks = []
    for line in out.strip().splitlines():
        cols = line.split("\t")
        if len(cols) >= 2:
            name = cols[1]
            if pattern.match(name):
                sinks.append(name)
    return sinks

def create_virtual_device_interactive(parent: tk.Tk):
    """
    Создаёт виртуальный sink (module-null-sink). Автоматически появится source <sink>.monitor.
    """
    if not has_pactl():
        messagebox.showerror("Ошибка", "pactl не найден. Установите pulseaudio-utils (или PipeWire с совместимостью).", parent=parent)
        return
    vdev_base = PREFIX + "VIRTUAL_SPEAKER"
    existings = set(get_null_sinks())
    unique_name = vdev_base
    idx = 1
    while unique_name in existings:
        unique_name = f"{vdev_base}{idx}"
        idx += 1
    desc = simpledialog.askstring("Имя устройства", f"Введите название sink (по умолчанию: {unique_name})",
                                  initialvalue=unique_name, parent=parent)
    if not desc:
        return
    if not desc.startswith(PREFIX):
        desc = PREFIX + desc
    if desc in existings:
        messagebox.showerror("Ошибка", "Устройство с таким именем уже существует!", parent=parent)
        return
    try:
        subprocess.check_call([
            PACTL_BIN, "load-module", "module-null-sink",
            f"sink_name={desc}",
            f"sink_properties=device.description={desc}_Device"
        ])
        messagebox.showinfo("Успех", f"Создан виртуальный sink: {desc}\nИсточник для записи: {desc}.monitor", parent=parent)
    except Exception as e:
        messagebox.showerror("Ошибка", f"Не удалось создать виртуальный sink: {e}", parent=parent)

def delete_virtual_device_interactive(parent: tk.Tk):
    """
    Удаляет наш виртуальный sink (по имени), выгружая соответствующий модуль.
    """
    if not has_pactl():
        messagebox.showerror("Ошибка", "pactl не найден.", parent=parent)
        return
    sinks = get_null_sinks()
    if not sinks:
        messagebox.showinfo("Нет устройств", f"Нет виртуальных устройств с префиксом {PREFIX}", parent=parent)
        return
    sink = simpledialog.askstring("Удаление устройства",
                                  "Выберите sink для удаления:\n" + "\n".join(sinks),
                                  initialvalue=sinks[0], parent=parent)
    if not sink or not sink.startswith(PREFIX):
        messagebox.showwarning("Внимание", "Можно удалять только свои устройства (с префиксом).", parent=parent)
        return
    try:
        out = subprocess.check_output([PACTL_BIN, "list", "short", "modules"], encoding="utf-8")
        module_id = None
        for line in out.splitlines():
            if f"sink_name={sink}" in line:
                module_id = line.split("\t")[0].strip()
                break
        if not module_id:
            messagebox.showerror("Ошибка", "Не нашли модуль для удаления!", parent=parent)
            return
        subprocess.check_call([PACTL_BIN, "unload-module", module_id])
        messagebox.showinfo("Удалено", f"Виртуальный sink {sink} удалён", parent=parent)
    except Exception as e:
        messagebox.showerror("Ошибка", f"Ошибка удаления: {e}", parent=parent)

# ---------------- ffmpeg command ----------------

def build_ffmpeg_cmd(input_backend: str, device: str, channels: int, rate: int, bitrate_kbps: int):
    if not FFMPEG_BIN:
        raise RuntimeError("ffmpeg не найден. Установите пакет ffmpeg.")
    if input_backend == "pulse":
        return [
            FFMPEG_BIN,
            "-hide_banner",
            "-loglevel", "error",
            "-f", "pulse",
            "-i", device,
            "-ac", str(channels),
            "-ar", str(rate),
            "-codec:a", "libmp3lame",
            "-b:a", f"{bitrate_kbps}k",
            "-f", "mp3",
            "-"  # stdout
        ]
    elif input_backend == "alsa":
        return [
            FFMPEG_BIN,
            "-hide_banner",
            "-loglevel", "error",
            "-f", "alsa",
            "-i", device,
            "-ac", str(channels),
            "-ar", str(rate),
            "-codec:a", "libmp3lame",
            "-b:a", f"{bitrate_kbps}k",
            "-f", "mp3",
            "-"
        ]
    else:
        raise ValueError(f"Неизвестный backend: {input_backend}")


def request_publisher_token(helper_url: str, room: str, identity: str, pairing_secret: str) -> str:
    query = urlencode(
        {
            "room": room,
            "identity": identity,
            "pairing_secret": pairing_secret,
        }
    )
    url = f"{helper_url.rstrip('/')}/livekit/publisher_token?{query}"
    try:
        with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=10.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        try:
            msg = json.loads(detail).get("error", detail)
        except json.JSONDecodeError:
            msg = detail or str(e)
        raise RuntimeError(f"Helper HTTP {e.code}: {msg}") from e
    except URLError as e:
        raise RuntimeError(
            f"Не удалось достучаться до helper ({helper_url}). "
            f"Проверьте URL, порт (часто :8000) и firewall. Причина: {e.reason}"
        ) from e
    token = payload.get("token", "")
    if not token:
        err = payload.get("error", "")
        raise RuntimeError(err or "Не удалось получить publisher token от helper-сервера.")
    return token

# ---------------- Streaming core (asyncio in a background thread) ----------------

@dataclass
class StreamState:
    running: bool = False
    ack: bool = False
    listeners: int = 0
    uptime_sec: float = 0.0
    sent_bytes: int = 0
    last_error: str = ""

UpdateCallback = Callable[[StreamState], None]

class StreamClient:
    def __init__(self, ui_callback: UpdateCallback):
        self.ui_callback = ui_callback
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.loop_thread: Optional[threading.Thread] = None
        self.session: Optional[ClientSession] = None
        self.ws = None
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.send_task: Optional[asyncio.Task] = None
        self.recv_task: Optional[asyncio.Task] = None
        self.stop_event: Optional[asyncio.Event] = None
        self.state = StreamState()

    # ---- event loop management ----
    def ensure_loop(self):
        if self.loop and self.loop.is_running():
            return
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever, name="stream-loop", daemon=True)
        self.loop_thread.start()

    def run_coro(self, coro):
        self.ensure_loop()
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # ---- public API called from GUI thread ----
    def start(self, server_url: str, backend: str, device: str, channels: int, rate: int, bitrate: int, chunk_size: int):
        if self.state.running:
            return
        self.state = StreamState(running=True)
        self.ui_callback(self.state)
        self.run_coro(self._start_async(server_url, backend, device, channels, rate, bitrate, chunk_size))

    def stop(self):
        if not self.state.running:
            return
        self.run_coro(self._stop_async())

    # ---- internal async logic ----
    async def _start_async(self, server_url: str, backend: str, device: str, channels: int, rate: int, bitrate: int, chunk_size: int):
        self.stop_event = asyncio.Event()
        self.state = StreamState(running=True)
        self.ui_callback(self.state)

        # Start ffmpeg
        try:
            ff_cmd = build_ffmpeg_cmd(backend, device, channels, rate, bitrate)
        except Exception as e:
            self.state.last_error = str(e)
            self.state.running = False
            self.ui_callback(self.state)
            return

        try:
            self.proc = await asyncio.create_subprocess_exec(
                *ff_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
        except Exception as e:
            self.state.last_error = f"ffmpeg не запустился: {e}"
            self.state.running = False
            self.ui_callback(self.state)
            return

        # Open WebSocket
        try:
            self.session = ClientSession()
            self.ws = await self.session.ws_connect(server_url, heartbeat=10.0)
        except (ClientConnectorError, WSServerHandshakeError) as e:
            self.state.last_error = f"WS ошибка подключения: {e}"
            self.state.running = False
            self.ui_callback(self.state)
            await self._cleanup_subprocess()
            await self._cleanup_session()
            return
        except Exception as e:
            self.state.last_error = f"WS исключение: {e}"
            self.state.running = False
            self.ui_callback(self.state)
            await self._cleanup_subprocess()
            await self._cleanup_session()
            return

        # Launch loops
        self.send_task = asyncio.create_task(self._send_loop(chunk_size))
        self.recv_task = asyncio.create_task(self._recv_loop())

        # Await stop
        await self.stop_event.wait()
        # Teardown
        await self._teardown()

    async def _send_loop(self, chunk_size: int):
        last_report = time.time()
        try:
            while not self.stop_event.is_set():
                if not self.proc or not self.proc.stdout:
                    await asyncio.sleep(0.05)
                    continue
                chunk = await self.proc.stdout.read(chunk_size)
                if not chunk:
                    if self.proc.returncode is not None:
                        self.state.last_error = f"ffmpeg завершился с кодом {self.proc.returncode}"
                        break
                    await asyncio.sleep(0.01)
                    continue
                if self.ws is not None:
                    await self.ws.send_bytes(chunk)
                self.state.sent_bytes += len(chunk)
                now = time.time()
                if now - last_report >= 1.0:
                    self.ui_callback(self.state)
                    last_report = now
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.state.last_error = f"Ошибка отправки: {e}"
        finally:
            self.ui_callback(self.state)
            if self.stop_event and not self.stop_event.is_set():
                self.stop_event.set()

    async def _recv_loop(self):
        try:
            async for msg in self.ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except Exception:
                        data = None
                    if data:
                        if data.get("type") == "ack":
                            self.state.ack = True
                            self.state.listeners = int(data.get("listeners", 0))
                            self.ui_callback(self.state)
                        elif data.get("type") == "stats":
                            self.state.listeners = int(data.get("listeners", 0))
                            self.state.uptime_sec = float(data.get("uptime_sec", 0.0))
                            self.ui_callback(self.state)
                elif msg.type == WSMsgType.ERROR:
                    self.state.last_error = f"WS ошибка: {self.ws.exception()}"
                    break
                elif msg.type == WSMsgType.CLOSE:
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.state.last_error = f"Ошибка приёма: {e}"
        finally:
            self.ui_callback(self.state)
            if self.stop_event and not self.stop_event.is_set():
                self.stop_event.set()

    async def _stop_async(self):
        if self.stop_event and not self.stop_event.is_set():
            self.stop_event.set()

    async def _teardown(self):
        for t in (self.send_task, self.recv_task):
            if t and not t.done():
                t.cancel()
                with contextlib_suppress():
                    await t
        await self._cleanup_ws()
        await self._cleanup_session()
        await self._cleanup_subprocess()
        self.state.running = False
        self.ui_callback(self.state)

    async def _cleanup_ws(self):
        try:
            if self.ws is not None:
                with contextlib_suppress():
                    await self.ws.close()
        finally:
            self.ws = None

    async def _cleanup_session(self):
        try:
            if self.session is not None:
                with contextlib_suppress():
                    await self.session.close()
        finally:
            self.session = None

    async def _cleanup_subprocess(self):
        if self.proc:
            with contextlib_suppress():
                self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=3.0)
            except Exception:
                with contextlib_suppress():
                    self.proc.kill()
            self.proc = None


class contextlib_suppress:
    def __init__(self, *exc):
        self.exc = exc or (Exception,)
    def __enter__(self): return None
    def __exit__(self, exc_type, exc, tb): return exc_type and issubclass(exc_type, self.exc)


class LiveKitClientAdapter:
    def __init__(self, ui_callback: Callable[[LiveKitState], None]):
        self._client = LiveKitStreamClient(ui_callback)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="livekit-loop")
        self._thread.start()

    @property
    def state(self) -> LiveKitState:
        return self._client.state

    def start(
        self,
        server_url: str,
        token: str,
        device_id: Optional[str],
        channels: int,
        sample_rate: int,
    ) -> None:
        asyncio.run_coroutine_threadsafe(
            self._client.start(
                server_url=server_url,
                token=token,
                device_id=device_id,
                channels=channels,
                sample_rate=sample_rate,
            ),
            self._loop,
        )

    def stop(self) -> None:
        asyncio.run_coroutine_threadsafe(self._client.stop(), self._loop)

# ---------------- Tkinter GUI ----------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Audio Streamer (LiveKit)")
        self.geometry("940x680")
        self.minsize(880, 620)
        self.resizable(True, True)

        self._audio_drawer_open = False
        self._helper_url_for_viewer: str = ""
        self._qr_photo_ref: Optional[tk.PhotoImage] = None

        self.legacy_client = StreamClient(self.on_state_update)
        self.livekit_client = LiveKitClientAdapter(self.on_livekit_state_update)
        self.transport_values = ["LiveKit (native)"] + (["Legacy WS+FFmpeg"] if ENABLE_LEGACY_TRANSPORT else [])
        self.input_devices: List[AudioInputDevice] = []
        self.loopback_devices: List[AudioInputDevice] = []
        self.mic_devices: List[AudioInputDevice] = []

        pad = {"padx": 8, "pady": 6}
        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Транспорт:").grid(row=0, column=0, sticky="w", **pad)
        self.var_transport = tk.StringVar(value=self.transport_values[0])
        self.combo_transport = ttk.Combobox(frm, state="readonly", width=24, textvariable=self.var_transport, values=self.transport_values)
        self.combo_transport.grid(row=0, column=1, sticky="w", **pad)
        self.combo_transport.bind("<<ComboboxSelected>>", lambda e: self.on_transport_changed())

        # Сервер: IP/hostname (порты 8000 и 7880), полный URL helper или ws(s) LiveKit
        self.var_server = tk.StringVar(value=_default_gui_server_field())
        ttk.Label(frm, text="Сервер:").grid(row=1, column=0, sticky="nw", **pad)
        self.entry_server = ttk.Entry(frm, textvariable=self.var_server, width=55)
        self.entry_server.grid(row=1, column=1, sticky="we", **pad, columnspan=3)
        ttk.Label(
            frm,
            text="Достаточно указать IP — http://…:8000 и ws://…:7880 подставятся; при старте "
            "запрашивается /client/discovery у helper (как на сервере в livekit.env).",
            wraplength=640,
            foreground="#555",
        ).grid(row=2, column=1, sticky="w", padx=8, pady=(0, 2), columnspan=3)

        ttk.Label(frm, text="Комната:").grid(row=3, column=0, sticky="w", **pad)
        self.var_room = tk.StringVar(value=DEFAULT_LIVEKIT_ROOM)
        ttk.Entry(frm, textvariable=self.var_room, width=20).grid(row=3, column=1, sticky="w", **pad)

        ttk.Label(frm, text="Identity:").grid(row=3, column=2, sticky="e", **pad)
        self.var_identity = tk.StringVar(value=DEFAULT_LIVEKIT_IDENTITY)
        ttk.Entry(frm, textvariable=self.var_identity, width=20).grid(row=3, column=3, sticky="we", **pad)

        ttk.Label(frm, text="Pairing secret:").grid(row=4, column=0, sticky="w", **pad)
        self.var_pairing_secret = tk.StringVar(value=os.getenv("LIVEKIT_PAIRING_SECRET", ""))
        self.entry_pairing_secret = ttk.Entry(frm, textvariable=self.var_pairing_secret, width=50, show="*")
        self.entry_pairing_secret.grid(row=4, column=1, sticky="we", **pad, columnspan=3)

        # Источник: выдвижная панель (по умолчанию свёрнута; системный захват — без обязательного выбора в списке)
        self.var_win_route = tk.StringVar(value="system")
        self.var_web_viewer = tk.StringVar(value="")

        self.frm_audio_outer = ttk.Frame(frm)
        self.frm_audio_outer.grid(row=5, column=0, columnspan=4, sticky="ew", padx=8, pady=4)
        self.btn_audio_drawer = ttk.Button(
            self.frm_audio_outer,
            text="▶ Источник аудио",
            width=22,
            command=self._toggle_audio_drawer,
        )
        self.btn_audio_drawer.grid(row=0, column=0, sticky="nw")
        self.lbl_audio_drawer_summary = ttk.Label(self.frm_audio_outer, text="", foreground="#333")
        self.lbl_audio_drawer_summary.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.frm_audio_outer.columnconfigure(1, weight=1)

        self.frm_audio = ttk.LabelFrame(self.frm_audio_outer, text="Источник для трансляции")

        self.frm_audio_win = ttk.Frame(self.frm_audio)
        self.frm_audio_simple = ttk.Frame(self.frm_audio)
        pad_a = {"padx": 8, "pady": 4}
        ttk.Radiobutton(
            self.frm_audio_win,
            text="Системный звук (музыки, браузер, игры) — не микрофон",
            variable=self.var_win_route,
            value="system",
            command=self._on_win_audio_mode,
        ).grid(row=0, column=0, columnspan=2, sticky="w", **pad_a)
        ttk.Radiobutton(
            self.frm_audio_win,
            text="Микрофон",
            variable=self.var_win_route,
            value="mic",
            command=self._on_win_audio_mode,
        ).grid(row=1, column=0, columnspan=2, sticky="w", **pad_a)
        ttk.Label(
            self.frm_audio_win,
            text="Запись выхода (наушники/колонки = тот же вывод, куда играет музыка):",
        ).grid(row=2, column=0, sticky="nw", **pad_a)
        self.combo_win_loopback = ttk.Combobox(self.frm_audio_win, state="readonly", width=68)
        self.combo_win_loopback.grid(row=2, column=1, sticky="we", **pad_a)
        self.combo_win_loopback.bind("<<ComboboxSelected>>", lambda e: self._update_audio_drawer_summary())
        ttk.Label(self.frm_audio_win, text="Микрофон:").grid(row=3, column=0, sticky="nw", **pad_a)
        self.combo_win_mic = ttk.Combobox(self.frm_audio_win, state="readonly", width=68)
        self.combo_win_mic.grid(row=3, column=1, sticky="we", **pad_a)
        self.combo_win_mic.bind("<<ComboboxSelected>>", lambda e: self._update_audio_drawer_summary())
        self.lbl_loopback_hint = ttk.Label(
            self.frm_audio_win,
            text="",
            foreground="#555",
            wraplength=760,
            justify="left",
        )
        self.lbl_loopback_hint.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0), padx=8)
        self.frm_audio_win.columnconfigure(1, weight=1)

        ttk.Label(self.frm_audio_simple, text="Устройство:").grid(row=0, column=0, sticky="w", **pad_a)
        self.combo_device = ttk.Combobox(self.frm_audio_simple, state="readonly", width=68)
        self.combo_device.grid(row=0, column=1, sticky="we", **pad_a)
        self.combo_device.bind("<<ComboboxSelected>>", lambda e: self._update_audio_drawer_summary())
        self.frm_audio_simple.columnconfigure(1, weight=1)
        self.frm_audio.columnconfigure(0, weight=1)

        # Create/Delete virtual device (PulseAudio only)
        self.btn_create = ttk.Button(frm, text="Создать виртуальное устройство", command=self.on_create_vdev)
        self.btn_delete = ttk.Button(frm, text="Удалить виртуальное устройство", command=self.on_delete_vdev)
        self.btn_refresh = ttk.Button(frm, text="Обновить источники", command=self.on_refresh_devices)
        self.btn_create.grid(row=6, column=1, sticky="we", **pad)
        self.btn_delete.grid(row=6, column=2, sticky="we", **pad)
        self.btn_refresh.grid(row=6, column=3, sticky="we", **pad)

        # Channels / Rate / Bitrate
        ttk.Label(frm, text="Каналы:").grid(row=7, column=0, sticky="w", **pad)
        self.var_channels = tk.IntVar(value=2)
        ttk.Spinbox(frm, from_=1, to=2, textvariable=self.var_channels, width=5).grid(row=7, column=1, sticky="w", **pad)

        ttk.Label(frm, text="Частота, Гц:").grid(row=8, column=0, sticky="w", **pad)
        self.var_rate = tk.IntVar(value=48000)
        ttk.Spinbox(frm, from_=16000, to=96000, increment=1000, textvariable=self.var_rate, width=9)\
            .grid(row=8, column=1, sticky="w", **pad)

        ttk.Label(frm, text="Битрейт MP3, кбит/с (legacy):").grid(row=9, column=0, sticky="w", **pad)
        self.var_bitrate = tk.IntVar(value=128)
        self.spin_bitrate = ttk.Spinbox(frm, from_=64, to=320, increment=16, textvariable=self.var_bitrate, width=9)
        self.spin_bitrate.grid(row=9, column=1, sticky="w", **pad)

        # Status
        self.lbl_status = ttk.Label(frm, text="Статус: offline", foreground="#b00")
        self.lbl_status.grid(row=10, column=0, sticky="w", **pad, columnspan=4)

        self.lbl_extra = ttk.Label(frm, text="LiveKit room: - | Connected: no", foreground="#333")
        self.lbl_extra.grid(row=11, column=0, sticky="w", **pad, columnspan=4)

        # Buttons
        self.btn_start = ttk.Button(frm, text="Старт", command=self.on_start)
        self.btn_stop = ttk.Button(frm, text="Стоп", command=self.on_stop, state="disabled")
        self.btn_start.grid(row=12, column=2, **pad, sticky="we")
        self.btn_stop.grid(row=12, column=3, **pad, sticky="we")

        self.frm_web = ttk.LabelFrame(frm, text="Веб-просмотр")
        self.frm_web.grid(row=13, column=0, columnspan=4, sticky="nsew", padx=8, pady=6)
        self.frm_web_inner = ttk.Frame(self.frm_web)
        self.frm_web_inner.pack(fill="both", expand=True, padx=6, pady=6)
        self.lbl_qr = tk.Label(
            self.frm_web_inner,
            text="После успешного старта трансляции (LiveKit)\nздесь появится QR со ссылкой на страницу слушателя.",
            fg="#666",
            justify="center",
            width=36,
            height=5,
        )
        self.lbl_qr.pack(side="left", anchor="nw")
        self.frm_web_right = ttk.Frame(self.frm_web_inner)
        self.frm_web_right.pack(side="left", fill="both", expand=True, padx=(12, 0))
        ttk.Label(
            self.frm_web_right,
            text="Та же ссылка текстом (страница слушателя на helper):",
        ).pack(anchor="w")
        self.entry_web = ttk.Entry(self.frm_web_right, textvariable=self.var_web_viewer, width=72)
        self.entry_web.pack(fill="x", pady=(4, 0))
        try:
            self.entry_web.state(["readonly"])
        except tk.TclError:
            self.entry_web.configure(state="readonly")

        self.frm_errors = ttk.LabelFrame(frm, text="Ошибки и диагностика")
        self.txt_errors = scrolledtext.ScrolledText(self.frm_errors, height=4, wrap="word", font=("Segoe UI", 9))
        self.txt_errors.pack(fill="both", expand=True, padx=6, pady=6)
        self.txt_errors.configure(state="disabled")

        frm.columnconfigure(1, weight=1)
        frm.columnconfigure(2, weight=1)
        frm.columnconfigure(3, weight=1)
        frm.rowconfigure(13, weight=1)

        # Initial populate
        self.on_transport_changed()

        # Periodic UI updater
        self.after(1000, self._tick)

        # Close handler
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _make_qr_photo(self, url: str, size_px: int = 200) -> Optional[tk.PhotoImage]:
        try:
            from PIL import Image, ImageTk
            import qrcode
        except ImportError:
            return None
        try:
            qr = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=4,
                border=2,
            )
            qr.add_data(url)
            qr.make(fit=True)
            pil_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            pil_img = pil_img.resize((size_px, size_px), Image.Resampling.NEAREST)
            return ImageTk.PhotoImage(pil_img)
        except Exception:
            return None

    def _refresh_qr_image(self, qr_url: Optional[str]) -> None:
        url = (qr_url or "").strip()
        if url and not url.endswith("/"):
            url = url + "/"
        if url.startswith(("http://", "https://")):
            photo = self._make_qr_photo(url)
            if photo is not None:
                self._qr_photo_ref = photo
                self.lbl_qr.configure(image=photo, text="", width=0, height=0)
                return
            self._qr_photo_ref = None
            self.lbl_qr.configure(
                image="",
                text="Не удалось построить QR.\nУстановите: pip install qrcode pillow",
                fg="#a00",
                width=40,
                height=8,
            )
            return
        self._qr_photo_ref = None
        if self.var_transport.get() != "LiveKit (native)":
            self.lbl_qr.configure(
                image="",
                text="QR со ссылкой на страницу слушателя\nдоступен только для транспорта LiveKit.",
                fg="#666",
                justify="center",
                width=36,
                height=5,
            )
            return
        self.lbl_qr.configure(
            image="",
            text="После успешного старта трансляции (LiveKit)\nздесь появится QR со ссылкой на страницу слушателя.",
            fg="#666",
            justify="center",
            width=36,
            height=5,
        )

    def _set_web_viewer(self, display: str, qr_url: Optional[str] = None) -> None:
        try:
            self.entry_web.state(["!readonly"])
        except tk.TclError:
            self.entry_web.configure(state="normal")
        self.var_web_viewer.set(display)
        try:
            self.entry_web.state(["readonly"])
        except tk.TclError:
            self.entry_web.configure(state="readonly")
        resolved = (qr_url or "").strip()
        if not resolved and display:
            m = re.search(r"https?://[^\s]+", display)
            if m:
                resolved = m.group(0).strip()
        self._refresh_qr_image(resolved if resolved.startswith(("http://", "https://")) else None)

    def _ensure_audio_drawer_open(self) -> None:
        if self._audio_drawer_open:
            return
        self._audio_drawer_open = True
        self.frm_audio.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.frm_audio_outer.columnconfigure(0, weight=1)
        self.btn_audio_drawer.configure(text="▼ Источник аудио")
        self._update_audio_drawer_summary()

    def _set_error_log(self, text: str) -> None:
        self.txt_errors.configure(state="normal")
        self.txt_errors.delete("1.0", "end")
        if text:
            self.txt_errors.insert("1.0", text)
        self.txt_errors.configure(state="disabled")
        err = (text or "").strip()
        if err:
            self.frm_errors.grid(row=14, column=0, columnspan=4, sticky="ew", padx=8, pady=(0, 6))
        else:
            self.frm_errors.grid_remove()

    def _toggle_audio_drawer(self) -> None:
        self._audio_drawer_open = not self._audio_drawer_open
        if self._audio_drawer_open:
            self.frm_audio.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
            self.frm_audio_outer.columnconfigure(0, weight=1)
            self.btn_audio_drawer.configure(text="▼ Источник аудио")
        else:
            self.frm_audio.grid_remove()
            self.btn_audio_drawer.configure(text="▶ Источник аудио")
        self._update_audio_drawer_summary()

    def _update_audio_drawer_summary(self) -> None:
        if self.var_transport.get() != "LiveKit (native)":
            idx = self.combo_device.current()
            if getattr(self, "pulse_sources", None) and 0 <= idx < len(self.pulse_sources):
                name = self.pulse_sources[idx][1][:70]
                self.lbl_audio_drawer_summary.configure(text=f"PulseAudio · {name}")
            elif getattr(self, "pulse_sources", None):
                self.lbl_audio_drawer_summary.configure(text="PulseAudio · выберите источник (разверните панель)")
            else:
                self.lbl_audio_drawer_summary.configure(text="PulseAudio · нет источников")
            return
        if platform.system().lower() != "windows":
            idx = self.combo_device.current()
            if self.input_devices and 0 <= idx < len(self.input_devices):
                d = self.input_devices[idx]
                self.lbl_audio_drawer_summary.configure(text=f"{d.name[:55]} · {d.backend}")
            elif self.input_devices:
                d = self.input_devices[0]
                self.lbl_audio_drawer_summary.configure(text=f"{d.name[:55]} · {d.backend}")
            else:
                self.lbl_audio_drawer_summary.configure(text="Нет устройств ввода")
            return
        if self.var_win_route.get() == "system":
            if self.loopback_devices:
                li = self.combo_win_loopback.current()
                i = li if li >= 0 else 0
                d = self.loopback_devices[i] if i < len(self.loopback_devices) else self.loopback_devices[0]
                self.lbl_audio_drawer_summary.configure(text=f"Системный звук · {d.name[:50]}")
            else:
                self.lbl_audio_drawer_summary.configure(text="Системный звук · loopback не найден (см. подсказку в панели)")
        else:
            if self.mic_devices:
                mi = self.combo_win_mic.current()
                i = mi if mi >= 0 else 0
                d = self.mic_devices[i] if i < len(self.mic_devices) else self.mic_devices[0]
                self.lbl_audio_drawer_summary.configure(text=f"Микрофон · {d.name[:50]}")
            else:
                self.lbl_audio_drawer_summary.configure(text="Микрофон не найден")

    def _tick(self):
        if self.var_transport.get() == "LiveKit (native)":
            self._render_livekit_state(self.livekit_client.state)
        else:
            self._render_state(self.legacy_client.state)
        self.after(1000, self._tick)

    def on_transport_changed(self):
        is_livekit = self.var_transport.get() == "LiveKit (native)"
        self.entry_pairing_secret.config(state="normal" if is_livekit else "disabled")
        self.spin_bitrate.config(state="disabled" if is_livekit else "normal")
        self.btn_create.config(state="disabled" if is_livekit else "normal")
        self.btn_delete.config(state="disabled" if is_livekit else "normal")
        self.on_refresh_devices()
        self._update_audio_panel_visibility()
        self._update_audio_drawer_summary()
        self._set_web_viewer("", None)

    def _update_audio_panel_visibility(self) -> None:
        lk = self.var_transport.get() == "LiveKit (native)"
        win = platform.system().lower() == "windows"
        self.frm_audio_win.pack_forget()
        self.frm_audio_simple.pack_forget()
        if lk and win:
            self.frm_audio_win.pack(fill="x", padx=4, pady=4)
        else:
            self.frm_audio_simple.pack(fill="x", padx=4, pady=4)

    def _on_win_audio_mode(self) -> None:
        if self.var_win_route.get() == "mic":
            self._ensure_audio_drawer_open()
        if self.var_win_route.get() == "system":
            self.combo_win_loopback.configure(state="readonly")
            self.combo_win_mic.configure(state="disabled")
        else:
            self.combo_win_loopback.configure(state="disabled")
            self.combo_win_mic.configure(state="readonly")
        self._update_audio_drawer_summary()

    def on_refresh_devices(self):
        if self.var_transport.get() == "LiveKit (native)":
            self.mic_devices = list_microphone_devices_only()
            self.loopback_devices, lb_err = list_windows_loopback_devices()
            self.input_devices = list_input_devices()

            if platform.system().lower() == "windows":
                self.combo_win_loopback["values"] = [
                    f"{d.device_id} — {d.name} ({d.backend})" for d in self.loopback_devices
                ]
                self.combo_win_mic["values"] = [
                    f"{d.device_id} — {d.name} [{d.backend}]" for d in self.mic_devices
                ]
                if self.loopback_devices:
                    self.combo_win_loopback.current(0)
                if self.mic_devices:
                    self.combo_win_mic.current(0)
                if lb_err and not self.loopback_devices:
                    self.lbl_loopback_hint.config(text=lb_err)
                elif self.loopback_devices:
                    self.lbl_loopback_hint.config(
                        text="Первый пункт в списке — выход «по умолчанию» в Windows (куда сейчас играет звук). "
                        "Если музыка в наушниках, а выбран loopback монитора (HDMI/NVIDIA) — в эфире будет тишина. "
                        "Пункты без префикса sc_lb: — это микрофоны (режим «Микрофон»)."
                    )
                else:
                    self.lbl_loopback_hint.config(
                        text="Loopback не найден. Проверьте: pip install soundcard. Для музыки выберите выход "
                        "совпадающий с «Параметры звука → вывод»."
                    )
                self._on_win_audio_mode()
            else:
                values = [f"{d.device_id} — {d.name} [{d.backend}]" for d in self.input_devices]
                self.combo_device["values"] = values
                if values:
                    self.combo_device.current(0)
        else:
            self.pulse_sources = list_pulse_sources()
            values = [f"{i} — {l}" for i, l in self.pulse_sources]
            self.combo_device["values"] = values
            if values:
                self.combo_device.current(0)
        self._update_audio_drawer_summary()

    def on_create_vdev(self):
        create_virtual_device_interactive(self)
        # После создания обновить список
        self.on_refresh_devices()

    def on_delete_vdev(self):
        delete_virtual_device_interactive(self)
        # После удаления обновить список
        self.on_refresh_devices()

    def on_start(self):
        raw_server = self.var_server.get().strip()
        if not raw_server:
            messagebox.showerror("Ошибка", "Укажите сервер (IP, hostname или URL).")
            return

        if self.var_transport.get() == "LiveKit (native)":
            pairing_secret = self.var_pairing_secret.get().strip()
            room = self.var_room.get().strip()
            identity = self.var_identity.get().strip()
            if not (pairing_secret and room and identity):
                messagebox.showerror(
                    "Ошибка",
                    "Заполните pairing secret, комнату и identity.\n\n"
                    "Pairing secret должен совпадать с LIVEKIT_PAIRING_SECRET на машине helper "
                    "(файл livekit.env). При старте server.py в консоли выводится блок «параметры для клиента».",
                )
                return
            try:
                server_lk, helper_url = livekit_urls_with_discovery(raw_server)
                self._helper_url_for_viewer = helper_url.rstrip("/")
                token = request_publisher_token(
                    helper_url=helper_url,
                    room=room,
                    identity=identity,
                    pairing_secret=pairing_secret,
                )
                if platform.system().lower() == "windows":
                    if self.var_win_route.get() == "system":
                        if not self.loopback_devices:
                            messagebox.showerror(
                                "Системный звук недоступен",
                                "Список «Запись выхода» пуст — захват музыки/игр через LiveKit невозможен.\n\n"
                                "Установите пакет: pip install soundcard\n"
                                "Перезапустите клиент и нажмите «Обновить источники».\n\n"
                                "В трансляции должны быть строки вида sc_lb:N — это не то же самое, что пункты 0 или 9 "
                                "из списка микрофонов.",
                            )
                            return
                        li = self.combo_win_loopback.current()
                        if li < 0 or li >= len(self.loopback_devices):
                            messagebox.showerror("Ошибка", "Выберите выход для системного звука (sc_lb:…).")
                            return
                        device = self.loopback_devices[li]
                    else:
                        if not self.mic_devices:
                            messagebox.showerror("Ошибка", "Не найдено микрофонов.")
                            return
                        mi = self.combo_win_mic.current()
                        if mi < 0 or mi >= len(self.mic_devices):
                            messagebox.showerror("Ошибка", "Выберите микрофон.")
                            return
                        device = self.mic_devices[mi]
                else:
                    dev_idx = self.combo_device.current()
                    if not self.input_devices:
                        messagebox.showerror("Ошибка", "Не найдено устройств ввода.")
                        return
                    device = (
                        self.input_devices[dev_idx]
                        if (0 <= dev_idx < len(self.input_devices))
                        else self.input_devices[0]
                    )
                self.livekit_client.start(
                    server_url=server_lk,
                    token=token,
                    device_id=device.device_id,
                    channels=int(self.var_channels.get()),
                    sample_rate=int(self.var_rate.get()),
                )
                self.btn_start.config(state="disabled")
                self.btn_stop.config(state="normal")
                base = self._helper_url_for_viewer.rstrip("/")
                viewer_url = f"{base}/"
                self._set_web_viewer(
                    f"{viewer_url}  — в браузере укажите ту же комнату: «{room}»",
                    qr_url=viewer_url,
                )
                self._set_error_log("")
            except Exception as e:
                self._helper_url_for_viewer = ""
                self._set_web_viewer("", None)
                self._set_error_log(str(e))
        else:
            if not FFMPEG_BIN:
                messagebox.showerror("Ошибка", "ffmpeg не найден. Установите пакет ffmpeg.")
                return
            try:
                server = resolve_legacy_websocket_url(raw_server)
                self.legacy_client.start(
                    server_url=server,
                    backend="pulse",
                    device=self.pulse_sources[0][0] if self.pulse_sources else "default",
                    channels=int(self.var_channels.get()),
                    rate=int(self.var_rate.get()),
                    bitrate=int(self.var_bitrate.get()),
                    chunk_size=4096,
                )
                self.btn_start.config(state="disabled")
                self.btn_stop.config(state="normal")
                self._set_web_viewer("— (legacy WebSocket: отдельной веб-страницы helper нет)", None)
                self._set_error_log("")
            except Exception as e:
                self._set_web_viewer("", None)
                self._set_error_log(str(e))

    def on_stop(self):
        try:
            if self.var_transport.get() == "LiveKit (native)":
                self.livekit_client.stop()
            else:
                self.legacy_client.stop()
        except Exception as e:
            messagebox.showwarning("Внимание", f"Не удалось остановить: {e}")
        finally:
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")

    def on_livekit_state_update(self, state: LiveKitState):
        self.after(0, lambda s=state: self._render_livekit_state(s))

    def _render_livekit_state(self, state: LiveKitState):
        if state.running:
            self.lbl_status.config(
                text=f"Статус: {'online' if state.connected else 'подключение...'}",
                foreground=("#0a0" if state.connected else "#b60"),
            )
            self.btn_start.config(state="disabled")
            self.btn_stop.config(state="normal")
        else:
            self.lbl_status.config(text="Статус: offline", foreground="#b00")
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")
        self.lbl_extra.config(
            text=f"LiveKit room: {state.room_name or '-'} | Connected: {'yes' if state.connected else 'no'}"
        )
        self.title("Audio Streamer (LiveKit)")
        self._set_error_log(state.last_error or "")

    def on_state_update(self, state: StreamState):
        self.after(0, lambda s=state: self._render_state(s))

    def _render_state(self, state: StreamState):
        if state.running:
            if state.ack:
                self.lbl_status.config(text="Статус: online (сервер подтвердил стрим)", foreground="#0a0")
            else:
                self.lbl_status.config(text="Статус: подключение/отправка...", foreground="#b60")
            self.btn_start.config(state="disabled")
            self.btn_stop.config(state="normal")
        else:
            self.lbl_status.config(text="Статус: offline", foreground="#b00")
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")

        kib = state.sent_bytes / 1024.0
        self.lbl_extra.config(
            text=f"Слушателей: {state.listeners} | Отправлено: {kib:.1f} KiB | Аптайм: {int(state.uptime_sec)} c"
        )
        self.title("Audio Streamer (LiveKit)")
        self._set_error_log(state.last_error or "")

    def on_close(self):
        try:
            self.livekit_client.stop()
            self.legacy_client.stop()
        except Exception:
            pass
        self.after(300, self.destroy)

if __name__ == "__main__":
    app = App()
    app.mainloop()