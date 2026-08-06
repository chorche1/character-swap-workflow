"""fal.ai Kling Video v3 image-to-video client.

The OFFICIAL Kling API (clients/kling.py) only generates 5s or 10s clips — its
`duration` field is an enum of exactly {"5","10"}. fal.ai's Kling v3 endpoint
instead accepts any duration 3–15s, so we route the `kling-v3` model through
here to give per-second clip lengths (the thing the official API can't do).

Auth + upload mirror clients/fal_veed.py: FAL_API_KEY → FAL_KEY env, fal_client
for upload + submit. Submit returns the fal `request_id`; `wait_for_video`
polls the queue and downloads the finished MP4 — matching the
submit_video / wait_for_video two-phase shape the swap runner expects.

API: https://fal.ai/models/fal-ai/kling-video/v3/{standard|pro}/image-to-video
  Tier picked by settings.kling_v3_tier — "pro" (default since 2026-06-12)
  renders 1080p; "standard" is the cheaper 720p tier. Same request schema.
  start_image_url  (required)  URL or data URI — we upload the local frame first
  prompt           (string)    motion prompt
  duration         (enum str)  "3".."15" seconds (default "5")
  generate_audio   (bool)      native audio (default true)
Response: {video: {url, ...}, ...}
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import httpx

from character_swap import call_log
from character_swap.clients import ProviderNotConfigured
from character_swap.config import settings

log = logging.getLogger(__name__)


class FalAccountError(RuntimeError):
    """Non-retryable ACCOUNT-level fal error: balance exhausted / account
    locked. Backlog #6 (2026-06-12): calls.jsonl showed 47 doomed Kling
    submits fired in 6 minutes, every one failing with the same 'Exhausted
    balance / User is locked' — each clip slot burned an upload + submit on
    an account that could not accept work."""


_ACCOUNT_ERROR_MARKERS = (
    "exhausted balance", "user is locked", "insufficient credits",
    "insufficient balance", "payment required",
)

# Process-wide circuit breaker: after one account-level rejection, sibling
# submits fail FAST with the same actionable message instead of re-burning
# uploads/submits for the whole batch. Cleared by time (account fixes are
# human-speed) or process restart; the ↻ retry-all-failed button recovers
# the failed clips after a top-up.
_ACCOUNT_BLOCK_SECS = 600.0
_account_block: dict = {"until": 0.0, "reason": ""}


def error_detail(e: Exception) -> str:
    """`str(e)` PLUS the HTTP response body, which is where fal puts the real
    reason.

    httpx's `HTTPStatusError` message is only the status + URL — e.g.
    `Client error '403 Forbidden' for url '…/storage/auth/token…'` — while the
    body carries the actionable part:
    `{"detail":"User is locked. Reason: Exhausted balance. Top up your balance
    at fal.ai/dashboard/billing"}`.

    Hugo 2026-07-29: a locked fal account (balance −$69.86) therefore looked
    like a bare 403 to BOTH `_is_account_error` (so the circuit breaker never
    tripped and every retry re-burned an upload) and the clip's error chip in
    the UI (so the run showed a cryptic URL instead of "top up fal"). Every
    fal client's error path funnels through here."""
    msg = str(e)
    resp = getattr(e, "response", None)
    body = ""
    if resp is not None:
        try:
            body = (resp.text or "").strip()
        except Exception:            # unread stream / decode error — ignore
            body = ""
    if body and body[:120] not in msg:
        msg = f"{msg} | {body[:400]}"
    return msg


def _is_account_error(e: Exception) -> bool:
    s = error_detail(e).lower()
    return any(m in s for m in _ACCOUNT_ERROR_MARKERS)


# Live balance probe, so the pause above releases the moment the account is
# topped up instead of always sitting out its full 10 minutes (Hugo
# 2026-08-06). Measured that day: after a top-up to $47.90 fal's OWN lock flag
# lingered and cleared UNEVENLY — of 14 retried clips 5 were accepted and 9
# still came back "User is locked". The first of those 9 re-armed the pause,
# so the remaining ~41 clips of the batch were never even attempted. A pause
# is the right answer for a dead account; on an account that demonstrably has
# money it just holds the batch hostage.
_BALANCE_URL = "https://rest.alpha.fal.ai/billing/user_balance"
_BALANCE_CACHE_SECS = 30.0
_BALANCE_TIMEOUT_SECS = 8.0
_balance_probe: dict = {"at": 0.0, "balance": None}
_balance_lock = threading.Lock()


