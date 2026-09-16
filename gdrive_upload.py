"""
Lets the CLIENT'S BROWSER upload large files straight to Google Drive,
bypassing Render (and the Cloudflare edge in front of it) entirely for the
actual file bytes - that edge has a hard upload size limit that has nothing
to do with this app's own code, and no amount of server-side tuning gets
around it. Only small JSON requests (file names/sizes, then a "done" signal)
ever go through Render; the big transfer goes directly browser -> Google.

This uses Drive's own REST API directly via `requests`, the same way
emailer.py/transfernow_upload.py talk to their providers, rather than
pulling in Google's official (much heavier) client library - one less
dependency to install and pin.

Requires a ONE-TIME authorization against a real Google account (NOT a bare
"service account", which has no storage of its own - see README). Once
GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN are set (see
the /admin/gdrive-auth flow in app.py, and README.md "Large files: Google
Drive bypass"), this module refreshes its own short-lived access token as
needed - nothing else to configure.

Files are only ever staged in Drive temporarily: app.py downloads them back
for analysis and deletes the Drive copies once done (or immediately, for a
"count only" request). WeTransfer/TransferNow (file_transfer.py) is
unchanged and still does the actual "here's a link to download the
originals" step in the report - Drive is purely a relay to get big files
past the edge limit, not the final delivery mechanism.
"""

import os
import time
import logging
import threading
import requests

logger = logging.getLogger("portal.gdrive")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN")

TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
# drive.file: the app can only see/manage files IT creates, not your whole
# Drive - the least access that gets the job done, and it's what lets the
# Workspace OAuth consent screen skip Google's full verification review.
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"

REQUEST_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 300

_token_lock = threading.Lock()
_cached_access_token = None
_cached_expiry = 0  # unix timestamp


class GoogleDriveError(Exception):
    pass


def is_configured():
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN)


def _get_access_token():
    """Returns a valid access token, refreshing it if the cached one has
    expired (or this is the first call in this worker process). Each
    gunicorn worker refreshes independently - that's fine, Google allows a
    refresh token to be used concurrently from more than one process."""
    global _cached_access_token, _cached_expiry
    if not is_configured():
        raise GoogleDriveError(
            "Google Drive isn't set up yet (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / "
            "GOOGLE_REFRESH_TOKEN not all set) - see README.md 'Large files: Google Drive bypass'."
        )
    with _token_lock:
        if _cached_access_token and time.time() < _cached_expiry - 60:
            return _cached_access_token
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": GOOGLE_REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
            timeout=REQUEST_TIMEOUT,
        )
        if not resp.ok:
            raise GoogleDriveError(f"Refreshing Google access token failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        _cached_access_token = data["access_token"]
        _cached_expiry = time.time() + data.get("expires_in", 3600)
        return _cached_access_token


def _headers(extra=None):
    h = {"Authorization": f"Bearer {_get_access_token()}"}
    if extra:
        h.update(extra)
    return h


def create_submission_folder(name):
    """Creates a Drive folder (in the authorized account's 'My Drive') to
    hold one submission's files, so they're easy to find/clean up as a
    group. Returns the folder's file id."""
    resp = requests.post(
        f"{DRIVE_API}/files",
        headers=_headers({"Content-Type": "application/json"}),
        json={"name": name, "mimeType": "application/vnd.google-apps.folder"},
        timeout=REQUEST_TIMEOUT,
    )
    if not resp.ok:
        raise GoogleDriveError(f"Creating Drive folder failed ({resp.status_code}): {resp.text}")
    return resp.json()["id"]


def create_resumable_upload_session(filename, folder_id, origin=None):
    """Starts a Drive resumable-upload session and returns its session URI -
    this is the URL the CLIENT'S BROWSER will PUT the raw file bytes to
    directly. No Authorization header is needed for that PUT (or any further
    auth exposed to the browser) - the session URI itself is the credential,
    and it only works for this one upload.

    `origin` should be the portal's own address (e.g.
    https://client-portal-2.onrender.com). Because the session is opened here
    on the server but the bytes are sent from a browser on a different site,
    Google needs to be told that site up front so the browser is allowed to
    make the cross-site upload (CORS)."""
    extra = {"Content-Type": "application/json; charset=UTF-8"}
    if origin:
        extra["Origin"] = origin
    resp = requests.post(
        f"{DRIVE_UPLOAD_API}/files?uploadType=resumable",
        headers=_headers(extra),
        json={"name": filename, "parents": [folder_id]},
        timeout=REQUEST_TIMEOUT,
    )
    if not resp.ok:
        raise GoogleDriveError(f"Starting upload session for '{filename}' failed ({resp.status_code}): {resp.text}")
    location = resp.headers.get("Location")
    if not location:
        raise GoogleDriveError(f"Drive didn't return an upload URL for '{filename}'.")
    return location


def list_files_in_folder(folder_id):
    """Returns [{'id', 'name', 'size'}, ...] for every file currently in the
    given folder - used after the browser reports it's finished uploading,
    to find out what actually landed in Drive."""
    files, page_token = [], None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": "nextPageToken, files(id,name,size)",
            "pageSize": 1000,
        }
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(f"{DRIVE_API}/files", headers=_headers(), params=params, timeout=REQUEST_TIMEOUT)
        if not resp.ok:
            raise GoogleDriveError(f"Listing files in Drive folder failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        files.extend(data.get("files", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            return files


def rename_file(file_id, new_name):
    """Renames a file or folder in Drive."""
    resp = requests.patch(
        f"{DRIVE_API}/files/{file_id}",
        headers=_headers({"Content-Type": "application/json"}),
        json={"name": new_name},
        timeout=REQUEST_TIMEOUT,
    )
    if not resp.ok:
        raise GoogleDriveError(f"Renaming Drive file {file_id} failed ({resp.status_code}): {resp.text}")


def keep_for_attention(folder_id, reference_number):
    """Something went wrong after the client's files reached Drive, so rather
    than deleting them, rename the folder so it's easy to spot and so the
    abandoned-folder sweep (which only looks for 'Portal submission' folders)
    leaves it alone. Returns the new folder name."""
    new_name = f"NEEDS ATTENTION - quote ref {reference_number}"
    rename_file(folder_id, new_name)
    return new_name


def download_file(file_id, dest_path):
    """Streams a Drive file's content to a local path."""
    resp = requests.get(
        f"{DRIVE_API}/files/{file_id}",
        headers=_headers(),
        params={"alt": "media"},
        timeout=DOWNLOAD_TIMEOUT,
        stream=True,
    )
    if not resp.ok:
        raise GoogleDriveError(f"Downloading Drive file {file_id} failed ({resp.status_code}): {resp.text}")
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)


