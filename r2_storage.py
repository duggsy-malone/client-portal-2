"""
Temporary storage for client uploads on Cloudflare R2.

Why this exists: Render sits behind Cloudflare's edge, which rejects big
uploads before they reach this app. So the client's browser sends files
straight to R2 instead, using one-off upload links this module creates,
and the app pulls them back down afterwards to count pages.

R2 replaced an earlier Google Drive version of this. Google treats bursts
of requests from shared cloud-server addresses (like Render's) as bot
traffic and blocks them, which broke jobs with hundreds of files. R2 is
storage built for this: upload links are created locally (no network call
per file) and it doesn't do that kind of bot blocking.

R2 speaks Amazon S3's API. Requests are signed with AWS "Signature Version 4",
written out here directly (no boto3 dependency). sign_v4() is checked against
Amazon's published worked examples in the tests.

Settings (Render -> Environment): R2_ACCOUNT_ID, R2_ACCESS_KEY_ID,
R2_SECRET_ACCESS_KEY, R2_BUCKET. See README "Large files: Cloudflare R2".
"""

import os
import hmac
import time
import hashlib
import logging
import datetime
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests

logger = logging.getLogger("portal.r2")

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "").strip()
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET = os.environ.get("R2_BUCKET", "").strip()
# Normally worked out from the account id; overridable for testing.
R2_ENDPOINT = os.environ.get("R2_ENDPOINT", "").strip().rstrip("/")

REGION = "auto"          # what R2 expects
SERVICE = "s3"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
UNSIGNED = "UNSIGNED-PAYLOAD"
TIMEOUT = (10, 120)      # (connect, read) seconds
DOWNLOAD_TIMEOUT = (10, 600)
MAX_ATTEMPTS = 5


class StorageError(Exception):
    pass


def is_configured():
    return bool(R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET)


def _endpoint():
    return R2_ENDPOINT or f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"


def _host_and_scheme():
    ep = _endpoint()
    scheme, rest = ep.split("://", 1)
    return scheme, rest.split("/", 1)[0]


# ---------------------------------------------------------------------------
# AWS Signature Version 4
# ---------------------------------------------------------------------------

def _uri_encode(value, keep_slash=False):
    return quote(value, safe="-_.~/" if keep_slash else "-_.~")


def _canonical_query(params):
    return "&".join(f"{_uri_encode(k)}={_uri_encode(str(v))}" for k, v in sorted(params.items()))


def sign_v4(method, host, canonical_uri, query, headers, payload_hash,
            access_key, secret_key, region, amz_date, service=SERVICE):
    """Returns (signature, signed_headers_string). `headers` must include
    host and every header to be signed; names are lower-cased here.
    `canonical_uri` is the already-encoded path."""
    lower = {k.lower().strip(): " ".join(str(v).strip().split()) for k, v in headers.items()}
    signed_names = sorted(lower)
    canonical_headers = "".join(f"{n}:{lower[n]}\n" for n in signed_names)
    signed_headers = ";".join(signed_names)
    canonical_request = "\n".join([
        method, canonical_uri, _canonical_query(query), canonical_headers, signed_headers, payload_hash,
    ])
    date = amz_date[:8]
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])

    def _hmac(key, msg):
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k = _hmac(("AWS4" + secret_key).encode(), date)
    k = _hmac(k, region)
    k = _hmac(k, service)
    k = _hmac(k, "aws4_request")
    return hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest(), signed_headers


def _amz_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _object_path(key):
    return f"/{_uri_encode(R2_BUCKET)}/{_uri_encode(key, keep_slash=True)}"


def presigned_url(method, key, expires_seconds=86400, amz_date=None, extra_query=None):
    """A link that lets whoever has it do one kind of request (e.g. PUT this
    one file) until it expires - no password needed. Created locally."""
    if not is_configured():
        raise StorageError("R2 isn't set up (R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET).")
    scheme, host = _host_and_scheme()
    amz_date = amz_date or _amz_now()
    path = _object_path(key)
    query = dict(extra_query or {})
    query.update({
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{R2_ACCESS_KEY_ID}/{amz_date[:8]}/{REGION}/{SERVICE}/aws4_request",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(int(expires_seconds)),
        "X-Amz-SignedHeaders": "host",
    })
    sig, _ = sign_v4(method, host, path, query, {"host": host}, UNSIGNED,
                     R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, REGION, amz_date)
    return f"{scheme}://{host}{path}?{_canonical_query(query)}&X-Amz-Signature={sig}"


