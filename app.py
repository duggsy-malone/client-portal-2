"""
Client file-upload portal.

Clients drag & drop files or folders. Files are analysed (page/slide/sheet
counts + physical sizes, flagging anything non-standard), the originals are
sent on via TransferNow/WeTransfer, and a report is emailed to whoever
REPORT_RECIPIENT is set to (see emailer.py / README.md).

Two ways files get here:
  - The direct route (`POST /upload`, one request with the files in it).
    Used automatically when Cloudflare R2 isn't configured, including local
    testing. Big uploads fail this way on Render, because Cloudflare's edge
    in front of Render rejects them before they reach this app.
  - The R2 route (`POST /upload/init` -> the browser sends each file straight
    to Cloudflare R2 storage -> `POST /upload/finalize`). Used whenever R2 is
    configured. The app then pulls the files back down in the background to
    analyse them. See r2_storage.py and README "Large files: Cloudflare R2".

Run locally:
    python3 app.py
Then open http://localhost:5000
"""

import os
import re
import json
import time
import uuid
import hmac
import fcntl
import shutil
import hashlib
import logging
import threading
import html as html_lib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlencode

from flask import Flask, request, jsonify, render_template, redirect, url_for, Response
from werkzeug.middleware.proxy_fix import ProxyFix

from analyzer import analyze_file
from report import rows_to_csv, rows_to_html, build_size_summary, job_totals
from emailer import send_report_email, send_client_email
from file_transfer import upload_submission, WETRANSFER_API_KEY
from reference_number import next_reference_number
import r2_storage
import history
import structure
from version import APP_NAME, VERSION

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("portal")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
JOBS_DIR = os.path.join(BASE_DIR, "jobs")
for _d in (UPLOAD_DIR, REPORTS_DIR, JOBS_DIR):
    os.makedirs(_d, exist_ok=True)

MAX_CONTENT_LENGTH = 5 * 1024 * 1024 * 1024  # 5GB per submission
RETENTION_DAYS = 30
UPLOAD_LINK_HOURS = 24          # how long a client's upload links stay valid
ABANDONED_AFTER_HOURS = 24      # unfinished uploads are deleted after this
STALLED_AFTER_MINUTES = 120     # a job with no progress this long gets restarted
COUNT_STALLED_AFTER_MINUTES = 15  # page counts are picked up again sooner - someone's waiting
MAX_JOB_ATTEMPTS = 3
HEARTBEAT_SECONDS = 600
SWEEP_EVERY_SECONDS = 3600
PROGRESS_SAVE_SECONDS = 30      # how often progress is written to the log/attention page

SUBMISSION_ID_RE = r"\d{8}-\d{6}-[0-9a-f]{6}"

# Password for the /admin pages. GDRIVE_ADMIN_KEY still works so the value
# already saved in Render doesn't need renaming.
ADMIN_KEY = os.environ.get("ADMIN_KEY") or os.environ.get("GDRIVE_ADMIN_KEY")

