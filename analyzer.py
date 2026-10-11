"""
Core analysis engine for the client upload portal.

Given a file (PDF, JPG/JPEG/PNG, ZIP, PPTX, XLSX), returns a list of "rows"
describing each page / slide / sheet / image found, its estimated physical
size in mm, whether that matches a standard paper/slide size, and a flag
for anything unusual that needs a human look before quoting.

Design notes / honest limitations (see README for the full explanation):
  - JPG/PNG: physical size is only as good as the DPI metadata embedded in
    the file. Where no DPI tag exists we assume 300 DPI and say so.
  - PPTX: a .pptx has ONE slide size for the whole deck (PowerPoint does not
    support mixed slide sizes), so all slides share one measurement.
  - XLSX: Excel doesn't reliably store a physical "page size" unless the
    print setup was explicitly configured. Where it was, we use it
    (authoritative). Where it wasn't, we estimate the used range's physical
    size from column widths / row heights, which is an approximation, not
    an exact print-page size.
  - ZIP: opened one file at a time (never unpacked as a whole), recursively
    and depth-limited, with every file inside analysed and listed
    individually, prefixed with its path inside the archive. Memory and disk
    stay flat however big the archive is.
"""

import os
import zipfile
import tempfile
import shutil
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass, field, asdict

from paper_sizes import (
    STANDARD_PAGE_SIZES,
    STANDARD_SLIDE_SIZES,
    EXCEL_PAPER_SIZE_CODES,
    LABEL_ALIASES,
    match_size,
    match_page_size,
    mm_from_points,
    mm_from_emu,
    mm_from_pixels,
)

SUPPORTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".zip", ".pptx", ".xlsx", ".xlsm"}
ASSUMED_IMAGE_DPI = 300
MAX_ZIP_DEPTH = 3
# ZIPs are opened one file at a time, so their total size doesn't matter -
# these guard against the two things that do: an archive with a silly number
# of files in it, and a "zip bomb" entry that expands enormously.
MAX_ZIP_ENTRIES = 20000
MAX_ZIP_EXPANSION_RATIO = 200
MAX_ZIP_ENTRY_CHECK_BYTES = 100 * 1024 * 1024  # only large entries are ratio-checked

# A single file bigger than this isn't opened at all. Reading a very large
# file can use more memory than the server has, which takes the whole job
# down with it - one file listed as "check manually" is a much better
# outcome. ZIPs are exempt: they're opened one file at a time, so their own
# size doesn't matter. Raise it with ANALYSE_MAX_FILE_MB if there's memory.
MAX_ANALYSE_BYTES = int(os.environ.get("ANALYSE_MAX_FILE_MB", "750")) * 1024 * 1024


@dataclass
class Row:
    source_file: str        # top-level uploaded filename
    location: str           # path within archive, or "" if not from a zip
    file_type: str          # PDF / JPG / PPTX / XLSX / ...
    unit_label: str         # "Page 1", "Slide 3", "Sheet: Budget", "Image"
    width_mm: float = None
    height_mm: float = None
    matched_size: str = ""
    is_standard: bool = True
    flagged: bool = False
    notes: str = ""
    source_bytes: int = None   # how big the file is on disk, for spotting copies
    items: int = 1             # pages/sides this row stands for (see analyze_docx)

    def as_dict(self):
        d = asdict(self)
        for k in ("width_mm", "height_mm"):
            if d[k] is not None:
                d[k] = round(d[k], 1)
        return d


# The brand guide's wording for a file we can't open (page 7): say what to do
# next, don't just report a failure. The technical reason is kept in brackets
# because the studio reads the same report.
UNREADABLE = ("We can't open this file. It may be password-protected. Remove the password and "
              "check it in again.")


def _unreadable(kind, error):
    return f"{UNREADABLE} ({kind}: {error})"


def _bleed_note(m):
    """A sentence naming the bleed, when a size only matched because it had
    some. Blank otherwise, so it can always be appended."""
    if not getattr(m, "bleed_mm", None):
        return ""
    return (f"Supplied with {m.bleed_mm:g} mm bleed "
            f"({abs(m.width_mm):.0f} x {abs(m.height_mm):.0f} mm), so it is counted as "
            f"{m.label} and will need trimming.")


