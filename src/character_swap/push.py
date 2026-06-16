"""Best-effort phone push notifications via ntfy (https://ntfy.sh).

Opt-in: set ``NTFY_TOPIC`` in the environment / ``.env``. When it is empty,
every call here is a silent no-op, so nothing changes for installs that have
not configured it.

The server (running on Hugo's Mac) POSTs a short message to
``<NTFY_SERVER>/<NTFY_TOPIC>`` at milestone events — approval gates and
finished jobs — so his phone gets pinged even when no browser is open. He
subscribes to the same topic in the free ntfy app on the phone.

Design constraints:
- **Never raises.** A down/slow push service must not break or stall a render.
- **Never blocks.** ``notify()`` hands the HTTP POST to a tiny background
  thread pool and returns immediately, so it is safe to call from both sync
  code and inside the asyncio event loop without stalling either.
- **ASCII headers.** HTTP header values must be latin-1-safe and ntfy renders
  the ``Title`` header as ASCII, so the title is transliterated (å→a, ä→a,
  ö→o, …) and any remaining non-ASCII is dropped. The message BODY is sent as
  UTF-8 and may contain Swedish characters / emoji freely. Emoji in the
  notification chrome is done the ntfy way: via ``Tags`` (emoji short codes).
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import httpx

from character_swap.config import settings

_log = logging.getLogger(__name__)

# Small dedicated pool — pushes are rare and tiny; 2 workers is plenty and
# keeps a burst of milestones (e.g. several characters compiling) from piling
# up unbounded.
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ntfy")

# Transliterate the characters that actually show up in our Swedish titles so
# the ASCII Title header stays readable instead of being silently stripped.
_TRANSLIT = str.maketrans({
    "å": "a", "ä": "a", "ö": "o", "Å": "A", "Ä": "A", "Ö": "O",
    "é": "e", "è": "e", "ü": "u", "–": "-", "—": "-", "→": "->",
})


def enabled() -> bool:
    """True iff a push topic is configured."""
    return bool((settings.ntfy_topic or "").strip())


def _ascii_header(s: str) -> str:
    """Make a string safe for an HTTP/ntfy header value (ASCII only)."""
    return s.translate(_TRANSLIT).encode("ascii", "ignore").decode("ascii").strip()


def build_request(
    title: str,
    body: str = "",
    *,
    priority: int = 3,
    tags: list[str] | None = None,
    click: str | None = None,
) -> tuple[str, dict[str, str], bytes] | None:
    """Build the (url, headers, body_bytes) for a push, or None if disabled.

    Pure + side-effect-free so it can be unit-tested without any network.
    ``priority`` follows ntfy's 1 (min) .. 5 (max) scale.
    """
    topic = (settings.ntfy_topic or "").strip()
    if not topic:
        return None
    base = (settings.ntfy_server or "https://ntfy.sh").strip().rstrip("/")
    url = f"{base}/{topic}"
    headers: dict[str, str] = {
        "Title": _ascii_header(title) or "Character Swap",
        "Priority": str(max(1, min(5, int(priority)))),
    }
    if tags:
        headers["Tags"] = ",".join(tags)
    link = click if click is not None else (settings.ntfy_click or "")
    if link.strip():
        headers["Click"] = link.strip()
    return url, headers, (body or "").encode("utf-8")


def _send(req: tuple[str, dict[str, str], bytes]) -> None:
    url, headers, data = req
    try:
        httpx.post(url, headers=headers, content=data, timeout=10.0)
    except Exception as e:  # noqa: BLE001 — push must never break a render
        _log.warning("ntfy push failed: %s", e)


def notify(
    title: str,
    body: str = "",
    *,
    priority: int = 3,
    tags: list[str] | None = None,
    click: str | None = None,
) -> None:
    """Fire a best-effort phone push. Non-blocking, never raises, no-op if
    ``NTFY_TOPIC`` is unset."""
    # build_request() must be inside the guard too: a bad arg (e.g. a
    # non-int priority or non-str tag) would otherwise raise straight through
    # notify() and into the calling render — the exact thing this module
    # promises never to do.
    try:
        req = build_request(title, body, priority=priority, tags=tags, click=click)
        if req is None:
            return
        _EXECUTOR.submit(_send, req)
    except Exception as e:  # noqa: BLE001 — push must never break a render
        _log.warning("ntfy push failed to enqueue: %s", e)