REQUIRED_FIELDS_ALWAYS = ["subject", "full_name", "company", "email", "phone"]
REQUIRED_FIELDS_FULL_SUBMISSION = ["deadline", "delivery_address"]
FIELD_LABELS = {
    "subject": "Subject",
    "full_name": "Full name",
    "company": "Company",
    "email": "Email",
    "phone": "Phone number",
    "deadline": "Deadline",
    "delivery_address": "Delivery address",
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
# Render terminates HTTPS in front of the app; this lets Flask see the
# original https:// address (used for links in alert emails).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# ==========================================================================
# Small helpers
# ==========================================================================

def _rss_mb():
    """How much memory this server process is using right now, in MB. Logged
    while counting, so a job that runs the server out of memory can be traced
    to the file that did it."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _validate_contact(contact, count_only):
    required_fields = list(REQUIRED_FIELDS_ALWAYS)
    if not count_only:
        required_fields += REQUIRED_FIELDS_FULL_SUBMISSION
    missing = [FIELD_LABELS[f] for f in required_fields if not contact.get(f)]
    if missing:
        return f"Missing required field(s): {', '.join(missing)}"
    return None


def _validate_file_names(names):
    """Any file type is accepted, ZIPs included - they're opened one file at a
    time. Types the analyser can't count pages for still go to Kaye in the
    download link, marked 'check manually'."""
    if not names or not any((n or "").strip() for n in names):
        return "No files received."
    return None


def _unique_dest(directory, filename):
    """A path in `directory` for `filename` that doesn't overwrite anything
    already there ('plan.pdf', then 'plan (2).pdf')."""
    safe = os.path.basename(filename or "").strip() or "file"
    stem, ext = os.path.splitext(safe)
    candidate, n = safe, 2
    while os.path.exists(os.path.join(directory, candidate)):
        candidate = f"{stem} ({n}){ext}"
        n += 1
    return os.path.join(directory, candidate)


def _clean_name(name):
    """A file name safe to use as part of a storage key and a local file name."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name[:200] or "file"


def _new_submission_id():
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def _token_secret():
    return (os.environ.get("APP_SECRET") or r2_storage.R2_SECRET_ACCESS_KEY or "local-dev").encode()


def _make_token(submission_id, reference_number):
    """Proves a later request is about a submission this app really started,
    so nobody can ask it to process or report on someone else's upload."""
    msg = f"{submission_id}|{reference_number}".encode()
    return hmac.new(_token_secret(), msg, hashlib.sha256).hexdigest()[:40]


def _check_token(submission_id, reference_number, token):
    if not (isinstance(submission_id, str) and re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", submission_id)):
        return False
    if not (isinstance(reference_number, str) and re.fullmatch(r"\d{6}-\d{5,}", reference_number)):
        return False
    return hmac.compare_digest(_make_token(submission_id, reference_number), str(token or ""))


def _files_prefix(submission_id):
    return f"incoming/{submission_id}/files/"


def _status_key(submission_id):
    return f"incoming/{submission_id}/status.json"


def _contact_from(source):
    return {field: (str(source.get(field) or "")).strip() for field in FIELD_LABELS}


def _folder_list(value):
    """The browser sends, for each file in upload order, the folder it came
    from ("Archway/Drawings", or "" for a loose file)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = []
    if not isinstance(value, list):
        return []
    return [(v if isinstance(v, str) else "")[:2000] for v in value[:20000]]


def _folder_info(saved_paths, folders, indices=None):
    """{analysed file name: {"folders": [...], "name": original name}} for the
    report's tabs, dividers and folder diagram. `indices` gives each saved
    file's position in the upload (when it isn't simply the list order)."""
    info = {}
    for n, path in enumerate(saved_paths):
        i = indices[n] if indices is not None else n
        parts = structure.clean_folder_path(folders[i]) if 0 <= i < len(folders) else []
        base = os.path.basename(path)
        info[base] = {"folders": parts, "name": structure.original_name(base, parts)}
    return info


def _plans_flag(value):
    return str(value).strip().lower() in ("1", "true", "on", "yes")


def _no_store(resp):
    """Private pages (links with a token in them): don't cache them, don't
    leak the address to other sites, keep them out of search engines."""
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


def _transfer_title(reference_number, contact):
    """e.g. "Quote request 170926-00003 - Acme Ltd - Archway campus" - the
    name the transfer gets in TransferNow/WeTransfer, so each is recognisable."""
    parts = [f"Quote request {reference_number}", contact.get("company", ""), contact.get("subject", "")]
    return " - ".join(p.strip() for p in parts if p and p.strip())[:200]


def _transfer_service_name():
    return "WeTransfer" if WETRANSFER_API_KEY else "TransferNow"


# ==========================================================================
# Pages
# ==========================================================================

@app.before_request
def _start_background_jobs():
    _ensure_background_started()


@app.route("/")
def index():
    return render_template("index.html", version=VERSION, app_name=APP_NAME)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "app": APP_NAME, "version": VERSION})


# ==========================================================================
# Direct route: one request with the files in it. Used when R2 isn't set up.
# ==========================================================================

@app.route("/upload", methods=["POST"])
def upload():
    uploaded_files = request.files.getlist("files")
    client_note = request.form.get("client_note", "").strip()
    count_only = request.form.get("count_only", "").strip().lower() in ("1", "true", "on", "yes")
    contact = _contact_from(request.form)
    folders = _folder_list(request.form.get("folders"))
    plans_to_scale = None if count_only else _plans_flag(request.form.get("plans_to_scale"))

    error = _validate_contact(contact, count_only) or _validate_file_names([f.filename for f in uploaded_files])
    if error:
        return jsonify({"error": error}), 400

    submission_id = _new_submission_id()
    submission_dir = os.path.join(UPLOAD_DIR, submission_id)
    os.makedirs(submission_dir, exist_ok=True)
    saved_paths = []
    for f in uploaded_files:
        dest = _unique_dest(submission_dir, _clean_name(f.filename))
        f.save(dest)
        saved_paths.append(dest)

    reference_number = next_reference_number()
    logger.info(f"Submission {submission_id} (ref {reference_number}): "
                f"{len(saved_paths)} file(s) received directly (count_only={count_only}).")

    if count_only:
        try:
            html_body, _rows = _count_report(saved_paths, submission_id, client_note, contact, reference_number,
                                             folder_info=_folder_info(saved_paths, folders))
        except Exception as e:
            logger.exception(f"Count-only submission {submission_id} failed: {e}")
            return jsonify({"error": "Something went wrong analysing your files. Please try again."}), 500
        finally:
            shutil.rmtree(submission_dir, ignore_errors=True)
        return jsonify({"status": "counted", "submission_id": submission_id,
                        "reference_number": reference_number, "report_html": html_body})

    threading.Thread(target=process_submission,
                     args=(submission_id, saved_paths, client_note, contact, reference_number),
                     kwargs={"folder_info": _folder_info(saved_paths, folders), "plans_to_scale": plans_to_scale},
                     daemon=True).start()
    return jsonify({"status": "received", "submission_id": submission_id,
                    "reference_number": reference_number, "files": len(saved_paths)})


# ==========================================================================
# R2 route
# ==========================================================================

@app.route("/upload/init", methods=["POST"])
def upload_init():
    """Hands the browser one upload link per file. Creating the links needs
    no network calls at all, so this is instant however many files there are."""
    data = request.get_json(silent=True) or {}
    count_only = bool(data.get("count_only"))
    contact = _contact_from(data)
    files = [f for f in (data.get("files") or []) if isinstance(f, dict)]

    error = _validate_contact(contact, count_only) or _validate_file_names([f.get("name") for f in files])
    if error:
        return jsonify({"error": error}), 400

    total_bytes = sum(int(f.get("size") or 0) for f in files)
    if total_bytes > MAX_CONTENT_LENGTH:
        limit_gb = MAX_CONTENT_LENGTH // (1024 ** 3)
        return jsonify({"error": f"Total size exceeds the {limit_gb}GB limit - please remove some files."}), 400

    if not r2_storage.is_configured():
        return jsonify({"relay_configured": False})  # browser falls back to POST /upload

    submission_id = _new_submission_id()
    reference_number = next_reference_number()
    prefix = _files_prefix(submission_id)
    uploads = []
    for i, f in enumerate(files):
        key = f"{prefix}{i:05d}-{_clean_name(f.get('name'))}"
        uploads.append({"name": f.get("name"),
                        "upload_url": r2_storage.presigned_url("PUT", key, UPLOAD_LINK_HOURS * 3600)})

    logger.info(f"Submission {submission_id} (ref {reference_number}): upload links issued for "
                f"{len(uploads)} file(s), {total_bytes / 1024 / 1024:.1f} MB, count_only={count_only}.")
    return jsonify({
        "relay_configured": True,
        "submission_id": submission_id,
        "reference_number": reference_number,
        "token": _make_token(submission_id, reference_number),
        "uploads": uploads,
    })


@app.route("/upload/finalize", methods=["POST"])
def upload_finalize():
    """Called once the browser has finished sending every file to R2."""
    data = request.get_json(silent=True) or {}
    submission_id = data.get("submission_id")
    reference_number = data.get("reference_number")
    if not _check_token(submission_id, reference_number, data.get("token")):
        return jsonify({"error": "Invalid submission details - please start again."}), 400

    count_only = bool(data.get("count_only"))
    client_note = (data.get("client_note") or "").strip()
    contact = _contact_from(data.get("contact") or {})
    folders = _folder_list(data.get("folders"))
    plans_to_scale = None if count_only else _plans_flag(data.get("plans_to_scale"))
    error = _validate_contact(contact, count_only)
    if error:
        return jsonify({"error": error}), 400

    portal_url = request.host_url.rstrip("/")
    if count_only:
        status = _new_count_status(submission_id, reference_number, contact, client_note, folders, portal_url)
        _save_status_safe(status)  # recorded in storage too, so a restart can pick it up
        _write_job(submission_id, state="processing", stage="Fetching your files...")
        threading.Thread(target=_run_count_job, args=(submission_id,), kwargs={"status": status},
                         daemon=True).start()
        return jsonify({"status": "processing", "submission_id": submission_id,
                        "reference_number": reference_number, "history": history.is_enabled()})

    status = {
        "submission_id": submission_id,
        "reference_number": reference_number,
        "status": "received",
        "contact": contact,
        "client_note": client_note,
        "portal_url": portal_url,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "attempts": 0,
        "error": "",
        "stage": "",
        "plans_to_scale": plans_to_scale,
        "folders": folders,
    }
    try:
        # Recording this first means the files are never mistaken for an
        # abandoned upload, whatever happens next.
        _save_status(status)
    except Exception as e:
        logger.error(f"Submission {submission_id}: couldn't record submission - {e}")
        return jsonify({"error": "Your files uploaded, but we couldn't confirm them. Please press submit again."}), 502

    logger.info(f"Submission {submission_id} (ref {reference_number}): upload complete, processing in background.")
    threading.Thread(target=_run_quote_job, args=(submission_id,), daemon=True).start()
    threading.Thread(target=_send_confirmation_email, args=(contact, reference_number, portal_url, plans_to_scale),
                     daemon=True).start()
    return jsonify({"status": "received", "submission_id": submission_id, "reference_number": reference_number,
                    "history": history.is_enabled()})


@app.route("/upload/status")
def upload_status():
    """The page checks this every couple of seconds during 'count my pages'."""
    submission_id = request.args.get("submission_id")
    reference_number = request.args.get("reference_number")
    if not _check_token(submission_id, reference_number, request.args.get("token")):
        return jsonify({"state": "error", "error": "Invalid submission details - please start again."}), 400
    job = _read_job(submission_id)
    if job and job.get("state") == "done":
        report_path = os.path.join(JOBS_DIR, f"{submission_id}.html")
        try:
            with open(report_path) as f:
                report_html = f.read()
        except OSError:
            report_html = history.get_report(submission_id)
        if not report_html:
            return jsonify({"state": "error", "error": "The report went missing - please try again."})
        return jsonify({"state": "done", "report_html": report_html, "reference_number": reference_number})
    if job:
        return jsonify({k: v for k, v in job.items() if k in ("state", "stage", "error")})

    # Nothing on this server's disk: either the server restarted (the job is
    # recorded in storage, so it can be picked up again), or it's long gone.
    status = _resume_count_job(submission_id) if r2_storage.is_configured() else None
    if not status:
        return jsonify({"state": "error",
                        "error": "We lost track of this page count (the server may have restarted). Please try again."})
    if status.get("status") == "done":
        report_html = history.get_report(submission_id)
        if report_html:
            return jsonify({"state": "done", "report_html": report_html, "reference_number": reference_number})
        return jsonify({"state": "error", "error": "The report went missing - please try again."})
    if status.get("status") == "failed":
        return jsonify({"state": "error",
                        "error": "We couldn't finish counting these files. Please try again, or send them "
                                 "over for a quote and we'll count them for you."})
    return jsonify({"state": "processing",
                    "stage": "Picking up where we left off (the server restarted)..."})


# --------------------------------------------------------------------------
# Background work for the R2 route
# --------------------------------------------------------------------------

_running_jobs = set()
_running_lock = threading.Lock()


def _save_status(status):
    status["updated_at"] = _now_iso()
    r2_storage.put_bytes(_status_key(status["submission_id"]),
                         json.dumps(status).encode(), "application/json")
    history.save(_record_from_status(status))


HISTORY_FIELDS = ("submission_id", "reference_number", "contact", "client_note", "created_at", "updated_at",
                  "status", "stage", "error", "file_count", "total_items", "flagged", "sizes",
                  "transfer_link", "has_report", "plans_to_scale", "sheets", "tabs", "dividers",
                  "folders_needed")


def _record_from_status(status):
    record = {k: status[k] for k in HISTORY_FIELDS if k in status}
    record["kind"] = status.get("kind", "quote")
    return record


def _progress_updater(status):
    """Returns update(text, force=False): records what a quote job is doing
    right now (e.g. "Counting pages (120 of 433)") in the log and on the
    attention page. Written at most every PROGRESS_SAVE_SECONDS unless
    `force`, so hundreds of files don't mean hundreds of storage writes."""
    lock = threading.Lock()
    last = [0.0]
    label = f"Submission {status['submission_id']} (ref {status['reference_number']})"

    def update(text, force=False):
        with lock:
            status["stage"] = text
            now = time.time()
            if not force and now - last[0] < PROGRESS_SAVE_SECONDS:
                return
            last[0] = now
            logger.info(f"{label}: {text}")
            try:
                _save_status(status)
            except Exception as e:
                logger.warning(f"{label}: couldn't save progress - {e}")
    return update


def _load_status(submission_id):
    data = r2_storage.get_bytes(_status_key(submission_id))
    return json.loads(data) if data else None


def _job_path(submission_id):
    return os.path.join(JOBS_DIR, f"{submission_id}.json")


def _write_job(submission_id, **fields):
    path = _job_path(submission_id)
    job = _read_job(submission_id) or {}
    job.update(fields)
    job["updated_at"] = _now_iso()
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump(job, f)
    os.replace(tmp, path)


def _read_job(submission_id):
    try:
        with open(_job_path(submission_id)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _download_submission(submission_id, dest_dir, on_progress=None, indices_out=None):
    """Pulls every uploaded file down from R2 (a few at a time). Returns local
    paths in upload order, using the client's original file names. If given a
    list as `indices_out`, fills it with each file's position in the upload."""
    objects = sorted(r2_storage.list_objects(_files_prefix(submission_id)), key=lambda o: o["key"])
    if not objects:
        raise r2_storage.StorageError("no uploaded files were found in storage")
    os.makedirs(dest_dir, exist_ok=True)
    dests = []
    for obj in objects:
        original = re.sub(r"^\d{5}-", "", obj["key"].rsplit("/", 1)[-1])
        dest = _unique_dest(dest_dir, original)
        open(dest, "wb").close()  # reserve the name before downloading in parallel
        dests.append(dest)
        if indices_out is not None:
            m = re.match(r"^(\d{5})-", obj["key"].rsplit("/", 1)[-1])
            indices_out.append(int(m.group(1)) if m else -1)

    done = [0]
    lock = threading.Lock()

    def fetch(pair):
        r2_storage.download(pair[0]["key"], pair[1])
        with lock:
            done[0] += 1
            if on_progress:
                on_progress(done[0], len(objects))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(fetch, zip(objects, dests)))
    return dests


def _save_status_safe(status):
    """Saves the job's record, but never raises: losing the record is worth a
    log line, not a failed job."""
    try:
        _save_status(status)
        return True
    except Exception as e:
        logger.warning(f"Submission {status.get('submission_id')}: couldn't save job record - {e}")
        return False


def _key_index(key):
    """The upload position from a stored file's name ("00007-plan.pdf" -> 7)."""
    m = re.match(r"^(\d{5})-", key.rsplit("/", 1)[-1])
    return int(m.group(1)) if m else -1


def _original_name(key):
    return re.sub(r"^\d{5}-", "", key.rsplit("/", 1)[-1])


def _count_report(saved_paths, submission_id, client_note, contact, reference_number, on_file=None,
                  folder_info=None):
    """Returns (report html, rows). Used by the direct upload route, where the
    files are already on disk."""
    all_rows = []
    for i, path in enumerate(saved_paths, start=1):
        if on_file:
            on_file(i, len(saved_paths))
        all_rows.extend(analyze_file(path, os.path.basename(path)))
    html_body = rows_to_html(all_rows, submission_id, client_note, contact=contact,
                             count_only=True, reference_number=reference_number, folder_info=folder_info)
    return html_body, all_rows


def _new_count_status(submission_id, reference_number, contact, client_note, folders, portal_url):
    return {
        "submission_id": submission_id,
        "reference_number": reference_number,
        "kind": "count",
        "status": "processing",
        "contact": contact,
        "client_note": client_note,
        "folders": folders,
        "portal_url": portal_url,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "attempts": 0,
        "error": "",
        "stage": "Fetching your files...",
        "file_count": None,
        "total_items": None,
        "flagged": None,
        "sizes": [],
        "sheets": None,
        "tabs": None,
        "dividers": None,
        "folders_needed": None,
        "has_report": False,
    }


def _send_count_notice(status, totals):
    """Tells Kaye that someone used the self-service page count: who they are
    and the totals, nothing else. No file list, and no download link - the
    files were never sent to us."""
    try:
        contact = status.get("contact", {})
        ref = status.get("reference_number", "?")
        rows = "".join(
            f"<tr><td style='padding:3px 10px 3px 0'><b>{FIELD_LABELS[k]}</b></td>"
            f"<td>{html_lib.escape(contact.get(k, ''))}</td></tr>"
            for k in FIELD_LABELS if contact.get(k))
        figures = [("Pages / items", status.get("total_items")), ("Sheets of paper", status.get("sheets")),
                   ("Folders", status.get("folders_needed")), ("Tabs", status.get("tabs")),
                   ("Dividers", status.get("dividers")), ("Files", status.get("file_count"))]
        totals_rows = "".join(
            f"<tr><td style='padding:3px 10px 3px 0'><b>{label}</b></td><td>{value:,}</td></tr>"
            for label, value in figures if isinstance(value, int))
        note = status.get("client_note")
        body = (f"<p><b>Self-service page count</b> - this client counted their own pages. "
                f"Nothing was sent to us and their files have been deleted.</p>"
                f"<h3>Client details</h3><table>{rows}</table>"
                + (f"<p><b>Client note:</b> {html_lib.escape(note)}</p>" if note else "")
                + f"<h3>Totals</h3><table>{totals_rows}</table>"
                + f"<p style='font-size:12px;color:#777'>{APP_NAME} V{VERSION}. "
                  f"This count is also in {html_lib.escape(status.get('portal_url', ''))}/admin/submissions.</p>")
        subject = (f"[{APP_NAME} page count {ref}] {contact.get('subject') or ''} - "
                   f"{status.get('file_count') or 0} file(s)")
        sent, message = send_report_email(
            subject=subject,
            html_body=f"<html><body style='font-family:Arial,sans-serif'>{body}</body></html>",
            csv_attachment_name=f"page_count_{ref}.csv",
            csv_attachment_bytes=("Page count," + str(ref) + "\n"
                                  + "\n".join(f"{label},{value}" for label, value in figures
                                               if isinstance(value, int)) + "\n").encode("utf-8"),
            original_files=[],
            reports_dir=REPORTS_DIR,
        )
        logger.info(f"Page count notice for {ref}: sent={sent} - {message}")
    except Exception as e:
        logger.warning(f"Couldn't send the page count notice: {e}")


def _inside_zip_progress(stage, file_number, file_total, file_name):
    """A job is often a single ZIP with everything in it, so progress has to
    come from inside the archive too - otherwise it sits on "1 of 1" for an
    hour. Updated every few files, not every one."""
    last = [0.0]

    def on_item(position, items, _name):
        if items < 2:
            return
        now = time.time()
        if position != items and now - last[0] < 3:
            return
        last[0] = now
        of_files = f"file {file_number} of {file_total}: " if file_total > 1 else ""
        stage(f"Counting pages ({of_files}{position} of {items} inside {file_name})...")
    return on_item


def _run_count_job(submission_id, status=None):
    """"Count my pages": fetch one file, count it, delete it, then the next.
    Only ever one file on disk, and memory has a chance to settle between
    files - a 5 GB job of big drawings could otherwise take the server down.

    The job is recorded in R2 as well as on disk, so if the server restarts
    part-way the work can be picked up again instead of being lost."""
    with _running_lock:
        if submission_id in _running_jobs:
            return
        _running_jobs.add(submission_id)
    local_dir = os.path.join(UPLOAD_DIR, submission_id)
    finished = False
    try:
        if status is None:
            status = _load_status(submission_id)
        if not status:
            logger.error(f"Count-only {submission_id}: no job record found - nothing to do.")
            _write_job(submission_id, state="error",
                       error="We lost track of this page count. Please try again.")
            return
        ref = status.get("reference_number", "?")
        label = f"Count-only {submission_id} (ref {ref})"
        status["attempts"] = int(status.get("attempts") or 0) + 1
        if status["attempts"] > MAX_JOB_ATTEMPTS:
            logger.error(f"{label}: giving up after {status['attempts'] - 1} attempts.")
            status["status"] = "failed"
            status["error"] = "Counting was interrupted repeatedly."
            _save_status_safe(status)
            _write_job(submission_id, state="error",
                       error=("We couldn't finish counting these files - they may be too large for the "
                              "self-service count. Please send them over for a quote instead, or contact us."))
            finished = True
            return
        _save_status_safe(status)
        _write_job(submission_id, state="processing", stage=status.get("stage") or "Fetching your files...")

        contact = status.get("contact", {})
        client_note = status.get("client_note", "")
        folders = status.get("folders") or []
        objects = sorted(r2_storage.list_objects(_files_prefix(submission_id)), key=lambda o: o["key"])
        if not objects:
            raise r2_storage.StorageError("no uploaded files were found in storage")

        os.makedirs(local_dir, exist_ok=True)
        rows = []
        folder_info = {}
        total = len(objects)
        last_saved = [0.0]

        def stage(text):
            _write_job(submission_id, state="processing", stage=text)
            status["stage"] = text
            if time.time() - last_saved[0] >= PROGRESS_SAVE_SECONDS:
                last_saved[0] = time.time()
                _save_status_safe(status)

        for i, obj in enumerate(objects, start=1):
            name = _original_name(obj["key"])
            size_mb = obj["size"] / 1024 / 1024
            logger.info(f"{label}: file {i} of {total} - {name} ({size_mb:.1f} MB), memory {_rss_mb():.0f} MB")
            stage(f"Counting pages ({i} of {total})...")
            path = _unique_dest(local_dir, name)
            r2_storage.download(obj["key"], path)
            inside = _inside_zip_progress(stage, i, total, name)
            index = _key_index(obj["key"])
            parts = structure.clean_folder_path(folders[index]) if 0 <= index < len(folders) else []
            base = os.path.basename(path)
            folder_info[base] = {"folders": parts, "name": structure.original_name(base, parts)}
            try:
                rows.extend(analyze_file(path, base, on_item=inside))
            finally:
                # One file on disk at a time, whatever happens with this one.
                try:
                    os.remove(path)
                except OSError:
                    pass

        stage("Building your report...")
        logger.info(f"{label}: counted {len(rows)} item(s) from {total} file(s), memory {_rss_mb():.0f} MB")
        html_body = rows_to_html(rows, submission_id, client_note, contact=contact, count_only=True,
                                 reference_number=ref, folder_info=folder_info)
        with open(os.path.join(JOBS_DIR, f"{submission_id}.html"), "w") as f:
            f.write(html_body)
        _write_job(submission_id, state="done", stage="Done")

        totals = job_totals(rows, folder_info)
        status.update(status="done", stage="", file_count=total, total_items=len(rows),
                      flagged=sum(1 for r in rows if r.flagged),
                      sizes=history.sizes_for_record(build_size_summary(rows)),
                      sheets=totals["sheets"], tabs=totals["tabs"], dividers=totals["dividers"],
                      folders_needed=totals["folders_needed"],
                      has_report=history.save_report(submission_id, html_body))
        _save_status_safe(status)
        finished = True
        logger.info(f"{label}: report ready.")
        _send_count_notice(status, totals)
    except Exception as e:
        logger.exception(f"Count-only {submission_id}: failed - {e}")
        _write_job(submission_id, state="error",
                   error="Something went wrong counting your pages. Please try again.")
        if status:
            status["status"] = "failed"
            status["error"] = str(e)[:300]
            _save_status_safe(status)
        finished = True
    finally:
        shutil.rmtree(local_dir, ignore_errors=True)
        with _running_lock:
            _running_jobs.discard(submission_id)
        if finished:
            # Only clear storage once there's nothing left to come back for.
            try:
                r2_storage.delete_prefix(f"incoming/{submission_id}/")
            except Exception as e:
                logger.warning(f"Count-only {submission_id}: couldn't delete files from storage yet - {e}")


def _resume_count_job(submission_id):
    """Starts a page count again after the server restarted mid-job. Called
    when the waiting page checks in and there's no job on this server's disk."""
    status = _load_status(submission_id)
    if not status or status.get("kind") != "count" or status.get("status") not in ("processing", "received"):
        return status
    with _running_lock:
        already = submission_id in _running_jobs
    if not already:
        logger.info(f"Count-only {submission_id}: picking the job up again after a restart.")
        threading.Thread(target=_run_count_job, args=(submission_id,), kwargs={"status": status},
                         daemon=True).start()
    return status

def _run_quote_job(submission_id):
    """Download -> analyse -> TransferNow/WeTransfer link -> email. The copy in
    R2 is only deleted once the originals have definitely reached Kaye via a
    download link; otherwise it's kept and flagged 'needs attention'."""
    with _running_lock:
        if submission_id in _running_jobs:
            return
        _running_jobs.add(submission_id)
    local_dir = os.path.join(UPLOAD_DIR, submission_id)
    stop_heartbeat = threading.Event()
    try:
        status = _load_status(submission_id)
        if not status:
            logger.error(f"Submission {submission_id}: no status record found - nothing to do.")
            return
        ref = status["reference_number"]
        status["status"] = "processing"
        status["attempts"] = int(status.get("attempts") or 0) + 1
        # Every field is created up front: the heartbeat thread saves this same
        # dict, and adding keys while it's being saved would break that save.
        for field, empty in (("stage", ""), ("file_count", None), ("total_items", None), ("flagged", None),
                             ("sizes", []), ("transfer_link", ""), ("has_report", False),
                             ("plans_to_scale", None), ("folders", []), ("sheets", None), ("tabs", None),
                             ("dividers", None), ("folders_needed", None)):
            status.setdefault(field, empty)
        status["stage"] = "Starting"
        _save_status(status)
        progress = _progress_updater(status)

        def heartbeat():
            # Keeps updated_at fresh so the hourly check doesn't think a long
            # job has stalled and start it a second time.
            while not stop_heartbeat.wait(HEARTBEAT_SECONDS):
                try:
                    _save_status(status)
                except Exception:
                    pass
        threading.Thread(target=heartbeat, daemon=True).start()

        try:
            indices = []
            saved_paths = _download_submission(
                submission_id, local_dir,
                on_progress=lambda n, total: progress(f"Fetching files from storage ({n} of {total})",
                                                      force=(n == 1)),
                indices_out=indices)
            folder_info = _folder_info(saved_paths, status.get("folders") or [], indices)
        except Exception as e:
            logger.error(f"Submission {submission_id} (ref {ref}): couldn't fetch files from storage - {e}")
            _mark_needs_attention(status, f"Couldn't fetch the uploaded files from storage: {e}")
            return

        def on_analysed(rows):
            client_copy = rows_to_html(
                rows, submission_id, status.get("client_note", ""), contact=status.get("contact", {}),
                reference_number=ref, title=f"Your quote request - ref {ref}",
                banner="Your copy of what we received for this quote request.",
                folder_info=folder_info, plans_to_scale=status.get("plans_to_scale"))
            totals = job_totals(rows, folder_info)
            status["sheets"] = totals["sheets"]
            status["folders_needed"] = totals["folders_needed"]
            status["tabs"] = totals["tabs"]
            status["dividers"] = totals["dividers"]
            status["file_count"] = len(saved_paths)
            status["total_items"] = len(rows)
            status["flagged"] = sum(1 for r in rows if r.flagged)
            status["sizes"] = history.sizes_for_record(build_size_summary(rows))
            status["has_report"] = history.save_report(submission_id, client_copy)

        outcome = {}
        delivered = process_submission(
            submission_id, saved_paths, status.get("client_note", ""), status.get("contact", {}), ref,
            on_link_failed=lambda: _mark_needs_attention(status, "Couldn't create the download link for the "
                                                                 "original files.", send_alert=False),
            on_stage=progress, on_analysed=on_analysed, outcome=outcome,
            folder_info=folder_info, plans_to_scale=status.get("plans_to_scale"))
        if not delivered and status.get("status") != "needs_attention":
            # Crashed somewhere other than the download-link step, so no report
            # email went out - flag it and tell Kaye.
            _mark_needs_attention(status, "Processing failed unexpectedly - the portal's logs have details.")
        if delivered:
            status["status"] = "sent"
            status["stage"] = ""
            status["transfer_link"] = outcome.get("link", "")
            try:
                _save_status(status)  # also updates the history record
            except Exception as e:
                logger.warning(f"Submission {submission_id}: couldn't record completion - {e}")
            try:
                removed = r2_storage.delete_prefix(f"incoming/{submission_id}/")
                logger.info(f"Submission {submission_id} (ref {ref}): done, removed {removed} file(s) from storage.")
            except Exception as e:
                logger.warning(f"Submission {submission_id}: done, but couldn't clear storage yet - {e}")
    except Exception as e:
        logger.exception(f"Submission {submission_id}: job crashed - {e}")
    finally:
        stop_heartbeat.set()
        shutil.rmtree(local_dir, ignore_errors=True)
        with _running_lock:
            _running_jobs.discard(submission_id)


def _mark_needs_attention(status, reason, send_alert=True):
    """Keeps the files in R2 and flags the job. Returns a sentence for the
    report email explaining where the files are."""
    status["status"] = "needs_attention"
    status["error"] = reason
    try:
        _save_status(status)
    except Exception as e:
        logger.error(f"Submission {status['submission_id']}: couldn't flag for attention - {e}")
    link = f"{status.get('portal_url', '')}/admin/attention"
    note = (f"The original files are safe in the portal's storage - open {link} "
            f"to download them or try again.")
    if send_alert:
        contact = status.get("contact", {})
        rows = "".join(f"<tr><td><b>{FIELD_LABELS[k]}</b></td><td>{html_lib.escape(contact.get(k, ''))}</td></tr>"
                       for k in FIELD_LABELS if contact.get(k))
        client_note = status.get("client_note")
        body = (f"<p>Quote request <b>{html_lib.escape(status['reference_number'])}</b> was uploaded, "
                f"but couldn't be processed automatically.</p>"
                f"<p><b>Reason:</b> {html_lib.escape(reason)}</p>"
                f"<p>Nothing is lost: the files are kept in the portal's storage. "
                f"<a href=\"{html_lib.escape(link)}\">Open the attention page</a> to download them or try again.</p>"
                f"<h3>Client details</h3><table>{rows}</table>"
                + (f"<p><b>Client note:</b> {html_lib.escape(client_note)}</p>" if client_note else ""))
        _send_alert(status["reference_number"], "Needs attention - couldn't process automatically", body)
    return note


def _send_alert(reference_number, subject, html_body):
    try:
        sent, message = send_report_email(
            subject=f"[{APP_NAME} quote {reference_number}] {subject}",
            html_body=f"<html><body style='font-family:Arial,sans-serif'>{html_body}</body></html>",
            csv_attachment_name=f"alert_{reference_number}.csv",
            csv_attachment_bytes=f"Quote request,{reference_number}\nStatus,Needs attention\n".encode("utf-8"),
            original_files=[],
            reports_dir=REPORTS_DIR,
        )
        logger.info(f"Alert for {reference_number}: sent={sent} - {message}")
    except Exception as e:
        logger.exception(f"Couldn't send alert for {reference_number}: {e}")


def process_submission(submission_id, saved_paths, client_note, contact, reference_number,
                       on_link_failed=None, on_stage=None, on_analysed=None, outcome=None,
                       folder_info=None, plans_to_scale=None):
    """Analyse, get a download link for the originals, email the report.
    Returns True only if the originals definitely reached Kaye via a link.
    `on_link_failed`, if given, is called when they didn't, and may return a
    sentence to add to the report saying where the files are instead.
    `on_stage(text, force=False)` hears about progress, `on_analysed(rows)`
    gets the page-count results, and `outcome` (a dict) is given the link."""
    stage = on_stage or (lambda text, force=False: None)
    wetransfer_link = None
    try:
        all_rows = []
        total = len(saved_paths)
        for i, path in enumerate(saved_paths, start=1):
            stage(f"Counting pages ({i} of {total})", force=(i == 1))
            try:
                size_mb = os.path.getsize(path) / 1024 / 1024
            except OSError:
                size_mb = 0
            # Big files and every 25th one are logged with the memory in use,
            # so a job that runs the server out of memory can be traced.
            if size_mb >= 20 or i % 25 == 0 or i == total:
                logger.info(f"Submission {submission_id}: counting {i} of {total} - "
                            f"{os.path.basename(path)} ({size_mb:.1f} MB), memory {_rss_mb():.0f} MB")
            inside = _inside_zip_progress(lambda text: stage(text), i, total, os.path.basename(path))
            all_rows.extend(analyze_file(path, os.path.basename(path), on_item=inside))
        stage(f"Counting pages done ({total} of {total})", force=True)
        if on_analysed:
            try:
                on_analysed(all_rows)
            except Exception as e:
                logger.warning(f"Submission {submission_id}: couldn't save history details - {e}")

        wetransfer_error = None
        service = _transfer_service_name()
        try:
            stage(f"Sending to {service} (0 of {total})", force=True)
            wetransfer_link = upload_submission(
                saved_paths, message=_transfer_title(reference_number, contact),
                on_progress=lambda n, t: stage(f"Sending to {service} ({n} of {t})", force=(n == t)))
        except Exception as e:
            wetransfer_error = str(e)
            logger.error(f"Submission {submission_id}: file transfer upload failed - {wetransfer_error}")

        if not wetransfer_link and on_link_failed:
            kept_note = on_link_failed()
            if kept_note:
                wetransfer_error = f"{wetransfer_error}. {kept_note}" if wetransfer_error else kept_note
            on_link_failed = None  # handled - don't run again below

        html_body = rows_to_html(all_rows, submission_id, client_note,
                                 wetransfer_link=wetransfer_link, wetransfer_error=wetransfer_error,
                                 contact=contact, reference_number=reference_number,
                                 folder_info=folder_info, plans_to_scale=plans_to_scale)
        csv_bytes = rows_to_csv(all_rows, folder_info, plans_to_scale).encode("utf-8")
        csv_name = f"analysis_{reference_number}.csv"
        flagged = sum(1 for r in all_rows if r.flagged)
        subject_line = (f"[{APP_NAME} quote {reference_number}] {contact.get('subject') or ''} - "
                        f"{len(saved_paths)} file(s)" + (f" - {flagged} flagged for review" if flagged else "")
                        + {True: " - TO SCALE", False: " - A3 FOLDED", None: ""}[plans_to_scale])
        files_to_attach = [] if wetransfer_link else saved_paths
        if outcome is not None:
            outcome["link"] = wetransfer_link or ""

        stage("Emailing the report", force=True)
        sent, message = send_report_email(
            subject=subject_line,
            html_body=html_body,
            csv_attachment_name=csv_name,
            csv_attachment_bytes=csv_bytes,
            original_files=files_to_attach,
            reports_dir=REPORTS_DIR,
        )
        logger.info(f"Submission {submission_id}: email sent={sent} - {message}")
    except Exception as e:
        logger.exception(f"Failed to process submission {submission_id}: {e}")
        return False  # caller keeps the files and alerts Kaye
    return bool(wetransfer_link)


# ==========================================================================
# Hourly housekeeping
# ==========================================================================

def cleanup_old_submissions():
    """Local tidy-up, plus (with R2) deleting abandoned uploads and restarting
    jobs that were interrupted - e.g. by a redeploy, which stops any work in
    progress on the old server."""
    cutoff = time.time() - RETENTION_DAYS * 86400
    for name in os.listdir(UPLOAD_DIR):
        path = os.path.join(UPLOAD_DIR, name)
        if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
            shutil.rmtree(path, ignore_errors=True)
            logger.info(f"Deleted expired local submission folder: {name}")
    for name in os.listdir(JOBS_DIR):
        path = os.path.join(JOBS_DIR, name)
        if os.path.getmtime(path) < time.time() - 86400:
            os.remove(path)

    if not r2_storage.is_configured():
        return
    groups = {}
    for obj in r2_storage.list_objects("incoming/"):
        parts = obj["key"].split("/")
        if len(parts) >= 3:
            groups.setdefault(parts[1], []).append(obj)

    now = datetime.now(timezone.utc)
    for submission_id, objects in groups.items():
        has_status = any(o["key"] == _status_key(submission_id) for o in objects)
        if not has_status:
            newest = max(o["last_modified"] for o in objects)
            if (now - newest).total_seconds() > ABANDONED_AFTER_HOURS * 3600:
                removed = r2_storage.delete_prefix(f"incoming/{submission_id}/")
                logger.info(f"Deleted abandoned upload {submission_id} ({removed} file(s)).")
            continue
        status = _load_status(submission_id)
        if not status or status.get("status") not in ("received", "processing"):
            continue  # needs_attention stays until Kaye deals with it
        if submission_id in _running_jobs:
            continue
        is_count = status.get("kind") == "count"
        idle = (now - _parse_iso(status.get("updated_at"))).total_seconds()
        if idle < (COUNT_STALLED_AFTER_MINUTES if is_count else STALLED_AFTER_MINUTES) * 60:
            continue
        if is_count:
            # The client may have closed the tab, but the count still finishes
            # and lands in their submission history.
            if int(status.get("attempts") or 0) >= MAX_JOB_ATTEMPTS:
                continue  # _run_count_job marks it failed and clears up
            logger.info(f"Restarting interrupted page count {submission_id} (ref {status['reference_number']}).")
            threading.Thread(target=_run_count_job, args=(submission_id,), kwargs={"status": status},
                             daemon=True).start()
            continue
        if int(status.get("attempts") or 0) >= MAX_JOB_ATTEMPTS:
            _mark_needs_attention(status, f"Processing was interrupted {status['attempts']} times.")
            continue
        logger.info(f"Restarting interrupted job {submission_id} (ref {status['reference_number']}).")
        threading.Thread(target=_run_quote_job, args=(submission_id,), daemon=True).start()


CLEANUP_FIRST_RUN_DELAY = int(os.environ.get("CLEANUP_FIRST_RUN_DELAY", "300"))
_background_started_pid = None
_background_lock = threading.Lock()


def cleanup_loop():
    time.sleep(CLEANUP_FIRST_RUN_DELAY)
    lock_path = os.path.join(BASE_DIR, ".housekeeping.lock")
    while True:
        # Two server workers share this disk; only whichever holds the lock
        # does the housekeeping, so interrupted jobs aren't restarted twice.
        with open(lock_path, "a") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                pass
            else:
                try:
                    cleanup_old_submissions()
                except Exception as e:
                    logger.error(f"Housekeeping error: {e}")
                finally:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
        time.sleep(SWEEP_EVERY_SECONDS)


def _ensure_background_started():
    """Started inside each worker on its first request, not at import:
    starting threads before the server copies the app into its workers can
    leave a worker stuck forever."""
    global _background_started_pid
    if _background_started_pid == os.getpid():
        return
    with _background_lock:
        if _background_started_pid != os.getpid():
            threading.Thread(target=cleanup_loop, daemon=True).start()
            _background_started_pid = os.getpid()


# ==========================================================================
# Admin pages (password = ADMIN_KEY, or GDRIVE_ADMIN_KEY)
# ==========================================================================

def _admin_ok():
    supplied = request.values.get("key", "")
    return bool(ADMIN_KEY) and hmac.compare_digest(supplied, ADMIN_KEY)


@app.route("/admin/storage-check")
def storage_check():
    if not _admin_ok():
        return "Not found.", 404
    started = time.time()
    steps = r2_storage.diagnose()
    all_ok = all(s["ok"] for s in steps)
    logger.info(f"Storage check: {'all OK' if all_ok else 'FAILED'} in {time.time() - started:.1f}s - {steps}")
    return jsonify({"all_ok": all_ok, "total_seconds": round(time.time() - started, 2), "steps": steps})


@app.route("/admin/attention")
def attention():
    if not ADMIN_KEY:
        return "Not found.", 404
    if not _admin_ok():
        return render_template("admin_login.html", wrong=bool(request.values.get("key")), version=VERSION, app_name=APP_NAME)
    if not r2_storage.is_configured():
        return render_template("admin_attention.html", jobs=[], key=request.values["key"], storage_missing=True,
                               version=VERSION, app_name=APP_NAME)
    groups = {}
    for obj in r2_storage.list_objects("incoming/"):
        parts = obj["key"].split("/")
        if len(parts) >= 3:
            groups.setdefault(parts[1], []).append(obj)
    jobs = []
    for submission_id, objects in groups.items():
        if not any(o["key"] == _status_key(submission_id) for o in objects):
            continue
        status = _load_status(submission_id) or {}
        files = [o for o in objects if "/files/" in o["key"]]
        jobs.append({
            "submission_id": submission_id,
            "reference_number": status.get("reference_number", "?"),
            "status": status.get("status", "?"),
            "error": status.get("error", ""),
            "stage": status.get("stage", ""),
            "kind": status.get("kind", "quote"),
            "contact": status.get("contact", {}),
            "updated_at": status.get("updated_at", ""),
            "file_count": len(files),
            "total_mb": round(sum(o["size"] for o in files) / 1024 / 1024, 1),
        })
    jobs.sort(key=lambda j: (j["status"] != "needs_attention", j["updated_at"]), reverse=False)
    return render_template("admin_attention.html", jobs=jobs, key=request.values["key"], storage_missing=False,
                           version=VERSION, app_name=APP_NAME)


@app.route("/admin/attention/files")
def attention_files():
    if not _admin_ok():
        return "Not found.", 404
    submission_id = request.args.get("submission_id", "")
    if not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", submission_id):
        return "Not found.", 404
    status = _load_status(submission_id) or {}
    files = []
    for obj in sorted(r2_storage.list_objects(_files_prefix(submission_id)), key=lambda o: o["key"]):
        name = re.sub(r"^\d{5}-", "", obj["key"].rsplit("/", 1)[-1])
        disposition = f"attachment; filename*=UTF-8''{r2_storage._uri_encode(name)}"
        files.append({"name": name, "mb": round(obj["size"] / 1024 / 1024, 2),
                      "url": r2_storage.presigned_url("GET", obj["key"], 3600,
                                                      extra_query={"response-content-disposition": disposition})})
    return render_template("admin_files.html", files=files, key=request.args["key"],
                           reference_number=status.get("reference_number", "?"), submission_id=submission_id)


@app.route("/admin/attention/retry", methods=["POST"])
def attention_retry():
    if not _admin_ok():
        return "Not found.", 404
    submission_id = request.form.get("submission_id", "")
    status = _load_status(submission_id) if re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", submission_id) else None
    if status:
        status["status"] = "received"
        status["attempts"] = 0
        status["error"] = ""
        _save_status(status)
        threading.Thread(target=_run_quote_job, args=(submission_id,), daemon=True).start()
        logger.info(f"Retry requested for {submission_id} (ref {status['reference_number']}).")
    return redirect(url_for("attention", key=request.form["key"]))


@app.route("/admin/attention/delete", methods=["POST"])
def attention_delete():
    if not _admin_ok():
        return "Not found.", 404
    submission_id = request.form.get("submission_id", "")
    if re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", submission_id):
        status = _load_status(submission_id)
        if status:
            status["status"] = "files_deleted"
            status["stage"] = ""
            status["updated_at"] = _now_iso()
            history.save(_record_from_status(status))
        removed = r2_storage.delete_prefix(f"incoming/{submission_id}/")
        logger.info(f"Deleted {submission_id} from storage on request ({removed} object(s)).")
    return redirect(url_for("attention", key=request.form["key"]))


# ==========================================================================
# Client submission history - no accounts, just a private emailed link
# ==========================================================================

def _history_link(portal_url, email):
    params = history.make_link_params(_token_secret(), history.email_key(email))
    return f"{portal_url}/my-submissions?{urlencode(params)}"


def _email_wrapper(inner_html):
    return ("<html><body style=\"font-family:Arial,Helvetica,sans-serif;color:#222;line-height:1.5;"
            "max-width:560px\">" + inner_html + "</body></html>")


def _button(url, text):
    return (f'<p style="margin:22px 0"><a href="{html_lib.escape(url)}" style="display:inline-block;'
            f'background:#2980b9;color:#fff;padding:10px 18px;border-radius:6px;text-decoration:none;'
            f'font-weight:bold">{html_lib.escape(text)}</a></p>')


def _send_confirmation_email(contact, reference_number, portal_url, plans_to_scale=None):
    """Tells the client their quote request arrived, with their reference
    number and a link to their submission history. Never raises."""
    if not history.is_enabled():
        return
    try:
        first_name = (contact.get("full_name") or "").split(" ")[0]
        link = _history_link(portal_url, contact.get("email"))
        subject_line = contact.get("subject") or "your quote request"
        body = _email_wrapper(
            f"<p>Hi {html_lib.escape(first_name) or 'there'},</p>"
            f"<p>Thanks - we've received the files for <b>{html_lib.escape(subject_line)}</b>. "
            f"Your reference number is <b>{html_lib.escape(reference_number)}</b>. "
            f"We'll be in touch with your quote shortly.</p>"
            + ({True: "<p>You asked for plans to be <b>printed to scale</b>.</p>",
                False: "<p>You didn't tick <b>plans printed to scale</b>, so plans will be printed at A3 and "
                       "folded. If that's not right, just reply to let us know.</p>",
                None: ""}[plans_to_scale]) +
            f"<p>You can see everything you've sent us from this email address here:</p>"
            + _button(link, "View my submissions") +
            f"<p style=\"font-size:12px;color:#777\">This link is private to you and works for "
            f"{history.LINK_VALID_DAYS} days. You can get a new one any time at "
            f"{html_lib.escape(portal_url)}/my-submissions</p>")
        sent, message = send_client_email(contact.get("email"),
                                          f"{APP_NAME}: we've received your files - ref {reference_number}", body)
        logger.info(f"Confirmation email for {reference_number}: sent={sent} - {message}")
    except Exception as e:
        logger.warning(f"Confirmation email for {reference_number} failed: {e}")


CLIENT_STATUS_LABELS = {
    "count": {"processing": "Counting", "done": "Page count complete", "failed": "Didn't finish - please try again"},
    "quote": {},  # quotes always show "Received" - the internal steps are ours to worry about
}


def _client_view(record, link_params):
    kind = record.get("kind", "quote")
    label = CLIENT_STATUS_LABELS.get(kind, {}).get(record.get("status"), "Received")
    report_url = None
    if record.get("has_report"):
        report_url = url_for("my_submission_report", sid=record["submission_id"], **link_params)
    contact = record.get("contact") or {}
    return {
        "reference_number": record.get("reference_number", ""),
        "date": (record.get("created_at") or "")[:10],
        "kind": "Page count" if kind == "count" else "Quote request",
        "subject": contact.get("subject", ""),
        "status": label,
        "file_count": record.get("file_count"),
        "total_items": record.get("total_items"),
        "sheets": record.get("sheets"),
        "folders_needed": record.get("folders_needed"),
        "tabs": record.get("tabs"),
        "dividers": record.get("dividers"),
        "plans": {True: "Plans printed to scale", False: "Plans printed at A3 and folded"}.get(record.get("plans_to_scale"), ""),
        "sizes": record.get("sizes") or [],
        "report_url": report_url,
    }


_link_request_times = {}
_link_request_lock = threading.Lock()
LINK_REQUEST_GAP_SECONDS = 120
LINK_REQUESTS_PER_IP_PER_HOUR = 10


def _link_request_allowed(email_key):
    """Stops the 'email me a link' form being used to flood someone's inbox."""
    now = time.time()
    ip = request.remote_addr or "?"
    with _link_request_lock:
        for k in [k for k, v in _link_request_times.items() if now - max(v) > 3600]:
            del _link_request_times[k]
        ip_times = [t for t in _link_request_times.get(("ip", ip), []) if now - t < 3600]
        last_for_email = _link_request_times.get(("email", email_key), [0])[-1]
        if len(ip_times) >= LINK_REQUESTS_PER_IP_PER_HOUR or now - last_for_email < LINK_REQUEST_GAP_SECONDS:
            return False
        _link_request_times[("ip", ip)] = ip_times + [now]
        _link_request_times[("email", email_key)] = [now]
        return True


@app.route("/my-submissions", methods=["GET"])
def my_submissions():
    if not history.is_enabled():
        return _no_store(Response(render_template("my_submissions.html", version=VERSION, app_name=APP_NAME, mode="unavailable")))
    c, x, t = request.args.get("c"), request.args.get("x"), request.args.get("t")
    if not (c or x or t):
        return _no_store(Response(render_template("my_submissions.html", version=VERSION, app_name=APP_NAME, mode="ask")))
    ok, reason = history.check_link_params(_token_secret(), c, x, t)
    if not ok:
        return _no_store(Response(render_template("my_submissions.html", version=VERSION, app_name=APP_NAME, mode="ask", link_problem=reason)))
    try:
        records = history.list_for_client(c)
    except Exception as e:
        logger.error(f"History: couldn't list submissions - {e}")
        return _no_store(Response(render_template("my_submissions.html", version=VERSION, app_name=APP_NAME, mode="error"), status=503))
    link_params = {"c": c, "x": x, "t": t}
    expires = datetime.fromtimestamp(int(x), timezone.utc).strftime("%d %B %Y")
    return _no_store(Response(render_template(
        "my_submissions.html", version=VERSION, app_name=APP_NAME, mode="list", expires=expires,
        submissions=[_client_view(r, link_params) for r in records])))


@app.route("/my-submissions", methods=["POST"])
def my_submissions_request_link():
    if not history.is_enabled():
        return redirect(url_for("my_submissions"))
    email = (request.form.get("email") or "").strip()
    if "@" in email and len(email) <= 254:
        key = history.email_key(email)
        if _link_request_allowed(key):
            threading.Thread(target=_send_history_link, args=(email, key, request.host_url.rstrip("/")),
                             daemon=True).start()
        else:
            logger.info("History link request skipped (too soon since the last one).")
    # The same reply whether or not that address has submissions, so the form
    # can't be used to find out who's a client.
    return _no_store(Response(render_template("my_submissions.html", version=VERSION, app_name=APP_NAME, mode="sent")))


def _send_history_link(email, key, portal_url):
    try:
        if not any(True for _ in r2_storage.list_objects(f"history/clients/{key}/")):
            logger.info("History link requested for an address with no submissions - nothing sent.")
            return
        link = _history_link(portal_url, email)
        body = _email_wrapper(
            "<p>Hi,</p><p>Here's your private link to see everything you've sent us from this email address:</p>"
            + _button(link, "View my submissions") +
            f"<p style=\"font-size:12px;color:#777\">The link works for {history.LINK_VALID_DAYS} days. "
            f"If you didn't ask for it, you can ignore this email.</p>")
        sent, message = send_client_email(email, f"{APP_NAME}: your submissions link", body)
        logger.info(f"History link email: sent={sent} - {message}")
    except Exception as e:
        logger.warning(f"History link email failed: {e}")


@app.route("/my-submissions/report")
def my_submission_report():
    c, x, t, sid = (request.args.get(k) for k in ("c", "x", "t", "sid"))
    ok, _reason = history.check_link_params(_token_secret(), c, x, t)
    if not ok or not re.fullmatch(SUBMISSION_ID_RE, sid or "") or not history.is_enabled():
        return redirect(url_for("my_submissions"))
    if not history.client_owns(c, sid):
        return "Not found.", 404
    report_html = history.get_report(sid)
    if not report_html:
        return "That report isn't available.", 404
    return _no_store(Response(report_html, mimetype="text/html"))


# ==========================================================================
# Admin: every submission
# ==========================================================================

ADMIN_STATUS_LABELS = {
    "received": "Received", "processing": "Processing", "needs_attention": "Needs attention",
    "sent": "Sent", "files_deleted": "Files deleted by admin",
    "done": "Counted", "failed": "Count failed",
}


@app.route("/admin/submissions")
def admin_submissions():
    if not ADMIN_KEY:
        return "Not found.", 404
    if not _admin_ok():
        return render_template("admin_login.html", wrong=bool(request.values.get("key")),
                               action=url_for("admin_submissions"), version=VERSION, app_name=APP_NAME)
    key = request.values["key"]
    if not history.is_enabled():
        return render_template("admin_submissions.html", key=key, storage_missing=True, rows=[], q="",
                               kind="", total=0, page=1, pages=1, version=VERSION, app_name=APP_NAME)
    q = (request.args.get("q") or "").strip()
    kind = request.args.get("kind") or ""
    try:
        records = history.list_all()
    except Exception as e:
        logger.error(f"Admin submissions: couldn't list history - {e}")
        return "Couldn't load submissions from storage just now - please refresh in a minute.", 503
    if kind in ("quote", "count"):
        records = [r for r in records if r.get("kind") == kind]
    if q:
        needle = q.lower()

        def matches(r):
            c = r.get("contact") or {}
            hay = " ".join(str(v) for v in (r.get("reference_number"), c.get("email"), c.get("company"),
                                             c.get("full_name"), c.get("subject"), c.get("phone")))
            return needle in hay.lower()
        records = [r for r in records if matches(r)]
    per_page = 100
    pages = max(1, (len(records) + per_page - 1) // per_page)
    page_arg = request.args.get("page") or "1"
    page = min(max(1, int(page_arg) if page_arg.isdigit() else 1), pages)
    rows = []
    for r in records[(page - 1) * per_page: page * per_page]:
        rows.append(dict(r, status_label=ADMIN_STATUS_LABELS.get(r.get("status"), r.get("status") or "?"),
                         created=(r.get("created_at") or "")[:16].replace("T", " ")))
    return render_template("admin_submissions.html", key=key, storage_missing=False, rows=rows, q=q, kind=kind,
                           total=len(records), page=page, pages=pages, version=VERSION, app_name=APP_NAME)


@app.route("/admin/submissions/report")
def admin_submission_report():
    if not _admin_ok():
        return "Not found.", 404
    sid = request.args.get("sid", "")
    if not re.fullmatch(SUBMISSION_ID_RE, sid):
        return "Not found.", 404
    report_html = history.get_report(sid)
    if not report_html:
        return "That report isn't available.", 404
    return _no_store(Response(report_html, mimetype="text/html"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
