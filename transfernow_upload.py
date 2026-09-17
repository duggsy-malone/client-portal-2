"""
Uploads a submission's original files to TransferNow and returns a shareable
download link - the same role wetransfer_upload.py plays, just a different
provider. See file_transfer.py for how the two are chosen between.

TransferNow's free tier is a 14-day / 100GB trial, not free forever - after
that it's pay-as-you-go (roughly $0.20/GB/month at time of writing). Their
developer docs (https://developers.transfernow.net/) were reliably reachable
and precisely documented when this was written, unlike WeTransfer's portal
at the time, so this was built directly against their documented request/
response shapes.
"""

import os
import logging
import requests

from file_part import put_file_part
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("portal.transfernow")

TRANSFERNOW_API_KEY = os.environ.get("TRANSFERNOW_API_KEY")
BASE_URL = "https://api.transfernow.net/v1"
REQUEST_TIMEOUT = 60
LINK_VALID_DAYS = int(os.environ.get("TRANSFERNOW_VALID_DAYS", "30"))


class TransferNowError(Exception):
    pass


def _headers():
    return {"x-api-key": TRANSFERNOW_API_KEY, "Content-Type": "application/json"}


def upload_via_transfernow(file_paths, message="Client portal submission"):
    """Uploads all given files as one TransferNow transfer and returns the
    shareable link (str), or raises TransferNowError."""
    if not TRANSFERNOW_API_KEY:
        return None
    if not file_paths:
        return None

    files_payload = [
        {"name": os.path.basename(p), "size": os.path.getsize(p)}
        for p in file_paths
    ]
    now = datetime.now(timezone.utc)
    body = {
        "files": files_payload,
        "message": message,
        "subject": "Client portal submission",
        "validityStart": now.isoformat(),
        "validityEnd": (now + timedelta(days=LINK_VALID_DAYS)).isoformat(),
    }
    resp = requests.post(f"{BASE_URL}/transfers", headers=_headers(), json=body, timeout=REQUEST_TIMEOUT)
    if not resp.ok:
        raise TransferNowError(f"Create transfer failed ({resp.status_code}): {resp.text}")
    transfer = resp.json()
    transfer_id = transfer["transferId"]
    link = transfer["link"]

    # Match each response entry back to its local file by (name, size) rather
    # than by list position - the API isn't guaranteed to echo files back in
    # the same order they were submitted, and uploading the wrong local
    # file's bytes against another file's upload slot produces exactly the
    # "wrong size" error this replaced.
    remaining = list(file_paths)
    for file_info in transfer["files"]:
        match = next(
            (p for p in remaining
             if os.path.basename(p) == file_info["name"] and os.path.getsize(p) == file_info["size"]),
            None
        )
        if match is None:
            raise TransferNowError(
                f"Could not match response file '{file_info['name']}' ({file_info['size']} bytes) "
                f"back to an uploaded file."
            )
        remaining.remove(match)
        _upload_one_file(transfer_id, match, file_info)

    finish_resp = requests.put(
        f"{BASE_URL}/transfers/{transfer_id}/upload-done",
        headers=_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    if not finish_resp.ok:
        raise TransferNowError(f"Finalizing transfer failed ({finish_resp.status_code}): {finish_resp.text}")

    return link


def _upload_one_file(transfer_id, file_path, file_info):
    file_id = file_info["id"]
    upload_id = file_info["multipartUpload"]["uploadId"]
    parts = file_info["multipartUpload"]["parts"]

    # Each part is streamed from disk rather than read into memory first -
    # parts can be large, and this server may only have 512 MB.
    for part in parts:
        part_number = part["partNumber"]

        url_resp = requests.get(
            f"{BASE_URL}/transfers/{transfer_id}/files/{file_id}/parts/{part_number}",
            headers=_headers(),
            params={"uploadId": upload_id},
            timeout=REQUEST_TIMEOUT,
        )
        if not url_resp.ok:
            raise TransferNowError(
                f"Getting upload URL failed for {file_path} part {part_number} "
                f"({url_resp.status_code}): {url_resp.text}"
            )
        upload_url = url_resp.json()["uploadUrl"]
        put_resp = put_file_part(upload_url, file_path, part["start"], part["size"], REQUEST_TIMEOUT)
        if not put_resp.ok:
            raise TransferNowError(
                f"Uploading part {part_number} of {file_path} failed ({put_resp.status_code})"
            )

    done_resp = requests.put(
        f"{BASE_URL}/transfers/{transfer_id}/files/{file_id}/upload-done",
        headers=_headers(),
        params={"uploadId": upload_id},
        timeout=REQUEST_TIMEOUT,
    )
    if not done_resp.ok:
        raise TransferNowError(
            f"Marking {file_path} complete failed ({done_resp.status_code}): {done_resp.text}"
        )