# Excel's paper-size names that don't match the keys in STANDARD_PAGE_SIZES.
DECLARED_ALIASES = {
    "Tabloid": "Tabloid/Ledger",
    "Ledger": "Tabloid/Ledger",
}


def analyze_pdf(path, source_file, location=""):
    rows = []
    try:
        from pypdf import PdfReader
        # Pass an open file, not the path: given a path, pypdf reads the whole
        # file into memory first (+240 MB for a 250 MB PDF), which is enough to
        # crash a 512 MB server. Given a file, it reads only what it needs.
        pdf_file = open(path, "rb")
        reader = PdfReader(pdf_file)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                rows.append(Row(source_file, location, "PDF", "Whole document",
                                 flagged=True, notes="Password-protected - could not open to inspect pages."))
                return rows
        for i, page in enumerate(reader.pages, start=1):
            box = page.mediabox
            # Some PDFs define the page corners "upside down", which makes the
            # height (or width) come out negative, e.g. 594 x -841. The size
            # is the same either way, so drop the minus sign.
            w_mm = abs(mm_from_points(float(box.width)))
            h_mm = abs(mm_from_points(float(box.height)))
            m = match_page_size(w_mm, h_mm)
            rows.append(Row(
                source_file, location, "PDF", f"Page {i}",
                width_mm=w_mm, height_mm=h_mm,
                matched_size=m.label, is_standard=m.is_standard,
                flagged=not m.is_standard,
                notes=(_bleed_note(m) if m.is_standard
                       else "Non-standard page size for a PDF page.")
            ))
    except Exception as e:
        rows.append(Row(source_file, location, "PDF", "Whole document",
                         flagged=True, notes=_unreadable("PDF", e)))
    finally:
        try:
            pdf_file.close()
        except NameError:
            pass
    return rows


def analyze_image(path, source_file, location=""):
    rows = []
    try:
        from PIL import Image
        with Image.open(path) as img:
            width_px, height_px = img.size
            dpi = img.info.get("dpi")
            assumed = False
            if dpi and dpi[0] and dpi[1]:
                dpi_x, dpi_y = dpi
            else:
                dpi_x = dpi_y = ASSUMED_IMAGE_DPI
                assumed = True
            w_mm = mm_from_pixels(width_px, dpi_x)
            h_mm = mm_from_pixels(height_px, dpi_y)
            m = match_page_size(w_mm, h_mm)
            notes = []
            if assumed:
                notes.append(f"No DPI metadata found - assumed {ASSUMED_IMAGE_DPI} DPI to estimate print size.")
            if not m.is_standard:
                notes.append("Estimated print size doesn't match a standard paper size.")
            elif _bleed_note(m):
                notes.append(_bleed_note(m))
            rows.append(Row(
                source_file, location, "Image", f"{width_px}x{height_px}px",
                width_mm=w_mm, height_mm=h_mm,
                matched_size=m.label, is_standard=m.is_standard,
                flagged=assumed or (not m.is_standard),
                notes=" ".join(notes)
            ))
    except Exception as e:
        rows.append(Row(source_file, location, "Image", "Whole file",
                         flagged=True, notes=_unreadable("Image", e)))
    return rows


def _local(tag):
    """XML tag name without its namespace, e.g. '{...main}sheet' -> 'sheet'."""
    return tag.rsplit("}", 1)[-1]


def _zip_part_path(base_dir, target):
    import posixpath
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(base_dir, target))


def analyze_pptx(path, source_file, location=""):
    """Slide size and count, read straight from the deck's small
    presentation.xml. (python-pptx loads every image in the deck into memory
    - +180 MB for a 150 MB deck - which a 512 MB server can't afford.)"""
    rows = []
    try:
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(path) as zf:
            main = "ppt/presentation.xml"
            try:
                rels = ET.fromstring(zf.read("_rels/.rels"))
                for rel in rels:
                    if rel.get("Type", "").endswith("/officeDocument"):
                        main = _zip_part_path("", rel.get("Target", main))
            except KeyError:
                pass
            root = ET.fromstring(zf.read(main))
        size = next((el for el in root.iter() if _local(el.tag) == "sldSz"), None)
        if size is None:
            raise ValueError("no slide size found in presentation")
        w_mm = mm_from_emu(int(size.get("cx")))
        h_mm = mm_from_emu(int(size.get("cy")))
        slide_count = sum(1 for el in root.iter() if _local(el.tag) == "sldId")
        m = match_size(w_mm, h_mm, STANDARD_SLIDE_SIZES)
        # A deck is one row standing for all of its slides, the same way a
        # Word document is one row standing for all of its pages - PowerPoint
        # has a single slide size per file, so there's nothing to split.
        rows.append(Row(
            source_file, location, "PPTX", f"{slide_count} slide(s) @ deck size",
            width_mm=w_mm, height_mm=h_mm,
            matched_size=m.label, is_standard=m.is_standard,
            flagged=not m.is_standard, items=max(1, slide_count),
            notes="" if m.is_standard else "Non-standard slide size for the whole deck."
        ))
    except Exception as e:
        rows.append(Row(source_file, location, "PPTX", "Whole file",
                         flagged=True, notes=_unreadable("PowerPoint", e)))
    return rows


