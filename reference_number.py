"""
Generates the short, human-friendly reference number shown in each report and
email - format FP-NNNNN, e.g. FP-24001, FP-24002, ... as set out in the brand
guide. The prefix and the number it counts up from are both configurable
(REFERENCE_PREFIX and REFERENCE_START), so the series can be moved without
touching anything else.

References issued before this format are untouched: they're stored with each
job, so old links and old emails still match.

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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COUNTER_FILE = os.path.join(BASE_DIR, "submission_counter.txt")

# Guards against two threads in this same process racing each other; the
# fcntl lock below additionally guards against two separate processes
# (e.g. multiple gunicorn workers) racing on the same file.
_lock = threading.Lock()


REMOTE_KEY = "meta/reference-counter.txt"

# "FP-24001" and up. The start is an offset, not a floor: the counter itself
# still goes 1, 2, 3..., and this is simply added to it, so the series reads as
# an established one rather than starting at FP-00001.
PREFIX = os.environ.get("REFERENCE_PREFIX", "FP")
START = int(os.environ.get("REFERENCE_START", "24000"))


def format_reference(number):
    """Turns a counter value into the reference clients see."""
    return f"{PREFIX}-{START + int(number):05d}"


def _remote_value():
    """The last number saved in R2, or 0. Used when this server's local copy
    is missing - which happens after every redeploy, since Render gives a
    fresh disk - so numbering carries on instead of restarting at 00001."""
    try:
        import r2_storage
        if not r2_storage.is_configured():
            return 0
        data = r2_storage.get_bytes(REMOTE_KEY, attempts=2)
        return int(data.decode().strip()) if data else 0
    except Exception:
        return 0


def _save_remote(value):
    def _save():
        try:
            import r2_storage
            if r2_storage.is_configured():
                r2_storage.put_bytes(REMOTE_KEY, str(value).encode(), "text/plain", attempts=3)
        except Exception:
            pass  # best effort - the local copy still works until the next redeploy
    threading.Thread(target=_save, daemon=True).start()


def next_reference_number():
    """Returns the next reference number as a string, e.g. "FP-24001"."""
    with _lock:
        with open(COUNTER_FILE, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                content = f.read().strip()
                current = int(content) if content else _remote_value()
                next_value = current + 1
                f.seek(0)
                f.truncate()
                f.write(str(next_value))
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    _save_remote(next_value)
    return format_reference(next_value)
