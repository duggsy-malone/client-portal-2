"""
Sends the analysis report by email.

IMPORTANT: this is a standalone script, not this Claude session - it has no
access to any Gmail/Claude connector. It sends real email itself via SMTP,
using credentials you provide as environment variables (see README.md for
the 2-minute Gmail App Password setup). Until those are set, it fails safe:
the report is written to disk under reports/ and a clear message is logged,
so uploads still work while you're getting email set up.
"""

import os
import smtplib
import logging
from email.message import EmailMessage

logger = logging.getLogger("portal.emailer")

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME")          # e.g. quotes@sandyboy.co.uk
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")          # Gmail App Password (not your normal password)
REPORT_RECIPIENT = os.environ.get("REPORT_RECIPIENT", "kaye@sandyboy.co.uk")

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # keep well under Gmail's 25MB message cap


def send_report_email(subject, html_body, csv_attachment_name, csv_attachment_bytes,
                       original_files=None, reports_dir="reports"):
    """Returns (sent: bool, message: str)."""
    original_files = original_files or []

    if not SMTP_USERNAME or not SMTP_PASSWORD:
        os.makedirs(reports_dir, exist_ok=True)
        fallback_path = os.path.join(reports_dir, csv_attachment_name.replace(".csv", ".html"))
        with open(fallback_path, "w") as f:
            f.write(html_body)
        msg = (f"SMTP_USERNAME/SMTP_PASSWORD not configured - report saved locally to "
               f"{fallback_path} instead of being emailed. See README.md to enable real email.")
        logger.warning(msg)
        return False, msg

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_USERNAME
    msg["To"] = REPORT_RECIPIENT
    msg.set_content("This email requires an HTML-capable client to view the report table.")
    msg.add_alternative(html_body, subtype="html")
    msg.add_attachment(csv_attachment_bytes, maintype="text", subtype="csv", filename=csv_attachment_name)

    attached_bytes = len(csv_attachment_bytes)
    skipped_files = []
    for fpath in original_files:
        try:
            size = os.path.getsize(fpath)
        except OSError:
            continue
        if attached_bytes + size > MAX_ATTACHMENT_BYTES:
            skipped_files.append(os.path.basename(fpath))
            continue
        with open(fpath, "rb") as f:
            data = f.read()
        msg.add_attachment(data, maintype="application", subtype="octet-stream",
                            filename=os.path.basename(fpath))
        attached_bytes += size

    if skipped_files:
        note = f"\n\n(Not attached - too large for email, retrieve from server storage: {', '.join(skipped_files)})"
        msg.set_content(msg.get_body(preferencelist=("plain",)).get_content() + note)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        return True, "Report emailed successfully."
    except Exception as e:
        os.makedirs(reports_dir, exist_ok=True)
        fallback_path = os.path.join(reports_dir, csv_attachment_name.replace(".csv", ".html"))
        with open(fallback_path, "w") as f:
            f.write(html_body)
        msg_text = f"Email send failed ({e}). Report saved locally to {fallback_path} instead."
        logger.error(msg_text)
        return False, msg_text
