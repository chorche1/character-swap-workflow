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
import threading
from pathlib import Path
from typing import Any

from character_swap.config import settings


DRIVE_READONLY_SCOPE = ["https://www.googleapis.com/auth/drive.readonly"]
# `drive.file` lets us create + manage files OUR app uploads (Hugo's
# captioned MP4s) — but explicitly NOT see anything else in his Drive.
# This is Google's recommended scope for "upload a file from my app".
DRIVE_FILE_SCOPE = ["https://www.googleapis.com/auth/drive.file"]


def _credentials_path() -> Path:
    """Shared OAuth client config — same file Phase 4 uses for Drive upload."""
    return (settings.state_dir.parent / "credentials.json").resolve()


def _token_path() -> Path:
    """Per-scope token cache. Separate from Phase 4's token.json since the
    `drive.readonly` scope is different from `drive.file` and Google issues
    a new token per scope set."""
    return (settings.state_dir.parent / "drive_read_token.json").resolve()


def _write_token_path() -> Path:
    """Token cache for the drive.file (upload) scope. Independent of the
    readonly token so the user can grant one without the other."""
    return (settings.state_dir.parent / "drive_write_token.json").resolve()


def _load_credentials(*, scopes: list[str] | None = None,
                      token_file: Path | None = None,
                      interactive: bool = True):
    """Return google-auth Credentials for the requested scope set, refreshing
    or running the OAuth flow as needed. Returns None if credentials.json is
    missing or OAuth fails.

    `interactive=False` NEVER launches the consent flow — a request-thread
    caller (the push endpoints) must get None back (→ 409 → the UI runs the
    bootstrap deliberately) instead of blocking the HTTP request inside
    run_local_server while a browser window opens on the server machine.

    Defaults to the read-only Drive scope (back-compat with the original
    Higgsfield-inbox watcher). Pass DRIVE_FILE_SCOPE + _write_token_path()
    for the upload path."""
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
    except ImportError:
        # google libs not installed — the FastAPI server starts fine, the
        # Drive watcher just stays inert.
        return None

    scopes = scopes or DRIVE_READONLY_SCOPE
    token_path = token_file or _token_path()
    creds_path = _credentials_path()
    if not creds_path.exists():
        return None

    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(
                str(token_path), scopes,
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
    if not interactive:
        return None
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(creds_path), scopes,
        )
        creds = flow.run_local_server(port=0, open_browser=True)
        token_path.write_text(creds.to_json())
        return creds
    except Exception:
        return None


def _service(*, scopes: list[str] | None = None,
             token_file: Path | None = None,
             interactive: bool = True):
    """Build a Drive v3 service handle for the requested scope, or None if
    auth fails."""
    creds = _load_credentials(scopes=scopes, token_file=token_file,
                              interactive=interactive)
    if creds is None:
        return None
    try:
        from googleapiclient.discovery import build
        return build("drive", "v3", credentials=creds, cache_discovery=False)
    except Exception:
        return None


def _write_service(*, interactive: bool = True):
    """Drive service authorized for `drive.file` — can upload but only sees
    files our app created. Used by the Editor's 'Export to Drive' button and
    the push-to-Drive endpoints (those pass interactive=False)."""
    return _service(scopes=DRIVE_FILE_SCOPE, token_file=_write_token_path(),
                    interactive=interactive)


def status() -> dict[str, Any]:
    """Quick health check for /api/health and the Editor UI's 'connected?' badge."""
    creds_path = _credentials_path()
    token_path = _token_path()
    return {
        "credentials_present": creds_path.exists(),
        "token_present": token_path.exists(),
        "ready": creds_path.exists() and token_path.exists(),
    }


def write_status() -> dict[str, Any]:
    """Same shape as `status` but for the drive.file (upload) token. Drives
    the Editor UI's 'Export to Drive — authorize first' nag."""
    creds_path = _credentials_path()
    write_token = _write_token_path()
    return {
        "credentials_present": creds_path.exists(),
        "token_present": write_token.exists(),
        "ready": creds_path.exists() and write_token.exists(),
    }


