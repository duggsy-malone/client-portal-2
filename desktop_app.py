"""
Page Counter - the desktop version, for Kaye's own use.

Runs entirely on this computer: files are read from the computer's own disk
into a temporary folder, counted, and the temporary copies deleted. Nothing
is uploaded anywhere, so there are no size limits beyond disk space.

Start it with "Page Counter.command" (Mac) or "Page Counter.bat" (Windows),
which set everything up on first run. Or directly: python3 desktop_app.py
"""

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

from analyzer import analyze_file          # noqa: E402
from report import rows_to_html, rows_to_csv  # noqa: E402

app = Flask(__name__, template_folder=os.path.join(HERE, "templates"))
app.config["MAX_CONTENT_LENGTH"] = None  # it's your own computer - no limit


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


@app.route("/")
def index():
    return render_template("desktop.html")


@app.route("/count", methods=["POST"])
def count():
    uploaded = request.files.getlist("files")
    if not uploaded:
        return jsonify({"error": "No files received."}), 400
    job_name = (request.form.get("job_name") or "").strip()
    workdir = tempfile.mkdtemp(prefix="page_counter_")
    try:
        paths = []
        for f in uploaded:
            dest = _unique_dest(workdir, _safe_name(f.filename))
            f.save(dest)
            paths.append(dest)
        rows = []
        for path in paths:
            rows.extend(analyze_file(path, os.path.basename(path)))
        html = rows_to_html(rows, "desktop", count_only=True,
                            title=f"Page count - {job_name}" if job_name else "Page count",
                            banner="Counted on this computer with Page Counter - nothing was uploaded anywhere.")
        return jsonify({"report_html": html, "csv": rows_to_csv(rows), "items": len(rows), "files": len(paths)})
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
    print("  Page Counter is running at " + url)
    print("  (It should open in your browser. To stop it, close this window.)")
    print("")
    if not os.environ.get("PAGE_COUNTER_NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    # 127.0.0.1 = only this computer can reach it, never anyone on the network.
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
