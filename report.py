"""Builds the report Kaye receives: an HTML email body + a CSV attachment,
from the list of Row objects produced by the analyzer."""

import csv
import html as _html
import io
from collections import OrderedDict
from datetime import datetime

import structure


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
SHEETS_EXPLANATION = "A4, A5, A6 & US Letter printed double-sided, larger sizes single-sided"


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


def job_totals(rows, folder_info=None, documents=None):
    """The headline numbers shown together at the top of the report."""
    documents = documents if documents is not None else structure.build_documents(rows, folder_info)
    return {
        "pages": len(rows),
        "sheets": estimate_sheets(rows),
        "tabs": structure.count_tabs(documents),
        "dividers": len(documents),
        "files": len(set(r.source_file for r in rows)),
        "flagged": sum(1 for r in rows if r.flagged),
    }


def plans_label(plans_to_scale):
    if plans_to_scale is None:
        return ""
    return "PRINTED TO SCALE" if plans_to_scale else "A3 FOLDED (not to scale)"


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
    writer.writerow(["Tabs (one per folder)", totals["tabs"]])
    writer.writerow(["Dividers (one per document)", totals["dividers"]])
    writer.writerow(["Files uploaded", totals["files"]])
    writer.writerow(["Flagged for review", totals["flagged"]])
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

    writer.writerow(["QUANTITY BY SIZE"])
    writer.writerow(["Size", "Quantity", "Standard?"])
    for s in summary:
        label = s["label"] + (" (total)" if s["breakdown"] else "")
        writer.writerow([label, s["qty"], "Yes" if s["is_standard"] else "No"])
        for size_label, qty in s["breakdown"]:
            # plain "x": Excel can garble the multiply sign in a CSV
            writer.writerow(["    " + size_label.replace("×", "x"), qty, "No"])
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
                      f'color:#2980b9">show {n} file{"" if n == 1 else "s"}</summary>'
                      f'<ul style="{_TREE_CHILDREN}">' + "".join(_doc_line(d) for d in child.documents)
                      + "</ul></details>")
        if child.children:
            inner += _tree_html(child, interactive)
        items.append(f'<li style="{_TREE_FOLDER}">{label}{inner}</li>')
    if not items:
        return ""
    style = _TREE_CHILDREN if node.name is not None else "margin:0;padding:0;list-style:none"
    return f'<ul style="{style}">' + "".join(items) + "</ul>"


