"""Veo 3.1 Fast image-to-video through VERTEX AI (Google Cloud).

WHY A THIRD HOST (Hugo 2026-08-10). The same model is reachable three ways and
they differ only in who moderates and who rate-limits:

  fal      refuses 46% of this app's clips (measured over 127 identical start
           frames) but has no practical daily ceiling.
  Gemini   refuses almost none (5/5 on frames fal refuses 90-100% of the time)
   API     but accepted ~14 videos a day on his Tier 1 key — measured, and far
           short of five 40-clip runs. Its ceiling lifts only when a spend
           threshold is crossed ($100 + 3 days for Tier 2), which is circular:
           the quota is what stops him spending.
  VERTEX   same model again, but the quota is a PER-PROJECT, PER-REGION Google
   (here)  Cloud quota — visible in the Cloud Console and raised through a
           normal quota-increase request rather than by waiting for a spend
           threshold. That is the entire reason this file exists.

AUTH is the one real difference in shape: Vertex does not accept API keys. A
probe on 2026-08-10 returned, verbatim, "API keys are not supported by this
API. Expected OAuth2 access token or other authentication credentials that
assert a principal." So this client mints a short-lived OAuth token and sends
it as a bearer, caching it until shortly before expiry so a 40-clip batch mints
once rather than forty times. Two identities are accepted, in order of least
friction for the user:

  1. the USER'S OWN ADC, from a single `gcloud auth application-default login`
     — one browser consent, no long-lived private key left on disk. This is the
     path Hugo actually uses. A user identity has no project of its own, so
     every request carries `x-goog-user-project`; without it Google has nothing
     to bill or count the quota against and answers 403.
  2. a SERVICE-ACCOUNT key file (`VERTEX_CREDENTIALS_FILE`), for a headless or
     CI machine where nobody can click through a browser prompt.

`google.auth.default` also resolves GOOGLE_APPLICATION_CREDENTIALS and a
metadata-server identity, so branch 1 keeps working unchanged if this ever runs
on a cloud VM. The client never shells out to `gcloud` — it only reads the
credentials that command leaves behind.

STATUS — VERIFIED LIVE 2026-08-10 against project gen-lang-client-0942298729:
submit accepted, operation polled, an 8 s 1080x1920 clip with audio downloaded
from the inline base64 form, and the RAI-filter path exercised for real.

BUT — THE FINDING THAT DECIDES WHERE THIS HOST BELONGS: **Vertex applies
STRICTER content filtering than the Gemini API on the same model.** Frank's
shirtless start frame, which the Gemini API rendered 5 times out of 5, was
filtered here on BOTH attempts ("1 videos were filtered out because they
violated Vertex AI's usage guidelines … Support codes: 15236754"), while a
fully-clothed frame rendered first try. So Vertex is NOT the answer for the
shirtless half of this app's cast; it is real extra capacity for everything
else, which is why it sits LAST in the chain — it only ever sees clips the
other two hosts already refused. Vertex states plainly that a filtered video is
not billed.

API (documented shape):
  POST https://{LOC}-aiplatform.googleapis.com/v1/projects/{PROJ}/locations/
       {LOC}/publishers/google/models/{MODEL}:predictLongRunning
    instances[0].prompt / .image{bytesBase64Encoded,mimeType} / .lastFrame{…}
    parameters: aspectRatio, resolution, durationSeconds, personGeneration,
                sampleCount, generateAudio
  → {"name": "projects/…/operations/…"}
  POST  …/models/{MODEL}:fetchPredictOperation  {"operationName": name}
  → {"done": true, "response": {"videos": [{"bytesBase64Encoded"|"gcsUri"}]}}

Unlike the Gemini API path, Vertex DOES accept `generateAudio`, so a silent
clip is actually expressible here.
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

_log = logging.getLogger("vertex_veo")

# NOT the same id as the Gemini API path. Vertex serves the GA build
# (`-generate-001`); `-generate-preview`, which is what generativelanguage
# answers to, 404s here — probed 2026-08-10 across six candidate ids, and
# `veo-3.1-fast-generate-001` and `veo-3.1-generate-001` were the only two
# that existed. Do not "unify" these two constants.
MODEL = "veo-3.1-fast-generate-001"
_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

_ALLOWED_DURATIONS = (4, 6, 8)
_ALLOWED_RESOLUTIONS = ("720p", "1080p")
_ALLOWED_ASPECTS = ("9:16", "16:9")

# Token cache. Minting is a signed-JWT round trip to Google, so a 40-clip batch
# must not do it 40 times; refreshed a minute before expiry.
_token: dict = {"value": "", "expires_at": 0.0}
_token_lock = threading.Lock()


class VertexNotConfigured(ProviderNotConfigured):
    """Project id or credentials missing — the host is simply not available."""


# Where Application Default Credentials land after
# `gcloud auth application-default login`. Preferred over a service-account key
# file: it costs the user ONE browser consent instead of five console pages,
# and it leaves no long-lived private key sitting on disk.
_ADC_PATH = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"


def _adc_available() -> bool:
    return _ADC_PATH.is_file()


def configured() -> bool:
    """True when this host can actually be reached. `veo_host_chain` gates on
    it, so a half-configured Vertex never occupies a leg of the chain.

    Two ways in, checked in the order of least friction: the user's own ADC
    (one `gcloud auth application-default login`), or an explicit
    service-account key file for a headless/CI machine where no human can
    consent to a browser prompt."""
    if not settings.vertex_project_id:
        return False
    if settings.vertex_credentials_file:
        return Path(settings.vertex_credentials_file).is_file()
    return _adc_available()


def _access_token() -> str:
    """A cached OAuth2 bearer token for the service account."""
    with _token_lock:
        now = time.time()
        if _token["value"] and now < _token["expires_at"]:
            return _token["value"]
        if not configured():
            raise VertexNotConfigured(
                "Vertex is not configured — set VERTEX_PROJECT_ID and then "
                "EITHER run `gcloud auth application-default login` (one "
                "browser consent, nothing left on disk) OR point "
                "VERTEX_CREDENTIALS_FILE at a service-account JSON key with "
                "the 'Vertex AI User' role."
            )
        try:
            import google.auth                              # type: ignore
            from google.oauth2 import service_account       # type: ignore
            from google.auth.transport.requests import Request  # type: ignore
        except ImportError as e:                            # pragma: no cover
            raise RuntimeError(
                "google-auth is required for the Vertex path. "
                "Run `uv add google-auth`."
            ) from e
        if settings.vertex_credentials_file:
            creds = service_account.Credentials.from_service_account_file(
                settings.vertex_credentials_file, scopes=[_SCOPE])
        else:
            # The user's own ADC. `google.auth.default` also picks up
            # GOOGLE_APPLICATION_CREDENTIALS and metadata-server identities, so
            # this same branch works unchanged on a cloud VM later.
            creds, _ = google.auth.default(scopes=[_SCOPE])
        creds.refresh(Request())
        _token["value"] = creds.token
        # `expiry` is naive UTC; keep a minute of slack rather than trusting a
        # clock we do not control.
        _token["expires_at"] = now + 3000
        return _token["value"]


def _headers(token: str) -> dict[str, str]:
    """Auth plus the billing/quota project.

    `x-goog-user-project` is required when the credentials are a USER identity
    (ADC from `gcloud auth application-default login`): without it Google has
    no project to bill or count the quota against and answers 403. It is
    harmless on a service account, whose project is implicit."""
    return {"Authorization": f"Bearer {token}",
            "x-goog-user-project": settings.vertex_project_id}


def _base_url() -> str:
    loc = (settings.vertex_location or "us-central1").strip()
    return (f"https://{loc}-aiplatform.googleapis.com/v1/projects/"
            f"{settings.vertex_project_id}/locations/{loc}/publishers/google/"
            f"models/{MODEL}")


def clamp_duration(duration_secs: int | None) -> int:
    try:
        d = int(duration_secs) if duration_secs else 8
    except (TypeError, ValueError):
        d = 8
    return min(_ALLOWED_DURATIONS, key=lambda v: abs(v - d))


def _resolution(dur: int | None = None) -> str:
    """Same 1080p-needs-8s rule the other two hosts enforce. Measured on the
    Gemini path (1080p + 6 s is refused there) and documented on fal; assumed
    to hold here, and degrading is the safe direction — the clip renders at
    720p instead of failing."""
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
    mime = ("image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg")
            else "image/png")
    return {"bytesBase64Encoded": base64.b64encode(path.read_bytes()).decode(),
            "mimeType": mime}


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
    """Start a Veo job on Vertex and return the OPERATION NAME, which is what
    `wait_for_video` polls (stored in the same field every other provider's job
    id goes in)."""
    token = _access_token()
    dur = clamp_duration(duration_secs)
    instance: dict = {"prompt": (prompt or "")[:2500], "image": _b64(image)}
    if end_image is not None:
        instance["lastFrame"] = _b64(end_image)
    body = {
        "instances": [instance],
        "parameters": {
            "personGeneration": "allow_adult",
            "aspectRatio": _aspect_ratio(aspect_ratio),
            "resolution": _resolution(dur),
            "durationSeconds": dur,
            "sampleCount": 1,
            # Vertex accepts this field; the Gemini API rejects it outright.
            "generateAudio": bool(generate_audio),
        },
    }
    with call_log.record(
        phase="veo_vertex_submit", model=MODEL, character="veo-3.1-fast-vertex",
        job_id=app_job_id, duration_secs=dur,
    ) as payload:
        payload["end_frame"] = end_image is not None
        r = httpx.post(f"{_base_url()}:predictLongRunning",
                       headers=_headers(token), json=body, timeout=180)
        if r.status_code != 200:
            raise RuntimeError(
                f"vertex veo submit failed ({r.status_code}): {r.text[:500]}")
        op = (r.json() or {}).get("name")
        if not op:
            raise RuntimeError(
                f"vertex veo submit returned no operation name: {r.text[:300]}")
        payload["operation"] = op
        return op


def wait_for_video(
    *,
    operation: str,
    dest: Path,
    app_job_id: str | None = None,
    timeout_secs: int | None = None,
    poll_secs: int | None = None,
) -> Path:
    """Poll `fetchPredictOperation` until done, then write the MP4.

    Vertex returns the clip either inline (base64) or as a GCS uri, depending
    on whether `storageUri` was set — we never set it, so the inline form is
    expected, but both are handled: a client that only knew one of them would
    fail on a finished, already-billed render."""
    token = _access_token()
    timeout = timeout_secs or settings.video_timeout_secs
    interval = poll_secs or max(5, settings.video_poll_interval_secs)

    with call_log.record(
        phase="veo_vertex_wait", model=MODEL, character="veo-3.1-fast-vertex",
        job_id=app_job_id,
    ):
        deadline = time.monotonic() + timeout
        status: dict = {}
        while time.monotonic() < deadline:
            r = httpx.post(f"{_base_url()}:fetchPredictOperation",
                           headers=_headers(token),
                           json={"operationName": operation}, timeout=60)
            if r.status_code != 200:
                raise RuntimeError(
                    f"vertex veo poll failed ({r.status_code}): {r.text[:300]}")
            status = r.json() or {}
            if status.get("done"):
                break
            time.sleep(interval)
        else:
            raise RuntimeError(
                f"vertex veo job {operation} timed out after {timeout}s")

        if "error" in status:
            raise RuntimeError(f"vertex veo job failed: {status['error']}")

        resp = status.get("response") or {}
        videos = resp.get("videos") or []
        if not videos:
            # Vertex reports its post-generation RAI filtering by returning
            # FEWER videos than asked for — say that plainly rather than
            # "missing url", which reads like a bug in our code.
            filtered = resp.get("raiMediaFilteredCount")
            reasons = resp.get("raiMediaFilteredReasons")
            if filtered:
                raise RuntimeError(
                    f"content-policy: Vertex filtered the generated video "
                    f"(raiMediaFilteredCount={filtered}, reasons={reasons})")
            raise RuntimeError(
                f"vertex veo response carried no video: {str(resp)[:300]}")
        video = videos[0]

    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = video.get("bytesBase64Encoded")
    if raw:
        dest.write_bytes(base64.b64decode(raw))
    elif video.get("gcsUri"):
        _download_gcs(video["gcsUri"], token, dest)
    else:
        raise RuntimeError(f"vertex veo video had neither bytes nor a uri: "
                           f"{str(video)[:200]}")
    if dest.stat().st_size < 1024:
        dest.unlink(missing_ok=True)
        raise RuntimeError("vertex veo returned an empty clip")
    return dest


def _download_gcs(gcs_uri: str, token: str, dest: Path) -> None:
    """Fetch gs://bucket/object through the JSON API with the same bearer
    token. Only reachable when someone configures `storageUri`; kept so a
    finished render is never lost to an unhandled response shape."""
    if not gcs_uri.startswith("gs://"):
        raise RuntimeError(f"unexpected gcs uri: {gcs_uri}")
    bucket, _, obj = gcs_uri[5:].partition("/")
    from urllib.parse import quote
    url = (f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/"
           f"{quote(obj, safe='')}?alt=media")
    with httpx.stream("GET", url, headers={"Authorization": f"Bearer {token}"},
                      timeout=300, follow_redirects=True) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_bytes(chunk_size=65536):
                f.write(chunk)