def _excel_col_width_to_mm(width_chars):
    # Approximation for the default Calibri 11 font (~7px max digit width @ 96 DPI).
    if width_chars is None:
        width_chars = 8.43  # Excel default
    width_px = width_chars * 7 + 5
    return mm_from_pixels(width_px, 96)


def _excel_row_height_to_mm(height_pt):
    if height_pt is None:
        height_pt = 15.0  # Excel default row height in points
    width_px = height_pt * 96 / 72
    return mm_from_pixels(width_px, 96)


def _col_letters_to_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def _col_index_to_letters(n):
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _scan_sheet(zf, part):
    """Streams through one worksheet's XML, keeping only what the size
    estimate needs: print setup, column widths, custom row heights and the
    extent of the cells that have something in them. Memory stays flat
    however big the sheet is.

    Only cells with a value or formula count towards the extent. Sheets often
    have formatting (borders, colours) applied to whole rows, which Excel
    stores as empty cells running out to the very last column (XFD) - counting
    those made one-page sheets come out 22 metres wide."""
    import re
    import xml.etree.ElementTree as ET
    info = {"paper": None, "orientation": None, "cols": [], "heights": {},
            "min_row": None, "max_row": None, "min_col": None, "max_col": None, "a1_has_value": False}
    sheet_data = None
    row_num = 0
    col_num = 0
    in_cell = None  # (row, col) of the cell being read
    cell_counted = False
    with zf.open(part) as fh:
        for event, el in ET.iterparse(fh, events=("start", "end")):
            name = _local(el.tag)
            if event == "start":
                if name == "sheetData":
                    sheet_data = el
                elif name == "row":
                    r = el.get("r")
                    row_num = int(r) if r else row_num + 1
                    col_num = 0
                    if el.get("ht"):
                        info["heights"][row_num] = float(el.get("ht"))
                elif name == "c":
                    ref = el.get("r")
                    m = re.match(r"([A-Z]+)(\d+)$", ref or "")
                    if m:
                        col_num = _col_letters_to_index(m.group(1))
                        row_here = int(m.group(2))
                    else:
                        col_num += 1
                        row_here = row_num
                    in_cell = (row_here, col_num)
                    cell_counted = False
            else:
                if name in ("v", "is", "f") and in_cell is not None and not cell_counted and (
                        name == "is" or (el.text or "") != ""):
                    cell_counted = True
                    row_here, col_here = in_cell
                    for key, val, fn in (("min_row", row_here, min), ("max_row", row_here, max),
                                         ("min_col", col_here, min), ("max_col", col_here, max)):
                        info[key] = val if info[key] is None else fn(info[key], val)
                    if in_cell == (1, 1):
                        info["a1_has_value"] = True
                elif name == "c":
                    in_cell = None
                elif name == "col":
                    info["cols"].append((int(el.get("min", 0)), int(el.get("max", 0)),
                                         float(el.get("width")) if el.get("width") else None))
                elif name == "pageSetup":
                    info["paper"] = el.get("paperSize")
                    info["orientation"] = el.get("orientation")
                if name == "row" and sheet_data is not None:
                    sheet_data.clear()  # drop finished rows so memory doesn't grow
    return info


# A spreadsheet estimated bigger than this on either side (a bit over A0's
# long side x 2) is almost certainly a quirk of the file, not a real print size.
MAX_PLAUSIBLE_SHEET_MM = 2500


