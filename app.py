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

from flask import Flask, request, jsonify, render_template, redirect, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from analyzer import analyze_file
from report import rows_to_csv, rows_to_html
from emailer import send_report_email
from file_transfer import upload_submission
from reference_number import next_reference_number
import r2_storage

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
MAX_JOB_ATTEMPTS = 3
HEARTBEAT_SECONDS = 600
SWEEP_EVERY_SECONDS = 3600

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
    """Any file type is accepted. Types the analyser can't count pages for
    still go to Kaye in the download link, marked 'check manually'."""
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


# ==========================================================================
# Pages
# ==========================================================================

@app.before_request
def _start_background_jobs():
    _ensure_background_started()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ==========================================================================
# Direct route: one request with the files in it. Used when R2 isn't set up.
# ==========================================================================

@app.route("/upload", methods=["POST"])
def upload():
    uploaded_files = request.files.getlist("files")
    client_note = request.form.get("client_note", "").strip()
    count_only = request.form.get("count_only", "").strip().lower() in ("1", "true", "on", "yes")
    contact = _contact_from(request.form)

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
            html_body = _count_report(saved_paths, submission_id, client_note, contact, reference_number)
        except Exception as e:
            logger.exception(f"Count-only submission {submission_id} failed: {e}")
            return jsonify({"error": "Something went wrong analysing your files. Please try again."}), 500
        finally:
            shutil.rmtree(submission_dir, ignore_errors=True)
        return jsonify({"status": "counted", "submission_id": submission_id,
                        "reference_number": reference_number, "report_html": html_body})

    threading.Thread(target=process_submission,
                     args=(submission_id, saved_paths, client_note, contact, reference_number),
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
    error = _validate_contact(contact, count_only)
    if error:
        return jsonify({"error": error}), 400

    if count_only:
        _write_job(submission_id, state="processing", stage="Fetching your files...")
        threading.Thread(target=_run_count_job,
                         args=(submission_id, reference_number, contact, client_note), daemon=True).start()
        return jsonify({"status": "processing", "submission_id": submission_id,
                        "reference_number": reference_number})

    status = {
        "submission_id": submission_id,
        "reference_number": reference_number,
        "status": "received",
        "contact": contact,
        "client_note": client_note,
        "portal_url": request.host_url.rstrip("/"),
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "attempts": 0,
        "error": "",
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
    return jsonify({"status": "received", "submission_id": submission_id, "reference_number": reference_number})


@app.route("/upload/status")
def upload_status():
    """The page checks this every couple of seconds during 'count my pages'."""
    submission_id = request.args.get("submission_id")
    reference_number = request.args.get("reference_number")
    if not _check_token(submission_id, reference_number, request.args.get("token")):
        return jsonify({"state": "error", "error": "Invalid submission details - please start again."}), 400
    job = _read_job(submission_id)
    if not job:
        return jsonify({"state": "error",
                        "error": "We lost track of this page count (the server may have restarted). Please try again."})
    if job.get("state") == "done":
        report_path = os.path.join(JOBS_DIR, f"{submission_id}.html")
        try:
            with open(report_path) as f:
                report_html = f.read()
        except OSError:
            return jsonify({"state": "error", "error": "The report went missing - please try again."})
        return jsonify({"state": "done", "report_html": report_html, "reference_number": reference_number})
    return jsonify({k: v for k, v in job.items() if k in ("state", "stage", "error")})


# --------------------------------------------------------------------------
# Background work for the R2 route
# --------------------------------------------------------------------------

def _save_status(status):
    status["updated_at"] = _now_iso()
    r2_storage.put_bytes(_status_key(status["submission_id"]),
                         json.dumps(status).encode(), "application/json")


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


def _download_submission(submission_id, dest_dir, on_progress=None):
    """Pulls every uploaded file down from R2 (a few at a time). Returns local
    paths in upload order, using the client's original file names."""
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


def _count_report(saved_paths, submission_id, client_note, contact, reference_number):
    all_rows = []
    for path in saved_paths:
        all_rows.extend(analyze_file(path, os.path.basename(path)))
    return rows_to_html(all_rows, submission_id, client_note, contact=contact,
                        count_only=True, reference_number=reference_number)


def _run_count_job(submission_id, reference_number, contact, client_note):
    local_dir = os.path.join(UPLOAD_DIR, submission_id)
    try:
        saved = _download_submission(
            submission_id, local_dir,
            on_progress=lambda n, total: _write_job(submission_id, stage=f"Fetching your files ({n} of {total})..."))
        _write_job(submission_id, stage="Counting pages...")
        html_body = _count_report(saved, submission_id, client_note, contact, reference_number)
        with open(os.path.join(JOBS_DIR, f"{submission_id}.html"), "w") as f:
            f.write(html_body)
        _write_job(submission_id, state="done", stage="Done")
        logger.info(f"Count-only {submission_id} (ref {reference_number}): report ready.")
    except Exception as e:
        logger.exception(f"Count-only {submission_id}: failed - {e}")
        _write_job(submission_id, state="error",
                   error="Something went wrong counting your pages. Please try again.")
    finally:
        shutil.rmtree(local_dir, ignore_errors=True)
        try:
            r2_storage.delete_prefix(f"incoming/{submission_id}/")
        except Exception as e:
            logger.warning(f"Count-only {submission_id}: couldn't delete files from storage yet - {e}")


_running_jobs = set()
_running_lock = threading.Lock()


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
        _save_status(status)

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
            saved_paths = _download_submission(submission_id, local_dir)
        except Exception as e:
            logger.error(f"Submission {submission_id} (ref {ref}): couldn't fetch files from storage - {e}")
            _mark_needs_attention(status, f"Couldn't fetch the uploaded files from storage: {e}")
            return

        delivered = process_submission(
            submission_id, saved_paths, status.get("client_note", ""), status.get("contact", {}), ref,
            on_link_failed=lambda: _mark_needs_attention(status, "Couldn't create the download link for the "
                                                                 "original files.", send_alert=False))
        if not delivered and status.get("status") != "needs_attention":
            # Crashed somewhere other than the download-link step, so no report
            # email went out - flag it and tell Kaye.
            _mark_needs_attention(status, "Processing failed unexpectedly - the portal's logs have details.")
        if delivered:
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
            subject=f"[Quote request {reference_number}] {subject}",
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
                       on_link_failed=None):
    """Analyse, get a download link for the originals, email the report.
    Returns True only if the originals definitely reached Kaye via a link.
    `on_link_failed`, if given, is called when they didn't, and may return a
    sentence to add to the report saying where the files are instead."""
    wetransfer_link = None
    try:
        all_rows = []
        for path in saved_paths:
            all_rows.extend(analyze_file(path, os.path.basename(path)))

        wetransfer_error = None
        try:
            wetransfer_link = upload_submission(saved_paths, message=f"Quote request {reference_number}")
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
                                 contact=contact, reference_number=reference_number)
        csv_bytes = rows_to_csv(all_rows).encode("utf-8")
        csv_name = f"analysis_{reference_number}.csv"
        flagged = sum(1 for r in all_rows if r.flagged)
        subject_line = (f"[Quote request {reference_number}] {contact.get('subject') or ''} - "
                        f"{len(saved_paths)} file(s)" + (f" - {flagged} flagged for review" if flagged else ""))
        files_to_attach = [] if wetransfer_link else saved_paths

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
        idle = (now - _parse_iso(status.get("updated_at"))).total_seconds()
        if idle < STALLED_AFTER_MINUTES * 60:
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
        return render_template("admin_login.html", wrong=bool(request.values.get("key")))
    if not r2_storage.is_configured():
        return render_template("admin_attention.html", jobs=[], key=request.values["key"], storage_missing=True)
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
            "contact": status.get("contact", {}),
            "updated_at": status.get("updated_at", ""),
            "file_count": len(files),
            "total_mb": round(sum(o["size"] for o in files) / 1024 / 1024, 1),
        })
    jobs.sort(key=lambda j: (j["status"] != "needs_attention", j["updated_at"]), reverse=False)
    return render_template("admin_attention.html", jobs=jobs, key=request.values["key"], storage_missing=False)


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
        removed = r2_storage.delete_prefix(f"incoming/{submission_id}/")
        logger.info(f"Deleted {submission_id} from storage on request ({removed} object(s)).")
    return redirect(url_for("attention", key=request.form["key"]))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
