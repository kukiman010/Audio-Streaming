#!/usr/bin/env python3
import asyncio
import json
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Callable

import tkinter as tk
from tkinter import ttk, messagebox

try:
    from aiohttp import ClientSession, WSMsgType, ClientConnectorError, WSServerHandshakeError
except Exception as e:
    print("Не установлен aiohttp. Установите: pip install aiohttp", file=sys.stderr)
    raise

FFMPEG_BIN = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"

# ---------------- ALSA device discovery ----------------

def list_alsa_devices() -> List[Tuple[str, str]]:
    """
    Возвращает список устройств ALSA в виде [(id, label)], где id можно передать в ffmpeg.
    Требуется пакет alsa-utils (arecord).
    """
    result: List[Tuple[str, str]] = [("default", "default (Алса по умолчанию)")]
    arecord = shutil.which("arecord") or "/usr/bin/arecord"
    try:
        # arecord -l выводит список карт/устройств
        out = subprocess.check_output([arecord, "-l"], stderr=subprocess.STDOUT, text=True, errors="replace")
        # Пример строк:
        # card 1: USB [USB PnP Audio Device], device 0: USB Audio [USB Audio]
        # card 2: PCH [HDA Intel PCH], device 0: ALC245 Analog [ALC245 Analog]
        lines = out.splitlines()
        for line in lines:
            line = line.strip()
            if line.startswith("card ") and ", device " in line:
                # card 1: USB [...] , device 0: ...
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
        # Если arecord недоступен — оставим только default
        pass
    # Удалим дубликаты, сохраним порядок
    seen = set()
    uniq: List[Tuple[str, str]] = []
    for i, l in result:
        if i not in seen:
            uniq.append((i, l))
            seen.add(i)
    return uniq

# ---------------- ffmpeg command ----------------