def analyze_xlsx(path, source_file, location=""):
    """Per sheet: the declared print paper size if set, otherwise an estimate
    of the used range's physical size. Read by streaming the sheet XML rather
    than openpyxl, which builds an object per cell (+670 MB for a 6 MB,
    150,000-row sheet)."""
    rows = []
    try:
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(path) as zf:
            wb = ET.fromstring(zf.read("xl/workbook.xml"))
            rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            targets = {rel.get("Id"): rel.get("Target", "") for rel in rels}
            sheets = []
            for el in wb.iter():
                if _local(el.tag) != "sheet":
                    continue
                rid = next((v for k, v in el.attrib.items() if _local(k) == "id"), None)
                part = _zip_part_path("xl", targets.get(rid, ""))
                if "/worksheets/" in f"/{part}":  # skip chart sheets etc, like openpyxl's wb.worksheets
                    sheets.append((el.get("name"), part))

            for title, part in sheets:
                ws = _scan_sheet(zf, part)
                if ws["paper"] is not None:
                    try:
                        code_int = int(ws["paper"])
                    except (TypeError, ValueError):
                        code_int = None
                    declared = EXCEL_PAPER_SIZE_CODES.get(code_int)
                    if declared:
                        # "US Letter" is one key, not "US" - look the whole
                        # label up first, then the first word (e.g. "A2 (rotated)").
                        std_w, std_h = (STANDARD_PAGE_SIZES.get(declared)
                                        or STANDARD_PAGE_SIZES.get(DECLARED_ALIASES.get(declared, ""))
                                        or STANDARD_PAGE_SIZES.get(declared.split(" ")[0], (None, None)))
                        # A sheet that declares a paper size we hold no
                        # dimensions for must still be flagged - otherwise it
                        # is silently left out of every size and sheet total.
                        known = std_w is not None and std_h is not None
                        rows.append(Row(
                            source_file, location, "Excel", f"Sheet '{title}' (declared print setup)",
                            width_mm=std_w, height_mm=std_h,
                            matched_size=LABEL_ALIASES.get(declared, declared), is_standard=known,
                            flagged=not known,
                            notes=(f"Orientation: {ws['orientation'] or 'not set'}." if known else
                                   f"This sheet is set to print at {declared}, which we hold no "
                                   f"measurements for, so it isn't in the size totals - please check it "
                                   f"manually. Orientation: {ws['orientation'] or 'not set'}.")
                        ))
                    else:
                        rows.append(Row(
                            source_file, location, "Excel", f"Sheet '{title}' (declared print setup)",
                            matched_size=f"Unrecognised paper code {code_int}", is_standard=False,
                            flagged=True, notes="Sheet specifies a paper size we don't recognise - check manually."
                        ))
                    continue

                if ws["min_row"] is None or (
                        (ws["min_row"], ws["max_row"], ws["min_col"], ws["max_col"]) == (1, 1, 1, 1)
                        and not ws["a1_has_value"]):
                    rows.append(Row(source_file, location, "Excel", f"Sheet '{title}'",
                                     notes="Sheet appears empty.", flagged=False))
                    continue

                dim = (f"{_col_index_to_letters(ws['min_col'])}{ws['min_row']}:"
                       f"{_col_index_to_letters(ws['max_col'])}{ws['max_row']}")
                total_w_mm = 0.0
                for c in range(ws["min_col"], ws["max_col"] + 1):
                    width = next((w for lo, hi, w in ws["cols"] if lo <= c <= hi and w is not None), None)
                    total_w_mm += _excel_col_width_to_mm(width)
                total_h_mm = 0.0
                for r in range(ws["min_row"], ws["max_row"] + 1):
                    total_h_mm += _excel_row_height_to_mm(ws["heights"].get(r))

                if max(total_w_mm, total_h_mm) > MAX_PLAUSIBLE_SHEET_MM:
                    rows.append(Row(
                        source_file, location, "Excel", f"Sheet '{title}' (used range {dim})",
                        flagged=True,
                        notes=(f"No print size set, and the estimate came out implausibly large "
                               f"({total_w_mm:,.0f} x {total_h_mm:,.0f} mm) - check manually.")))
                    continue

                m = match_page_size(total_w_mm, total_h_mm)
                rows.append(Row(
                    source_file, location, "Excel", f"Sheet '{title}' (estimated from used range {dim})",
                    width_mm=total_w_mm, height_mm=total_h_mm,
                    matched_size=m.label, is_standard=m.is_standard,
                    flagged=True,  # always flag estimated (non-declared) sheets for a manual glance
                    notes=("No print area/paper size set in the file - size is an ESTIMATE from column/row "
                           "dimensions, not a true print size. Please sanity-check before quoting."
                           + ((" " + _bleed_note(m)) if _bleed_note(m) else ""))
                ))
    except Exception as e:
        rows.append(Row(source_file, location, "Excel", "Whole file",
                         flagged=True, notes=_unreadable("Spreadsheet", e)))
    return rows