def delete_file(file_id):
    """Permanently deletes a file or folder (and, for a folder, everything
    in it) - not just moves it to trash. Errors are logged, not raised:
    cleanup best-effort shouldn't fail a submission that otherwise worked."""
    try:
        resp = requests.delete(f"{DRIVE_API}/files/{file_id}", headers=_headers(), timeout=REQUEST_TIMEOUT)
        if not resp.ok and resp.status_code != 404:
            logger.warning(f"Deleting Drive file {file_id} failed ({resp.status_code}): {resp.text}")
    except Exception as e:
        logger.warning(f"Deleting Drive file {file_id} raised an error: {e}")


def delete_abandoned_folders(older_than_hours=24):
    """Deletes submission folders left behind when a client started an upload
    but never finished it (closed the tab, lost connection, etc.) - those never
    reach /upload/finalize, so nothing else would clean them up. Only ever
    sees folders this app created (drive.file scope). Returns how many it removed."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=older_than_hours)).strftime("%Y-%m-%dT%H:%M:%S")
    resp = requests.get(
        f"{DRIVE_API}/files",
        headers=_headers(),
        params={
            "q": ("mimeType = 'application/vnd.google-apps.folder' and "
                  "name contains 'Portal submission' and trashed = false and "
                  f"createdTime < '{cutoff}'"),
            "fields": "files(id,name)",
            "pageSize": 1000,
        },
        timeout=REQUEST_TIMEOUT,
    )
    if not resp.ok:
        raise GoogleDriveError(f"Listing abandoned folders failed ({resp.status_code}): {resp.text}")
    folders = resp.json().get("files", [])
    for folder in folders:
        delete_file(folder["id"])  # deleting a folder removes its contents too
    return len(folders)


def cleanup_folder(folder_id, file_ids):
    """Deletes every known file in a submission's folder, then the folder
    itself. Best-effort - see delete_file()."""
    for fid in file_ids:
        delete_file(fid)
    delete_file(folder_id)


# --- One-time authorization (see /admin/gdrive-auth + /admin/gdrive-callback
# in app.py). These two functions are only ever used during that one-off
# setup, to turn "Kaye clicks through Google's consent screen" into a
# GOOGLE_REFRESH_TOKEN value - not used again afterwards. ---

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def build_authorization_url(redirect_uri):
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
        raise GoogleDriveError("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET before starting authorization.")
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": DRIVE_SCOPE,
        "access_type": "offline",
        # Forces Google to hand back a refresh_token even if this account
        # authorized the app before (otherwise a repeat authorization only
        # returns an access_token, with no way to get a new refresh_token).
        "prompt": "consent",
    }
    query = "&".join(f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in params.items())
    return f"{AUTH_URL}?{query}"


def exchange_code_for_tokens(code, redirect_uri):
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=REQUEST_TIMEOUT,
    )
    if not resp.ok:
        raise GoogleDriveError(f"Exchanging authorization code failed ({resp.status_code}): {resp.text}")
    return resp.json()
