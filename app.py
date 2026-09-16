"""
Client file-upload portal.

Clients drag & drop PDF / JPG / ZIP / PPTX / XLSX files. Files are stored,
analysed in the background (page/slide/sheet counts + physical sizes,
flagging anything non-standard), and a report is emailed automatically
to whoever REPORT_RECIPIENT is set to (see emailer.py / README.md).

Two ways files get here:
  - The legacy path (`POST /upload`, one multipart request) - simple, and
    still used automatically whenever Google Drive isn't configured (see
    below), including local testing.
  - The Drive-relay path (`POST /upload/init` then the browser PUTs
    straight to Google Drive, then `POST /upload/finalize`) - used whenever
    Drive IS configured, because Render sits behind a Cloudflare edge with
    its own hard upload size limit that has nothing to do with this app.
    Routing large files through Drive first, then pulling them back down
    server-side just to analyse them, gets around that edge entirely. See
    gdrive_upload.py and README.md "Large files: Google Drive bypass".

Run locally:
    python3 app.py
Then open http://localhost:5000
"""

import os
import time
import uuid
import shutil
import logging
import threading
from datetime import datetime, timedelta

from flask import Flask, request, jsonify, render_template, redirect, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from analyzer import analyze_file, SUPPORTED_EXTENSIONS
from report import rows_to_csv, rows_to_html
from emailer import send_report_email
from file_transfer import upload_submission, FileTransferError
from reference_number import next_reference_number
import gdrive_upload

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("portal")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)

MAX_CONTENT_LENGTH = 5 * 1024 * 1024 * 1024  # 5GB per submission
RETENTION_DAYS = 30

GDRIVE_ADMIN_KEY = os.environ.get("GDRIVE_ADMIN_KEY")

# Fields the client-facing form always requires, and the extra two that are
# only required for a real quote submission (not for a "just count my pages"
# check).
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
# Render (like most PaaS hosts) terminates HTTPS in front of the app and
# forwards plain HTTP internally - without this, Flask can't tell the
# original request was HTTPS, and url_for(..., _external=True) below (used
# to build the Google OAuth redirect URL) would generate an http:// link
# that Google rejects as insecure.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def cleanup_old_submissions():
    """Delete submission folders older than RETENTION_DAYS."""
    cutoff = time.time() - RETENTION_DAYS * 86400
    if not os.path.isdir(UPLOAD_DIR):
        return
    for name in os.listdir(UPLOAD_DIR):
        path = os.path.join(UPLOAD_DIR, name)
        if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
            shutil.rmtree(path, ignore_errors=True)
            logger.info(f"Deleted expired submission folder: {name}")

    if gdrive_upload.is_configured():
        try:
            removed = gdrive_upload.delete_abandoned_folders(older_than_hours=24)
            if removed:
                logger.info(f"Deleted {removed} abandoned Google Drive upload folder(s).")
        except Exception as e:  # network blips etc. - just try again tomorrow
            logger.error(f"Drive abandoned-folder cleanup failed: {e}")


def cleanup_loop():
    while True:
        try:
            cleanup_old_submissions()
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        time.sleep(24 * 3600)  # once a day


def _validate_contact(contact, count_only):
    """Returns an error message string, or None if everything required is present."""
    required_fields = list(REQUIRED_FIELDS_ALWAYS)
    if not count_only:
        required_fields += REQUIRED_FIELDS_FULL_SUBMISSION
    missing = [FIELD_LABELS[f] for f in required_fields if not contact.get(f)]
    if missing:
        return f"Missing required field(s): {', '.join(missing)}"
    return None


def _validate_file_names(names):
    """Returns an error message string, or None if the file list is OK.

    Any file type is accepted. Types the analyser can't count pages for
    (e.g. .msg emails, .ai, .indd) still go to Kaye in the download link and
    are listed in the report as 'not analysed - check manually', rather than
    turning the client away."""
    if not names or not any(n.strip() for n in names):
        return "No files received."
    return None