def build_ffmpeg_cmd(alsa_device: str, channels: int, rate: int, bitrate_kbps: int):
    if not FFMPEG_BIN:
        raise RuntimeError("ffmpeg не найден. Установите пакет ffmpeg.")
    return [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel", "error",
        "-f", "alsa",
        "-i", alsa_device,
        "-ac", str(channels),
        "-ar", str(rate),
                "-codec:a", "libmp3lame",
        "-b:a", f"{bitrate_kbps}k",
        "-f", "mp3",
        "-"  # stdout
    ]

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
    def start(self, server_url: str, alsa: str, channels: int, rate: int, bitrate: int, chunk_size: int):
        if self.state.running:
            return
        self.state = StreamState(running=True)
        self.ui_callback(self.state)
        self.run_coro(self._start_async(server_url, alsa, channels, rate, bitrate, chunk_size))

    def stop(self):
        if not self.state.running:
            return
        self.run_coro(self._stop_async())

    # ---- internal async logic ----
    async def _start_async(self, server_url: str, alsa: str, channels: int, rate: int, bitrate: int, chunk_size: int):
        self.stop_event = asyncio.Event()
        self.state = StreamState(running=True)
        self.ui_callback(self.state)

        # Start ffmpeg
        try:
            ff_cmd = build_ffmpeg_cmd(alsa, channels, rate, bitrate)
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
                    # ffmpeg мог завершиться
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
                    # Пытаемся распарсить JSON
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
        # Cancel tasks
        for t in (self.send_task, self.recv_task):
            if t and not t.done():
                t.cancel()
                with contextlib_suppress():
                    await t
        # Close WS / session / ffmpeg
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
        self.geometry("600x280")
        self.resizable(False, False)

        self.client = StreamClient(self.on_state_update)

        # Controls
        pad = {"padx": 8, "pady": 6}

        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True)

        # Server URL
        ttk.Label(frm, text="Сервер (WebSocket):").grid(row=0, column=0, sticky="w", **pad)
        self.var_server = tk.StringVar(value="ws://127.0.0.1:8000/uplink")
        ttk.Entry(frm, textvariable=self.var_server, width=45).grid(row=0, column=1, sticky="we", **pad, columnspan=2)

        # ALSA device
        ttk.Label(frm, text="Источник аудио (ALSA):").grid(row=1, column=0, sticky="w", **pad)
        self.devices = list_alsa_devices()
        self.var_device = tk.StringVar(value=(self.devices[0][0] if self.devices else "default"))
        self.combo_dev = ttk.Combobox(frm, state="readonly", width=45,
                                      values=[f"{i} — {l}" for i, l in self.devices])
        if self.devices:
            self.combo_dev.current(0)
        self.combo_dev.grid(row=1, column=1, sticky="we", **pad, columnspan=2)

        # Channels / Rate / Bitrate
        ttk.Label(frm, text="Каналы:").grid(row=2, column=0, sticky="w", **pad)
        self.var_channels = tk.IntVar(value=2)
        ttk.Spinbox(frm, from_=1, to=2, textvariable=self.var_channels, width=5).grid(row=2, column=1, sticky="w", **pad)

        ttk.Label(frm, text="Частота, Гц:").grid(row=3, column=0, sticky="w", **pad)
        self.var_rate = tk.IntVar(value=48000)
        ttk.Spinbox(frm, from_=16000, to=96000, increment=1000, textvariable=self.var_rate, width=9)\
            .grid(row=3, column=1, sticky="w", **pad)

        ttk.Label(frm, text="Битрейт MP3, кбит/с:").grid(row=4, column=0, sticky="w", **pad)
        self.var_bitrate = tk.IntVar(value=128)
        ttk.Spinbox(frm, from_=64, to=256, increment=16, textvariable=self.var_bitrate, width=9)\
            .grid(row=4, column=1, sticky="w", **pad)

        # Status
        self.lbl_status = ttk.Label(frm, text="Статус: offline", foreground="#b00")
        self.lbl_status.grid(row=5, column=0, sticky="w", **pad, columnspan=3)

        self.lbl_extra = ttk.Label(frm, text="Слушателей: 0 | Отправлено: 0.0 KiB | Аптайм: 0 c", foreground="#333")
        self.lbl_extra.grid(row=6, column=0, sticky="w", **pad, columnspan=3)

        # Buttons
        self.btn_start = ttk.Button(frm, text="Старт", command=self.on_start)
        self.btn_stop = ttk.Button(frm, text="Стоп", command=self.on_stop, state="disabled")
        self.btn_refresh = ttk.Button(frm, text="Обновить устройства", command=self.on_refresh_devices)
        self.btn_start.grid(row=7, column=0, **pad, sticky="we")
        self.btn_stop.grid(row=7, column=1, **pad, sticky="we")
        self.btn_refresh.grid(row=7, column=2, **pad, sticky="we")

        frm.columnconfigure(1, weight=1)
        frm.columnconfigure(2, weight=1)

        # Periodic UI updater for sent bytes
        self.after(1000, self._tick)

        # Close handler
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _tick(self):
        # Периодически обновляем строку с байтами (увеличивается в send loop)
        st = self.client.state
        self._render_state(st)
        self.after(1000, self._tick)

    def on_refresh_devices(self):
        self.devices = list_alsa_devices()
        self.combo_dev["values"] = [f"{i} — {l}" for i, l in self.devices]
        if self.devices:
            self.combo_dev.current(0)

    def on_start(self):
        if not FFMPEG_BIN:
            messagebox.showerror("Ошибка", "ffmpeg не найден. Установите пакет ffmpeg.")
            return
        server = self.var_server.get().strip()
        if not server:
            messagebox.showerror("Ошибка", "Укажите URL сервера (ws://.../uplink).")
            return
        dev_idx = self.combo_dev.current()
        alsa = self.devices[dev_idx][0] if (0 <= dev_idx < len(self.devices)) else "default"
        try:
            self.client.start(
                server_url=server,
                alsa=alsa,
                channels=int(self.var_channels.get()),
                rate=int(self.var_rate.get()),
                bitrate=int(self.var_bitrate.get()),
                chunk_size=4096
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
        # Вызывается из фонового потока — пересылаем в UI-поток
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
            # Показываем в заголовке кратко, чтобы не спамить messagebox
            self.title(f"Simple Audio Streamer — ошибка: {state.last_error}")
        else:
            self.title("Simple Audio Streamer (MP3 over WebSocket)")

    def on_close(self):
        try:
            self.client.stop()
        except Exception:
            pass
        # Дадим корутинам шанс завершиться
        self.after(300, self.destroy)

# **
if __name__ == "__main__":
    app = App()
    app.mainloop()
