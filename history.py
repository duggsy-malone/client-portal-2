"""
Submission history: a small record of every submission, kept in Cloudflare R2.

No accounts. A client sees their history through a private link emailed to
the address they submitted with (see /my-submissions in app.py), and Kaye
sees everyone's on /admin/submissions.

What's stored (a few KB per submission, plus a copy of the client's report):
    history/all/<submission id>.json                  - one record per submission
    history/clients/<email key>/<submission id>.json  - the same record, filed
                                                        under that client's email
    history/reports/<submission id>.html              - the client's copy of the report

The files themselves are never kept here - they still go to TransferNow and
are deleted from storage as before.

Every function here swallows storage errors (after logging them): a hiccup
saving history must never stop a quote from being processed.
"""

import hashlib
import hmac
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import r2_storage

logger = logging.getLogger("portal.history")

LINK_VALID_DAYS = 30
MAX_BREAKDOWN_ROWS = 25  # odd sizes listed per record; the rest are summed as "other sizes"


def is_enabled():
    return r2_storage.is_configured()


def email_key(email):
    """A fixed-length stand-in for an email address, used in storage names and
    links so the address itself never appears in a URL."""
    normalised = (email or "").strip().lower()
    return hashlib.sha256(normalised.encode()).hexdigest()[:32]


def _record_keys(record):
    sid = record["submission_id"]
    return [f"history/all/{sid}.json",
            f"history/clients/{email_key(record.get('contact', {}).get('email'))}/{sid}.json"]


def sizes_for_record(summary):
    """Trims build_size_summary() output to something small enough to store."""
    out = []
    for s in summary:
        breakdown = [list(b) for b in s.get("breakdown", [])]
        if len(breakdown) > MAX_BREAKDOWN_ROWS:
            rest = sum(q for _, q in breakdown[MAX_BREAKDOWN_ROWS:])
            breakdown = breakdown[:MAX_BREAKDOWN_ROWS] + [[f"{len(s['breakdown']) - MAX_BREAKDOWN_ROWS} other sizes", rest]]
        out.append({"label": s["label"], "qty": s["qty"], "is_standard": s["is_standard"], "breakdown": breakdown})
    return out


def save(record):
    """Writes (or overwrites) one submission's record. Never raises."""
    if not is_enabled() or not record.get("submission_id"):
        return False
    data = json.dumps(record).encode()
    try:
        for key in _record_keys(record):
            r2_storage.put_bytes(key, data, "application/json")
        return True
    except Exception as e:
        logger.warning(f"History: couldn't save record for {record.get('submission_id')} - {e}")
        return False


def save_report(submission_id, html):
    if not is_enabled():
        return False
    try:
        r2_storage.put_bytes(f"history/reports/{submission_id}.html", html.encode("utf-8"), "text/html; charset=utf-8")
        return True
    except Exception as e:
        logger.warning(f"History: couldn't save report for {submission_id} - {e}")
        return False


def get_report(submission_id):
    try:
        data = r2_storage.get_bytes(f"history/reports/{submission_id}.html")
        return data.decode("utf-8") if data else None
    except Exception as e:
        logger.warning(f"History: couldn't load report for {submission_id} - {e}")
        return None


def get_record(submission_id):
    try:
        data = r2_storage.get_bytes(f"history/all/{submission_id}.json")
        return json.loads(data) if data else None
    except Exception as e:
        logger.warning(f"History: couldn't load record {submission_id} - {e}")
        return None


# Records don't change once a job is finished, so each server worker keeps
# the ones it has already read (keyed by storage name + last-modified time)
# instead of fetching hundreds again every time the admin page loads.
_cache = {}
_cache_lock = threading.Lock()


def _load_many(objects):
    def fetch(obj):
        stamp = (obj["key"], str(obj["last_modified"]), obj["size"])
        with _cache_lock:
            if stamp in _cache:
                return _cache[stamp]
        data = r2_storage.get_bytes(obj["key"])
        record = json.loads(data) if data else None
        if record is not None:
            with _cache_lock:
                _cache[stamp] = record
        return record

    with ThreadPoolExecutor(max_workers=8) as pool:
        records = [r for r in pool.map(fetch, objects) if r]
    records.sort(key=lambda r: r.get("submission_id", ""), reverse=True)  # ids start with date+time
    return records


def list_for_client(key):
    """Every record filed under one client's email key, newest first."""
    objects = [o for o in r2_storage.list_objects(f"history/clients/{key}/") if o["key"].endswith(".json")]
    return _load_many(objects)


def client_owns(key, submission_id):
    return r2_storage.get_bytes(f"history/clients/{key}/{submission_id}.json") is not None


def list_all():
    objects = [o for o in r2_storage.list_objects("history/all/") if o["key"].endswith(".json")]
    return _load_many(objects)


# --------------------------------------------------------------------------
# Private links for clients
# --------------------------------------------------------------------------

def make_link_params(secret, key, now=None):
    expires = int((now or time.time()) + LINK_VALID_DAYS * 86400)
    token = hmac.new(secret, f"history|{key}|{expires}".encode(), hashlib.sha256).hexdigest()[:40]
    return {"c": key, "x": str(expires), "t": token}


def check_link_params(secret, key, expires, token, now=None):
    """(ok, reason) where reason is 'expired' or 'invalid' when not ok."""
    try:
        expires_int = int(expires)
    except (TypeError, ValueError):
        return False, "invalid"
    if not (isinstance(key, str) and len(key) == 32 and all(ch in "0123456789abcdef" for ch in key)):
        return False, "invalid"
    expected = hmac.new(secret, f"history|{key}|{expires_int}".encode(), hashlib.sha256).hexdigest()[:40]
    if not hmac.compare_digest(expected, str(token or "")):
        return False, "invalid"
    if expires_int < (now or time.time()):
        return False, "expired"
    return True, ""