def _unique_dest(directory, filename):
    """A path in `directory` for `filename` that doesn't overwrite anything
    already saved there - so two files with the same name (e.g. from
    different subfolders) both survive, as 'plan.pdf' and 'plan (2).pdf'."""
    safe = os.path.basename(filename).strip() or "file"
    stem, ext = os.path.splitext(safe)
    candidate, n = safe, 2
    while os.path.exists(os.path.join(directory, candidate)):
        candidate = f"{stem} ({n}){ext}"
        n += 1
    return os.path.join(directory, candidate)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# --------------------------------------------------------------------------
# Legacy path: one multipart POST, file bytes go straight through this app.
# Used automatically whenever Google Drive isn't configured (is_configured()
# below is False) - see /upload/init, which is what the front-end actually
# calls first to decide which path to use.
# --------------------------------------------------------------------------

@app.route("/upload", methods=["POST"])
def upload():
    uploaded_files = request.files.getlist("files")
    client_note = request.form.get("client_note", "").strip()
    count_only = request.form.get("count_only", "").strip().lower() in ("1", "true", "on", "yes")
    contact = {field: request.form.get(field, "").strip() for field in FIELD_LABELS}

    error = _validate_contact(contact, count_only)
    if error:
        return jsonify({"error": error}), 400

    error = _validate_file_names([f.filename for f in uploaded_files])
    if error:
        return jsonify({"error": error}), 400

    submission_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    submission_dir = os.path.join(UPLOAD_DIR, submission_id)
    os.makedirs(submission_dir, exist_ok=True)

    saved_paths = []
    for f in uploaded_files:
        dest = _unique_dest(submission_dir, f.filename)
        f.save(dest)
        saved_paths.append(dest)

    reference_number = next_reference_number()

    logger.info(f"Submission {submission_id} (ref {reference_number}): "
                f"{len(saved_paths)} file(s) received via legacy upload (count_only={count_only}).")

    return _handle_received_files(submission_id, saved_paths, client_note, contact,
                                   reference_number, count_only, on_done=None)


# --------------------------------------------------------------------------
# Drive-relay path: the browser gets an upload URL per file from /init, PUTs
# the bytes straight to Google (never touching this app or Render/Cloudflare's
# edge), then calls /finalize once done. See gdrive_upload.py for why.
# --------------------------------------------------------------------------

@app.route("/upload/init", methods=["POST"])
def upload_init():
    data = request.get_json(silent=True) or {}
    count_only = bool(data.get("count_only"))
    contact = {field: (data.get(field) or "").strip() for field in FIELD_LABELS}
    files = data.get("files") or []  # [{"name": "...", "size": 12345}, ...]
    file_names = [f.get("name", "") for f in files if isinstance(f, dict)]

    error = _validate_contact(contact, count_only)
    if error:
        return jsonify({"error": error}), 400

    error = _validate_file_names(file_names)
    if error:
        return jsonify({"error": error}), 400

    total_bytes = sum(int(f.get("size") or 0) for f in files if isinstance(f, dict))
    if total_bytes > MAX_CONTENT_LENGTH:
        limit_gb = MAX_CONTENT_LENGTH // (1024 ** 3)
        return jsonify({"error": f"Total size exceeds the {limit_gb}GB limit - please remove some files."}), 400

    if not gdrive_upload.is_configured():
        # Front-end falls back to POSTing straight to /upload instead.
        return jsonify({"drive_configured": False})

    submission_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    reference_number = next_reference_number()

    # The site the browser is on, e.g. https://client-portal-2.onrender.com -
    # Google needs this to allow the browser's direct upload (see gdrive_upload.py).
    origin = request.host_url.rstrip("/")

    try:
        folder_id = gdrive_upload.create_submission_folder(f"Portal submission {reference_number}")
        uploads = []
        for f in files:
            name = os.path.basename(f.get("name") or "file")
            upload_url = gdrive_upload.create_resumable_upload_session(name, folder_id, origin=origin)
            uploads.append({"name": f.get("name"), "upload_url": upload_url})
    except gdrive_upload.GoogleDriveError as e:
        logger.error(f"Submission {submission_id}: Drive init failed - {e}")
        return jsonify({"error": "Could not prepare the upload right now. Please try again shortly."}), 502

    logger.info(f"Submission {submission_id} (ref {reference_number}): Drive init, "
                f"{len(uploads)} file(s), count_only={count_only}.")

    return jsonify({
        "drive_configured": True,
        "submission_id": submission_id,
        "reference_number": reference_number,
        "drive_folder_id": folder_id,
        "uploads": uploads,
    })