def _section(title, body, collapsible, open_=True):
    if not body:
        return ""
    if collapsible:
        return (f'<details class="section"{" open" if open_ else ""}><summary><h3>{title}</h3></summary>'
                f'{body}</details>')
    return f"<h3>{title}</h3>{body}"


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

    style = """
    <style>
      body { font-family: Arial, Helvetica, sans-serif; color: #222; }
      table { border-collapse: collapse; width: 100%; font-size: 13px; margin-bottom: 24px; }
      th, td { border: 1px solid #ccc; padding: 6px 8px; text-align: left; }
      th { background: #2c3e50; color: #fff; }
      tr.flagged { background: #fff3cd; }
      tr.nonstandard-summary { background: #fff3cd; }
      tr.nonstandard-sub { background: #fffaeb; color: #555; }
      .summary { margin-bottom: 16px; }
      .badge { display:inline-block; background:#c0392b; color:#fff; padding:2px 8px; border-radius:10px; font-size:12px; }
      h3 { color: #2c3e50; margin-top: 0; }
      .qty { font-weight: bold; text-align: right; }
      details.section { margin: 0 0 18px; border-top: 1px solid #e0e0e0; padding-top: 12px; }
      details.section > summary { cursor: pointer; list-style: none; }
      details.section > summary::-webkit-details-marker { display: none; }
      details.section > summary h3 { display: inline-block; margin: 0 0 12px; }
      details.section > summary h3::before { content: "\\25B8  "; color: #90a4ae; }
      details.section[open] > summary h3::before { content: "\\25BE  "; }
      .tree summary::-webkit-details-marker { color: #90a4ae; }
    </style>
    """

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
            '<p style="background:#eaf3fa;border:1px solid #b8d9ec;padding:10px 14px;'
            'border-radius:8px;color:#1c5a80">This is a self-service page count only &ndash; '
            'nothing has been sent to us, and your files were deleted as soon as they\'d been counted.</p>'
        )
    if banner:
        count_only_banner = (
            '<p style="background:#eaf3fa;border:1px solid #b8d9ec;padding:10px 14px;'
            f'border-radius:8px;color:#1c5a80">{_e(banner)}</p>'
        )

    plans_html = ""
    if plans_to_scale is not None:
        if plans_to_scale:
            plans_html = ('<p style="background:#1e3a5f;color:#fff;padding:12px 16px;border-radius:8px;'
                          'font-size:17px;margin:0 0 16px"><b>&#128208; PLANS: PRINTED TO SCALE</b></p>')
        else:
            plans_html = ('<p style="background:#f39c12;color:#222;padding:12px 16px;border-radius:8px;'
                          'font-size:17px;margin:0 0 16px"><b>&#128208; PLANS: A3 FOLDED</b> '
                          '<span style="font-size:13px">(not to scale)</span></p>')

    if wetransfer_link:
        files_link_html = (
            f'<p style="margin-top:10px"><a href="{wetransfer_link}" '
            f'style="display:inline-block;background:#2980b9;color:#fff;padding:8px 16px;'
            f'border-radius:6px;text-decoration:none;font-weight:bold">Download original files</a>'
            f'<br><span style="font-size:11px;color:#888">{_e(wetransfer_link)}</span></p>'
        )
    elif wetransfer_error:
        files_link_html = (
            f'<p style="margin-top:10px;color:#c0392b;font-size:12px">'
            f'Couldn\'t create a download link for the original files: {_e(wetransfer_error)} '
            f'(Small files may also be attached to this email.)</p>'
        )
    else:
        files_link_html = ""

    def stat(label, value, note=""):
        note_html_ = f'<div style="font-size:11px;color:#78909c;margin-top:2px">{note}</div>' if note else ""
        return (f'<td style="border:1px solid #dfe6ea;background:#f7f9fa;padding:10px 12px;text-align:center;'
                f'vertical-align:top"><div style="font-size:12px;color:#546e7a">{label}</div>'
                f'<div style="font-size:22px;font-weight:bold;color:#2c3e50">{value:,}</div>{note_html_}</td>')
    totals_html = (
        '<table style="width:auto;margin:8px 0 14px"><tr>'
        + stat("Pages / items", totals["pages"])
        + stat("Sheets of paper", totals["sheets"], "estimate")
        + stat("Tabs", totals["tabs"], "one per folder")
        + stat("Dividers", totals["dividers"], "one per document")
        + stat("Files", totals["files"])
        + "</tr></table>"
        + f'<p style="font-size:12px;color:#777;margin:0 0 8px">Sheets of paper: {SHEETS_EXPLANATION}.'
        + (f' &nbsp;<span class="badge">{totals["flagged"]} flagged for review</span>' if totals["flagged"] else "")
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
            tree_body = '<p style="font-size:12px;color:#777;margin:0 0 8px">Click &ldquo;show files&rdquo; to see the documents in a folder.</p>' + tree_body

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
    heading = f"Page count summary - ref {display_ref}" if count_only else f"New quote request - ref {display_ref}"
    if title:
        heading = _e(title)

    html = f"""<html><head><meta charset="utf-8">{style}</head><body>
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

    {_section(f"Spreadsheets ({len(spreadsheets)}) - check these", spreadsheet_body, collapsible)}
    {_section("Quantity by size (all files combined)", sizes_body, collapsible)}
    {_section("Folders", tree_body, collapsible)}
    {_section("Full breakdown - every page / slide / sheet / image", breakdown_body, collapsible, open_=False)}
    <p style="margin-top:16px;font-size:12px;color:#777">
      Full-size estimates for images and unformatted spreadsheets are approximations - see the
      accompanying README for details.
      {"Files used for this page count were analysed and then deleted immediately - nothing was kept."
       if count_only else "The portal deletes its own copy of the original files once they've been passed on."}
    </p>
    </body></html>"""
    return html