def upload_file(source: Path, *,
                drive_filename: str,
                folder_id: str | None = None) -> dict[str, Any] | None:
    """Upload `source` to the user's Drive as `drive_filename`. Returns the
    Drive file resource ({id, name, webViewLink, ...}) on success, None on
    failure (auth or upload error).

    `folder_id` optionally drops the file in a specific folder; default is
    My Drive root.
    """
    svc = _write_service()
    if svc is None:
        return None
    try:
        from googleapiclient.http import MediaFileUpload
        # Guess MIME from extension. Drive's UI handles unknown types fine,
        # but giving it a real type means thumbnails + previews work.
        ext = source.suffix.lower()
        mime_by_ext = {
            ".mp4": "video/mp4", ".mov": "video/quicktime",
            ".webm": "video/webm", ".mkv": "video/x-matroska",
            ".m4v": "video/x-m4v", ".png": "image/png",
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".mp3": "audio/mpeg", ".wav": "audio/wav",
        }
        mime = mime_by_ext.get(ext, "application/octet-stream")
        body: dict[str, Any] = {"name": drive_filename}
        if folder_id:
            body["parents"] = [folder_id]
        media = MediaFileUpload(str(source), mimetype=mime, resumable=True)
        result = svc.files().create(
            body=body,
            media_body=media,
            fields="id, name, webViewLink, webContentLink, size, mimeType",
        ).execute()
        return result
    except Exception:
        return None


def bootstrap_write_oauth() -> dict[str, Any]:
    """Force the drive.file OAuth flow even when called non-interactively.
    Returns a status dict suitable for the UI."""
    svc = _write_service()
    return {
        "ok": svc is not None,
        **write_status(),
    }


class DriveNotAuthorized(RuntimeError):
    """drive.file write access hasn't been granted yet — the caller should
    point the user at the bootstrap flow instead of a generic 500."""


def _q_escape(name: str) -> str:
    return name.replace("\\", "\\\\").replace("'", "\\'")


def _find_child_folder(svc, parent_id: str | None, name: str) -> str | None:
    """ID of an app-visible folder `name` under `parent_id` (Drive root when
    None). drive.file only sees folders THIS app created — exactly the set the
    push feature manages."""
    parent_clause = f" and '{parent_id}' in parents" if parent_id else ""
    results = svc.files().list(
        q=(f"name = '{_q_escape(name)}' "
           f"and mimeType = 'application/vnd.google-apps.folder' "
           f"and trashed = false{parent_clause}"),
        spaces="drive",
        fields="files(id, name)",
        pageSize=5,
    ).execute()
    items = results.get("files", [])
    return items[0]["id"] if items else None


# Serializes find-or-create across concurrent pushes: without it, two pushes
# that both miss a folder each create it → duplicate folders, and the
# overwrite-by-name guarantee silently breaks (files scatter across dupes).
_folder_lock = threading.Lock()


def ensure_folder_path(names: list[str]) -> str:
    """Find-or-create the nested folder path `names` (e.g. ["Character Swap",
    "Chang"]) and return the leaf folder's ID. Raises DriveNotAuthorized when
    write access is missing and RuntimeError (with the real reason) on API
    errors — the push endpoints surface both loudly, never silently."""
    svc = _write_service(interactive=False)
    if svc is None:
        raise DriveNotAuthorized(
            "Google Drive write access is not authorized — run the one-time "
            "Drive authorization first."
        )
    with _folder_lock:
        return _ensure_folder_path_locked(svc, names)


def _ensure_folder_path_locked(svc, names: list[str]) -> str:
    parent: str | None = None
    for name in names:
        try:
            found = _find_child_folder(svc, parent, name)
            if found is None:
                body: dict[str, Any] = {
                    "name": name,
                    "mimeType": "application/vnd.google-apps.folder",
                }
                if parent:
                    body["parents"] = [parent]
                found = svc.files().create(body=body, fields="id").execute()["id"]
            parent = found
        except DriveNotAuthorized:
            raise
        except Exception as e:
            raise RuntimeError(
                f"Drive folder setup failed at {name!r}: {type(e).__name__}: {e}"
            ) from e
    assert parent is not None
    return parent