# --------------------------------------------------------------------------
# Word documents
# --------------------------------------------------------------------------
# A .docx has no page count in it. It holds content, and the pages only exist
# once something lays it out - so the answer depends on the fonts installed,
# the paper size and even the Word version. What IS stored reliably is the
# page SIZE, which is what matters most for quoting, plus enough clues to put
# a floor under the page count:
#
#   w:lastRenderedPageBreak  where Word put the breaks the last time it
#                            repaginated. The best signal when it's there.
#   w:br w:type="page"       breaks the author typed in. A floor, not a count.
#   w:sectPr                 section breaks, which usually start a new page.
#   docProps/app.xml <Pages> a cached count from whatever last saved the file.
#                            Often absent, often stale - used only as a
#                            cross-check, never on its own.
#
# Whatever comes out is flagged, and the note says where the figure came from,
# so nobody quotes off it as though it were exact.

WORD_TWIPS_PER_MM = 1440.0 / 25.4   # 1440 twentieths of a point to the inch


def _twips_to_mm(value):
    return float(value) / WORD_TWIPS_PER_MM


def _docx_cached_pages(zf):
    """The page count cached by whatever last saved the file, or None."""
    try:
        with zf.open("docProps/app.xml") as handle:
            for _event, el in ET.iterparse(handle, events=("end",)):
                if _local(el.tag) == "Pages":
                    text = (el.text or "").strip()
                    return int(text) if text.isdigit() else None
                el.clear()
    except Exception:
        return None
    return None


def _docx_scan_body(zf):
    """One streamed pass over the document: page sizes, and the three kinds of
    break that hint at where the pages fall."""
    sizes = []            # (width_mm, height_mm) per section, in order
    rendered = 0          # w:lastRenderedPageBreak
    typed = 0             # w:br w:type="page"
    section_starts = 0    # sections that begin on a new page
    with zf.open("word/document.xml") as handle:
        for _event, el in ET.iterparse(handle, events=("end",)):
            tag = _local(el.tag)
            if tag == "lastRenderedPageBreak":
                rendered += 1
            elif tag == "br":
                for key, value in el.attrib.items():
                    if _local(key) == "type" and value == "page":
                        typed += 1
            elif tag == "pgSz":
                w = h = None
                for key, value in el.attrib.items():
                    name = _local(key)
                    if name == "w":
                        w = value
                    elif name == "h":
                        h = value
                if w and h:
                    try:
                        sizes.append((_twips_to_mm(w), _twips_to_mm(h)))
                    except ValueError:
                        pass
            elif tag == "sectPr":
                # A "continuous" section carries on down the same page; any
                # other kind starts a new one.
                kind = "nextPage"
                for child in el:
                    if _local(child.tag) == "type":
                        for key, value in child.attrib.items():
                            if _local(key) == "val":
                                kind = value
                if kind != "continuous":
                    section_starts += 1
                el.clear()
            elif tag in ("p", "tbl"):
                el.clear()   # keep memory flat on a long document
    return sizes, rendered, typed, section_starts


