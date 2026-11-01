# Flightpath - the iPrintManage client upload portal.
# Copyright (c) 2026 Sandyboy Design and Print Ltd. All rights reserved.
# Licensed to iPrintManage for its own business use - see LICENSE.

"""Assembles the Flightpath Tester: the whole portal, set up to run on one
computer only, with everything that could reach a client switched off.

Built from the current portal code, so the tester and the live site can't
drift apart. Run: python3 build_tester.py
"""
import os
import shutil

SRC = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("TESTER_BUILD_DIR") or os.path.join(SRC, "build")
APP = os.path.join(OUT, "Flightpath Tester")

MODULES = ["app.py", "analyzer.py", "branding.py", "emailer.py", "file_part.py",
           "file_transfer.py", "history.py", "paper_sizes.py", "r2_storage.py",
           "reference_number.py", "report.py", "structure.py",
           "transfernow_upload.py", "version.py", "wetransfer_upload.py"]
TEMPLATES = ["_head.html", "_header.html", "_lockup.html", "_footer.html",
             "index.html", "standard.html", "my_submissions.html",
             "admin_login.html", "admin_submissions.html", "admin_attention.html",
             "admin_files.html"]
BRAND = ["flightpath.css", "mark.svg", "mark-navy.svg", "mark-mono.svg",
         "dart.svg", "dart-on-navy.svg", "favicon.png", "favicon.ico",
         "apple-touch-icon.png", "mark-transparent.png"]
SAMPLES = ["brochure_mixed_sizes.pdf", "poster_a3.pdf", "scan_a4_300dpi.jpg",
           "invoice_a4.xlsx", "pitch_deck_widescreen.pptx", "client_job_bundle.zip",
           "banner_slide_unusual.pptx", "budget_no_pagesetup.xlsx"]

shutil.rmtree(APP, ignore_errors=True)
os.makedirs(os.path.join(APP, "templates"), exist_ok=True)
os.makedirs(os.path.join(APP, "static", "brand"), exist_ok=True)
os.makedirs(os.path.join(APP, "Files to test with"), exist_ok=True)

for name in MODULES:
    shutil.copy2(os.path.join(SRC, name), os.path.join(APP, name))
for name in TEMPLATES:
    shutil.copy2(os.path.join(SRC, "templates", name), os.path.join(APP, "templates", name))
for name in BRAND:
    src = os.path.join(SRC, "static", "brand", name)
    if os.path.exists(src):
        shutil.copy2(src, os.path.join(APP, "static", "brand", name))
for name in SAMPLES:
    src = os.path.join(SRC, "samples", name)
    if os.path.exists(src):
        shutil.copy2(src, os.path.join(APP, "Files to test with", name))

# This folder is the portal's source, so the licence goes in it.
shutil.copy2(os.path.join(SRC, "LICENSE"), os.path.join(APP, "LICENSE"))

with open(os.path.join(APP, "requirements.txt"), "w") as fh:
    fh.write("Flask>=3.0\npypdf>=4.0\nPillow>=10.0\nrequests>=2.31\n")

# ---------------------------------------------------------------- the banner
# The tester's own copy of the header carries a bar across the top, so a
# screenshot of it can never be mistaken for the live portal. The live
# templates are untouched - only this copy is changed.
header_path = os.path.join(APP, "templates", "_header.html")
with open(header_path) as fh:
    header = fh.read()
BANNER = ('<div class="fp-testbar"><b>TEST COPY</b> &mdash; runs on this computer only. '
          'Nothing here reaches a client: no email is sent, no files leave this machine, '
          'and references start <b>TEST-</b>.</div>\n')
assert header.count("<header class=\"fp-header\">") == 1
header = header.replace("<header class=\"fp-header\">", BANNER + "<header class=\"fp-header\">")
with open(header_path, "w") as fh:
    fh.write(header)

css_path = os.path.join(APP, "static", "brand", "flightpath.css")
with open(css_path, "a") as fh:
    fh.write("""

/* --- the tester's bar, not in the live site ---------------------------- */
.fp-testbar {
  margin: -18px -18px 18px; padding: 11px 18px; background: #C2410C; color: #fff;
  font-size: 13.5px; line-height: 1.45; text-align: center;
  border-radius: 0 0 10px 10px;
}
.fp-testbar b { color: #FFD9A8; }
@media (max-width: 620px) { .fp-testbar { margin: -12px -12px 14px; } }
""")

# ------------------------------------------------ emails, written to disk
# The portal already saves REPORT emails to disk when no email is configured.
# Client emails - the confirmation, the page count, the note about files, and
# the copies that go to anyone in Copy to - are only logged, which would make
# the Copy to field impossible to check here. This patches the TESTER'S OWN
# copy of the emailer so those are written out as HTML too. The live emailer
# is untouched.
emailer_path = os.path.join(APP, "emailer.py")
with open(emailer_path) as fh:
    mailer = fh.read()

