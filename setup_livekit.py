#!/usr/bin/env python3
import argparse
import secrets
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / "livekit.env"
ENV_TEMPLATE = ROOT / "livekit.env.example"
LK_CFG = ROOT / "deploy" / "livekit" / "livekit.yaml"
LK_CFG_TEMPLATE = ROOT / "deploy" / "livekit" / "livekit.yaml.example"
TURN_CFG = ROOT / "deploy" / "livekit" / "turnserver.conf"
TURN_CFG_TEMPLATE = ROOT / "deploy" / "livekit" / "turnserver.conf.example"


ASSUME_DEFAULTS = False


def ask(prompt: str, default: str) -> str:
    if ASSUME_DEFAULTS:
        print(f"{prompt} [{default}]: {default}")
        return default
    value = input(f"{prompt} [{default}]: ").strip()
    return value or default


def upsert_keyed_line(text: str, key: str, value: str) -> str:
    lines = text.splitlines()
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}:"):
            lines[i] = f"{key}: {value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}: {value}")
    return "\n".join(lines) + "\n"


def upsert_env_value(text: str, key: str, value: str) -> str:
    lines = text.splitlines()
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def read_env_value(text: str, key: str) -> str:
    for line in text.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def keep_or_generate(env_text: str, key: str, nbytes: int) -> str:
    """Секреты не перегенерируются: иначе уже раздатые клиентам значения перестанут работать."""
    current = read_env_value(env_text, key)
    if current and not current.startswith("replace_with"):
        return current
    return secrets.token_urlsafe(nbytes)


def strip_legacy_livekit_keys(text: str) -> str:
    """Удаляет секцию keys: из livekit.yaml — ключи теперь приходят из livekit.env (LIVEKIT_KEYS)."""
    out, in_keys = [], False
    for line in text.splitlines():
        if line.startswith("keys:"):
            in_keys = True
            continue
        if in_keys and (line.startswith((" ", "\t")) or not line.strip()):
            continue
        in_keys = False
        out.append(line)
    return "\n".join(out) + "\n"


def strip_legacy_turn_secret(text: str) -> tuple[str, str]:
    """Удаляет static-auth-secret из turnserver.conf, возвращая найденное значение для переноса в env."""
    out, found = [], ""
    for line in text.splitlines():
        if line.startswith("static-auth-secret="):
            value = line.split("=", 1)[1].strip()
            if not value.startswith("REPLACE_WITH"):
                found = value
            continue
        out.append(line)
    return "\n".join(out) + "\n", found


def main() -> int:
    global ASSUME_DEFAULTS
    parser = argparse.ArgumentParser(description="Generate livekit.env, livekit.yaml and turnserver.conf")
    parser.add_argument("--public-host", default=None,
                        help="Public IP/domain clients use to reach this server "
                             "(default: keep URLs from livekit.env, else 127.0.0.1)")
    parser.add_argument("-y", "--yes", action="store_true", help="Non-interactive: accept all defaults")
    args = parser.parse_args()
    ASSUME_DEFAULTS = args.yes

    if not ENV_FILE.exists():
        shutil.copyfile(ENV_TEMPLATE, ENV_FILE)
    if not LK_CFG.exists():
        shutil.copyfile(LK_CFG_TEMPLATE, LK_CFG)
    if not TURN_CFG.exists():
        shutil.copyfile(TURN_CFG_TEMPLATE, TURN_CFG)

    env_text = ENV_FILE.read_text(encoding="utf-8")
    # Повторный запуск не должен затирать уже настроенные значения — они становятся умолчаниями.
    existing = {k: read_env_value(env_text, k) for k in
                ("LIVEKIT_API_KEY", "HELPER_HOST", "HELPER_PORT", "LIVEKIT_URL", "HELPER_URL")}
    keep_urls = args.public_host is None

    print("=== LiveKit bootstrap setup ===")
    api_key = ask("LiveKit API key", existing["LIVEKIT_API_KEY"] or "devkey")
    public_host = ask("Public IP/domain of this server", args.public_host or "127.0.0.1")
    helper_host = ask("Helper bind host", existing["HELPER_HOST"] or "0.0.0.0")
    helper_port = ask("Helper port", existing["HELPER_PORT"] or "8000")
    livekit_url = ask("LiveKit URL for clients",
                      (keep_urls and existing["LIVEKIT_URL"]) or f"ws://{public_host}:7880")
    helper_url = ask("Helper URL for clients",
                     (keep_urls and existing["HELPER_URL"]) or f"http://{public_host}:{helper_port}")
    turn_mode = ask("Use external coturn (recommended)? yes/no", "yes").lower().startswith("y")

    lk_text = strip_legacy_livekit_keys(LK_CFG.read_text(encoding="utf-8"))
    lk_text = upsert_keyed_line(lk_text, "  enabled", "false" if turn_mode else "true")
    LK_CFG.write_text(lk_text, encoding="utf-8")

    turn_text, legacy_turn_secret = strip_legacy_turn_secret(TURN_CFG.read_text(encoding="utf-8"))
    TURN_CFG.write_text(turn_text, encoding="utf-8")
    if legacy_turn_secret and not read_env_value(env_text, "TURN_STATIC_AUTH_SECRET"):
        env_text = upsert_env_value(env_text, "TURN_STATIC_AUTH_SECRET", legacy_turn_secret)

    api_secret = keep_or_generate(env_text, "LIVEKIT_API_SECRET", 36)
    pairing_secret = keep_or_generate(env_text, "LIVEKIT_PAIRING_SECRET", 24)
    turn_secret = keep_or_generate(env_text, "TURN_STATIC_AUTH_SECRET", 24)

    env_text = upsert_env_value(env_text, "LIVEKIT_API_KEY", api_key)
    env_text = upsert_env_value(env_text, "LIVEKIT_API_SECRET", api_secret)
    env_text = upsert_env_value(env_text, "LIVEKIT_PAIRING_SECRET", pairing_secret)
    env_text = upsert_env_value(env_text, "TURN_STATIC_AUTH_SECRET", turn_secret)
    env_text = upsert_env_value(env_text, "LIVEKIT_URL", livekit_url)
    env_text = upsert_env_value(env_text, "HELPER_URL", helper_url)
    env_text = upsert_env_value(env_text, "HELPER_HOST", helper_host)
    env_text = upsert_env_value(env_text, "HELPER_PORT", helper_port)
    ENV_FILE.write_text(env_text, encoding="utf-8")

    print("\nGenerated credentials:")
    print(f"LIVEKIT_API_KEY={api_key}")
    print(f"LIVEKIT_API_SECRET={api_secret}")
    print(f"LIVEKIT_PAIRING_SECRET={pairing_secret}")
    print("\nSecrets saved to livekit.env (the only place they are stored).")
    print("deploy/livekit/livekit.yaml and turnserver.conf contain no secrets.")
    print("Now run: ./start_server.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
