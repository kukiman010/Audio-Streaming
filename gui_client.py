#!/usr/bin/env python3
import asyncio
import json
import shutil
import subprocess
import sys
import threading
import time
import os
import re
from dataclasses import dataclass
from typing import List, Tuple, Optional, Callable

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

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

# ---------------- Tkinter GUI ----------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Simple Audio Streamer (MP3 over WebSocket)")
        self.geometry("740x360")
        self.resizable(False, False)

        self.client = StreamClient(self.on_state_update)

        # Списки устройств по бэкендам
        self.backend_values = ["PulseAudio", "ALSA"]
        self.pulse_sources: List[Tuple[str, str]] = []
        self.alsa_devices: List[Tuple[str, str]] = []

        pad = {"padx": 8, "pady": 6}
        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True)

        # Server URL
        ttk.Label(frm, text="Сервер (WebSocket):").grid(row=0, column=0, sticky="w", **pad)
        self.var_server = tk.StringVar(value="ws://127.0.0.1:8000/uplink")
        ttk.Entry(frm, textvariable=self.var_server, width=55).grid(row=0, column=1, sticky="we", **pad, columnspan=3)

        # Backend
        ttk.Label(frm, text="Бэкенд аудио:").grid(row=1, column=0, sticky="w", **pad)
        self.var_backend = tk.StringVar(value="PulseAudio")
        self.combo_backend = ttk.Combobox(frm, state="readonly", width=20, textvariable=self.var_backend,
                                          values=self.backend_values)
        self.combo_backend.grid(row=1, column=1, sticky="w", **pad)
        self.combo_backend.bind("<<ComboboxSelected>>", lambda e: self.on_backend_changed())

        # Device
        ttk.Label(frm, text="Источник аудио:").grid(row=2, column=0, sticky="w", **pad)
        self.var_device = tk.StringVar()
        self.combo_device = ttk.Combobox(frm, state="readonly", width=55)
        self.combo_device.grid(row=2, column=1, sticky="we", **pad, columnspan=3)

        # Create/Delete virtual device (PulseAudio only)
        self.btn_create = ttk.Button(frm, text="Создать виртуальное устройство", command=self.on_create_vdev)
        self.btn_delete = ttk.Button(frm, text="Удалить виртуальное устройство", command=self.on_delete_vdev)
        self.btn_refresh = ttk.Button(frm, text="Обновить источники", command=self.on_refresh_devices)
        self.btn_create.grid(row=3, column=1, sticky="we", **pad)
        self.btn_delete.grid(row=3, column=2, sticky="we", **pad)
        self.btn_refresh.grid(row=3, column=3, sticky="we", **pad)

        # Channels / Rate / Bitrate
        ttk.Label(frm, text="Каналы:").grid(row=4, column=0, sticky="w", **pad)
        self.var_channels = tk.IntVar(value=2)
        ttk.Spinbox(frm, from_=1, to=2, textvariable=self.var_channels, width=5).grid(row=4, column=1, sticky="w", **pad)

        ttk.Label(frm, text="Частота, Гц:").grid(row=5, column=0, sticky="w", **pad)
        self.var_rate = tk.IntVar(value=48000)
        ttk.Spinbox(frm, from_=16000, to=96000, increment=1000, textvariable=self.var_rate, width=9)\
            .grid(row=5, column=1, sticky="w", **pad)

        ttk.Label(frm, text="Битрейт MP3, кбит/с:").grid(row=6, column=0, sticky="w", **pad)
        self.var_bitrate = tk.IntVar(value=128)
        ttk.Spinbox(frm, from_=64, to=320, increment=16, textvariable=self.var_bitrate, width=9)\
            .grid(row=6, column=1, sticky="w", **pad)

        # Status
        self.lbl_status = ttk.Label(frm, text="Статус: offline", foreground="#b00")
        self.lbl_status.grid(row=7, column=0, sticky="w", **pad, columnspan=4)

        self.lbl_extra = ttk.Label(frm, text="Слушателей: 0 | Отправлено: 0.0 KiB | Аптайм: 0 c", foreground="#333")
        self.lbl_extra.grid(row=8, column=0, sticky="w", **pad, columnspan=4)

        # Buttons
        self.btn_start = ttk.Button(frm, text="Старт", command=self.on_start)
        self.btn_stop = ttk.Button(frm, text="Стоп", command=self.on_stop, state="disabled")
        self.btn_start.grid(row=9, column=2, **pad, sticky="we")
        self.btn_stop.grid(row=9, column=3, **pad, sticky="we")

        frm.columnconfigure(1, weight=1)
        frm.columnconfigure(2, weight=1)
        frm.columnconfigure(3, weight=1)

        # Initial populate
        self.on_backend_changed()

        # Periodic UI updater
        self.after(1000, self._tick)

        # Close handler
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _tick(self):
        st = self.client.state
        self._render_state(st)
        self.after(1000, self._tick)

    def on_backend_changed(self):
        backend = self.var_backend.get()
        # В PulseAudio — активируем кнопки создания/удаления; в ALSA — выключаем
        pulse_mode = (backend == "PulseAudio")
        self.btn_create.config(state=("normal" if pulse_mode else "disabled"))
        self.btn_delete.config(state=("normal" if pulse_mode else "disabled"))
        # Обновить список источников
        self.on_refresh_devices()

    def on_refresh_devices(self):
        backend = self.var_backend.get()
        if backend == "PulseAudio":
            self.pulse_sources = list_pulse_sources()
            values = [f"{i} — {l}" for i, l in self.pulse_sources]
            self.combo_device["values"] = values
            if values:
                self.combo_device.current(0)
        else:
            self.alsa_devices = list_alsa_devices()
            values = [f"{i} — {l}" for i, l in self.alsa_devices]
            self.combo_device["values"] = values
            if values:
                self.combo_device.current(0)

    def on_create_vdev(self):
        create_virtual_device_interactive(self)
        # После создания обновить список
        self.on_refresh_devices()

    def on_delete_vdev(self):
        delete_virtual_device_interactive(self)
        # После удаления обновить список
        self.on_refresh_devices()

    def on_start(self):
        if not FFMPEG_BIN:
            messagebox.showerror("Ошибка", "ffmpeg не найден. Установите пакет ffmpeg.")
            return
        server = self.var_server.get().strip()
        if not server:
            messagebox.showerror("Ошибка", "Укажите URL сервера (ws://.../uplink).")
            return

        backend = self.var_backend.get()
        dev_idx = self.combo_device.current()
        device = None
        if backend == "PulseAudio":
            if not self.pulse_sources:
                messagebox.showerror("Ошибка", "Нет источников PulseAudio. Убедитесь, что запущен PulseAudio/PipeWire и установлен pactl.")
                return
            device = self.pulse_sources[dev_idx][0] if (0 <= dev_idx < len(self.pulse_sources)) else self.pulse_sources[0][0]
            backend_key = "pulse"
        else:
            if not self.alsa_devices:
                # fallback
                self.alsa_devices = [("default", "default")]
            device = self.alsa_devices[dev_idx][0] if (0 <= dev_idx < len(self.alsa_devices)) else "default"
            backend_key = "alsa"

        try:
            self.client.start(
                server_url=server,
                backend=backend_key,
                device=device,
                channels=int(self.var_channels.get()),
                rate=int(self.var_rate.get()),
                bitrate=int(self.var_bitrate.get()),
                chunk_size=4096
                # chunk_size=8196
            )
            self.btn_start.config(state="disabled")
            self.btn_stop.config(state="normal")
        except Exception as e:
            messagebox.showerror("Ошибка запуска", str(e))

    def on_stop(self):
        try:
            self.client.stop()
        except Exception as e:
            messagebox.showwarning("Внимание", f"Не удалось остановить: {e}")
        finally:
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")

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
        if state.last_error:
            self.title(f"Simple Audio Streamer — ошибка: {state.last_error}")
        else:
            self.title("Simple Audio Streamer (MP3 over WebSocket)")

    def on_close(self):
        try:
            self.client.stop()
        except Exception:
            pass
        self.after(300, self.destroy)

# **
if __name__ == "__main__":
    app = App()
    app.mainloop()