OLD_FALLBACK = (
    '        logger.warning(f"No email method configured - client email to '
    '{to_address} not sent.")\n'
    '        return False, "No email method configured."\n')
NEW_FALLBACK = (
    '        return _save_client_email(to_address, subject, html_body, cc, reply_to)\n')
assert mailer.count(OLD_FALLBACK) == 1, "the client-email fallback has moved - check emailer.py"
mailer = mailer.replace(OLD_FALLBACK, NEW_FALLBACK)

SAVER = [
    '',
    '',
    '# --- added by build_tester.py - TESTER ONLY -----------------------------',
    'def _save_client_email(to_address, subject, html_body, cc=None, reply_to=None):',
    '    """With no email configured, write the email into reports/ rather than',
    '    dropping it. The tester leans on this: it is the only way to see what the',
    '    client gets and what the people in Copy to get, side by side."""',
    '    import re as _re',
    '    from datetime import datetime as _dt',
    '    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")',
    '    os.makedirs(folder, exist_ok=True)',
    '    stamp = _dt.now().strftime("%Y%m%d-%H%M%S")',
    '    who = _re.sub(r"[^A-Za-z0-9._-]", "_", (to_address or "unknown"))',
    '    safe = _re.sub(r"[^A-Za-z0-9 ._-]", "", subject or "email")[:60].strip()',
    '    name = f"{stamp} TO {who} - {safe}.html"',
    '    header = ("<!-- TESTER: this email was NOT sent." + chr(10)',
    '              + f"     To:       {to_address}" + chr(10)',
    '              + f"     Cc:       {chr(44).join(cc or []) or chr(40) + chr(41)}" + chr(10)',
    '              + f"     Reply-to: {reply_to or chr(40) + chr(41)}" + chr(10)',
    '              + f"     Subject:  {subject}" + chr(10) + "-->" + chr(10))',
    '    with open(os.path.join(folder, name), "w") as fh:',
    '        fh.write(header + (html_body or ""))',
    '    logger.info(f"Tester: email to {to_address} saved as reports/{name}")',
    '    return True, f"Saved to reports/{name} (tester - nothing was sent)."',
    '',
]
with open(emailer_path, "w") as fh:
    fh.write(mailer + "\n".join(SAVER))

# The live portal skips the client's confirmation email when there is no cloud
# storage, because the My jobs link in it needs storage to work. In the tester
# that would hide the main email the Copy to field affects, so the tester's own
# copy sends it anyway - into reports/. The link lands on "My jobs isn't
# available", which is expected here.
app_path = os.path.join(APP, "app.py")
with open(app_path) as fh:
    portal_code = fh.read()
OLD_GUARD = ('    same news without the link. Never raises."""\n'
             '    if not history.is_enabled():\n'
             '        return\n')
NEW_GUARD = ('    same news without the link. Never raises."""\n'
             '    # TESTER: the storage check that normally guards this email is removed,\n'
             '    # so the wording and the Copy to split can be checked offline. The My\n'
             '    # jobs link it contains will say it is unavailable - that is expected.\n')
assert portal_code.count(OLD_GUARD) == 1, "the confirmation-email guard has moved - check app.py"
portal_code = portal_code.replace(OLD_GUARD, NEW_GUARD)

# Without cloud storage the portal uses its direct upload route, and that route
# doesn't send the client's confirmation - the cloud route does. The tester adds
# it, so the email a client actually gets, and the copy anyone in Copy to gets,
# can both be read in reports/.
OLD_DIRECT = (
    '                     kwargs={"folder_info": _folder_info(saved_paths, folders), '
    '"plans_to_scale": plans_to_scale},\n'
    '                     daemon=True).start()\n'
    '    return jsonify({"status": "received", "submission_id": submission_id,\n')
NEW_DIRECT = (
    '                     kwargs={"folder_info": _folder_info(saved_paths, folders), '
    '"plans_to_scale": plans_to_scale},\n'
    '                     daemon=True).start()\n'
    '    # TESTER: the cloud route sends this; the direct route normally does not.\n'
    '    threading.Thread(target=_send_confirmation_email,\n'
    '                     args=(contact, reference_number, request.host_url.rstrip("/"),\n'
    '                           plans_to_scale),\n'
    '                     daemon=True).start()\n'
    '    return jsonify({"status": "received", "submission_id": submission_id,\n')
assert portal_code.count(OLD_DIRECT) == 1, "the direct upload route has moved - check app.py"
portal_code = portal_code.replace(OLD_DIRECT, NEW_DIRECT)

with open(app_path, "w") as fh:
    fh.write(portal_code)

