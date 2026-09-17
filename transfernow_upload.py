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
import time
import logging
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from file_part import put_file_part
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("portal.transfernow")

TRANSFERNOW_API_KEY = os.environ.get("TRANSFERNOW_API_KEY")
BASE_URL = "https://api.transfernow.net/v1"
REQUEST_TIMEOUT = 60
LINK_VALID_DAYS = int(os.environ.get("TRANSFERNOW_VALID_DAYS", "30"))
# How many files are sent at the same time. Each streams from disk in 1 MB
# pieces, so this barely affects memory - it just cuts the time a job with
# hundreds of files takes.
PARALLEL_FILES = int(os.environ.get("TRANSFERNOW_PARALLEL_FILES", "4"))


class TransferNowError(Exception):
    pass


def _headers():
    return {"x-api-key": TRANSFERNOW_API_KEY, "Content-Type": "application/json"}


def _api(method, url, attempts=4, **kwargs):
    """A TransferNow API call, retried briefly if they're busy (429), have a
    server error, or the connection drops. Sending several files at once
    makes a momentary "slow down" reply more likely, so this matters."""
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.request(method, url, headers=_headers(), **kwargs)
        except requests.RequestException:
            if attempt == attempts:
                raise
        else:
            if resp.status_code != 429 and resp.status_code < 500 or attempt == attempts:
                return resp
        time.sleep(3 * attempt)


def upload_via_transfernow(file_paths, message="Client portal submission", on_progress=None):
    """Uploads all given files as one TransferNow transfer and returns the
    shareable link (str), or raises TransferNowError.
    `on_progress(done, total)`, if given, is called as each file finishes."""
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
        # Each transfer is named after its submission (reference, company,
        # job) so they're easy to tell apart in the TransferNow account.
        "subject": message[:200],
        "validityStart": now.isoformat(),
        "validityEnd": (now + timedelta(days=LINK_VALID_DAYS)).isoformat(),
    }
    # Not retried: a retry after a lost reply could create a second transfer.
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
    work = []
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
        work.append((match, file_info))

    # Several files at once. If one fails for good, the rest stop starting
    # new parts and the error is raised.
    stop = threading.Event()
    done = [0]
    lock = threading.Lock()

    def send(pair):
        if stop.is_set():
            return
        _upload_one_file(transfer_id, pair[0], pair[1], stop)
        with lock:
            done[0] += 1
            count = done[0]
        if on_progress:
            try:
                on_progress(count, len(work))
            except Exception:
                pass

    with ThreadPoolExecutor(max_workers=max(1, PARALLEL_FILES)) as pool:
        futures = [pool.submit(send, pair) for pair in work]
        try:
            for fut in as_completed(futures):
                fut.result()
        except BaseException:
            stop.set()
            for fut in futures:
                fut.cancel()
            raise

    finish_resp = _api("PUT", f"{BASE_URL}/transfers/{transfer_id}/upload-done")
    if not finish_resp.ok:
        raise TransferNowError(f"Finalizing transfer failed ({finish_resp.status_code}): {finish_resp.text}")

    return link


def _upload_one_file(transfer_id, file_path, file_info, stop=None):
    file_id = file_info["id"]
    upload_id = file_info["multipartUpload"]["uploadId"]
    parts = file_info["multipartUpload"]["parts"]

    # Each part is streamed from disk rather than read into memory first -
    # parts can be large, and this server may only have 512 MB.
    for part in parts:
        if stop is not None and stop.is_set():
            return
        part_number = part["partNumber"]

        url_resp = _api("GET", f"{BASE_URL}/transfers/{transfer_id}/files/{file_id}/parts/{part_number}",
                        params={"uploadId": upload_id})
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

    done_resp = _api("PUT", f"{BASE_URL}/transfers/{transfer_id}/files/{file_id}/upload-done",
                     params={"uploadId": upload_id})
    if not done_resp.ok:
        raise TransferNowError(
            f"Marking {file_path} complete failed ({done_resp.status_code}): {done_resp.text}"
        )
