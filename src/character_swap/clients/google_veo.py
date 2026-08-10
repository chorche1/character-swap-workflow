"""Veo 3.1 Fast image-to-video through GOOGLE'S OWN Gemini API.

WHY THIS EXISTS (Hugo 2026-08-10, measured — do not undo without re-measuring).
The same model on fal refuses a large share of this app's clips. Measured over
`state/calls.jsonl`, fal's Veo path refused 42-79% of its calls per day through
August while Kling on the SAME fal key refused 0-7%, and 89% of those refusals
arrived AFTER a full render (median 62.5 s against 64.7 s for a success) — the
video was generated and then discarded. Three facts identify the layer:

  * On 127 IDENTICAL start images sent to both models through the same fal
    account, Veo refused 128/281 (46%) and Kling 16/198 (8%), and the two are
    uncorrelated (Kling refuses 7% of the images Veo hates, 13% of the ones Veo
    never touches). A fal account-wide checker would hit both alike.
  * fal blames the PROMPT in 451 of 451 refusals (`loc: ['body','prompt']`)
    while the actual predictor is the IMAGE: within one character, bare-torso
    frames were refused 88-89% and fully clothed frames 0%.
  * Five frames fal refuses ~90-100% of the time — including two it had NEVER
    once passed (0/9 and 0/8) — rendered here on the FIRST attempt, no RAI
    filtering, no prompt or image changed. Under fal's own per-image pass rates
    that outcome has probability ~1e-5.

So the blocker is fal's Veo deployment, not Google's model and not the content.
Going direct is also 20-33% cheaper ($0.10/s at 720p, $0.12/s at 1080p, audio
included, against fal's $0.15/s with audio) and Google states plainly that "you
will only be charged if your video is successfully generated", whereas fal's FAQ
warns a 422 may still be billed for the GPU time — which every one of those
~450 post-render refusals spent.

API (verified live against Hugo's key on 2026-08-10, not from documentation):
  POST /v1beta/models/veo-3.1-fast-generate-preview:predictLongRunning
    instances[0].prompt      (required)
    instances[0].image       {bytesBase64Encoded, mimeType} — the start frame
    instances[0].lastFrame   {bytesBase64Encoded, mimeType} — OPTIONAL 🎯 end
                             pose; verified to render start→end
    parameters.personGeneration  "allow_adult" (the only value Google permits
                             for image-to-video; "allow_all"/"dont_allow" pass
                             validation but are not legal for i2v)
    parameters.aspectRatio   "9:16" | "16:9"
    parameters.resolution    "720p" | "1080p"   (1080p verified → 1080x1920)
    parameters.durationSeconds  4 | 6 | 8       (6 verified → exactly 6.000s)
  → {"name": "models/…/operations/…"} — polled with GET /v1beta/{name}
    until {"done": true}, then
    response.generateVideoResponse.generatedSamples[0].video.uri
  Download that uri with the key in the `x-goog-api-key` HEADER (a `?key=`
  query param returns 0 bytes — measured).

NOT ACCEPTED, and both were tested rather than assumed:
  * `generateAudio` — the API rejects the field outright ("isn't supported by
    this model"). Audio comes anyway: every probe returned AAC stereo 48 kHz.
    So `generate_audio=False` CANNOT be honored here (see submit_image_to_video).
  * 1080p together with a non-8s duration — the same constraint fal documents
    ("1080p resolution is only supported with a duration of 8s"), so
    `_resolution` degrades to 720p exactly as the fal client does.
"""
from __future__ import annotations

import base64
import logging
import threading
import time
from pathlib import Path

import httpx

from character_swap import call_log
from character_swap.clients import ProviderNotConfigured
from character_swap.config import settings

_log = logging.getLogger("google_veo")

MODEL = "veo-3.1-fast-generate-preview"
_BASE = "https://generativelanguage.googleapis.com/v1beta"