def account_balance(*, force: bool = False) -> float | None:
    """Current fal balance in USD, or None when it can't be determined (no
    key / network error / unexpected payload).

    Cached for `_BALANCE_CACHE_SECS` behind a lock so a 55-clip batch makes
    ONE HTTP call, not 55. NEVER raises — an unknown balance must fall back to
    the conservative behavior (keep the pause), never break a submit."""
    if not settings.fal_api_key:
        return None
    with _balance_lock:
        now = time.monotonic()
        if not force and _balance_probe["at"] and \
                now - _balance_probe["at"] < _BALANCE_CACHE_SECS:
            return _balance_probe["balance"]
        balance: float | None = None
        try:
            r = httpx.get(_BALANCE_URL,
                          headers={"Authorization": f"Key {settings.fal_api_key}"},
                          timeout=_BALANCE_TIMEOUT_SECS)
            r.raise_for_status()
            body = r.text.strip()
            try:                              # bare number today, dict tomorrow
                balance = float(body)
            except ValueError:
                data = r.json()
                for key in ("balance", "user_balance", "amount"):
                    if isinstance(data, dict) and key in data:
                        balance = float(data[key])
                        break
        except Exception as e:                # unknown → caller keeps the pause
            log.warning("fal balance probe failed: %s", e)
            balance = None
        _balance_probe.update(at=now, balance=balance)
        return balance


def _funded() -> bool:
    """True only when fal ITSELF confirms a positive balance."""
    bal = account_balance()
    return bal is not None and bal > 0


def _check_account_block() -> None:
    remaining = _account_block["until"] - time.monotonic()
    if remaining <= 0:
        return
    if _funded():
        # The account has money — this pause is stale. Clear it and let the
        # clip through: fal may still reject it (its lock flag clears
        # unevenly), but that is now a per-clip failure the user can retry,
        # not a batch-wide freeze.
        log.info("fal pause cleared early — balance is positive (was: %s)",
                 _account_block["reason"][:120])
        _account_block.update(until=0.0, reason="")
        return
    raise FalAccountError(
        f"fal submits paused ({int(remaining)}s left): "
        f"{_account_block['reason']} — fix billing at "
        "fal.ai/dashboard/billing, then retry the failed clips")


def _trip_account_block(e: Exception) -> None:
    _account_block["until"] = time.monotonic() + _ACCOUNT_BLOCK_SECS
    _account_block["reason"] = error_detail(e)[:300]


def account_error(e: Exception) -> FalAccountError:
    """The FalAccountError every fal client raises on an account-level
    rejection. When the balance is positive the account is NOT out of money —
    fal's lock flag is simply still lingering after a top-up — so the clip's
    error chip must say that instead of "top up your balance", which would
    send the user to a billing page that already looks fine."""
    detail = error_detail(e)
    bal = account_balance()
    if bal is not None and bal > 0:
        return FalAccountError(
            f"fal rejected the job even though the balance is ${bal:.2f} — "
            "fal's account lock lingers for a few minutes after a top-up and "
            f"clears unevenly. Retry the clip shortly. ({detail})")
    return FalAccountError(f"fal account cannot accept work: {detail}")


def _endpoint() -> str:
    """Tier-resolved endpoint id. Submit and poll must use the SAME tier —
    request_ids are endpoint-scoped at fal, so don't flip KLING_V3_TIER while
    clips are in flight (a resumed poll on the other tier 404s → ↻ retry)."""
    tier = (settings.kling_v3_tier or "pro").strip().lower()
    if tier not in {"standard", "pro"}:
        tier = "pro"
    return f"fal-ai/kling-video/v3/{tier}/image-to-video"

# fal Kling v3 duration is an enum of whole seconds 3..15.
MIN_DURATION = 3
MAX_DURATION = 15


def _client():
    """Lazy import + auth check (mirrors fal_veed). Raises ProviderNotConfigured
    when FAL_API_KEY is missing."""
    if not settings.fal_api_key:
        raise ProviderNotConfigured(
            "FAL_API_KEY not set — sign up at https://fal.ai/dashboard/keys "
            "and add `FAL_API_KEY=fal_...` to your .env"
        )
    try:
        import fal_client  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "fal-client package not installed. Run `uv add fal-client` and retry."
        ) from e
    os.environ["FAL_KEY"] = settings.fal_api_key
    return fal_client


def clamp_duration(duration_secs: int | None) -> int:
    """Snap a requested duration into Kling v3's accepted 3–15s range."""
    try:
        d = int(duration_secs) if duration_secs else 5
    except (TypeError, ValueError):
        d = 5
    return max(MIN_DURATION, min(MAX_DURATION, d))


