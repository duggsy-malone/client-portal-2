"""Builds the report Kaye receives: an HTML email body + a CSV attachment,
from the list of Row objects produced by the analyzer."""

import csv
import html as _html
import io
from collections import OrderedDict
from datetime import datetime


def _e(v):
    """Escape anything that came from a client (names, notes, file names) before it goes into HTML."""
    return _html.escape("" if v is None else str(v))


def _fmt(v):
    return "" if v is None else v


def build_size_summary(rows):
    """Group every analysed item by its matched size label and count them,
    so the top of the report answers 'how many of each size in total'
    before anyone has to read the itemised list.

    Returns a list of dicts: {label, qty, is_standard}, largest quantity first,
    with items that have no measurable size (unsupported/unreadable files,
    empty sheets) grouped last under 'Not sized'.
    """
    counts = OrderedDict()
    standard_flags = {}
    for r in rows:
        if r.width_mm is None or r.height_mm is None:
            label = "Not sized (other file types, unreadable or empty - check manually)"
            is_standard = True  # not a "review this size" flag - different reason
        else:
            label = r.matched_size or "Non-standard"
            is_standard = r.is_standard
        counts[label] = counts.get(label, 0) + 1
        standard_flags[label] = is_standard

    summary = [{"label": label, "qty": qty, "is_standard": standard_flags[label]}
               for label, qty in counts.items()]
    # Sort: standard sizes by quantity desc, then non-standard/not-sized at the bottom.
    summary.sort(key=lambda s: (s["label"].startswith("Not sized"), not s["is_standard"], -s["qty"]))
    return summary


def rows_to_csv(rows):
    buf = io.StringIO()
    writer = csv.writer(buf)

    total_items = len(rows)
    flagged_count = sum(1 for r in rows if r.flagged)
    summary = build_size_summary(rows)

    writer.writerow(["SUMMARY - total quantity by page/item size"])
    writer.writerow(["Size", "Quantity", "Standard?"])
    for s in summary:
        writer.writerow([s["label"], s["qty"], "Yes" if s["is_standard"] else "No"])
    writer.writerow([])
    writer.writerow(["Total items", total_items])
    writer.writerow(["Flagged for review", flagged_count])
    writer.writerow([])
    writer.writerow([])

    writer.writerow(["DETAIL - every page/slide/sheet/image individually"])
    writer.writerow(["Uploaded file", "Location in archive", "Type", "Item",
                      "Width (mm)", "Height (mm)", "Matched size", "Standard?", "Flagged", "Notes"])
    for r in rows:
        d = r.as_dict()
        writer.writerow([
            d["source_file"], d["location"], d["file_type"], d["unit_label"],
            d["width_mm"], d["height_mm"], d["matched_size"],
            "Yes" if d["is_standard"] else "No",
            "YES - REVIEW" if d["flagged"] else "",
            d["notes"],
        ])
    return buf.getvalue()


def rows_to_html(rows, submission_id, client_note="", wetransfer_link=None, wetransfer_error=None,
                  contact=None, count_only=False, reference_number=None):
    flagged_count = sum(1 for r in rows if r.flagged)
    files = sorted(set(r.source_file for r in rows))
    summary = build_size_summary(rows)

    style = """
    <style>
      body { font-family: Arial, Helvetica, sans-serif; color: #222; }
      table { border-collapse: collapse; width: 100%; font-size: 13px; margin-bottom: 24px; }
      th, td { border: 1px solid #ccc; padding: 6px 8px; text-align: left; }
      th { background: #2c3e50; color: #fff; }
      tr.flagged { background: #fff3cd; }
      tr.nonstandard-summary { background: #fff3cd; }
      .summary { margin-bottom: 16px; }
      .badge { display:inline-block; background:#c0392b; color:#fff; padding:2px 8px; border-radius:10px; font-size:12px; }
      h3 { color: #2c3e50; margin-top: 0; }
      .qty { font-weight: bold; text-align: right; }
    </style>
    """

    summary_rows_html = ""
    for s in summary:
        cls = ' class="nonstandard-summary"' if not s["is_standard"] else ""
        flag_text = "&#9888; REVIEW" if not s["is_standard"] else ""
        summary_rows_html += f"""<tr{cls}>
            <td>{_e(s['label'])}</td>
            <td class="qty">{s['qty']}</td>
            <td>{flag_text}</td>
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

    display_ref = reference_number or submission_id
    heading = f"Page count summary - ref {display_ref}" if count_only else f"New quote request - ref {display_ref}"

    html = f"""<html><head>{style}</head><body>
    <h2>{heading}</h2>
    {count_only_banner}
    <div class="summary">
      <p>Received: {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
      <p>Files uploaded: {len(files)} &nbsp;|&nbsp; Items analysed: {len(rows)}
        {'&nbsp;|&nbsp; <span class="badge">' + str(flagged_count) + ' flagged for review</span>' if flagged_count else ''}
      </p>
      {note_html}
      {files_link_html}
    </div>

    {contact_html}

    <h3>Quantity by size (all files combined)</h3>
    <table>
      <tr><th>Size</th><th>Quantity</th><th>Flag</th></tr>
      {summary_rows_html}
    </table>

    <h3>Full breakdown - every page / slide / sheet / image</h3>
    <table>
      <tr><th>File</th><th>Location</th><th>Type</th><th>Item</th><th>Width (mm)</th><th>Height (mm)</th>
          <th>Matched size</th><th>Flag</th><th>Notes</th></tr>
      {detail_rows_html}
    </table>
    <p style="margin-top:16px;font-size:12px;color:#777">
      Full-size estimates for images and unformatted spreadsheets are approximations - see the
      accompanying README for details.
      {"Files used for this page count were analysed and then deleted immediately - nothing was kept."
       if count_only else "Original files are stored on the server under this submission's folder for 30 days."}
    </p>
    </body></html>"""
    return html
