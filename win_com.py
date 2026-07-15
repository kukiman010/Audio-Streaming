"""
Инициализация COM на текущем потоке (Windows).

soundcard импортирует mediafoundation один раз и вызывает CoInitializeEx только в потоке
первого импорта. В других потоках (asyncio LiveKit, поток захвата) COM нужно поднять
вручную, иначе HRESULT 0x800401F0 (CO_E_NOT_INITIALIZED).

Нельзя вызывать CoInitialize до первого import soundcard на этом же потоке, если модуль
ещё не загружен: иначе при импорте soundcard получит S_FALSE и упадёт в check_error
с ``Error 0x100000001`` (баг библиотеки: S_FALSE не обрабатывается).

Используйте ``import_soundcard()`` — он импортирует пакет безопасно, затем поднимает COM.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from typing import Any

_COINIT_MULTITHREADED = 0
_S_FALSE_HEX = "0x100000001"


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


def _is_s_false_runtime_error(exc: BaseException) -> bool:
    return isinstance(exc, RuntimeError) and _S_FALSE_HEX in str(exc)


def _patch_check_error_accept_s_false(mf: Any) -> None:
    """S_OK и S_FALSE не считаем ошибкой (soundcard иначе кидает Error 0x100000001)."""
    original = mf._COMLibrary.check_error

    def check_error(hresult: int) -> None:
        if hresult in (0, 1):
            return
        original(hresult)

    mf._COMLibrary.check_error = staticmethod(check_error)


def _mediafoundation_source_with_s_false_fix(source: str) -> str:
    """Патч исходников soundcard: S_FALSE при CoInitializeEx — не ошибка."""
    source = source.replace(
        "if hresult == S_OK:\n            return",
        "if hresult in (S_OK, 1):  # S_OK, S_FALSE\n            return",
        1,
    )
    old_init = """        try:
            self.check_error(hr)

            # Flag to keep track if this class directly initialized
            # COM, required for un-initializing in destructor:
            self.com_loaded = True

        except RuntimeError as e:
            # Error 0x80010106
            RPC_E_CHANGED_MODE = 0x80010106
            if hr + 2 ** 32 == RPC_E_CHANGED_MODE:
                # COM was already initialized before (e.g. by the
                # debugger), therefore trying to initialize it again
                # fails we can safely ignore this error, but we have
                # to make sure that we don't try to unload it
                # afterwards therefore we set this flag to False:
                self.com_loaded = False
            else:
                raise e"""
    new_init = """        # S_FALSE (1): COM already initialized with the same mode on this thread
        if hr in (0, 1):
            self.com_loaded = hr == 0
        else:
            try:
                self.check_error(hr)
                self.com_loaded = True
            except RuntimeError as e:
                RPC_E_CHANGED_MODE = 0x80010106
                if hr + 2 ** 32 == RPC_E_CHANGED_MODE:
                    self.com_loaded = False
                else:
                    raise e"""
    if old_init not in source:
        raise RuntimeError(
            "Не удалось пропатчить soundcard.mediafoundation (несовместимая версия). "
            "Обновите пакет soundcard или сообщите об ошибке."
        )
    return source.replace(old_init, new_init, 1)


def _import_soundcard_patched_for_s_false() -> Any:
    """Загрузка soundcard, если обычный import упал из‑за S_FALSE (COM уже поднят)."""
    for key in list(sys.modules):
        if key == "soundcard" or key.startswith("soundcard."):
            del sys.modules[key]

    pkg_spec = importlib.machinery.PathFinder.find_spec("soundcard", sys.path)
    if pkg_spec is None or not pkg_spec.submodule_search_locations:
        raise ImportError("Пакет soundcard не найден в sys.path")

    pkg_path = list(pkg_spec.submodule_search_locations)[0]
    mf_path = __import__("pathlib").Path(pkg_path) / "mediafoundation.py"
    if not mf_path.is_file():
        raise ImportError(f"Не найден {mf_path}")

    pkg = importlib.util.module_from_spec(pkg_spec)
    pkg.__file__ = pkg_spec.origin
    pkg.__path__ = list(pkg_spec.submodule_search_locations)
    pkg.__package__ = "soundcard"
    sys.modules["soundcard"] = pkg

    source = _mediafoundation_source_with_s_false_fix(
        mf_path.read_text(encoding="utf-8")
    )
    mf = importlib.util.module_from_spec(
        importlib.util.spec_from_file_location("soundcard.mediafoundation", mf_path)
    )
    sys.modules["soundcard.mediafoundation"] = mf
    exec(compile(source, str(mf_path), "exec"), mf.__dict__)
    exec("from soundcard.mediafoundation import *", pkg.__dict__)
    return pkg


def import_soundcard() -> Any:
    """
    Безопасный import soundcard + COM на текущем потоке.

    Обходит баг soundcard: RuntimeError Error 0x100000001 (HRESULT S_FALSE),
    если COM уже инициализирован (PortAudio, LiveKit, наш ensure_com и т.д.).
    """
    if sys.platform != "win32":
        import soundcard as sc  # type: ignore[import-untyped]

        return sc

    try:
        import soundcard as sc  # type: ignore[import-untyped]
    except RuntimeError as e:
        if not _is_s_false_runtime_error(e):
            raise
        sc = _import_soundcard_patched_for_s_false()
    else:
        try:
            from soundcard import mediafoundation as mf  # type: ignore[attr-defined]

            _patch_check_error_accept_s_false(mf)
        except Exception:
            pass

    ensure_com_initialized()
    return sc


def format_soundcard_error(exc: BaseException) -> str:
    """Человекочитаемое описание типичных ошибок soundcard/WASAPI."""
    msg = str(exc).strip() or type(exc).__name__
    if _S_FALSE_HEX in msg or _is_s_false_runtime_error(exc):
        return (
            f"{msg} (COM уже инициализирован на потоке; S_FALSE). "
            "Перезапустите клиент; если повторится — обновите soundcard."
        )
    return msg