_ALLOWED_DURATIONS = (4, 6, 8)
_ALLOWED_RESOLUTIONS = ("720p", "1080p")
_ALLOWED_ASPECTS = ("9:16", "16:9")

# Google returns 429 when too many video jobs are in flight for the key's tier.
# Measured 2026-08-10: the 4th concurrent submit was refused while three ran
# fine. A Reengineer run fans out dozens of clips at once, so submits are gated
# here rather than left to fail — a 429 is not a content refusal and must never
# reach the runner's refusal machinery, which would burn a take on it.
_SUBMIT_GATE = threading.BoundedSemaphore(
    max(1, settings.google_veo_concurrency))
# Backoff for a 429 that slips past the gate anyway (the tier limit is per key,
# and nothing stops a second process from sharing it).
_RETRY_WAITS = (20, 45, 90)

# ACCOUNT-LEVEL QUOTA BREAKER, mirroring fal_kling's balance breaker and added
# for the same reason (Hugo 2026-08-10, from a live run): the quota is per KEY,
# so once it is exhausted EVERY sibling clip is going to 429 too. Without a
# shared breaker each of them independently walked the full backoff ladder —
# measured 162 s, 322 s and 486 s per clip on a 9-clip batch, i.e. the run spent
# ~45 minutes discovering the same fact nine times over. The first clip to hit
# the wall now trips this and the rest fail in milliseconds, which is what lets
# the runner move them to the fal host while the batch is still alive.
#
# It EXPIRES rather than latching: the Gemini rate limit is a moving window, so
# a pause that never lifts would hold a long run hostage after the limit has
# already recovered — the exact failure fal_kling documents for its own lock.
_QUOTA_BLOCK_SECS = 600.0
_quota_block: dict = {"at": 0.0, "detail": ""}
_quota_lock = threading.Lock()


def _quota_blocked_for() -> float:
    """Seconds left on the shared quota pause, 0 when submits may proceed."""
    with _quota_lock:
        at = _quota_block["at"]
        if not at:
            return 0.0
        left = _QUOTA_BLOCK_SECS - (time.monotonic() - at)
        if left <= 0:
            _quota_block["at"] = 0.0
            return 0.0
        return left


def _trip_quota_block(detail: str) -> None:
    with _quota_lock:
        _quota_block["at"] = time.monotonic()
        _quota_block["detail"] = detail[:300]
    _log.warning("google_veo: quota exhausted — pausing submits for %.0fs so "
                 "sibling clips fail fast instead of each walking the backoff "
                 "ladder", _QUOTA_BLOCK_SECS)


def clear_quota_block() -> None:
    """Lift the pause immediately (used by tests and by a manual ↻)."""
    with _quota_lock:
        _quota_block["at"] = 0.0


class GoogleVeoQuotaError(RuntimeError):
    """Google refused the SUBMIT for quota/rate reasons, not content.

    Its own type so the runner can tell it apart from a refusal: re-submitting
    a quota rejection as if it were a stochastic content block would waste the
    clip's whole take budget on a limit that only time fixes."""


def _key() -> str:
    if not settings.gemini_api_key:
        raise ProviderNotConfigured(
            "GEMINI_API_KEY not set — create one at aistudio.google.com/apikey "
            "and add `GEMINI_API_KEY=...` to your .env. Veo needs the PAID "
            "tier; the free tier does not serve video models."
        )
    return settings.gemini_api_key


def clamp_duration(duration_secs: int | None) -> int:
    """Snap a requested duration to Veo's accepted bucket {4,6,8}."""
    try:
        d = int(duration_secs) if duration_secs else 8
    except (TypeError, ValueError):
        d = 8
    return min(_ALLOWED_DURATIONS, key=lambda v: abs(v - d))


