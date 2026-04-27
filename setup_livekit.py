#!/usr/bin/env python3
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


def ask(prompt: str, default: str) -> str:
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


def main() -> int:
    if not ENV_FILE.exists():
        shutil.copyfile(ENV_TEMPLATE, ENV_FILE)
    if not LK_CFG.exists():
        shutil.copyfile(LK_CFG_TEMPLATE, LK_CFG)
    if not TURN_CFG.exists():
        shutil.copyfile(TURN_CFG_TEMPLATE, TURN_CFG)

    print("=== LiveKit bootstrap setup ===")
    api_key = ask("LiveKit API key", "devkey")
    helper_host = ask("Helper host", "0.0.0.0")
    helper_port = ask("Helper port", "8000")
    livekit_url = ask("LiveKit URL for GUI", "ws://127.0.0.1:7880")
    turn_mode = ask("Use external coturn (recommended)? yes/no", "yes").lower().startswith("y")

    api_secret = secrets.token_urlsafe(36)
    pairing_secret = secrets.token_urlsafe(24)

    env_text = ENV_FILE.read_text(encoding="utf-8")
    env_text = upsert_env_value(env_text, "LIVEKIT_API_KEY", api_key)
    env_text = upsert_env_value(env_text, "LIVEKIT_API_SECRET", api_secret)
    env_text = upsert_env_value(env_text, "LIVEKIT_PAIRING_SECRET", pairing_secret)
    env_text = upsert_env_value(env_text, "LIVEKIT_URL", livekit_url)
    env_text = upsert_env_value(env_text, "HELPER_HOST", helper_host)
    env_text = upsert_env_value(env_text, "HELPER_PORT", helper_port)
    ENV_FILE.write_text(env_text, encoding="utf-8")

    lk_text = LK_CFG.read_text(encoding="utf-8")
    lk_text = lk_text.replace("REPLACE_WITH_LONG_SECRET", api_secret)
    lk_text = upsert_keyed_line(lk_text, "  enabled", "false" if turn_mode else "true")
    if f"{api_key}: " not in lk_text:
        lk_text = lk_text.replace("keys:\n  devkey: ", f"keys:\n  {api_key}: ")
    LK_CFG.write_text(lk_text, encoding="utf-8")

    print("\nGenerated credentials:")
    print(f"LIVEKIT_API_KEY={api_key}")
    print(f"LIVEKIT_API_SECRET={api_secret}")
    print(f"LIVEKIT_PAIRING_SECRET={pairing_secret}")
    print("\nSaved to livekit.env and deploy/livekit/livekit.yaml")
    print("Now run: ./start_server.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
