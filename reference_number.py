"""
Generates the short, human-friendly reference number shown to Kaye in each
report/email - format DDMMYY-NNNNN, e.g. 160926-00001, 160926-00002, ...

This is deliberately kept SEPARATE from the internal submission folder name
(still the old timestamp+random id). That folder name is what guarantees two
submissions never collide and overwrite each other's files on disk - the
reference number below is purely a cosmetic, sequential label for the report/
email, and is never used to name or locate anything on disk.

Why that separation matters: the counter here is just a number stored in
submission_counter.txt next to this file. It persists fine across restarts
AS LONG AS the underlying disk survives. Render's free tier does not
guarantee that - a redeploy, or the container spinning back up after the
free tier's inactivity sleep, can start from a fresh disk and reset this
counter back to 1. That's a real, known limitation of running on the free
tier without a paid persistent disk. Because the counter only ever feeds
this display-only reference number, a reset just means the numbers may
start again from 00001 at some point - it can never cause a file to be
lost or overwritten, unlike if it were used as the actual storage key.
"""

import os
import fcntl
import threading
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COUNTER_FILE = os.path.join(BASE_DIR, "submission_counter.txt")

# Guards against two threads in this same process racing each other; the
# fcntl lock below additionally guards against two separate processes
# (e.g. multiple gunicorn workers) racing on the same file.
_lock = threading.Lock()


def next_reference_number():
    """Returns the next reference number as a string, e.g. "160926-00001"."""
    with _lock:
        with open(COUNTER_FILE, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                content = f.read().strip()
                current = int(content) if content else 0
                next_value = current + 1
                f.seek(0)
                f.truncate()
                f.write(str(next_value))
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    date_part = datetime.now().strftime("%d%m%y")
    num_part = str(next_value).zfill(5)
    return f"{date_part}-{num_part}"
