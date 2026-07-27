"""Lifecycle manager for Telegram's official local Bot API server.

The local server is optional. When its binary and Keychain credentials are
available, Character Swap starts it with ``--local`` and automatically routes
Telegram uploads through it. This raises the upload ceiling to 2000 MB without
ever transcoding the source file.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import time

from character_swap.config import (
    PROJECT_ROOT,
    TELEGRAM_LOCAL_API_HASH_KEYCHAIN_ACCOUNT,
    TELEGRAM_LOCAL_API_ID_KEYCHAIN_ACCOUNT,
    load_keychain_secret,
    settings,
)

logger = logging.getLogger(__name__)

LOCAL_API_BASE = "http://127.0.0.1:8081"
LOCAL_API_PORT = 8081
BINARY_PATH = (
    PROJECT_ROOT / ".local" / "telegram-bot-api" / "bin" / "telegram-bot-api"
)
DATA_DIR = PROJECT_ROOT / ".local" / "telegram-bot-api-data"
LOG_PATH = PROJECT_ROOT / ".local" / "telegram-bot-api.log"

_process: subprocess.Popen | None = None
_log_handle = None


def _port_ready() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", LOCAL_API_PORT), timeout=0.4):
            return True
    except OSError:
        return False


def configured() -> bool:
    """Return whether the binary and both Keychain credentials are present."""
    return (
        BINARY_PATH.is_file()
        and bool(load_keychain_secret(TELEGRAM_LOCAL_API_ID_KEYCHAIN_ACCOUNT))
        and bool(load_keychain_secret(TELEGRAM_LOCAL_API_HASH_KEYCHAIN_ACCOUNT))
    )


def running() -> bool:
    return _port_ready()


def start(timeout: float = 20.0) -> bool:
    """Start or reuse the local server and route Telegram delivery through it."""
    global _process, _log_handle
    if _port_ready():
        settings.telegram_api_base = LOCAL_API_BASE
        return True
    if not BINARY_PATH.is_file():
        logger.info("Telegram local API binary is not installed")
        return False
    api_id = load_keychain_secret(TELEGRAM_LOCAL_API_ID_KEYCHAIN_ACCOUNT)
    api_hash = load_keychain_secret(TELEGRAM_LOCAL_API_HASH_KEYCHAIN_ACCOUNT)
    if not api_id or not api_hash:
        logger.info("Telegram local API credentials are not configured")
        return False

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "temp").mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _log_handle = LOG_PATH.open("ab")
    env = os.environ.copy()
    env["TELEGRAM_API_ID"] = api_id
    env["TELEGRAM_API_HASH"] = api_hash
    _process = subprocess.Popen(
        [
            str(BINARY_PATH),
            "--local",
            f"--http-port={LOCAL_API_PORT}",
            f"--dir={DATA_DIR}",
            f"--temp-dir={DATA_DIR / 'temp'}",
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=_log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_ready():
            settings.telegram_api_base = LOCAL_API_BASE
            logger.info("Telegram local Bot API ready on %s", LOCAL_API_BASE)
            return True
        if _process.poll() is not None:
            break
        time.sleep(0.2)
    code = _process.poll()
    logger.error(
        "Telegram local Bot API failed to start%s; see %s",
        f" (exit {code})" if code is not None else "",
        LOG_PATH,
    )
    return False


def stop() -> None:
    """Stop only the child process launched by this app instance."""
    global _process, _log_handle
    if _process is not None and _process.poll() is None:
        _process.terminate()
        try:
            _process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _process.kill()
            _process.wait(timeout=2)
    _process = None
    if _log_handle is not None:
        _log_handle.close()
        _log_handle = None
