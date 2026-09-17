"""
Picks which file-transfer provider to use for uploading a submission's
original files, so app.py doesn't need to know or care which one is active.

Priority: WeTransfer first if WETRANSFER_API_KEY is set (this is the intended
long-term home, once your paid WeTransfer account's API access is reachable
again), otherwise TransferNow if TRANSFERNOW_API_KEY is set (good right now -
free for 14 days/100GB while testing, then pay-as-you-go if you keep it).
If neither is set, uploads are skipped and the app falls back to attaching
what it can directly to the email (see emailer.py).

To swap providers later: just change which API key environment variable is
set on Render (Environment tab) and redeploy - no code changes needed.
"""

import logging

from wetransfer_upload import upload_via_wetransfer, WeTransferError, WETRANSFER_API_KEY
from transfernow_upload import upload_via_transfernow, TransferNowError, TRANSFERNOW_API_KEY

logger = logging.getLogger("portal.file_transfer")


class FileTransferError(Exception):
    pass


def upload_submission(file_paths, message="Client portal submission", on_progress=None):
    """Returns a shareable download link (str), or None if no provider is
    configured. Raises FileTransferError (with the underlying provider's
    message) if the configured provider fails."""
    if WETRANSFER_API_KEY:
        try:
            return upload_via_wetransfer(file_paths, message)
        except WeTransferError as e:
            raise FileTransferError(f"WeTransfer: {e}")

    if TRANSFERNOW_API_KEY:
        try:
            return upload_via_transfernow(file_paths, message, on_progress=on_progress)
        except TransferNowError as e:
            raise FileTransferError(f"TransferNow: {e}")

    return None
