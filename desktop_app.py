"""
Page Counter - the desktop version of Flightpath's pre-flight, for Kaye's own use.

Runs entirely on this computer: files are read from the computer's own disk
into a temporary folder, counted one at a time, and each temporary copy is
deleted as soon as it's been counted. Nothing is uploaded anywhere, so there
are no size limits beyond disk space - and the temp folder never holds more
than one file at a time, so a 20 GB job doesn't need 20 GB of spare space.

It shares its working parts with the website - the same analyzer, the same
report, the same brand - so a count here and a count there agree.

Start it with "Page Counter.command" (Mac) or "Page Counter.bat" (Windows),
which set everything up on first run. Or directly: python3 desktop_app.py
"""

import json
import os
import sys
import socket
import shutil
import tempfile
import threading
import webbrowser

from flask import Flask, request, jsonify, render_template

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import branding                              # noqa: E402
import structure                             # noqa: E402
from version import APP_NAME, VERSION        # noqa: E402
from analyzer import analyze_file            # noqa: E402
from report import rows_to_html, rows_to_csv  # noqa: E402

app = Flask(__name__, template_folder=os.path.join(HERE, "templates"),
            static_folder=os.path.join(HERE, "static"))
app.config["MAX_CONTENT_LENGTH"] = None  # it's your own computer - no limit


@app.context_processor
def _brand_values():
    """The same brand values the website's pages get, so the two look alike."""
    return {"brand": branding.as_dict(), "app_name": APP_NAME, "version": VERSION}


def _safe_name(name):
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    return name or "file"


def _unique_dest(directory, filename):
    stem, ext = os.path.splitext(filename)
    candidate, n = filename, 2
    while os.path.exists(os.path.join(directory, candidate)):
        candidate = f"{stem} ({n}){ext}"
        n += 1
    return os.path.join(directory, candidate)


def _folder_list(value):
    """For each file, in the order they were added, the folder it came from
    ("Archway/Drawings", or "" for a loose file). Sent by the page so the
    report can show tabs, dividers and the folder structure."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = []
    if not isinstance(value, list):
        return []
    return [(v if isinstance(v, str) else "")[:2000] for v in value[:20000]]


@app.route("/")
def index():
    return render_template("desktop.html")


@app.route("/count", methods=["POST"])
def count():
    uploaded = request.files.getlist("files")
    if not uploaded:
        return jsonify({"error": "No files received."}), 400
    job_name = (request.form.get("job_name") or "").strip()
    plans_to_scale = str(request.form.get("plans_to_scale") or "").strip().lower() in ("1", "true", "on", "yes")
    folders = _folder_list(request.form.get("folders"))

    workdir = tempfile.mkdtemp(prefix="page_counter_")
    try:
        rows = []
        folder_info = {}
        counted = 0
        for i, f in enumerate(uploaded):
            name = _safe_name(f.filename)
            dest = _unique_dest(workdir, name)
            f.save(dest)
            base = os.path.basename(dest)
            parts = structure.clean_folder_path(folders[i]) if 0 <= i < len(folders) else []
            folder_info[base] = {"folders": parts, "name": structure.original_name(base, parts)}
            try:
                rows.extend(analyze_file(dest, base))
                counted += 1
            finally:
                # One file on disk at a time - see the note at the top.
                try:
                    os.remove(dest)
                except OSError:
                    pass
        html = rows_to_html(
            rows, "desktop", count_only=True, folder_info=folder_info,
            plans_to_scale=plans_to_scale if plans_to_scale else None,
            title=f"Page count - {job_name}" if job_name else "Page count",
            banner=f"Counted on this computer with Page Counter - nothing was uploaded anywhere.")
        return jsonify({"report_html": html,
                        "csv": rows_to_csv(rows, folder_info, plans_to_scale if plans_to_scale else None),
                        "items": len(rows), "files": counted})
    except Exception as e:
        return jsonify({"error": f"Something went wrong counting these files: {e}"}), 500
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _free_port(preferred=8765):
    for port in [preferred] + list(range(8766, 8800)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def main():
    port = int(os.environ.get("PAGE_COUNTER_PORT") or _free_port())
    url = f"http://127.0.0.1:{port}/"
    print("")
    print(f"  Page Counter ({APP_NAME} V{VERSION}) is running at {url}")
    print("  (It should open in your browser. To stop it, close this window.)")
    print("")
    if not os.environ.get("PAGE_COUNTER_NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    # 127.0.0.1 = only this computer can reach it, never anyone on the network.
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
