"""
Client file-upload portal.

Clients drag & drop PDF / JPG / ZIP / PPTX / XLSX files. Files are stored,
analysed in the background (page/slide/sheet counts + physical sizes,
flagging anything non-standard), and a report is emailed automatically
to whoever REPORT_RECIPIENT is set to (see emailer.py / README.md).

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

from flask import Flask, request, jsonify, render_template

from analyzer import analyze_file, SUPPORTED_EXTENSIONS
from report import rows_to_csv, rows_to_html
from emailer import send_report_email
from file_transfer import upload_submission, FileTransferError
from reference_number import next_reference_number

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("portal")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)

MAX_CONTENT_LENGTH = 5 * 1024 * 1024 * 1024  # 5GB per submission
RETENTION_DAYS = 30

# Fields the client-facing form always requires, and the extra two that are
# only required for a real quote submission (not for a "just count my pages"
# check - see the REQUIRED_FIELDS_COUNT_ONLY note in the /upload route).
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


def cleanup_loop():
    while True:
        try:
            cleanup_old_submissions()
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        time.sleep(24 * 3600)  # once a day


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/upload", methods=["POST"])
def upload():
    uploaded_files = request.files.getlist("files")
    client_note = request.form.get("client_note", "").strip()
    count_only = request.form.get("count_only", "").strip().lower() in ("1", "true", "on", "yes")

    contact = {field: request.form.get(field, "").strip() for field in FIELD_LABELS}

    # Count-only submissions still need the client's name/company/email/phone/
    # subject so Kaye can follow up if needed, but skip the deadline and
    # delivery address since nothing is actually being quoted or sent yet.
    required_fields = list(REQUIRED_FIELDS_ALWAYS)
    if not count_only:
        required_fields += REQUIRED_FIELDS_FULL_SUBMISSION
    missing = [FIELD_LABELS[f] for f in required_fields if not contact.get(f)]
    if missing:
        return jsonify({"error": f"Missing required field(s): {', '.join(missing)}"}), 400

    if not uploaded_files:
        return jsonify({"error": "No files received."}), 400

    rejected = [f.filename for f in uploaded_files
                if os.path.splitext(f.filename)[1].lower() not in SUPPORTED_EXTENSIONS]
    if rejected:
        return jsonify({"error": f"Unsupported file type(s): {', '.join(rejected)}"}), 400

    submission_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    submission_dir = os.path.join(UPLOAD_DIR, submission_id)
    os.makedirs(submission_dir, exist_ok=True)

    saved_paths = []
    for f in uploaded_files:
        safe_name = os.path.basename(f.filename)
        dest = os.path.join(submission_dir, safe_name)
        f.save(dest)
        saved_paths.append(dest)

    # A human-friendly, sequential reference number (DDMMYY-NNNNN) shown in the
    # report/email - separate from submission_id, which stays the guaranteed-
    # unique folder name. Only generated once we know this is a real, valid
    # submission, so a rejected request doesn't burn a number.
    reference_number = next_reference_number()

    logger.info(f"Submission {submission_id} (ref {reference_number}): "
                f"{len(saved_paths)} file(s) received (count_only={count_only}).")

    if count_only:
        # "I just want to count my pages": analyse synchronously and hand the
        # report straight back to the browser. No email to Kaye, no upload to
        # a file-transfer provider, and the files are deleted immediately
        # afterwards rather than kept for 30 days like a real submission.
        try:
            all_rows = []
            for path in saved_paths:
                original_name = os.path.basename(path)
                all_rows.extend(analyze_file(path, original_name))
            html_body = rows_to_html(all_rows, submission_id, client_note,
                                      contact=contact, count_only=True,
                                      reference_number=reference_number)
        except Exception as e:
            logger.exception(f"Count-only submission {submission_id} failed: {e}")
            return jsonify({"error": "Something went wrong analysing your files. Please try again."}), 500
        finally:
            shutil.rmtree(submission_dir, ignore_errors=True)

        return jsonify({"status": "counted", "submission_id": submission_id,
                         "reference_number": reference_number, "report_html": html_body})

    # Run analysis + email synchronously in a background thread so the client
    # gets an instant "received" response without waiting on the full scan.
    thread = threading.Thread(
        target=process_submission,
        args=(submission_id, saved_paths, client_note, contact, reference_number),
        daemon=True,
    )
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


# Start the daily cleanup thread on import, not just under `python3 app.py` -
# a production server (gunicorn, etc.) imports this module directly and never
# runs the __main__ block below, so this must live at module level to run there too.
threading.Thread(target=cleanup_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
