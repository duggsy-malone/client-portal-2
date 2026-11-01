# Flightpath - the iPrintManage client upload portal.
# Copyright (c) 2026 Sandyboy Design and Print Ltd. All rights reserved.
# Licensed to iPrintManage for its own business use - see LICENSE.

"""
Sends the analysis report by email.

IMPORTANT: this is a standalone script, not this Claude session - it has no
access to any Gmail/Claude connector. It sends real email itself, using
credentials you provide as environment variables. Until those are set, it
fails safe: the report is written to disk under reports/ and a clear
message is logged, so uploads still work while you're getting email set up.

Two ways to send are supported:

1. BREVO (recommended for hosts like Render's free tier) - sends over a
   normal HTTPS API call, not old-fashioned SMTP. Render's free web
   services block outbound SMTP ports (25/465/587) entirely as an
   anti-abuse measure, so a plain Gmail SMTP login will fail there with
   "Network is unreachable" no matter how correct your credentials are.
   Brevo's API goes over HTTPS instead, which isn't blocked, and its free
   plan (300 emails/day, no credit card, no expiry) is more than enough
   for a quoting inbox. Set BREVO_API_KEY + EMAIL_FROM to use this path -
   see README.md for the 5-minute signup + sender verification steps.

2. SMTP (Gmail App Password) - works fine when running locally on your own
   computer, or on a host that doesn't block SMTP ports (a paid Render
   instance, most VPS's, etc). Set SMTP_USERNAME + SMTP_PASSWORD to use
   this path instead.

If BREVO_API_KEY is set, that path is used. Otherwise it falls back to SMTP
if configured. If neither is configured, the report is saved locally.
"""

import os
import base64
import smtplib
import logging
import urllib.request
import urllib.error
import json
from email.message import EmailMessage

logger = logging.getLogger("portal.emailer")

# --- Brevo (HTTPS API - works on Render's free tier) ---
BREVO_API_KEY = os.environ.get("BREVO_API_KEY")
BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"
EMAIL_FROM = os.environ.get("EMAIL_FROM")            # must be a sender verified in your Brevo account
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "Client Portal")

# --- SMTP (Gmail App Password - works locally / on hosts that allow SMTP) ---
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")

REPORT_RECIPIENT = os.environ.get("REPORT_RECIPIENT", "kaye@sandyboy.co.uk")

# Where a project-site enquiry goes when a client ticks the offer on the upload
# page. Comma-separated, same as REPORT_RECIPIENT - these two are the default
# so it works without anything being set in Render.
PROJECT_SITE_RECIPIENT = os.environ.get("PROJECT_SITE_RECIPIENT",
                                        "matthew@iprintmanage.com,kaye@iprintmanage.com")

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # keep well under typical provider message caps


def split_addresses(value):
    """"a@x.com, b@y.com; c@z.com" -> ["a@x.com", "b@y.com", "c@z.com"].
    Anything without an @ in it is dropped rather than sent to, and the same
    address twice is kept once."""
    out = []
    for part in (value or "").replace(";", ",").split(","):
        part = part.strip()
        if part and "@" in part and part.lower() not in [a.lower() for a in out]:
            out.append(part)
    return out


def project_site_recipients():
    """Who hears about a project-site enquiry."""
    return split_addresses(PROJECT_SITE_RECIPIENT)


def _recipient_list():
    """REPORT_RECIPIENT can be one address, or several separated by commas
    (e.g. "kaye@sandyboy.co.uk,sales@iprintmanage.com" - spaces around each
    address are fine too). Splits and trims that into a clean list, dropping
    any empty entries from a stray trailing comma."""
    return [addr.strip() for addr in REPORT_RECIPIENT.split(",") if addr.strip()]


def _save_fallback(html_body, csv_attachment_name, reports_dir, reason):
    os.makedirs(reports_dir, exist_ok=True)
    fallback_path = os.path.join(reports_dir, csv_attachment_name.replace(".csv", ".html"))
    with open(fallback_path, "w") as f:
        f.write(html_body)
    msg = f"{reason} Report saved locally to {fallback_path} instead."
    return msg


def _send_via_brevo(subject, html_body, csv_attachment_name, csv_attachment_bytes, original_files):
    attachments = [{
        "content": base64.b64encode(csv_attachment_bytes).decode("ascii"),
        "name": csv_attachment_name,
    }]
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
        attachments.append({
            "content": base64.b64encode(data).decode("ascii"),
            "name": os.path.basename(fpath),
        })
        attached_bytes += size

    body_html = html_body
    if skipped_files:
        body_html += (f"<p style='color:#c0392b'><b>Not attached (too large for email - "
                       f"retrieve from server storage):</b> {', '.join(skipped_files)}</p>")

    payload = {
        "sender": {"email": EMAIL_FROM, "name": EMAIL_FROM_NAME},
        "to": [{"email": addr} for addr in _recipient_list()],
        "subject": subject,
        "htmlContent": body_html,
        "attachment": attachments,
    }

    req = urllib.request.Request(
        BREVO_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "api-key": BREVO_API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()
    return True, "Report emailed successfully via Brevo."


def _send_via_smtp(subject, html_body, csv_attachment_name, csv_attachment_bytes, original_files):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_USERNAME
    msg["To"] = ", ".join(_recipient_list())
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

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)
    return True, "Report emailed successfully via SMTP."


