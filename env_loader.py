import os
from typing import Iterable


def _parse_env_line(line: str):
    raw = line.strip()
    if not raw or raw.startswith("#") or "=" not in raw:
        return None, None
    key, value = raw.split("=", 1)
    key = key.strip()
    value = value.strip().strip('"').strip("'")
    if not key:
        return None, None
    return key, value


def load_env_files(paths: Iterable[str], *, override_existing: bool = False) -> None:
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    key, value = _parse_env_line(line)
                    if key and (override_existing or key not in os.environ):
                        os.environ[key] = value
        except OSError:
            continue
