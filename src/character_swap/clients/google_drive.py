"""Google Drive client wrapper.

Used by the Higgsfield-inbox watcher: lists files in a user-configured
Drive folder (where Higgsfield Supercomputer exports its outputs), and
downloads new ones to the local `output/higgsfield-inbox/` directory so
the Editor's multi-clip mode can pick them up.

Auth model: OAuth 2.0 with a Desktop client. The first call triggers a
browser-redirect device flow; the resulting token is persisted at
`~/character-swap-data/drive_read_token.json` and silently refreshed on
subsequent runs. `credentials.json` (the OAuth client config from Google
Cloud Console) lives in the same shared-data dir.

Scope: `drive.readonly` — we only LIST + DOWNLOAD, never modify the
user's Drive. Phase 4's Drive uploader (runner_pipeline) uses
`drive.file` and a separate token file; the two flows don't clash.

All public functions return None / empty on auth failure rather than
raising — the watcher logs the issue and tries again on the next poll
cycle so a transient Google outage doesn't kill the background task.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from character_swap.config import settings


DRIVE_READONLY_SCOPE = ["https://www.googleapis.com/auth/drive.readonly"]


def _credentials_path() -> Path:
    """Shared OAuth client config — same file Phase 4 uses for Drive upload."""
    return (settings.state_dir.parent / "credentials.json").resolve()


def _token_path() -> Path:
    """Per-scope token cache. Separate from Phase 4's token.json since the
    `drive.readonly` scope is different from `drive.file` and Google issues
    a new token per scope set."""
    return (settings.state_dir.parent / "drive_read_token.json").resolve()


def _load_credentials():
    """Return google-auth Credentials, refreshing or running the OAuth flow
    as needed. Returns None if credentials.json is missing or OAuth fails."""
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
    except ImportError:
        # google libs not installed — the FastAPI server starts fine, the
        # Drive watcher just stays inert.
        return None

    creds_path = _credentials_path()
    token_path = _token_path()
    if not creds_path.exists():
        return None

    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(
                str(token_path), DRIVE_READONLY_SCOPE,
            )
        except (ValueError, OSError):
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
            return creds
        except Exception:
            creds = None

    # No valid token — run the auth flow. This opens the user's browser.
    # We only attempt this if we're in an interactive terminal; otherwise
    # we return None and the user has to run the bootstrap CLI command.
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(creds_path), DRIVE_READONLY_SCOPE,
        )
        creds = flow.run_local_server(port=0, open_browser=True)
        token_path.write_text(creds.to_json())
        return creds
    except Exception:
        return None


def _service():
    """Build a Drive v3 service handle, or None if auth fails."""
    creds = _load_credentials()
    if creds is None:
        return None
    try:
        from googleapiclient.discovery import build
        return build("drive", "v3", credentials=creds, cache_discovery=False)
    except Exception:
        return None


def status() -> dict[str, Any]:
    """Quick health check for /api/health and the Editor UI's 'connected?' badge."""
    creds_path = _credentials_path()
    token_path = _token_path()
    return {
        "credentials_present": creds_path.exists(),
        "token_present": token_path.exists(),
        "ready": creds_path.exists() and token_path.exists(),
    }


def resolve_folder_id(folder_name: str) -> str | None:
    """Look up a folder by name. Returns the first matching folder ID or
    None if not found. Used when the user sets HIGGSFIELD_DRIVE_FOLDER_NAME
    instead of an explicit ID."""
    svc = _service()
    if svc is None:
        return None
    try:
        # Sanitize: Drive's query syntax requires escaping single-quotes.
        safe_name = folder_name.replace("'", "\\'")
        results = svc.files().list(
            q=(f"name = '{safe_name}' "
               f"and mimeType = 'application/vnd.google-apps.folder' "
               f"and trashed = false"),
            spaces="drive",
            fields="files(id, name)",
            pageSize=5,
        ).execute()
        items = results.get("files", [])
        return items[0]["id"] if items else None
    except Exception:
        return None


def list_videos_in_folder(folder_id: str,
                          *, page_size: int = 100) -> list[dict[str, Any]]:
    """List video files in a Drive folder, newest first. Returns the bare
    Drive file resource (`id, name, mimeType, modifiedTime, size`)."""
    svc = _service()
    if svc is None:
        return []
    try:
        # MIME-prefix filter catches mp4/mov/webm/etc.
        results = svc.files().list(
            q=(f"'{folder_id}' in parents "
               f"and mimeType contains 'video/' "
               f"and trashed = false"),
            spaces="drive",
            fields="files(id, name, mimeType, modifiedTime, size, webViewLink)",
            orderBy="modifiedTime desc",
            pageSize=page_size,
        ).execute()
        return results.get("files", [])
    except Exception:
        return []


def download_file(file_id: str, dest: Path) -> bool:
    """Stream a Drive file to `dest`. Returns True on success."""
    svc = _service()
    if svc is None:
        return False
    try:
        from googleapiclient.http import MediaIoBaseDownload
        request = svc.files().get_media(fileId=file_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as fh:
            downloader = MediaIoBaseDownload(fh, request, chunksize=1024 * 1024)
            done = False
            while not done:
                _status, done = downloader.next_chunk()
        return True
    except Exception:
        # Caller logs the failure; we leave a partial file behind for them
        # to inspect if needed.
        return False


def bootstrap_oauth() -> dict[str, Any]:
    """CLI-callable helper: force the browser OAuth flow even when called
    non-interactively. Returns a status dict suitable for the UI."""
    # The Credentials() call already triggers the flow when the token is
    # missing. Calling _service() is enough to force-bootstrap.
    svc = _service()
    return {
        "ok": svc is not None,
        **status(),
    }
