"""Builds the report Kaye receives: an HTML email body + a CSV attachment,
from the list of Row objects produced by the analyzer."""

import csv
import html as _html
import re
import io
from collections import OrderedDict
from datetime import datetime

import branding
import structure
from paper_sizes import suggest_scale
from version import APP_NAME, VERSION


def _e(v):
    """Escape anything that came from a client (names, notes, file names) before it goes into HTML."""
    return _html.escape("" if v is None else str(v))


def _fmt(v):
    return "" if v is None else v


def build_size_summary(rows):
    """Group every analysed item by its matched size label and count them,
    so the top of the report answers 'how many of each size in total'
    before anyone has to read the itemised list.

    Returns a list of dicts: {label, qty, is_standard, breakdown}, largest
    quantity first, with items that have no measurable size (unsupported/
    unreadable files, empty sheets) grouped last under 'Not sized'.

    Non-standard items are one "Non-standard" entry whose `breakdown` lists
    each actual size and how many there are, e.g. [("7 x 7 mm", 800), ...].
    Sizes are rounded to the nearest mm and orientation doesn't matter
    (7 x 12 and 12 x 7 count together). Every other entry has breakdown [].
    """
    counts = OrderedDict()
    standard_flags = {}
    odd_sizes = {}
    for r in rows:
        if r.width_mm is None or r.height_mm is None:
            label = "Not sized (other file types, unreadable or empty - check manually)"
            is_standard = True  # not a "review this size" flag - different reason
        else:
            label = r.matched_size or "Non-standard"
            is_standard = r.is_standard
            if not is_standard:
                a, b = sorted((int(round(r.width_mm)), int(round(r.height_mm))))
                odd_sizes[(a, b)] = odd_sizes.get((a, b), 0) + 1
        counts[label] = counts.get(label, 0) + 1
        standard_flags[label] = is_standard

    breakdown = [(f"{a} \u00d7 {b} mm", qty)
                 for (a, b), qty in sorted(odd_sizes.items(), key=lambda kv: (-kv[1], kv[0]))]

    summary = [{"label": label, "qty": qty, "is_standard": standard_flags[label],
                "breakdown": breakdown if not standard_flags[label] else []}
               for label, qty in counts.items()]
    # Sort: standard sizes by quantity desc, then non-standard/not-sized at the bottom.
    summary.sort(key=lambda s: (s["label"].startswith("Not sized"), not s["is_standard"], -s["qty"]))
    return summary


# Sizes a printer would normally run double-sided. Everything larger (A3 and
# up, drawings, banners) is assumed single-sided.
DOUBLE_SIDED_SIZES = {"A4", "A5", "A6", "US Letter"}
SHEETS_EXPLANATION = "A4 and smaller (US Letter counts as A4) printed double-sided, larger sizes single-sided"

# One ring binder holds this many sheets of paper, printed on both sides.
SHEETS_PER_FOLDER = 380
FOLDERS_EXPLANATION = f"{SHEETS_PER_FOLDER} sheets per folder"


