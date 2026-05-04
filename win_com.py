"""
Инициализация COM на текущем потоке (Windows).

soundcard импортирует mediafoundation один раз и вызывает CoInitializeEx только в потоке
первого импорта. В других потоках (asyncio LiveKit, поток захвата) COM нужно поднять
вручную, иначе HRESULT 0x800401F0 (CO_E_NOT_INITIALIZED).

Нельзя вызывать CoInitialize до первого import soundcard на этом же потоке, если модуль
ещё не загружен: иначе при импорте soundcard получит S_FALSE и упадёт в check_error.
Порядок на новом потоке: сначала ``import soundcard``, затем ``ensure_com_initialized``.
"""

from __future__ import annotations

import sys

_COINIT_MULTITHREADED = 0


def _hr_unsigned(hr: int) -> int:
    return hr & 0xFFFFFFFF


def ensure_com_initialized() -> None:
    """CoInitializeEx(MTA) на текущем потоке; без парного CoUninitialize (жизнь процесса)."""
    if sys.platform != "win32":
        return
    import ctypes

    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    ole32.CoInitializeEx.restype = ctypes.HRESULT

    hr = int(ole32.CoInitializeEx(None, _COINIT_MULTITHREADED))
    u = _hr_unsigned(hr)
    # S_OK, S_FALSE (уже инициализировано в этом потоке), RPC_E_CHANGED_MODE
    if u in (0, 1, 0x80010106):
        return
    raise OSError(f"CoInitializeEx не удался: 0x{u:08x}")