def _request(method, key=None, query=None, data=None, stream=False, timeout=TIMEOUT, extra_headers=None,
             attempts=MAX_ATTEMPTS):
    """A signed request to the bucket, retried with increasing waits on
    network errors, 429 and 5xx. Returns the final response (which may still
    be an error status - callers check)."""
    if not is_configured():
        raise StorageError("R2 isn't set up (R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET).")
    scheme, host = _host_and_scheme()
    path = _object_path(key) if key is not None else f"/{_uri_encode(R2_BUCKET)}"
    query = query or {}
    payload_hash = hashlib.sha256(data).hexdigest() if data else EMPTY_SHA256
    url = f"{scheme}://{host}{path}" + (f"?{_canonical_query(query)}" if query else "")
    last_error = None
    for attempt in range(1, attempts + 1):
        amz_date = _amz_now()
        headers = {"host": host, "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash}
        if extra_headers:
            headers.update({k.lower(): v for k, v in extra_headers.items()})
        sig, signed = sign_v4(method, host, path, query, headers, payload_hash,
                              R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, REGION, amz_date)
        send_headers = {k: v for k, v in headers.items() if k != "host"}
        send_headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={R2_ACCESS_KEY_ID}/{amz_date[:8]}/{REGION}/{SERVICE}/aws4_request, "
            f"SignedHeaders={signed}, Signature={sig}")
        try:
            resp = requests.request(method, url, headers=send_headers, data=data, stream=stream, timeout=timeout)
        except requests.RequestException as e:
            last_error = f"couldn't reach storage: {e}"
        else:
            if resp.status_code != 429 and resp.status_code < 500:
                return resp
            last_error = f"storage returned {resp.status_code}: {resp.text[:300]}"
        if attempt < attempts:
            wait = min(30, 2 ** attempt)
            logger.warning(f"R2 {method} {key or ''} failed ({last_error}) - retrying in {wait}s")
            time.sleep(wait)
    raise StorageError(f"R2 {method} {key or ''} failed after {attempts} attempt(s) - {last_error}")


# ---------------------------------------------------------------------------
# Operations the app uses
# ---------------------------------------------------------------------------

def put_bytes(key, data, content_type="application/octet-stream", attempts=MAX_ATTEMPTS):
    resp = _request("PUT", key, data=data, extra_headers={"content-type": content_type}, attempts=attempts)
    if not resp.ok:
        raise StorageError(f"Saving {key} failed ({resp.status_code}): {resp.text[:300]}")


def get_bytes(key, attempts=MAX_ATTEMPTS):
    """Returns the object's bytes, or None if it doesn't exist."""
    resp = _request("GET", key, attempts=attempts)
    if resp.status_code == 404:
        return None
    if not resp.ok:
        raise StorageError(f"Reading {key} failed ({resp.status_code}): {resp.text[:300]}")
    return resp.content


def download(key, dest_path):
    """Streams an object to a local file, retrying the whole download if the
    connection drops part-way."""
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = _request("GET", key, stream=True, timeout=DOWNLOAD_TIMEOUT)
            if not resp.ok:
                raise StorageError(f"Downloading {key} failed ({resp.status_code}): {resp.text[:300]}")
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
            return
        except (requests.RequestException, OSError) as e:
            last = e
            if attempt < MAX_ATTEMPTS:
                time.sleep(min(30, 2 ** attempt))
    raise StorageError(f"Downloading {key} kept failing: {last}")


def delete(key):
    resp = _request("DELETE", key)
    if not resp.ok and resp.status_code != 404:
        raise StorageError(f"Deleting {key} failed ({resp.status_code}): {resp.text[:300]}")


_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def list_objects(prefix):
    """Every object under `prefix`: [{'key', 'size', 'last_modified' (datetime)}]."""
    out, token = [], None
    while True:
        query = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if token:
            query["continuation-token"] = token
        resp = _request("GET", None, query=query)
        if not resp.ok:
            raise StorageError(f"Listing {prefix} failed ({resp.status_code}): {resp.text[:300]}")
        root = ET.fromstring(resp.content)
        ns = _NS if root.tag.startswith(_NS) else ""
        for item in root.findall(f"{ns}Contents"):
            modified = item.findtext(f"{ns}LastModified") or ""
            try:
                when = datetime.datetime.fromisoformat(modified.replace("Z", "+00:00"))
            except ValueError:
                when = datetime.datetime.now(datetime.timezone.utc)
            out.append({"key": item.findtext(f"{ns}Key"), "size": int(item.findtext(f"{ns}Size") or 0),
                        "last_modified": when})
        if (root.findtext(f"{ns}IsTruncated") or "").lower() == "true":
            token = root.findtext(f"{ns}NextContinuationToken")
            if not token:
                break
        else:
            break
    return out


def delete_prefix(prefix):
    """Deletes everything under `prefix`. Returns how many objects."""
    from concurrent.futures import ThreadPoolExecutor
    keys = [o["key"] for o in list_objects(prefix)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(delete, keys))
    return len(keys)


def diagnose():
    """Runs each storage step the portal uses, timing each, stopping at the
    first failure. Used by /admin/storage-check."""
    results, key = [], f"_storage-check/{int(time.time())}.txt"

    def step(name, fn):
        t = time.time()
        try:
            detail = fn()
            results.append({"step": name, "ok": True, "seconds": round(time.time() - t, 2), "detail": detail})
            return True
        except Exception as e:
            results.append({"step": name, "ok": False, "seconds": round(time.time() - t, 2), "detail": str(e)[:500]})
            return False

    if not is_configured():
        return [{"step": "settings", "ok": False, "seconds": 0,
                 "detail": "R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET not all set"}]
    if not step("save a test file", lambda: (put_bytes(key, b"portal storage check", "text/plain"), "ok")[1]):
        return results

    def via_link():
        url = presigned_url("GET", key, 300)
        r = requests.get(url, timeout=TIMEOUT)
        if r.status_code != 200 or r.content != b"portal storage check":
            raise StorageError(f"download link returned {r.status_code}: {r.text[:200]}")
        return "ok"

    step("read it back", lambda: "ok" if get_bytes(key) == b"portal storage check" else (_ for _ in ()).throw(StorageError("content mismatch")))
    step("read it via a one-off link (what uploads use)", via_link)
    step("list files", lambda: f"{len(list_objects('_storage-check/'))} file(s)")
    step("delete the test file", lambda: (delete(key), "ok")[1])
    return results
