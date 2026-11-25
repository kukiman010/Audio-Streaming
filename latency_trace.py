"""Протокол и расчёты трассировки задержки (см. docs/superpowers/specs/2026-11-25-latency-trace-design.md)."""
import json
from dataclasses import dataclass
from typing import Dict, List, Optional

TOPIC_PING = "lat.ping"
TOPIC_PONG = "lat.pong"
MAX_LOG_LINES = 200
PROTOCOL_VERSION = 1


@dataclass
class Pong:
    id: int
    sfu_rtt_ms: Optional[float]
    jitter_buffer_ms: Optional[float]


@dataclass
class Stages:
    pub_rtt_ms: Optional[float]
    loop_ms: float
    sfu_client_ms: Optional[float]
    buffer_ms: Optional[float]
    total_ms: Optional[float]


def encode_ping(ping_id: int, t_ms: float, pub_rtt_ms: Optional[float]) -> bytes:
    return json.dumps(
        {"v": PROTOCOL_VERSION, "id": ping_id, "t": t_ms, "pub_rtt_ms": pub_rtt_ms}
    ).encode("utf-8")


def _opt_float(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def decode_pong(data: bytes) -> Optional[Pong]:
    """Любой мусор, чужая версия или не-число в id дают None."""
    try:
        d = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None
    if not isinstance(d, dict) or d.get("v") != PROTOCOL_VERSION:
        return None
    pid = d.get("id")
    if isinstance(pid, bool) or not isinstance(pid, int):
        return None
    return Pong(pid, _opt_float(d.get("sfu_rtt_ms")), _opt_float(d.get("jitter_buffer_ms")))


def estimate_ms(pub_rtt_ms: float, sfu_rtt_ms: float, buffer_ms: float) -> float:
    return (pub_rtt_ms + sfu_rtt_ms) / 2 + buffer_ms


def compute_stages(
    pub_rtt_ms: Optional[float], loop_ms: float, buffer_ms: Optional[float]
) -> Stages:
    sfu_client = None if pub_rtt_ms is None else max(0.0, loop_ms - pub_rtt_ms)
    total = None
    if pub_rtt_ms is not None and buffer_ms is not None:
        total = estimate_ms(pub_rtt_ms, sfu_client, buffer_ms)
    return Stages(pub_rtt_ms, loop_ms, sfu_client, buffer_ms, total)


def _ms(v: Optional[float]) -> str:
    return "н/д" if v is None else f"{v:.0f} мс"


def format_line(identity: str, stages: Optional[Stages]) -> str:
    if stages is None:
        return f"[замер] {identity}: нет ответа (обновите клиент)"
    total = "н/д" if stages.total_ms is None else f"{stages.total_ms / 1000:.1f}".replace(".", ",") + " с"
    return (
        f"[замер] {identity}: до сервера {_ms(stages.pub_rtt_ms)} | "
        f"сервер↔клиент {_ms(stages.sfu_client_ms)} | "
        f"буфер клиента {_ms(stages.buffer_ms)} | ≈ {total}"
    )


class TraceStats:
    """Накопление min/avg/max по слушателям для итоговой сводки."""

    _FIELDS = (
        ("итого", "total_ms"),
        ("до сервера", "pub_rtt_ms"),
        ("сервер↔клиент", "sfu_client_ms"),
        ("буфер клиента", "buffer_ms"),
    )

    def __init__(self) -> None:
        self._by_id: Dict[str, List[Stages]] = {}

    def add(self, identity: str, stages: Stages) -> None:
        self._by_id.setdefault(identity, []).append(stages)

    def summary_lines(self) -> List[str]:
        if not self._by_id:
            return ["[замер] сводка: ответов от слушателей не было"]
        out = []
        for identity, items in self._by_id.items():
            parts = []
            for label, attr in self._FIELDS:
                vals = [getattr(s, attr) for s in items if getattr(s, attr) is not None]
                if vals:
                    parts.append(
                        f"{label} min {min(vals):.0f} / avg {sum(vals) / len(vals):.0f} / max {max(vals):.0f} мс"
                    )
            out.append(f"[замер] сводка {identity} ({len(items)} замеров): " + "; ".join(parts))
        return out


def compose_log(error_text: str, trace_lines: List[str]) -> str:
    lines = ([error_text] if error_text else []) + list(trace_lines[-MAX_LOG_LINES:])
    return "\n".join(lines)