def submit_image_to_video(
    *,
    image: Path,
    prompt: str,
    duration_secs: int | None = 5,
    generate_audio: bool = True,
    end_image: Path | None = None,
    app_job_id: str | None = None,
) -> str:
    """Upload the start frame, submit a Kling v3 i2v job, return the fal
    `request_id` for polling in `wait_for_video`.

    `end_image` (optional) is uploaded as `end_image_url` so the clip
    interpolates from the start frame to this final frame."""
    _check_account_block()
    fal = _client()
    dur = clamp_duration(duration_secs)
    with call_log.record(
        phase="kling_fal_submit", model=_endpoint(), character="kling-v3",
        job_id=app_job_id, duration_secs=dur,
    ) as payload:
        try:
            start_url = fal.upload_file(str(image))
        except Exception as e:
            if _is_account_error(e):
                _trip_account_block(e)
                raise account_error(e) from e
            raise RuntimeError(f"fal.upload_file failed: {error_detail(e)}") from e
        payload["upload_url"] = start_url

        arguments = {
            "start_image_url": start_url,
            "prompt": (prompt or "")[:2500],
            "duration": str(dur),           # fal expects the enum as a string
            "generate_audio": generate_audio,
        }
        # Talking-head negative prompt (research 2026-06-12). Empty setting →
        # field omitted → fal's own default ("blur, distort, and low
        # quality") applies. cfg_scale/shot_type stay at fal defaults
        # (0.5 / "customize") — right for a single-take clip per the same
        # research; multi_prompt would insert hard CUTS, never use it here.
        neg = (settings.kling_negative_prompt or "").strip()
        if neg:
            arguments["negative_prompt"] = neg[:2500]
        if end_image is not None:
            try:
                end_url = fal.upload_file(str(end_image))
            except Exception as e:
                raise RuntimeError(f"fal.upload_file (end frame) failed: {error_detail(e)}") from e
            arguments["end_image_url"] = end_url
            payload["end_upload_url"] = end_url
        try:
            handler = fal.submit(_endpoint(), arguments=arguments)
        except Exception as e:
            if _is_account_error(e):
                _trip_account_block(e)
                raise account_error(e) from e
            raise RuntimeError(f"fal {_endpoint()} submit failed: {error_detail(e)}") from e
        request_id = handler.request_id
        payload["request_id"] = request_id
        return request_id


def wait_for_video(
    *,
    request_id: str,
    dest: Path,
    app_job_id: str | None = None,
    timeout_secs: int | None = None,
    poll_secs: int | None = None,
) -> Path:
    """Poll the fal queue until the job completes, then download the MP4 to
    `dest`. Raises RuntimeError on timeout / missing output."""
    fal = _client()
    import fal_client  # type: ignore
    timeout = timeout_secs or settings.video_timeout_secs
    interval = poll_secs or max(5, settings.video_poll_interval_secs)

    with call_log.record(
        phase="kling_fal_wait", model=_endpoint(), character="kling-v3",
        job_id=app_job_id,
    ):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = fal.status(_endpoint(), request_id, with_logs=False)
            if isinstance(status, fal_client.Completed):
                break
            time.sleep(interval)
        else:
            raise RuntimeError(
                f"fal Kling v3 job {request_id} timed out after {timeout}s"
            )
        result = fal.result(_endpoint(), request_id)

    video = result.get("video") if isinstance(result, dict) else None
    if not video or not isinstance(video, dict) or not video.get("url"):
        raise RuntimeError(f"fal Kling v3 response missing video.url; got {result!r}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    _download(video["url"], dest)
    return dest


def _download(url: str, dest: Path, *, attempts: int = 3) -> None:
    """Download with transient-error retries (backlog #34): a connection
    reset / SSL hiccup on the FINISHED clip's download used to fail the
    whole (already billed) generation."""
    import ssl
    last: Exception | None = None
    for i in range(attempts):
        try:
            with httpx.stream("GET", url, timeout=180,
                              follow_redirects=True) as r:
                r.raise_for_status()
                with dest.open("wb") as f:
                    for chunk in r.iter_bytes(chunk_size=65536):
                        f.write(chunk)
            return
        except (httpx.TransportError, ssl.SSLError) as e:
            last = e
            dest.unlink(missing_ok=True)
            if i < attempts - 1:
                time.sleep(2.0 * (i + 1))
    raise RuntimeError(
        f"download failed after {attempts} attempts: {last}") from last
