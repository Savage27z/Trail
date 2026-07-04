"""Env loading + validation. Fails loudly at startup with a clear list of missing vars."""

import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[config] WARNING: {name}={raw!r} is not an int, using default {default}")
        return default


@dataclass(frozen=True)
class Config:
    # BTL runtime
    btl_api_key: str
    btl_model: str
    btl_base_url: str
    btl_use_native_tools: bool
    # Data providers
    helius_api_key: str
    birdeye_api_key: str
    # Telegram (only required for bot.py, not cli.py)
    telegram_bot_token: str
    # Agent limits
    max_tool_rounds: int
    max_wall_seconds: int


def load_config(require_telegram: bool = False) -> Config:
    """Load and validate config. Exits with a readable error if anything required is missing."""
    required = {
        "BTL_API_KEY": "BTL runtime API key",
        "BTL_MODEL": "BTL model name (from their docs)",
        "HELIUS_API_KEY": "Helius API key (https://helius.dev)",
        "BIRDEYE_API_KEY": "Birdeye API key (https://birdeye.so)",
    }
    if require_telegram:
        required["TELEGRAM_BOT_TOKEN"] = "Telegram bot token (from @BotFather)"

    missing = []
    for var, hint in required.items():
        if not os.getenv(var, "").strip():
            missing.append(f"  - {var}  ({hint})")

    if missing:
        print("=" * 60)
        print("Trail cannot start — missing environment variables:")
        print("\n".join(missing))
        print("\nCopy .env.example to .env and fill in the values.")
        print("=" * 60)
        sys.exit(1)

    return Config(
        btl_api_key=os.environ["BTL_API_KEY"].strip(),
        btl_model=os.environ["BTL_MODEL"].strip(),
        btl_base_url=os.getenv("BTL_BASE_URL", "https://api.badtheorylabs.com/v1").strip(),
        btl_use_native_tools=_env_bool("BTL_USE_NATIVE_TOOLS", True),
        helius_api_key=os.environ["HELIUS_API_KEY"].strip(),
        birdeye_api_key=os.environ["BIRDEYE_API_KEY"].strip(),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        max_tool_rounds=_env_int("TRAIL_MAX_ROUNDS", 8),
        max_wall_seconds=_env_int("TRAIL_MAX_SECONDS", 90),
    )


def setup_logging() -> None:
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx logs every request at INFO; keep our own logs readable
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