def _resolution(dur: int | None = None) -> str:
    """Effective render resolution.

    1080p is only accepted together with an 8 s duration (verified: 1080p alone
    renders 1080x1920; 1080p + 6 s + lastFrame is refused with "Your use case is
    currently not supported"). Scene length is dictated by the source clip, so a
    sub-8s clip degrades to 720p and RENDERS rather than failing — identical to
    the rule fal_veo._resolution already applies."""
    r = (settings.veo_fal_resolution or "1080p").strip().lower()
    if r not in _ALLOWED_RESOLUTIONS:
        r = "1080p"
    if dur is not None and dur != 8 and r == "1080p":
        return "720p"
    return r


def _aspect_ratio(aspect_ratio: str | None) -> str:
    ar = (aspect_ratio or "").strip()
    return ar if ar in _ALLOWED_ASPECTS else "9:16"


def _b64(path: Path) -> dict:
    suffix = path.suffix.lower()
    mime = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
    return {"bytesBase64Encoded": base64.b64encode(path.read_bytes()).decode(),
            "mimeType": mime}


def _is_quota(status: int, body: str) -> bool:
    low = (body or "").lower()
    return status == 429 or "exceeded your current quota" in low or \
        "resource_exhausted" in low


def submit_image_to_video(
    *,
    image: Path,
    prompt: str,
    duration_secs: int | None = 8,
    aspect_ratio: str | None = None,
    generate_audio: bool = True,
    end_image: Path | None = None,
    app_job_id: str | None = None,
) -> str:
    """Start a Veo 3.1 Fast job and return the long-running OPERATION NAME,
    which is what `wait_for_video` polls (the app stores it in the same
    `grok_job_id` field every other provider's job id goes in).

    `end_image` rides as `instances[0].lastFrame`, so a 🎯 end pose survives
    this path — unlike the grok rescue, which has no end-frame input at all.

    `generate_audio=False` CANNOT be honored: the API rejects the field and
    produces audio regardless. Passing False logs a warning rather than failing
    the clip, because a clip WITH unwanted audio still beats no clip — and every
    caller in this app wants the audio anyway (Veo's native speech IS the
    dialogue).
    """
    key = _key()
    dur = clamp_duration(duration_secs)
    if not generate_audio:
        _log.warning("google_veo: generate_audio=False is not supported by %s "
                     "— the clip will have Veo's native audio", MODEL)
    instance: dict = {"prompt": (prompt or "")[:2500], "image": _b64(image)}
    if end_image is not None:
        instance["lastFrame"] = _b64(end_image)
    body = {
        "instances": [instance],
        "parameters": {
            # The only value Google permits for image-to-video. Sent
            # explicitly so a future default change on their side can't
            # silently make person generation stricter.
            "personGeneration": "allow_adult",
            "aspectRatio": _aspect_ratio(aspect_ratio),
            "resolution": _resolution(dur),
            "durationSeconds": dur,
        },
    }
    with call_log.record(
        phase="veo_google_submit", model=MODEL, character="veo-3.1-fast-google",
        job_id=app_job_id, duration_secs=dur,
    ) as payload:
        payload["end_frame"] = end_image is not None
        left = _quota_blocked_for()
        if left:
            # A sibling already proved the key is out of quota. Fail NOW with
            # the same error type, so the runner can move this clip to the
            # other host instead of the batch spending minutes per clip
            # re-discovering it.
            raise GoogleVeoQuotaError(
                f"google veo quota exhausted (paused {left:.0f}s more): "
                f"{_quota_block['detail']}")
        with _SUBMIT_GATE:
            last = ""
            for i, wait in enumerate((0, *_RETRY_WAITS)):
                if wait:
                    time.sleep(wait)
                r = httpx.post(
                    f"{_BASE}/models/{MODEL}:predictLongRunning",
                    headers={"x-goog-api-key": key},
                    json=body, timeout=180,
                )
                if r.status_code == 200:
                    op = (r.json() or {}).get("name")
                    if not op:
                        raise RuntimeError(
                            f"google veo submit returned no operation name: "
                            f"{r.text[:300]}")
                    payload["operation"] = op
                    return op
                last = r.text[:500]
                if not _is_quota(r.status_code, last):
                    raise RuntimeError(
                        f"google veo submit failed ({r.status_code}): {last}")
                _log.warning("google_veo: quota 429 on submit (attempt %d) — "
                             "backing off", i + 1)
            _trip_quota_block(last)
            raise GoogleVeoQuotaError(
                f"google veo quota exhausted after {len(_RETRY_WAITS)} retries: "
                f"{last}")