def analyze_docx(path, source_file, location=""):
    rows = []
    try:
        with zipfile.ZipFile(path) as zf:
            sizes, rendered, typed, section_starts = _docx_scan_body(zf)
            cached = _docx_cached_pages(zf)
    except Exception as e:
        return [Row(source_file, location, "DOCX", "Whole file", flagged=True,
                    notes=_unreadable("Word", e))]

    if not sizes:
        return [Row(source_file, location, "DOCX", "Whole file", flagged=True,
                    notes=("We opened this Word file but couldn't find a page size in it - please "
                           "check it manually, or send it as a PDF."))]

    # The last sectPr in the body applies to the final section, so the number
    # of section starts is one more than the number of breaks between them.
    floor = max(1, typed + max(0, section_starts - 1) + 1)
    if rendered:
        pages, source = rendered + 1, "as Word last laid it out"
    else:
        pages, source = floor, "a minimum - counted from the page and section breaks"
    if cached and cached > pages:
        pages, source = cached, "the count saved in the file by Word"

    # Group the sections by page size, biggest group first, so a document that
    # switches to landscape for its drawings shows both.
    grouped = OrderedDict()
    bleed_seen = ""
    for w, h in sizes:
        m = match_page_size(w, h)
        bleed_seen = bleed_seen or _bleed_note(m)
        key = (round(w, 1), round(h, 1), m.label, m.is_standard)
        grouped[key] = grouped.get(key, 0) + 1
    ordered = sorted(grouped.items(), key=lambda kv: -kv[1])

    note = (f"Word documents repaginate when they're opened, so this page count is {source}, "
            f"not a measurement. For an exact count, ask for it as a PDF.")
    if cached is not None and cached != pages:
        note += f" (The file's own saved count says {cached:,}.)"
    if len(ordered) > 1:
        note += " This document changes page size part-way through - the sizes are listed separately."
    if bleed_seen:
        note += " " + bleed_seen

    # All the pages are attributed to the dominant section's size, and any
    # other sizes are listed with no page count of their own, so nothing is
    # counted twice.
    for index, ((w, h, label, is_standard), sections) in enumerate(ordered):
        if index == 0:
            rows.append(Row(
                source_file, location, "DOCX",
                f"At least {pages} page(s) @ document size" if source.startswith("a minimum")
                else f"{pages} page(s) @ document size",
                width_mm=w, height_mm=h, matched_size=label, is_standard=is_standard,
                flagged=True, items=pages, notes=note))
        else:
            rows.append(Row(
                source_file, location, "DOCX",
                f"Also {sections} section(s) @ this size",
                width_mm=w, height_mm=h, matched_size=label, is_standard=is_standard,
                flagged=True, items=0,
                notes="Another page size used inside the same Word document - its pages are "
                      "included in the figure above."))
    return rows


DISPATCH = {
    ".pdf": analyze_pdf,
    ".jpg": analyze_image,
    ".jpeg": analyze_image,
    ".png": analyze_image,
    ".pptx": analyze_pptx,
    ".xlsx": analyze_xlsx,
    ".xlsm": analyze_xlsx,
    ".docx": analyze_docx,
    ".docm": analyze_docx,
    ".dotx": analyze_docx,
}


def _extract_one(zf, info, dest_dir):
    """Writes ONE file out of the archive to disk in small chunks, and returns
    its path. Nothing is held in memory, and the rest of the archive stays
    where it is - a 4 GB ZIP of drawings must not land on the disk (or in the
    memory) of a small server all at once."""
    dest = os.path.join(dest_dir, "entry" + os.path.splitext(info.filename)[1].lower())
    with zf.open(info) as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out, length=1024 * 1024)
    return dest


def _suspicious_entry(info):
    """A "zip bomb" is a small archive that expands to something enormous.
    Anything that expands more than this much, and is large with it, is
    listed rather than opened."""
    if info.file_size <= MAX_ZIP_ENTRY_CHECK_BYTES:
        return False
    if not info.compress_size:
        return False
    return info.file_size / info.compress_size > MAX_ZIP_EXPANSION_RATIO