def estimate_sheets(rows):
    """Sheets of paper this job would use, which is how some suppliers quote
    (a page count is really a count of printed sides). Within each document,
    A4-and-smaller pages go two to a sheet; every larger page is its own
    sheet. Items with no size (other file types, unreadable) aren't counted."""
    per_doc = OrderedDict()
    for r in rows:
        if r.width_mm is None or r.height_mm is None:
            continue
        doc = per_doc.setdefault((r.source_file, r.location), [0, 0])
        if r.matched_size in DOUBLE_SIDED_SIZES:
            doc[0] += 1
        else:
            doc[1] += 1
    return sum((small + 1) // 2 + large for small, large in per_doc.values())


# Notes that mean "we can't count this file type", as opposed to "someone
# needs to look at this". Kept apart so the flag count means something.
NOT_COUNTABLE_MARKERS = ("not a file type we can count", "file type not analysed by this tool")


def flag_counts(rows):
    """(needs_decision, not_countable). The first is odd sizes, unreadable
    files and estimates - things to look at before quoting. The second is
    file types this tool can't count, which are simply passed on."""
    needs_decision = not_countable = 0
    for r in rows:
        note = (r.notes or "").lower()
        if any(marker in note for marker in NOT_COUNTABLE_MARKERS):
            not_countable += 1
        elif r.flagged:
            needs_decision += 1
    return needs_decision, not_countable


def job_totals(rows, folder_info=None, documents=None):
    """The headline numbers shown together at the top of the report."""
    documents = documents if documents is not None else structure.build_documents(rows, folder_info)
    sheets = estimate_sheets(rows)
    needs_decision, not_countable = flag_counts(rows)
    # Design & access statements and non-technical summaries are bound as
    # their own documents, so they don't fill binders (they still get a tab).
    separate = structure.separate_documents(documents)
    separate_ids = {id(s["document"]) for s in separate}
    binder_rows = [r for d in documents if id(d) not in separate_ids for r in d.rows]
    sheets_in_binders = estimate_sheets(binder_rows)
    return {
        "pages": len(rows),
        "sheets": sheets,
        "sheets_in_binders": sheets_in_binders,
        "separate_documents": len(separate),
        "separate_pages": sum(s["pages"] for s in separate),
        "folders_needed": -(-sheets_in_binders // SHEETS_PER_FOLDER),  # rounded up
        "tabs": structure.count_tabs(documents),
        "dividers": len(documents),
        "files": len(set(r.source_file for r in rows)),
        "flagged": sum(1 for r in rows if r.flagged),
        "needs_decision": needs_decision,
        "not_countable": not_countable,
    }


def plans_label(plans_to_scale):
    if plans_to_scale is None:
        return ""
    return "PRINTED TO SCALE" if plans_to_scale else "A3 FOLDED (not to scale)"


SLIDE_SUGGESTION = "Scale to A4 - PowerPoint slides"


def _slide_count(row):
    """"3 slide(s) @ deck size" -> 3. A deck is one row but several slides."""
    m = re.match(r"\s*(\d+)\s+slide", row.unit_label or "")
    return int(m.group(1)) if m else 1


def build_scaling_suggestions(rows):
    """One entry per odd size, with what to do about it:
    [{size, qty, suggestion}], most common first. Deliberately separate from
    the size totals so the suggestions can be checked on their own.

    PowerPoint slides are always listed here, whatever their deck size,
    because they're always printed scaled to A4."""
    counts = OrderedDict()
    for r in rows:
        if r.width_mm is None or r.height_mm is None:
            continue
        is_slides = (r.file_type or "").upper() == "PPTX"
        if r.is_standard and not is_slides:
            continue
        a, b = sorted((int(round(abs(r.width_mm))), int(round(abs(r.height_mm)))))
        key = (a, b, is_slides)
        counts[key] = counts.get(key, 0) + (_slide_count(r) if is_slides else 1)
    entries = [{"size": f"{a} \u00d7 {b} mm" + (" (PowerPoint)" if is_slides else ""),
                "qty": qty, "suggestion": SLIDE_SUGGESTION if is_slides else suggest_scale(a, b)}
               for (a, b, is_slides), qty in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    return entries


# The order suggestions are grouped in, so a report always reads the same way.
SCALE_ORDER = ["Scale up to A4", "Scale up to A3", "Scale down to A3", SLIDE_SUGGESTION,
               "Too small to scale - check manually"]


def group_scaling_suggestions(entries):
    """Groups build_scaling_suggestions() by suggestion and adds a subtotal
    for each: [{suggestion, sizes: [...], qty}] plus an overall total."""
    groups = OrderedDict()
    for entry in entries:
        groups.setdefault(entry["suggestion"], []).append(entry)
    ordered = sorted(groups.items(),
                     key=lambda kv: (SCALE_ORDER.index(kv[0]) if kv[0] in SCALE_ORDER else len(SCALE_ORDER), kv[0]))
    return [{"suggestion": suggestion, "sizes": sizes, "qty": sum(e["qty"] for e in sizes)}
            for suggestion, sizes in ordered]


def rows_to_csv(rows, folder_info=None, plans_to_scale=None):
    buf = io.StringIO()
    writer = csv.writer(buf)
    documents = structure.build_documents(rows, folder_info)
    totals = job_totals(rows, documents=documents)
    summary = build_size_summary(rows)

    writer.writerow(["TOTALS"])
    if plans_to_scale is not None:
        writer.writerow(["Plans", plans_label(plans_to_scale)])
    writer.writerow(["Pages / items", totals["pages"]])
    writer.writerow(["Estimated sheets of paper", totals["sheets"], SHEETS_EXPLANATION])
    writer.writerow(["Folders needed", totals["folders_needed"], FOLDERS_EXPLANATION])
    writer.writerow(["Tabs (one per folder)", totals["tabs"]])
    writer.writerow(["Dividers (one per document)", totals["dividers"]])
    writer.writerow(["Files uploaded", totals["files"]])
    writer.writerow(["Needs a decision", totals["needs_decision"], "odd sizes, unreadable files, estimates"])
    writer.writerow(["Not countable (passed on anyway)", totals["not_countable"],
                     "file types this tool can't count"])
    writer.writerow([])

    spreadsheets = structure.spreadsheet_documents(documents)
    if spreadsheets:
        writer.writerow([f"SPREADSHEETS ({len(spreadsheets)}) - check these"])
        writer.writerow(["File", "Folder", "Worksheets", "Estimated pages", "Sizes", "Print size"])
        for d in spreadsheets:
            info = structure.describe_spreadsheet(d)
            writer.writerow([info["name"], info["folder"], info["worksheets"], info["pages"],
                             info["sizes"].replace("×", "x"), info["print_size"]])
        writer.writerow([])

    separate = structure.separate_documents(documents)
    if separate:
        writer.writerow([f"PRODUCED SEPARATELY ({len(separate)}) - not counted into the binders"])
        writer.writerow(["Document", "Folder", "What it is", "Pages", "Binding", "Note"])
        for entry in separate:
            writer.writerow([entry["name"], entry["folder"], entry["kind"], entry["pages"],
                             entry["binding"], entry["note"]])
        writer.writerow(["Separate documents total", "", "", sum(e["pages"] for e in separate), "", ""])
        writer.writerow([])

    writer.writerow(["AS PER THE STANDARD - what would be produced"])
    writer.writerow(["", "Pages", "Sheets"])
    separate_ids = {id(e["document"]) for e in separate}
    binder_rows = [r for d in documents if id(d) not in separate_ids for r in d.rows]
    for label, subset in (
            ("A4 and smaller, printed double-sided",
             [r for r in binder_rows if r.matched_size in DOUBLE_SIDED_SIZES]),
            ("A3 plans, single-sided and folded to A4", [r for r in binder_rows if r.matched_size == "A3"]),
            ("Larger formats, folded to keep their scale",
             [r for r in binder_rows if r.width_mm is not None and r.matched_size != "A3"
              and r.matched_size not in DOUBLE_SIDED_SIZES])):
        if subset:
            writer.writerow([label, len(subset), estimate_sheets(subset)])
    writer.writerow(["Into binders", len(binder_rows), totals["sheets_in_binders"]])
    writer.writerow(["Binders needed", totals["folders_needed"], FOLDERS_EXPLANATION])
    writer.writerow([f"Produced separately ({structure.SEPARATE_BINDING.lower()})",
                     totals["separate_documents"],
                     totals["separate_pages"]])
    writer.writerow([])

    split_combined = structure.find_split_and_combined(documents)
    if split_combined:
        writer.writerow(["THE SAME DOCUMENT, WHOLE AND IN PIECES"])
        writer.writerow(["Document", "Folder", "Pages", "Parts elsewhere", "Pages in those parts", "Where"])
        for item in split_combined:
            writer.writerow([item["document"], item["folder"], item["pages"], item["parts"],
                             item["part_pages"], item["where"]])
        writer.writerow([])

    suggestions = build_scaling_suggestions(rows)
    if suggestions:
        groups = group_scaling_suggestions(suggestions)
        writer.writerow([f"SUGGESTED SCALING ({len(suggestions)} odd size(s)) - separate from the totals above"])
        writer.writerow(["Suggestion", "Size", "Quantity"])
        for group in groups:
            for entry in group["sizes"]:
                writer.writerow([group["suggestion"], entry["size"].replace("\u00d7", "x"), entry["qty"]])
            writer.writerow([f"{group['suggestion']} - subtotal", f"{len(group['sizes'])} size(s)", group["qty"]])
        writer.writerow(["All odd sizes - total", f"{len(suggestions)} size(s)",
                         sum(g["qty"] for g in groups)])
        writer.writerow([])

    writer.writerow(["QUANTITY BY SIZE"])
    writer.writerow(["Size", "Quantity", "Standard?"])
    for s in summary:
        label = s["label"] + (" (total)" if s["breakdown"] else "")
        writer.writerow([label, s["qty"], "Yes" if s["is_standard"] else "No"])
        for size_label, qty in s["breakdown"]:
            # plain "x": Excel can garble the multiply sign in a CSV
            writer.writerow(["    " + size_label.replace("×", "x"), qty, "No"])
    writer.writerow([])

    duplicates = structure.find_possible_duplicates(documents)
    if duplicates:
        writer.writerow([f"POSSIBLE DUPLICATES ({len(duplicates)} set(s)) - same pages, same sizes, same order"])
        writer.writerow(["Set", "Document", "Folder", "Pages", "File size (bytes)", "Same file size?"])
        for n, group in enumerate(duplicates, start=1):
            for d in group["documents"]:
                writer.writerow([n, d["name"], d["folder"], group["pages"], d.get("bytes") or "",
                                 "Yes" if group["identical_files"] else "No"])
        writer.writerow(["Pages saved by printing one of each", sum(g["extra_pages"] for g in duplicates)])
        writer.writerow([])

    section_list = structure.sections(documents)
    if len(section_list) > 1:
        writer.writerow(["QUANTITY BY SIZE, BY SECTION"])
        writer.writerow(["Section", "Size", "Quantity", "Documents in section", "Pages in section",
                         "Sheets in section"])
        for name, docs in section_list:
            section_rows = [r for d in docs for r in d.rows]
            for entry in build_size_summary(section_rows):
                label = entry["label"] + (" (total)" if entry["breakdown"] else "")
                writer.writerow([name, label, entry["qty"], len(docs), len(section_rows),
                                 estimate_sheets(section_rows)])
                for size_label, qty in entry["breakdown"]:
                    writer.writerow([name, "    " + size_label.replace("\u00d7", "x"), qty, "", "", ""])
        writer.writerow([])

    tree = structure.build_tree(documents)
    folders = list(structure.walk_folders(tree))
    if folders:
        writer.writerow(["FOLDERS (documents and pages include subfolders)"])
        writer.writerow(["Folder", "Level", "Documents", "Pages"])
        for depth, node in folders:
            writer.writerow([" / ".join(node.path), depth + 1, node.total_documents, node.total_pages])
        if tree.documents:
            writer.writerow(["(files not in a folder)", "", len(tree.documents), sum(d.pages for d in tree.documents)])
        writer.writerow([])

    writer.writerow(["DETAIL - every page/slide/sheet/image individually"])
    writer.writerow(["Uploaded file", "Folder", "Location in archive", "Type", "Item",
                      "Width (mm)", "Height (mm)", "Matched size", "Standard?", "Flagged", "Notes"])
    folder_of = {r.source_file: " / ".join((folder_info or {}).get(r.source_file, {}).get("folders") or [])
                 for r in rows}
    for r in rows:
        d = r.as_dict()
        writer.writerow([
            d["source_file"], folder_of.get(r.source_file, ""), d["location"], d["file_type"], d["unit_label"],
            d["width_mm"], d["height_mm"], d["matched_size"],
            "Yes" if d["is_standard"] else "No",
            "YES - REVIEW" if d["flagged"] else "",
            d["notes"],
        ])
    return buf.getvalue()


# Inline styles for the folder diagram: email programs (Outlook especially)
# ignore a lot of <style> rules, so the diagram carries its own.
_TREE_FOLDER = "margin:3px 0;padding:0;list-style:none"
_TREE_CHILDREN = "margin:0 0 0 10px;padding:0 0 0 14px;border-left:1px solid #c5d0d8;list-style:none"
_TREE_COUNT = "color:#607d8b;font-size:12px;white-space:nowrap"


def _mb(n):
    if not n:
        return ""
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{max(1, n // 1024)} KB"


def _plural(n, word):
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _folder_counts(node):
    return f"{_plural(node.total_documents, 'document')} &middot; {_plural(node.total_pages, 'page')}"


def _doc_line(d):
    pages = f" &middot; {_plural(d.pages, 'page')}" if d.pages else " &middot; check manually"
    return (f'<li style="{_TREE_FOLDER};color:#37474f">&#128196; {_e(d.name)}'
            f'<span style="{_TREE_COUNT}">{pages}</span></li>')


def _tree_html(node, interactive):
    """Nested list of folders, always fully shown so the structure is visible.
    `interactive` (the page-count report, shown in a browser): each folder
    also has a "show files" toggle listing its documents. Otherwise (email):
    folders and their counts only."""
    items = []
    for child in node.children.values():
        label = (f'<b>&#128193; {_e(child.name)}</b> <span style="{_TREE_COUNT}">'
                 f'{_folder_counts(child)}</span>')
        inner = ""
        if interactive and child.documents:
            n = len(child.documents)
            inner += (f'<details style="margin:1px 0 2px 22px"><summary style="cursor:pointer;font-size:12px;'
                      f'color:#C2410C">show {n} file{"" if n == 1 else "s"}</summary>'
                      f'<ul style="{_TREE_CHILDREN}">' + "".join(_doc_line(d) for d in child.documents)
                      + "</ul></details>")
        if child.children:
            inner += _tree_html(child, interactive)
        items.append(f'<li style="{_TREE_FOLDER}">{label}{inner}</li>')
    if not items:
        return ""
    style = _TREE_CHILDREN if node.name is not None else "margin:0;padding:0;list-style:none"
    return f'<ul style="{style}">' + "".join(items) + "</ul>"


# Long section titles for the email, where everything is shown one after
# the other rather than as tabs.
_EMAIL_TITLES = {
    "As per the Standard": "As per the Standard - what we would produce",
    "Quantity by size": "Quantity by size (all files combined)",
    "Sizes by section": "Quantity by size, split by section",
    "Suggested scaling": "Suggested scaling for odd sizes (not part of the totals)",
    "Folder structure": "Folder structure",
    "Full breakdown": "Full breakdown - every page / slide / sheet / image",
}


def _sections(sections, tabbed):
    """The report's main sections. In a browser (page-count reports and the
    saved copies) they become tabs, so the page isn't one endless scroll; in
    an email they're simply stacked, because email can't do tabs."""
    sections = [(title, body) for title, body in sections if body]
    if not sections:
        return ""
    if not tabbed:
        return "".join(
            f'<h3>{_EMAIL_TITLES.get(title, title)}'
            f'{" - check these" if title.startswith("Spreadsheets") else ""}'
            f'{" - worth a look" if title.startswith("Possible duplicates") else ""}</h3>{body}'
            for title, body in sections)
    buttons = "".join(
        f'<button type="button" class="tab-btn{" active" if i == 0 else ""}" data-panel="panel-{i}">'
        f'{title}{" - check these" if title.startswith("Spreadsheets") else ""}</button>'
        for i, (title, _) in enumerate(sections))
    panels = "".join(
        f'<div class="tab-panel{" active" if i == 0 else ""}" id="panel-{i}">{body}</div>'
        for i, (_, body) in enumerate(sections))
    return f'<div class="tabs">{buttons}</div><div class="tab-panels">{panels}</div>{_TAB_SCRIPT}'


# Plain, old-fashioned JavaScript so it works in any browser a client might
# have. Nothing is loaded from the internet - the report has to work as a
# file saved on someone's computer.
_TAB_SCRIPT = """
    <script>
      (function () {
        var buttons = Array.prototype.slice.call(document.querySelectorAll('.tab-btn'));
        var panels = Array.prototype.slice.call(document.querySelectorAll('.tab-panel'));
        function show(id) {
          var found = false;
          buttons.forEach(function (b) {
            var on = b.getAttribute('data-panel') === id;
            b.className = on ? 'tab-btn active' : 'tab-btn';
            if (on) { found = true; }
          });
          panels.forEach(function (p) {
            p.className = (p.id === id) ? 'tab-panel active' : 'tab-panel';
          });
          if (found) { try { sessionStorage.setItem('portalReportTab', id); } catch (e) {} }
          return found;
        }
        buttons.forEach(function (b) {
          b.addEventListener('click', function () { show(b.getAttribute('data-panel')); });
        });
        var saved = null;
        try { saved = sessionStorage.getItem('portalReportTab'); } catch (e) {}
        if (saved) { show(saved); }
      })();
    </script>
    """


# The report is read in two places - in a browser, and inside an email - so it
# carries its own styling and never links to a stylesheet or a web font. The
# brand colours come from branding.py; the type falls back to what mail
# programs actually have.
_REPORT_CSS = """
      body { font-family: __BODY__; color: __NAVY__; background: __PAPER__; margin: 0; padding: 0;
             line-height: 1.55; }
      .fp-bar { background: __NAVY__; color: #fff; padding: 15px 20px; }
      .fp-bar .fp-wm { font-size: 20px; font-weight: bold; letter-spacing: -0.02em; }
      .fp-bar .fp-wm span { color: __BEACON__; }
      .fp-bar .fp-en { font-size: 10px; font-weight: bold; color: #B9AEE6; padding-top: 1px; }
      .fp-bar .fp-ref { float: right; font-family: __MONO__; font-size: 13px; color: __BEACON__; }
      .fp-body { max-width: 1040px; margin: 0 auto; padding: 18px 20px 30px; }
      h2 { font-size: 22px; letter-spacing: -0.02em; color: __NAVY__; margin: 0 0 12px; }
      h3 { color: __NAVY__; margin-top: 0; }
      h4 { color: __NAVY__; }
      a { color: __EMBER__; }
      table { border-collapse: collapse; width: 100%; font-size: 13px; margin-bottom: 24px;
              background: #fff; }
      th, td { border: 1px solid __LINE__; padding: 7px 9px; text-align: left; }
      th { background: __NAVY__; color: #fff; text-transform: uppercase; letter-spacing: 0.09em;
           font-size: 11px; }
      tr.flagged { background: __WARMTINT__; }
      tr.nonstandard-summary { background: __WARMTINT__; }
      tr.nonstandard-sub { background: #FFFBF5; color: __MUTED__; }
      tr.subtotal { background: #F4EFFA; }
      tr.grandtotal { background: __NAVY__; color: #fff; }
      tr.grandtotal b { color: #fff; }
      .summary { margin-bottom: 16px; }
      .badge { display:inline-block; background:__EMBER__; color:#fff; padding:2px 9px;
               border-radius:999px; font-size:12px; }
      .badge.grey { background:__MUTED__; }
      .qty { font-family: __MONO__; font-weight: bold; text-align: right; }
      .tabs { display: flex; flex-wrap: wrap; gap: 4px; border-bottom: 2px solid __NAVY__; margin: 18px 0 0; }
      .tab-btn {
        font: inherit; font-size: 14px; padding: 9px 16px; cursor: pointer; color: __NAVY__;
        background: #F7F1EB; border: 1px solid __LINE__; border-bottom: none;
        border-radius: 8px 8px 0 0; margin-bottom: -2px;
      }
      .tab-btn:hover { background: #F1E8E0; }
      .tab-btn.active { background: __NAVY__; color: #fff; border-color: __NAVY__; font-weight: bold; }
      .tab-panels { border: 1px solid __LINE__; border-top: none; border-radius: 0 0 8px 8px;
                    padding: 16px; background: #fff; }
      .tab-panel { display: none; }
      .tab-panel.active { display: block; max-height: 74vh; overflow: auto; }
      .tab-panel > table { margin-bottom: 0; }
      .tree summary::-webkit-details-marker { color: __MUTED__; }
"""


def _report_style():
    """The CSS above with the brand's values filled in. Written with tokens
    rather than an f-string because CSS is full of braces."""
    values = {
        "__BODY__": branding.FONT_EMAIL,
        "__MONO__": branding.FONT_EMAIL_MONO,
        "__NAVY__": branding.COLOUR_NAVY,
        "__BEACON__": branding.COLOUR_BEACON,
        "__PAPER__": branding.COLOUR_PAPER,
        "__EMBER__": branding.COLOUR_EMBER,
        "__LINE__": branding.COLOUR_LINE,
        "__MUTED__": branding.COLOUR_MUTED,
        "__WARMTINT__": "#FFF4E7",
    }
    css = _REPORT_CSS
    for token, value in values.items():
        css = css.replace(token, value)
    return "<style>" + css + "    </style>"


def _report_bar(display_ref):
    """The navy header bar, as on the emails in the brand guide."""
    ref = f'<span class="fp-ref">{_e(display_ref)}</span>' if display_ref else ""
    return (f'<div class="fp-bar">{ref}'
            f'<div class="fp-wm">{branding.WORDMARK_LEAD}<span>{branding.WORDMARK_TAIL}</span></div>'
            f'<div class="fp-en">{branding.ENDORSEMENT}</div></div>')


def rows_to_html(rows, submission_id, client_note="", wetransfer_link=None, wetransfer_error=None,
                  contact=None, count_only=False, reference_number=None, title=None, banner=None,
                  folder_info=None, plans_to_scale=None, collapsible=None):
    """`collapsible` (default: on for page-count reports, which open in a
    browser) folds each section away; emails can't do that, so they're flat."""
    if collapsible is None:
        collapsible = count_only
    documents = structure.build_documents(rows, folder_info)
    totals = job_totals(rows, documents=documents)
    summary = build_size_summary(rows)

    style = _report_style()

    summary_rows_html = ""
    for s in summary:
        cls = ' class="nonstandard-summary"' if not s["is_standard"] else ""
        flag_text = "&#9888; REVIEW" if not s["is_standard"] else ""
        label = s["label"] + (" (total)" if s["breakdown"] else "")
        summary_rows_html += f"""<tr{cls}>
            <td>{_e(label)}</td>
            <td class="qty">{s['qty']}</td>
            <td>{flag_text}</td>
        </tr>"""
        for size_label, qty in s["breakdown"]:
            summary_rows_html += f"""<tr class="nonstandard-sub">
            <td style="padding-left:28px">{_e(size_label)}</td>
            <td class="qty">{qty}</td>
            <td></td>
        </tr>"""

    detail_rows_html = ""
    for r in rows:
        d = r.as_dict()
        cls = ' class="flagged"' if d["flagged"] else ""
        flag_text = "&#9888; REVIEW" if d["flagged"] else ""
        detail_rows_html += f"""<tr{cls}>
            <td>{_e(d['source_file'])}</td>
            <td>{_e(d['location'])}</td>
            <td>{_e(d['file_type'])}</td>
            <td>{_e(d['unit_label'])}</td>
            <td>{_fmt(d['width_mm'])}</td>
            <td>{_fmt(d['height_mm'])}</td>
            <td>{_e(d['matched_size'])}</td>
            <td>{flag_text}</td>
            <td>{_e(d['notes'])}</td>
        </tr>"""

    note_html = f"<p><b>Client note:</b> {_e(client_note)}</p>" if client_note else ""

    contact_html = ""
    if contact:
        contact_rows = [
            ("Subject", contact.get("subject", "")),
            ("Name", contact.get("full_name", "")),
            ("Company", contact.get("company", "")),
            ("Email", contact.get("email", "")),
            ("Phone", contact.get("phone", "")),
        ]
        if contact.get("deadline"):
            contact_rows.append(("Deadline", contact["deadline"]))
        if contact.get("delivery_address"):
            contact_rows.append(("Delivery address", contact["delivery_address"]))
        contact_rows_html = "".join(
            f"<tr><td><b>{label}</b></td><td>{_e(value)}</td></tr>" for label, value in contact_rows
        )
        contact_html = f"""
        <h3>Client details</h3>
        <table style="max-width:480px">
          {contact_rows_html}
        </table>
        """

    count_only_banner = ""
    if count_only:
        count_only_banner = (
            '<p style="background:#EAF8FE;border:1px solid #B3E3F6;padding:10px 14px;'
            'border-radius:8px;color:#1c5a80">This is a self-service page count only &ndash; '
            'nothing has been sent to us, and your files were deleted as soon as they\'d been counted.</p>'
        )
    if banner:
        count_only_banner = (
            '<p style="background:#EAF8FE;border:1px solid #B3E3F6;padding:10px 14px;'
            f'border-radius:8px;color:#1c5a80">{_e(banner)}</p>'
        )

    plans_html = ""
    if plans_to_scale is not None:
        if plans_to_scale:
            plans_html = ('<p style="background:#1C0072;color:#fff;padding:12px 16px;border-radius:8px;'
                          'font-size:17px;margin:0 0 16px"><b>&#128208; PLANS: PRINTED TO SCALE</b></p>')
        else:
            plans_html = ('<p style="background:#FFA125;color:#1C0072;padding:12px 16px;border-radius:8px;'
                          'font-size:17px;margin:0 0 16px"><b>&#128208; PLANS: A3 FOLDED</b> '
                          '<span style="font-size:13px">(not to scale)</span></p>')

    if wetransfer_link:
        files_link_html = (
            f'<p style="margin-top:10px"><a href="{wetransfer_link}" '
            f'style="display:inline-block;background:#FF7A1A;color:#1C0072;padding:9px 18px;'
            f'border-radius:6px;text-decoration:none;font-weight:bold">Download original files</a>'
            f'<br><span style="font-size:11px;color:#6B6480">{_e(wetransfer_link)}</span></p>'
        )
    elif wetransfer_error:
        files_link_html = (
            f'<p style="margin-top:10px;color:#C2410C;font-size:12px">'
            f'Couldn\'t create a download link for the original files: {_e(wetransfer_error)} '
            f'(Small files may also be attached to this email.)</p>'
        )
    else:
        files_link_html = ""

    def stat(label, value, note=""):
        note_html_ = f'<div style="font-size:11px;color:#6B6480;margin-top:2px">{note}</div>' if note else ""
        return (f'<td style="border:1px solid #F0E2D6;background:#FDEDE1;padding:11px 14px;text-align:center;'
                f'vertical-align:top"><div style="font-size:12px;color:#6B6480">{label}</div>'
                f'<div style="font-size:23px;font-weight:bold;color:#1C0072">{value:,}</div>{note_html_}</td>')
    totals_html = (
        '<table style="width:auto;margin:8px 0 14px"><tr>'
        + stat("Pages / items", totals["pages"])
        + stat("Sheets of paper", totals["sheets"], "estimate")
        + stat("Folders", totals["folders_needed"],
               FOLDERS_EXPLANATION + (f", plus {totals['separate_documents']} "
                                      f"{structure.SEPARATE_BINDING.lower()}"
                                      if totals["separate_documents"] else ""))
        + stat("Tabs", totals["tabs"], "one per folder")
        + stat("Dividers", totals["dividers"], "one per document")
        + stat("Files", totals["files"])
        + "</tr></table>"
        + f'<p style="font-size:12px;color:#6B6480;margin:0 0 8px">Sheets of paper: {SHEETS_EXPLANATION}.'
        + (f' &nbsp;<span class="badge">{totals["needs_decision"]} need a decision</span>'
           if totals["needs_decision"] else "")
        + (f' &nbsp;<span class="badge grey">{totals["not_countable"]} not countable, passed on anyway</span>'
           if totals["not_countable"] else "")
        + "</p>"
    )

    spreadsheets = structure.spreadsheet_documents(documents)
    spreadsheet_body = ""
    if spreadsheets:
        sheet_rows = ""
        for d in spreadsheets:
            info = structure.describe_spreadsheet(d)
            estimated = not info["print_size"].startswith("Set in file")
            row_class = ' class="flagged"' if estimated else ""
            sheet_rows += (f'<tr{row_class}><td>{_e(info["name"])}</td>'
                           f'<td>{_e(info["folder"])}</td><td class="qty">{info["worksheets"]}</td>'
                           f'<td class="qty">{info["pages"]}</td><td>{_e(info["sizes"])}</td>'
                           f'<td>{_e(info["print_size"])}</td></tr>')
        spreadsheet_body = (
            '<table><tr><th>File</th><th>Folder</th><th>Worksheets</th><th>Est. pages</th>'
            f'<th>Sizes</th><th>Print size</th></tr>{sheet_rows}</table>')

    suggestions = build_scaling_suggestions(rows)
    scaling_body = ""
    if suggestions:
        groups = group_scaling_suggestions(suggestions)
        scale_rows = ""
        for group in groups:
            for entry in group["sizes"]:
                scale_rows += (f'<tr><td>{_e(group["suggestion"])}</td><td>{_e(entry["size"])}</td>'
                               f'<td class="qty">{entry["qty"]}</td></tr>')
            scale_rows += (f'<tr class="subtotal"><td><b>{_e(group["suggestion"])} &ndash; subtotal</b></td>'
                           f'<td>{len(group["sizes"])} size{"" if len(group["sizes"]) == 1 else "s"}</td>'
                           f'<td class="qty">{group["qty"]}</td></tr>')
        scale_rows += (f'<tr class="grandtotal"><td><b>All odd sizes</b></td>'
                       f'<td>{len(suggestions)} size{"" if len(suggestions) == 1 else "s"}</td>'
                       f'<td class="qty">{sum(g["qty"] for g in groups)}</td></tr>')
        scaling_body = (
            '<p style="font-size:12px;color:#6B6480;margin:0 0 8px">Sizes that don\'t match a recognised '
            'size, and what would fit them, with a subtotal for each suggestion. Shown on its own, not '
            'counted into the totals above.</p>'
            f'<table><tr><th>Suggestion</th><th>Size</th><th>Quantity</th></tr>{scale_rows}</table>')

    duplicates = structure.find_possible_duplicates(documents)
    duplicates_body = ""
    if duplicates:
        dup_rows = ""
        for group in duplicates:
            verdict = ("same file size too - almost certainly the same file"
                       if group["identical_files"] else "same pages and sizes - check they're not the same job twice")
            for i, d in enumerate(group["documents"]):
                first = ' class="subtotal"' if i == 0 else ""
                dup_rows += (f'<tr{first}><td>{_e(d["name"])}</td><td>{_e(d["folder"])}</td>'
                             f'<td class="qty">{group["pages"]}</td><td>{_mb(d.get("bytes"))}</td>'
                             f'<td>{verdict if i == 0 else ""}</td></tr>')
        saved = sum(g["extra_pages"] for g in duplicates)
        duplicates_body = (
            f'<p style="font-size:12px;color:#6B6480;margin:0 0 8px">Documents with the same number of pages, '
            f'the same sizes, in the same order - so they may be copies of each other (two revisions, or '
            f'high and low resolution versions of the same thing). Printing one of each would save about '
            f'{_plural(saved, "page")}. Nothing has been left out of the totals.</p>'
            f'<table><tr><th>Document</th><th>Folder</th><th>Pages</th><th>File size</th>'
            f'<th></th></tr>{dup_rows}</table>')

    split_combined = structure.find_split_and_combined(documents)
    if split_combined:
        sc_rows = "".join(
            f'<tr><td>{_e(item["document"])}</td><td>{_e(item["folder"])}</td>'
            f'<td class="qty">{item["pages"]}</td>'
            f'<td>also here as {item["parts"]} separate files totalling {item["part_pages"]} pages, '
            f'in {_e(item["where"])}</td></tr>' for item in split_combined)
        duplicates_body += (
            '<h4 style="margin:18px 0 6px;color:#1C0072">The same document, whole and in pieces</h4>'
            '<p style="font-size:12px;color:#6B6480;margin:0 0 8px">A file that looks like a complete document, '
            'where the same document is also present split into chapters. Print both and you print it twice.</p>'
            f'<table><tr><th>Document</th><th>Folder</th><th>Pages</th><th></th></tr>{sc_rows}</table>')

    section_body = ""
    section_list = structure.sections(documents)
    if len(section_list) > 1:
        blocks = ""
        for name, docs in section_list:
            section_rows = [r for d in docs for r in d.rows]
            rows_html = ""
            for entry in build_size_summary(section_rows):
                cls = ' class="nonstandard-summary"' if not entry["is_standard"] else ""
                label = entry["label"] + (" (total)" if entry["breakdown"] else "")
                rows_html += f'<tr{cls}><td>{_e(label)}</td><td class="qty">{entry["qty"]}</td></tr>'
                for size_label, qty in entry["breakdown"]:
                    rows_html += (f'<tr class="nonstandard-sub"><td style="padding-left:28px">'
                                  f'{_e(size_label)}</td><td class="qty">{qty}</td></tr>')
            blocks += (f'<h4 style="margin:14px 0 6px;color:#1C0072">&#128193; {_e(name)}'
                       f'<span style="font-weight:normal;font-size:12px;color:#607d8b"> &middot; '
                       f'{_plural(len(docs), "document")} &middot; {_plural(len(section_rows), "page")} '
                       f'&middot; {_plural(estimate_sheets(section_rows), "sheet")}</span></h4>'
                       f'<table style="max-width:520px"><tr><th>Size</th><th>Quantity</th></tr>{rows_html}</table>')
        section_body = ('<p style="font-size:12px;color:#6B6480;margin:0 0 8px">The same sizes, split by '
                        'top-level folder, so each chapter can be priced on its own.</p>' + blocks)

    separate = structure.separate_documents(documents)
    separate_ids = {id(entry["document"]) for entry in separate}
    binder_rows = [r for d in documents if id(d) not in separate_ids for r in d.rows]
    small_rows = [r for r in binder_rows if r.matched_size in DOUBLE_SIDED_SIZES]
    a3_rows = [r for r in binder_rows if r.matched_size == "A3"]
    other_rows = [r for r in binder_rows
                  if r.matched_size not in DOUBLE_SIDED_SIZES and r.matched_size != "A3"
                  and r.width_mm is not None]
    standard_rows = ""
    for label, subset, note in (
            ("A4 and smaller, printed double-sided", small_rows, "two pages to a sheet, within each document"),
            ("A3 plans, single-sided and folded to A4", a3_rows, "one page to a sheet"),
            ("Larger formats, folded to keep their scale", other_rows, "one page to a sheet")):
        if not subset:
            continue
        standard_rows += (f'<tr><td>{label}</td><td class="qty">{len(subset):,}</td>'
                          f'<td class="qty">{estimate_sheets(subset):,}</td>'
                          f'<td style="font-size:12px;color:#6B6480">{note}</td></tr>')
    binder_total = (f'<tr class="subtotal"><td><b>Into binders</b></td>'
                    f'<td class="qty">{len(binder_rows):,}</td>'
                    f'<td class="qty">{totals["sheets_in_binders"]:,}</td>'
                    f'<td style="font-size:12px;color:#6B6480">'
                    f'{_plural(totals["folders_needed"], "binder")} at {SHEETS_PER_FOLDER} sheets each, '
                    f'{_plural(totals["tabs"], "tab")}, {_plural(totals["dividers"], "divider")}</td></tr>')
    standard_body = (
        '<p style="font-size:12px;color:#6B6480;margin:0 0 8px">What we would produce from these files, the way '
        'planning sets are always made up. The totals above count what\'s in the files; this is the job.</p>'
        f'<table><tr><th>In the binders</th><th>Pages</th><th>Sheets</th><th></th></tr>'
        f'{standard_rows}{binder_total}</table>')
    if separate:
        sep_rows = "".join(
            f'<tr><td>{_e(entry["name"])}</td><td>{_e(entry["folder"])}</td>'
            f'<td style="font-size:12px">{_e(entry["kind"])}</td>'
            f'<td class="qty">{entry["pages"]}</td><td>{_e(entry["binding"])}</td>'
            f'<td style="font-size:12px;color:#8a5a00">{_e(entry["note"])}</td></tr>'
            for entry in separate)
        standard_body += (
            f'<h4 style="margin:18px 0 6px;color:#1C0072">Produced separately '
            f'<span style="font-weight:normal;font-size:12px;color:#607d8b">&middot; '
            f'{_plural(len(separate), "document")} &middot; {_plural(sum(e["pages"] for e in separate), "page")}'
            f'</span></h4>'
            '<p style="font-size:12px;color:#6B6480;margin:0 0 8px">Design and access statements and '
            'non-technical summaries are bound as their own documents, so they are not counted into the '
            'binders above. They still get a tab in the set.</p>'
            f'<table><tr><th>Document</th><th>Folder</th><th>What it is</th><th>Pages</th>'
            f'<th>Binding</th><th></th></tr>{sep_rows}</table>')
    else:
        standard_body += ('<p style="font-size:13px;color:#555">No design and access statement or '
                          'non-technical summary spotted in this job, so everything goes into the binders.</p>')
    if totals["needs_decision"] or totals["not_countable"]:
        standard_body += (
            f'<h4 style="margin:18px 0 6px;color:#1C0072">Before we print</h4>'
            f'<ul style="font-size:14px;margin:0;padding-left:20px">'
            + (f'<li><b>{totals["needs_decision"]}</b> item(s) need a decision - odd sizes, unreadable files '
               f'and estimated sizes. See Suggested scaling.</li>' if totals["needs_decision"] else "")
            + (f'<li><b>{totals["not_countable"]}</b> file(s) we can\'t count - they are passed on with the '
               f'job and listed in the full breakdown.</li>' if totals["not_countable"] else "")
            + '</ul>')

    tree = structure.build_tree(documents)
    tree_body = ""
    if tree.children:
        tree_body = f'<div class="tree" style="font-size:14px;line-height:1.5;margin-bottom:24px">{_tree_html(tree, collapsible)}'
        if tree.documents:
            loose_pages = sum(d.pages for d in tree.documents)
            tree_body += (f'<p style="font-size:13px;color:#555">Plus {_plural(len(tree.documents), "file")} not in a '
                          f'folder ({_plural(loose_pages, "page")}).</p>')
        tree_body += "</div>"
        if collapsible:
            tree_body = '<p style="font-size:12px;color:#6B6480;margin:0 0 8px">Click &ldquo;show files&rdquo; to see the documents in a folder.</p>' + tree_body

    sizes_body = f"""<table>
      <tr><th>Size</th><th>Quantity</th><th>Flag</th></tr>
      {summary_rows_html}
    </table>"""
    breakdown_body = f"""<table>
      <tr><th>File</th><th>Location</th><th>Type</th><th>Item</th><th>Width (mm)</th><th>Height (mm)</th>
          <th>Matched size</th><th>Flag</th><th>Notes</th></tr>
      {detail_rows_html}
    </table>"""

    display_ref = reference_number or submission_id
    heading = (f"{APP_NAME} page count - ref {display_ref}" if count_only
               else f"{APP_NAME} quote request - ref {display_ref}")
    if title:
        heading = _e(title)

    html = f"""<html><head><meta charset="utf-8">{style}</head><body>
    {_report_bar(display_ref)}
    <div class="fp-body">
    <h2>{heading}</h2>
    {plans_html}
    {count_only_banner}
    {contact_html}
    <div class="summary">
      <p>Received: {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
      {totals_html}
      {note_html}
      {files_link_html}
    </div>

    {_sections([("As per the Standard", standard_body),
                (f"Spreadsheets ({len(spreadsheets)})", spreadsheet_body),
                ("Quantity by size", sizes_body),
                ("Sizes by section", section_body),
                ("Suggested scaling", scaling_body),
                (f"Possible duplicates ({len(duplicates)})", duplicates_body),
                ("Folder structure", tree_body),
                ("Full breakdown", breakdown_body)], collapsible)}
    <p style="margin-top:16px;font-size:12px;color:#6B6480">
      {APP_NAME} V{VERSION}. Full-size estimates for images and unformatted spreadsheets are approximations - see the
      accompanying README for details.
      {"Files used for this page count were analysed and then deleted immediately - nothing was kept."
       if count_only else "The portal deletes its own copy of the original files once they've been passed on."}
    </p>
    </div>
    </body></html>"""
    return html