def wait_for_video(
    *,
    operation: str,
    dest: Path,
    app_job_id: str | None = None,
    timeout_secs: int | None = None,
    poll_secs: int | None = None,
) -> Path:
    """Poll the long-running operation until done, then download the MP4.

    A finished operation carrying an `error` is raised as a RuntimeError with
    Google's own message, so a genuine content block surfaces with its real
    reason instead of a generic failure. Google's RAI counters
    (`raiMediaFilteredCount` / `raiMediaFilteredReasons`) are surfaced the same
    way — unlike fal, this path can say WHY a clip died."""
    key = _key()
    timeout = timeout_secs or settings.video_timeout_secs
    interval = poll_secs or max(5, settings.video_poll_interval_secs)

    with call_log.record(
        phase="veo_google_wait", model=MODEL, character="veo-3.1-fast-google",
        job_id=app_job_id,
    ):
        deadline = time.monotonic() + timeout
        status: dict = {}
        while time.monotonic() < deadline:
            r = httpx.get(f"{_BASE}/{operation}",
                          headers={"x-goog-api-key": key}, timeout=60)
            if r.status_code != 200:
                raise RuntimeError(
                    f"google veo poll failed ({r.status_code}): {r.text[:300]}")
            status = r.json() or {}
            if status.get("done"):
                break
            time.sleep(interval)
        else:
            raise RuntimeError(
                f"google veo job {operation} timed out after {timeout}s")

        if "error" in status:
            raise RuntimeError(
                f"google veo job failed: {status['error']}")

        resp = status.get("response") or {}
        samples = ((resp.get("generateVideoResponse") or {})
                   .get("generatedSamples") or [])
        uri = (samples[0].get("video") or {}).get("uri") if samples else None
        if not uri:
            # Google's post-generation RAI filter drops samples rather than
            # erroring — say so with its own counters instead of a bare
            # "missing url", which reads like a bug in our code.
            gvr = resp.get("generateVideoResponse") or {}
            filtered = gvr.get("raiMediaFilteredCount")
            reasons = gvr.get("raiMediaFilteredReasons")
            if filtered:
                raise RuntimeError(
                    f"content-policy: Google filtered the generated video "
                    f"(raiMediaFilteredCount={filtered}, reasons={reasons})")
            raise RuntimeError(
                f"google veo response carried no video uri: {str(resp)[:300]}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    _download(uri, key, dest)
    return dest


def _download(uri: str, key: str, dest: Path, *, attempts: int = 3) -> None:
    """Download the finished MP4.

    The key MUST go in the `x-goog-api-key` header: with `?key=` in the query
    string the files endpoint answers 200 with an EMPTY body (measured
    2026-08-10), which would write a 0-byte clip and fail much later in ffmpeg.
    """
    import ssl
    last: Exception | None = None
    for i in range(attempts):
        try:
            with httpx.stream("GET", uri, headers={"x-goog-api-key": key},
                              timeout=300, follow_redirects=True) as r:
                r.raise_for_status()
                with dest.open("wb") as f:
                    for chunk in r.iter_bytes(chunk_size=65536):
                        f.write(chunk)
            if dest.stat().st_size < 1024:
                raise RuntimeError(
                    f"downloaded clip is {dest.stat().st_size} bytes — "
                    "the response was empty")
            return
        except (httpx.TransportError, ssl.SSLError, RuntimeError) as e:
            last = e
            dest.unlink(missing_ok=True)
            if i < attempts - 1:
                time.sleep(2.0 * (i + 1))
    raise RuntimeError(f"download failed after {attempts} attempts: {last}") from last
