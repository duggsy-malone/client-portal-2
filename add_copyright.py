"""Puts the copyright notice at the top of every source file, and leaves files
that already carry it alone. Safe to run again after any change - that's the
point of it, so a new file doesn't quietly go out without a notice on it.

    python3 add_copyright.py            # add the notice where it's missing
    python3 add_copyright.py --check    # list what's missing, change nothing

The wording and the ownership itself are in LICENSE, next to this file.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MARKER = "Sandyboy Design and Print Ltd"

LINE_1 = "Flightpath - the iPrintManage client upload portal."
LINE_2 = "Copyright (c) 2026 Sandyboy Design and Print Ltd. All rights reserved."
LINE_3 = "Licensed to iPrintManage for its own business use - see LICENSE."

PY_HEADER = f"# {LINE_1}\n# {LINE_2}\n# {LINE_3}\n\n"
CSS_HEADER = f"/* {LINE_1}\n   {LINE_2}\n   {LINE_3} */\n\n"
# Deliberately an HTML comment, not a Jinja one: this is the notice a person
# sees if they look at the page source, which is where someone copying it
# would start.
HTML_HEADER = (f"<!-- {LINE_1}\n     {LINE_2}\n     {LINE_3} -->\n")

SKIP = {"add_copyright.py"}


def targets():
    for name in sorted(os.listdir(HERE)):
        if name.endswith(".py") and name not in SKIP:
            yield os.path.join(HERE, name), PY_HEADER
    css = os.path.join(HERE, "static", "brand", "flightpath.css")
    if os.path.exists(css):
        yield css, CSS_HEADER
    # One HTML notice is enough: _head.html is included by every page, so the
    # notice lands in the source of all of them.
    head = os.path.join(HERE, "templates", "_head.html")
    if os.path.exists(head):
        yield head, HTML_HEADER


def main():
    check_only = "--check" in sys.argv
    missing, added = [], []
    for path, header in targets():
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        if MARKER in body[:600]:
            continue
        missing.append(os.path.relpath(path, HERE))
        if check_only:
            continue
        # A shebang, if there is one, has to stay on the first line.
        if body.startswith("#!"):
            first, _, rest = body.partition("\n")
            body = first + "\n" + header + rest
        else:
            body = header + body
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        added.append(os.path.relpath(path, HERE))

    if check_only:
        if missing:
            print("No copyright notice in:")
            for name in missing:
                print("  " + name)
            sys.exit(1)
        print("Every source file carries the notice.")
        return
    if added:
        print(f"Notice added to {len(added)} file(s):")
        for name in added:
            print("  " + name)
    else:
        print("Nothing to do - every source file already carries the notice.")


if __name__ == "__main__":
    main()