# ------------------------------------------------------------- the launcher
LAUNCH = '''"""Starts the Flightpath tester on this computer.

Everything that could reach the outside world is switched off here, before the
portal is imported - not in a settings file someone can forget to change:

  * no Brevo key and no SMTP login, so no email can be sent. Every email the
    portal would have sent - to you and to the client - is written into the
    "reports" folder as an HTML file instead, with who it was addressed to and
    who was copied in at the top.
  * no TransferNow or WeTransfer key, so no files are uploaded anywhere.
  * no Cloudflare R2, so files stay in this folder and "My jobs" is off.
  * references start at TEST-90001, so a test can never be mistaken for a real
    job in conversation or on paper.
  * it listens on 127.0.0.1 only - this computer, not the network. Nobody else
    can reach it, even on the same wifi.
"""
import os
import sys
import socket
import threading
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

SAFE = {
    "REFERENCE_PREFIX": "TEST",
    "REFERENCE_START": "90000",
    "ADMIN_KEY": "test",
    "REPORT_RECIPIENT": "tester@localhost",
    "EMAIL_FROM_NAME": "Flightpath TEST",
    "CLEANUP_FIRST_RUN_DELAY": "86400",
    "APP_SECRET": "flightpath-local-tester",
}
OFF = ("BREVO_API_KEY", "EMAIL_FROM", "EMAIL_REPLY_TO", "SMTP_USERNAME", "SMTP_PASSWORD",
       "TRANSFERNOW_API_KEY", "WETRANSFER_API_KEY", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
       "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "GDRIVE_ADMIN_KEY")

for key, value in SAFE.items():
    os.environ[key] = value
for key in OFF:
    os.environ.pop(key, None)


def free_port(start=5055):
    for port in range(start, start + 40):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


port = free_port()
os.environ["PORT"] = str(port)

import app as portal  # noqa: E402  (after the settings above)

url = f"http://127.0.0.1:{port}"
print("")
print("  Flightpath TESTER")
print("  =================")
print(f"  Open {url} if your browser didn't.")
print("")
print("  Email:        off - every email is written into the 'reports' folder instead")
print("  File sending: off - nothing leaves this computer")
print("  Storage:      this folder - 'uploads', 'jobs' and 'reports'")
print("  References:   TEST-90001 upwards")
print(f"  Admin pages:  {url}/admin/submissions  (password: test)")
print("")
print("  Close this window to stop the tester.")
print("")

threading.Timer(1.5, lambda: webbrowser.open(url)).start()
portal.app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
'''
with open(os.path.join(APP, "tester_launch.py"), "w") as fh:
    fh.write(LAUNCH)

MAC = r'''#!/bin/bash
# Flightpath Tester for Mac - double-click to start.
# First run sets up a private Python environment in this folder (a minute or two, needs internet).
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  osascript -e 'display dialog "The Flightpath Tester needs Python 3.\n\nInstall it from python.org (Downloads > macOS), then double-click the tester again." buttons {"OK"} default button "OK"'
  exit 1
fi

if [ ! -x ".venv/bin/python" ] || [ ! -f ".venv/.ready" ]; then
  echo ""
  echo "  Setting up the Flightpath Tester for the first time (a minute or two)..."
  echo ""
  rm -rf .venv
  if ! python3 -m venv .venv || ! .venv/bin/python -m pip install --quiet --upgrade pip \
     || ! .venv/bin/python -m pip install --quiet -r requirements.txt; then
    echo ""
    echo "  Setup didn't finish - check your internet connection and try again."
    echo "  (If it keeps failing, send Claude a screenshot of this window.)"
    read -r -p "  Press Enter to close. " _
    exit 1
  fi
  touch .venv/.ready
fi

.venv/bin/python tester_launch.py
'''

WIN = r'''@echo off
REM Flightpath Tester for Windows - double-click to start.
REM First run sets up a private Python environment in this folder (a minute or two, needs internet).
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   The Flightpath Tester needs Python 3.
  echo   Install it from https://www.python.org/downloads/ - tick "Add Python to PATH" -
  echo   then double-click the tester again.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\.ready" (
  echo.
  echo   Setting up the Flightpath Tester for the first time (a minute or two)...
  echo.
  if exist ".venv" rmdir /s /q ".venv"
  python -m venv .venv || goto setupfailed
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip || goto setupfailed
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto setupfailed
  echo ready> ".venv\.ready"
)

".venv\Scripts\python.exe" tester_launch.py
exit /b 0

:setupfailed
echo.
echo   Setup didn't finish - check your internet connection and try again.
echo   (If it keeps failing, send Claude a screenshot of this window.)
echo.
pause
exit /b 1
'''

with open(os.path.join(APP, "Flightpath Tester.command"), "w", newline="\n") as fh:
    fh.write(MAC)
os.chmod(os.path.join(APP, "Flightpath Tester.command"), 0o755)
with open(os.path.join(APP, "Flightpath Tester.bat"), "w", newline="\r\n") as fh:
    fh.write(WIN)

print("built", APP)
total = 0
for root, dirs, fs in os.walk(APP):
    for f in sorted(fs):
        p = os.path.join(root, f)
        total += os.path.getsize(p)
print(f"  {sum(len(fs) for _, _, fs in os.walk(APP))} files, {total/1024/1024:.1f} MB")
