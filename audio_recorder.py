import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import subprocess
import psutil
import os
import re

PREFIX = "MYAPP_"  # уникальный префикс для виртуальных устройств вашей программы

def get_sources():
    try:
        out = subprocess.check_output(['pactl', 'list', 'short', 'sources'], encoding='utf-8')
    except Exception as e:
        print("Ошибка получения источников:", e)
        return []
    sources = []
    for line in out.strip().split('\n'):
        cols = line.split('\t')
        if len(cols) >= 2:
            sources.append(cols[1])
    return sources

def get_null_sinks():
    try:
        out = subprocess.check_output(['pactl', 'list', 'short', 'sinks'], encoding='utf-8')
    except Exception as e:
        return []
    pattern = re.compile(f'^{PREFIX}')
    sinks = []
    for line in out.strip().split('\n'):
        cols = line.split('\t')
        if len(cols) >= 2:
            name = cols[1]
            if pattern.match(name):
                sinks.append(name)
    return sinks

def get_unique_filename(base_name="recording", ext="mp3"):
    filename = f"{base_name}.{ext}"
    idx = 1
    while os.path.exists(filename):
        filename = f"{base_name}_{idx}.{ext}"
        idx += 1
    return filename

class RecorderApp:
    def __init__(self, root):
        self.root = root
        root.title("Аудио-рекордер с MP3 и виртуальными устройствами")
        self.source_var = tk.StringVar()
        self.filename_var = tk.StringVar(value="recording")
        tk.Label(root, text="Источник аудио:").pack()
        self.sources = get_sources()
        self.src_box = ttk.Combobox(root, textvariable=self.source_var, values=self.sources, width=40)
        self.src_box.pack()
        if self.sources:
            self.src_box.current(0)
        tk.Label(root, text="Имя файла (без расширения):").pack()
        self.entry_filename = tk.Entry(root, textvariable=self.filename_var)
        self.entry_filename.pack()
        self.btn_rec = tk.Button(root, text="Запись", command=self.start_recording)
        self.btn_stop = tk.Button(root, text="Стоп", command=self.stop_recording, state=tk.DISABLED)
        self.btn_rec.pack(fill='x')
        self.btn_stop.pack(fill='x')

        # Новые кнопки
        frame = tk.Frame(root)
        frame.pack(fill="x", pady=5)
        tk.Button(frame, text="Создать виртуальное устройство", command=self.create_virtual_device).pack(side=tk.LEFT, expand=True, fill="x", padx=2)
        tk.Button(frame, text="Удалить виртуальное устройство", command=self.delete_virtual_device).pack(side=tk.LEFT, expand=True, fill="x", padx=2)
        tk.Button(frame, text="Обновить список источников", command=self.refresh_sources).pack(side=tk.LEFT, expand=True, fill="x", padx=2)

        self.proc = None
        self.recording_filename = None

    def refresh_sources(self):
        self.sources = get_sources()
        self.src_box['values'] = self.sources
        if self.sources:
            self.src_box.current(0)
        else:
            self.source_var.set("")

    def create_virtual_device(self):
        # Предлагаем уникальное имя пользователю (можно автогенерировать или спросить)
        vdev_base = PREFIX + "VIRTUAL_SPEAKER"
        existings = get_null_sinks()
        unique_name = vdev_base
        idx = 1
        while unique_name in existings:
            unique_name = f"{vdev_base}{idx}"
            idx += 1
        desc = simpledialog.askstring("Имя устройства", f"Введите название устройства:\n(По умолчанию: {unique_name})", initialvalue=unique_name)
        if not desc:
            return
        if not desc.startswith(PREFIX):
            desc = PREFIX + desc  # гарантируем префикс
        if desc in get_null_sinks():
            messagebox.showerror("Ошибка", "Устройство с таким именем уже существует!")
            return
        try:
            subprocess.check_call([
                "pactl", "load-module", "module-null-sink",
                f"sink_name={desc}",
                f"sink_properties=device.description={desc}_Device"
            ])
            self.refresh_sources()
            messagebox.showinfo("Успех", f"Создано виртуальное устройство: {desc}")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Ошибка создания: {e}")

    def delete_virtual_device(self):
        sinks = get_null_sinks()
        if not sinks:
            messagebox.showinfo("Нет устройств", f"Нет виртуальных устройств с префиксом {PREFIX}")
            return
        sink = simpledialog.askstring("Удаление устройства", f"Выберите для удаления:\n{chr(10).join(sinks)}", initialvalue=sinks[0])
        if not sink or not sink.startswith(PREFIX):
            messagebox.showwarning("Внимание", "Можно удалять только свои (с префиксом) устройства!")
            return
        # Найти индекс модуля по pactl list short modules
        try:
            out = subprocess.check_output(["pactl", "list", "short", "modules"], encoding='utf-8')
            module_id = None
            for line in out.splitlines():
                if f"sink_name={sink}" in line:
                    module_id = line.split('\t')[0].strip()
                    break
            if not module_id:
                messagebox.showerror("Ошибка", "Не нашли модуль для удаления!")
                return
            subprocess.check_call(["pactl", "unload-module", module_id])
            self.refresh_sources()
            messagebox.showinfo("Удалено", f"Виртуальное устройство {sink} удалено")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Ошибка удаления: {e}")

    def start_recording(self):
        source = self.source_var.get()
        if not source:
            messagebox.showerror("Ошибка", "Не выбран источник")
            return
        base_name = self.filename_var.get().strip() or "recording"
        self.recording_filename = get_unique_filename(base_name, ext="mp3")
        self.proc = subprocess.Popen([
            'ffmpeg',
            '-y',  # overwrite
            '-f', 'pulse', '-i', source,
            '-vn', '-acodec', 'libmp3lame', '-ar', '44100', '-ac', '2', self.recording_filename
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.btn_rec.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.entry_filename.config(state=tk.DISABLED)
        self.src_box.config(state=tk.DISABLED)

    def stop_recording(self):
        if self.proc and psutil.pid_exists(self.proc.pid):
            self.btn_stop.config(state=tk.DISABLED)
            self.proc.terminate()
            self.root.after(100, self.check_process_ended)
        else:
            self.on_recording_finished()
    def check_process_ended(self):
        if self.proc.poll() is not None:
            self.proc = None
            self.on_recording_finished()
        else:
            self.root.after(100, self.check_process_ended)
    def on_recording_finished(self):
        self.btn_rec.config(state=tk.NORMAL)
        self.entry_filename.config(state=tk.NORMAL)
        self.src_box.config(state=tk.NORMAL)
        messagebox.showinfo("Готово", f"Запись сохранена как {self.recording_filename}")

if __name__ == "__main__":
    root = tk.Tk()
    app = RecorderApp(root)
    root.mainloop()