def send_report_email(subject, html_body, csv_attachment_name, csv_attachment_bytes,
                       original_files=None, reports_dir="reports"):
    """Returns (sent: bool, message: str)."""
    original_files = original_files or []

    if BREVO_API_KEY and EMAIL_FROM:
        try:
            return _send_via_brevo(subject, html_body, csv_attachment_name, csv_attachment_bytes, original_files)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            msg = _save_fallback(html_body, csv_attachment_name, reports_dir,
                                  f"Brevo send failed ({e.code}: {body}).")
            logger.error(msg)
            return False, msg
        except Exception as e:
            msg = _save_fallback(html_body, csv_attachment_name, reports_dir, f"Brevo send failed ({e}).")
            logger.error(msg)
            return False, msg

    if SMTP_USERNAME and SMTP_PASSWORD:
        try:
            return _send_via_smtp(subject, html_body, csv_attachment_name, csv_attachment_bytes, original_files)
        except Exception as e:
            msg = _save_fallback(html_body, csv_attachment_name, reports_dir, f"SMTP send failed ({e}).")
            logger.error(msg)
            return False, msg

    msg = _save_fallback(
        html_body, csv_attachment_name, reports_dir,
        "No email method configured (set BREVO_API_KEY+EMAIL_FROM, or SMTP_USERNAME+SMTP_PASSWORD)."
    )
    logger.warning(msg)
    return False, msg


# Optional: where clients' replies to confirmation emails should go, e.g.
# sales@iprintmanage.com. Without it, replies go to EMAIL_FROM.
EMAIL_REPLY_TO = os.environ.get("EMAIL_REPLY_TO")


def send_client_email(to_address, subject, html_body, cc=None, reply_to=None):
    """A short email to a CLIENT (not the report inbox) - e.g. their
    confirmation or submission-history link. No attachments.

    `cc` is anyone the client asked to be copied in (see the Copy to field on
    the upload page). The caller decides what the copies are allowed to see:
    anything with a private link in it is sent to the copies separately, with
    the link taken out, rather than passed here.

    `reply_to` overrides EMAIL_REPLY_TO for this one email - used for the
    project-site enquiry, so replying goes to the client who asked.

    Returns (sent: bool, message: str). Never raises."""
    to_address = (to_address or "").strip()
    if not to_address or "@" not in to_address:
        return False, "No valid client email address."
    cc = [a for a in split_addresses(",".join(cc or [])) if a.lower() != to_address.lower()]
    reply_to = (reply_to or EMAIL_REPLY_TO or "").strip()
    try:
        if BREVO_API_KEY and EMAIL_FROM:
            payload = {
                "sender": {"email": EMAIL_FROM, "name": EMAIL_FROM_NAME},
                "to": [{"email": to_address}],
                "subject": subject,
                "htmlContent": html_body,
            }
            if cc:
                payload["cc"] = [{"email": a} for a in cc]
            if reply_to:
                payload["replyTo"] = {"email": reply_to}
            req = urllib.request.Request(
                BREVO_API_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json",
                         "Accept": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            return True, ("Client email sent via Brevo."
                          + (f" Copied to {len(cc)}." if cc else ""))
        if SMTP_USERNAME and SMTP_PASSWORD:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = SMTP_USERNAME
            msg["To"] = to_address
            if cc:
                msg["Cc"] = ", ".join(cc)
            if reply_to:
                msg["Reply-To"] = reply_to
            msg.set_content("This email needs an HTML-capable email program to display.")
            msg.add_alternative(html_body, subtype="html")
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.starttls()
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(msg)
            return True, ("Client email sent via SMTP."
                          + (f" Copied to {len(cc)}." if cc else ""))
        logger.warning(f"No email method configured - client email to {to_address} not sent.")
        return False, "No email method configured."
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error(f"Client email failed (Brevo {e.code}: {body})")
        return False, f"Brevo send failed ({e.code})."
    except Exception as e:
        logger.error(f"Client email failed ({e})")
        return False, f"Send failed ({e})."


def send_internal_email(recipients, subject, html_body, reply_to=None):
    """A short email to us, not to a client, and with nothing attached - the
    project-site enquiry uses this. The first address is the To, the rest are
    copied in, so a reply-all reaches everyone. Never raises."""
    recipients = split_addresses(",".join(recipients or []))
    if not recipients:
        logger.warning("No recipients configured for an internal email - not sent.")
        return False, "No recipients configured."
    return send_client_email(recipients[0], subject, html_body,
                             cc=recipients[1:], reply_to=reply_to)