@app.route("/upload/finalize", methods=["POST"])
def upload_finalize():
    data = request.get_json(silent=True) or {}
    submission_id = data.get("submission_id")
    reference_number = data.get("reference_number")
    folder_id = data.get("drive_folder_id")
    count_only = bool(data.get("count_only"))
    client_note = (data.get("client_note") or "").strip()
    raw_contact = data.get("contact") or {}
    contact = {field: (raw_contact.get(field) or "").strip() for field in FIELD_LABELS}

    if not (submission_id and reference_number and folder_id):
        return jsonify({"error": "Missing submission details - please start again."}), 400

    error = _validate_contact(contact, count_only)
    if error:
        return jsonify({"error": error}), 400

    # submission_id comes back from the browser, so make sure it's only ever
    # the plain id /upload/init issued - never a path like "../something".
    if os.path.basename(submission_id) != submission_id or submission_id.startswith("."):
        return jsonify({"error": "Invalid submission details - please start again."}), 400

    try:
        drive_files = gdrive_upload.list_files_in_folder(folder_id)
    except gdrive_upload.GoogleDriveError as e:
        logger.error(f"Submission {submission_id}: listing Drive folder failed - {e}")
        return jsonify({"error": "Could not find your uploaded files. Please try again."}), 502

    if not drive_files:
        return jsonify({"error": "No files were received - please try again."}), 400

    submission_dir = os.path.join(UPLOAD_DIR, submission_id)
    os.makedirs(submission_dir, exist_ok=True)

    saved_paths = []
    try:
        for finfo in drive_files:
            dest = _unique_dest(submission_dir, finfo["name"])
            gdrive_upload.download_file(finfo["id"], dest)
            saved_paths.append(dest)
    except gdrive_upload.GoogleDriveError as e:
        logger.error(f"Submission {submission_id}: downloading from Drive failed - {e}")
        shutil.rmtree(submission_dir, ignore_errors=True)
        return jsonify({"error": "Could not retrieve your uploaded files. Please try again."}), 502

    file_ids = [f["id"] for f in drive_files]

    def cleanup_drive():
        gdrive_upload.cleanup_folder(folder_id, file_ids)

    logger.info(f"Submission {submission_id} (ref {reference_number}): "
                f"{len(saved_paths)} file(s) pulled back from Drive (count_only={count_only}).")

    return _handle_received_files(submission_id, saved_paths, client_note, contact,
                                   reference_number, count_only, on_done=cleanup_drive)


def _handle_received_files(submission_id, saved_paths, client_note, contact,
                            reference_number, count_only, on_done):
    """Shared tail end for both upload paths, once files are sitting on local
    disk: either analyse synchronously and return the report (count-only), or
    hand off to a background thread for the full analyse+email+file-transfer
    pipeline. `on_done`, if given, is called after processing finishes either
    way (used by the Drive path to clean up the Drive copies)."""
    if count_only:
        try:
            all_rows = []
            for path in saved_paths:
                all_rows.extend(analyze_file(path, os.path.basename(path)))
            html_body = rows_to_html(all_rows, submission_id, client_note,
                                      contact=contact, count_only=True,
                                      reference_number=reference_number)
        except Exception as e:
            logger.exception(f"Count-only submission {submission_id} failed: {e}")
            return jsonify({"error": "Something went wrong analysing your files. Please try again."}), 500
        finally:
            shutil.rmtree(os.path.join(UPLOAD_DIR, submission_id), ignore_errors=True)
            if on_done:
                on_done()

        return jsonify({"status": "counted", "submission_id": submission_id,
                         "reference_number": reference_number, "report_html": html_body})

    def run():
        try:
            process_submission(submission_id, saved_paths, client_note, contact, reference_number)
        finally:
            if on_done:
                on_done()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    return jsonify({"status": "received", "submission_id": submission_id,
                     "reference_number": reference_number, "files": len(saved_paths)})