def upload_or_replace(source: Path, *,
                      drive_filename: str,
                      folder_id: str,
                      file_id: str | None = None) -> dict[str, Any]:
    """Upload `source` into `folder_id`, OVERWRITING the previous upload
    (Drive keeps its own version history — Hugo's re-push choice 2026-07-02).

    Overwrite resolution, most-robust first: (1) `file_id` from the persisted
    receipt of an earlier push — survives job/character RENAMES, and the
    Drive file is renamed to the new `drive_filename` in the same update;
    (2) exact-name match in the folder; (3) create new. Returns the Drive
    file resource. Raises DriveNotAuthorized / RuntimeError instead of
    returning None — a failed push must be loud."""
    svc = _write_service(interactive=False)
    if svc is None:
        raise DriveNotAuthorized(
            "Google Drive write access is not authorized — run the one-time "
            "Drive authorization first."
        )
    try:
        from googleapiclient.http import MediaFileUpload
        ext = source.suffix.lower()
        mime = {
            ".mp4": "video/mp4", ".mov": "video/quicktime",
            ".webm": "video/webm", ".mkv": "video/x-matroska",
            ".m4v": "video/x-m4v", ".png": "image/png",
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".mp3": "audio/mpeg", ".wav": "audio/wav",
        }.get(ext, "application/octet-stream")
        media = MediaFileUpload(str(source), mimetype=mime, resumable=True)
        fields = "id, name, webViewLink, webContentLink, size, mimeType"
        if file_id:
            try:
                return svc.files().update(
                    fileId=file_id, body={"name": drive_filename},
                    media_body=media, fields=fields,
                ).execute()
            except Exception:
                # Receipt points at a file deleted/trashed on Drive —
                # fall through to by-name resolution.
                pass
        existing = svc.files().list(
            q=(f"name = '{_q_escape(drive_filename)}' "
               f"and '{folder_id}' in parents and trashed = false"),
            spaces="drive",
            fields="files(id)",
            pageSize=2,
        ).execute().get("files", [])
        if existing:
            return svc.files().update(
                fileId=existing[0]["id"], media_body=media, fields=fields,
            ).execute()
        return svc.files().create(
            body={"name": drive_filename, "parents": [folder_id]},
            media_body=media,
            fields=fields,
        ).execute()
    except DriveNotAuthorized:
        raise
    except Exception as e:
        raise RuntimeError(
            f"Drive upload of {drive_filename!r} failed: "
            f"{type(e).__name__}: {e}"
        ) from e


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


def list_processable_in_folder(folder_id: str,
                               *, page_size: int = 100,
                               recursive: bool = False,
                               _depth: int = 0) -> list[dict[str, Any]]:
    """Like `list_videos_in_folder` but also returns ZIP files. Higgsfield
    Supercomputer's Drive export wraps each clip in a ZIP, so the watcher
    needs to pick those up too. Returns a uniform list with the same shape
    as `list_videos_in_folder`.

    When `recursive=True`, also walks subfolders (Higgsfield's actual
    export pattern is `AI INF Videos / <project name> / <clip>.zip`).
    Cycle-protected via `_depth` cap; we never go more than 5 levels deep
    so a malformed Drive structure can't lock the watcher.
    """
    svc = _service()
    if svc is None:
        return []
    if _depth > 5:
        return []
    try:
        # Either a `video/*` MIME or one of the ZIP-ish MIME types
        # Higgsfield / browsers use for `.zip`. Plus folders when we're
        # walking recursively.
        mime_clauses = [
            "mimeType contains 'video/'",
            "mimeType = 'application/zip'",
            "mimeType = 'application/x-zip-compressed'",
            "mimeType = 'application/octet-stream'",
        ]
        if recursive:
            mime_clauses.append("mimeType = 'application/vnd.google-apps.folder'")
        q = (f"'{folder_id}' in parents "
             f"and ({' or '.join(mime_clauses)}) "
             f"and trashed = false")
        results = svc.files().list(
            q=q,
            spaces="drive",
            fields="files(id, name, mimeType, modifiedTime, size, webViewLink)",
            orderBy="modifiedTime desc",
            pageSize=page_size,
        ).execute()
        out: list[dict[str, Any]] = []
        for f in results.get("files", []):
            mime = (f.get("mimeType") or "").lower()
            name = (f.get("name") or "").lower()
            if mime.startswith("video/"):
                out.append(f)
            elif mime in ("application/zip", "application/x-zip-compressed"):
                out.append(f)
            elif mime == "application/octet-stream" and name.endswith(".zip"):
                out.append(f)
            elif (recursive
                  and mime == "application/vnd.google-apps.folder"):
                # Recurse into the subfolder, concat its processable files.
                out.extend(list_processable_in_folder(
                    f["id"], page_size=page_size,
                    recursive=True, _depth=_depth + 1,
                ))
        return out
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