def analyze_zip(path, source_file, location="", depth=0, on_item=None):
    """Every file inside the archive, one at a time: written out, analysed,
    deleted, then the next one. Folders inside the archive are kept in each
    row's location, so the report can show the structure."""
    rows = []
    if depth >= MAX_ZIP_DEPTH:
        rows.append(Row(source_file, location, "ZIP", "Whole archive",
                         flagged=True, notes="Archive nested too deeply - stopped unpacking for safety."))
        return rows
    tmpdir = tempfile.mkdtemp(prefix="zipscan_")
    try:
        with zipfile.ZipFile(path) as zf:
            entries = [i for i in zf.infolist() if not i.is_dir()]
            if len(entries) > MAX_ZIP_ENTRIES:
                rows.append(Row(source_file, location, "ZIP", "Whole archive", flagged=True,
                                 notes=(f"Archive holds {len(entries):,} files, more than this tool opens "
                                        f"({MAX_ZIP_ENTRIES:,}) - please check it manually.")))
                return rows
            for position, info in enumerate(entries, start=1):
                if on_item:
                    try:
                        on_item(position, len(entries), os.path.basename(info.filename))
                    except Exception:
                        pass
                name = info.filename
                base = os.path.basename(name)
                if base.startswith(".") or "__MACOSX" in name or "__MACOSX" in name.upper():
                    continue
                ext = os.path.splitext(base)[1].lower()
                inner_location = f"{location}{os.path.basename(path)}/{name}" if not location else f"{location}{name}"
                if _suspicious_entry(info):
                    rows.append(Row(source_file, inner_location, ext.lstrip(".").upper() or "Unknown",
                                     "Whole file", flagged=True,
                                     notes=(f"This file expands from {info.compress_size / 1024 / 1024:.0f} MB to "
                                            f"{info.file_size / 1024 / 1024:.0f} MB inside the archive - not opened, "
                                            f"please check it manually.")))
                    continue
                if info.file_size > MAX_ANALYSE_BYTES and ext != ".zip":
                    rows.append(Row(source_file, inner_location, ext.lstrip(".").upper() or "Unknown",
                                     "Whole file", flagged=True,
                                     notes=(f"File is {info.file_size / 1024 / 1024:.0f} MB, too large to open "
                                            f"safely here - please check it manually.")))
                    continue
                if ext != ".zip" and ext not in DISPATCH:
                    rows.append(Row(source_file, inner_location, ext.lstrip(".").upper() or "Unknown",
                                     "Whole file", flagged=False,
                                     notes="File type not analysed by this tool (listed for reference only)."))
                    continue
                entry_path = None
                try:
                    entry_path = _extract_one(zf, info, tmpdir)
                    if ext == ".zip":
                        rows.extend(analyze_zip(entry_path, source_file,
                                                location=inner_location + " > ", depth=depth + 1,
                                                on_item=on_item))
                    else:
                        entry_rows = DISPATCH[ext](entry_path, source_file, location=inner_location)
                        for r in entry_rows:
                            r.source_bytes = info.file_size
                        rows.extend(entry_rows)
                except Exception as e:
                    rows.append(Row(source_file, inner_location, ext.lstrip(".").upper() or "Unknown",
                                     "Whole file", flagged=True,
                                     notes=_unreadable("inside the ZIP", e)))
                finally:
                    # One file out of the archive at a time.
                    if entry_path:
                        try:
                            os.remove(entry_path)
                        except OSError:
                            pass
    except Exception as e:
        rows.append(Row(source_file, location, "ZIP", "Whole archive",
                         flagged=True, notes=_unreadable("ZIP", e)))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if not rows:
        rows.append(Row(source_file, location, "ZIP", "Whole archive", flagged=True,
                         notes="Archive is empty, or holds nothing we can count - please check it manually."))
    return rows

def analyze_file(path, original_filename, on_item=None):
    """Entry point: dispatch on extension. Returns a list of Row objects.
    `on_item(position, total, name)`, if given, is called for each file inside
    a ZIP, so a job that's one big archive can still show progress."""
    ext = os.path.splitext(original_filename)[1].lower()
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    rows = _analyze_file_inner(path, original_filename, ext, size, on_item)
    for r in rows:
        if r.source_bytes is None and not r.location:
            r.source_bytes = size   # files inside a ZIP set their own
    return rows


def _analyze_file_inner(path, original_filename, ext, size, on_item=None):
    if size > MAX_ANALYSE_BYTES and ext != ".zip":
        return [Row(original_filename, "", ext.lstrip(".").upper() or "Unknown", "Whole file", flagged=True,
                    notes=(f"File is {size / 1024 / 1024:.0f} MB, too large to open safely here - it's included "
                           f"with the original files, please check it manually."))]
    if ext == ".zip":
        return analyze_zip(path, original_filename, on_item=on_item)
    elif ext in DISPATCH:
        return DISPATCH[ext](path, original_filename)
    elif ext == ".doc":
        return [Row(original_filename, "", "DOC", "Whole file", flagged=True,
                    notes=("This is an older Word file (.doc), which we can't open here. Re-save it "
                           "as .docx or send it as a PDF and we can measure it."))]
    else:
        return [Row(original_filename, "", ext.lstrip(".").upper() or "Unknown", "Whole file",
                     flagged=True,
                     notes="Not a file type we can count pages for - it's included with the original "
                           "files, please check it manually.")]