def process_submission(submission_id, saved_paths, client_note, contact, reference_number):
    try:
        all_rows = []
        for path in saved_paths:
            original_name = os.path.basename(path)
            all_rows.extend(analyze_file(path, original_name))

        wetransfer_link, wetransfer_error = None, None
        try:
            wetransfer_link = upload_submission(saved_paths, message=f"Quote request {reference_number}")
        except FileTransferError as e:
            wetransfer_error = str(e)
            logger.error(f"Submission {submission_id}: file transfer upload failed - {wetransfer_error}")

        html_body = rows_to_html(all_rows, submission_id, client_note,
                                  wetransfer_link=wetransfer_link, wetransfer_error=wetransfer_error,
                                  contact=contact, reference_number=reference_number)
        csv_bytes = rows_to_csv(all_rows).encode("utf-8")
        csv_name = f"analysis_{reference_number}.csv"

        flagged = sum(1 for r in all_rows if r.flagged)
        subject_line = f"[Quote request {reference_number}] {contact.get('subject') or ''} - {len(saved_paths)} file(s)" + \
                        (f" - {flagged} flagged for review" if flagged else "")

        # If the WeTransfer link came through, the originals are covered by that -
        # no need to also fight the email attachment size cap. Only fall back to
        # attaching originals directly when WeTransfer wasn't available.
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


# --------------------------------------------------------------------------
# One-time Google Drive authorization (see README.md "Large files: Google
# Drive bypass"). Kaye visits /admin/gdrive-auth herself, once, after setting
# GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET/GDRIVE_ADMIN_KEY - the callback shows
# the resulting refresh token once, for her to copy into
# GOOGLE_REFRESH_TOKEN. Not used again after that.
# --------------------------------------------------------------------------

@app.route("/admin/gdrive-auth")
def gdrive_auth():
    if not GDRIVE_ADMIN_KEY or request.args.get("key") != GDRIVE_ADMIN_KEY:
        return "Not found.", 404
    redirect_uri = url_for("gdrive_callback", _external=True)
    try:
        auth_url = gdrive_upload.build_authorization_url(redirect_uri)
    except gdrive_upload.GoogleDriveError as e:
        return f"Can't start authorization: {e}", 500
    return redirect(auth_url)


@app.route("/admin/gdrive-callback")
def gdrive_callback():
    google_error = request.args.get("error")
    if google_error:
        return f"Google returned an error: {google_error}", 400
    code = request.args.get("code")
    if not code:
        return "Missing authorization code.", 400

    redirect_uri = url_for("gdrive_callback", _external=True)
    try:
        tokens = gdrive_upload.exchange_code_for_tokens(code, redirect_uri)
    except gdrive_upload.GoogleDriveError as e:
        return f"Could not complete authorization: {e}", 500

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        return ("Google didn't return a refresh token. This usually means this account already "
                "authorized this app before, without the 'choose account/consent' step actually "
                "showing - go to https://myaccount.google.com/permissions, remove this app's access, "
                "then try this link again."), 400

    # Deliberately not logged anywhere - this value is a credential.
    return render_template("gdrive_success.html", refresh_token=refresh_token)


# Start the daily cleanup thread on import, not just under `python3 app.py` -
# a production server (gunicorn, etc.) imports this module directly and never
# runs the __main__ block below, so this must live at module level to run there too.
threading.Thread(target=cleanup_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
