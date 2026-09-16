"""
Uploads a submission's original files to WeTransfer and returns a shareable
download link to put in the report email, instead of (or alongside)
attaching files directly.

This solves two problems at once:
  1. Email attachments are capped (we cap at 20MB combined) - a WeTransfer
     link has no such limit for files up to the plan's per-transfer size cap.
  2. Files on the server aren't easily "retrievable" by you - a link you can
     just click is much more useful than a note saying a file was too big
     to attach.

Uses WeTransfer's public API v2 directly (https://developers.wetransfer.com/)
rather than a third-party wrapper library, since the available community
Python packages for this API haven't been updated since 2019 and may no
longer match the current API.

Free WeTransfer links expire after 3 days - fine for "here's a job to quote
this week," not for long-term archiving. Set WETRANSFER_API_KEY to enable;
if it's not set, this is skipped entirely and the app falls back to
attaching what it can directly to the email (see emailer.py).
"""

import os
import logging
import requests

logger = logging.getLogger("portal.wetransfer")

WETRANSFER_API_KEY = os.environ.get("WETRANSFER_API_KEY")
BASE_URL = "https://dev.wetransfer.com"
REQUEST_TIMEOUT = 60


class WeTransferError(Exception):
    pass


def _authorize():
    resp = requests.post(
        f"{BASE_URL}/v2/authorize",
        headers={"x-api-key": WETRANSFER_API_KEY, "Content-Type": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    if not resp.ok:
        raise WeTransferError(f"Authorize failed ({resp.status_code}): {resp.text}")
    return resp.json()["token"]


def _auth_headers(token):
    return {
        "x-api-key": WETRANSFER_API_KEY,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def upload_via_wetransfer(file_paths, message="Client portal submission"):
    """Uploads all given files as one WeTransfer transfer and returns the
    shareable link (str), or raises WeTransferError with a message good
    enough to show up clearly in the server logs."""
    if not WETRANSFER_API_KEY:
        return None
    if not file_paths:
        return None

    token = _authorize()

    files_payload = [
        {"name": os.path.basename(p), "size": os.path.getsize(p)}
        for p in file_paths
    ]
    resp = requests.post(
        f"{BASE_URL}/v2/transfers",
        headers=_auth_headers(token),
        json={"message": message, "files": files_payload},
        timeout=REQUEST_TIMEOUT,
    )
    if not resp.ok:
        raise WeTransferError(f"Create transfer failed ({resp.status_code}): {resp.text}")
    transfer = resp.json()
    transfer_id = transfer["id"]

    for file_path, file_info in zip(file_paths, transfer["files"]):
        _upload_one_file(token, transfer_id, file_path, file_info)

    finalize_resp = requests.put(
        f"{BASE_URL}/v2/transfers/{transfer_id}/finalize",
        headers=_auth_headers(token),
        timeout=REQUEST_TIMEOUT,
    )
    if not finalize_resp.ok:
        raise WeTransferError(f"Finalize transfer failed ({finalize_resp.status_code}): {finalize_resp.text}")
    finalized = finalize_resp.json()
    link = finalized.get("url") or finalized.get("shortened_url")
    if not link:
        raise WeTransferError(f"Finalize succeeded but no link in response: {finalized}")
    return link


def _upload_one_file(token, transfer_id, file_path, file_info):
    file_id = file_info["id"]
    multipart = file_info["multipart"]
    part_numbers = multipart["part_numbers"]
    chunk_size = multipart["chunk_size"]

    with open(file_path, "rb") as f:
        for part_number in range(1, part_numbers + 1):
            chunk = f.read(chunk_size)
            url_resp = requests.get(
                f"{BASE_URL}/v2/transfers/{transfer_id}/files/{file_id}/upload-url/{part_number}",
                headers=_auth_headers(token),
                timeout=REQUEST_TIMEOUT,
            )
            if not url_resp.ok:
                raise WeTransferError(
                    f"Getting upload URL failed for {file_path} part {part_number} "
                    f"({url_resp.status_code}): {url_resp.text}"
                )
            upload_url = url_resp.json()["url"]
            put_resp = requests.put(upload_url, data=chunk, timeout=REQUEST_TIMEOUT)
            if not put_resp.ok:
                raise WeTransferError(
                    f"Uploading part {part_number} of {file_path} failed ({put_resp.status_code})"
                )

    complete_resp = requests.post(
        f"{BASE_URL}/v2/transfers/{transfer_id}/files/{file_id}/upload-complete",
        headers=_auth_headers(token),
        json={"part_numbers": part_numbers},
        timeout=REQUEST_TIMEOUT,
    )
    if not complete_resp.ok:
        raise WeTransferError(
            f"Marking {file_path} complete failed ({complete_resp.status_code}): {complete_resp.text}"
        )
