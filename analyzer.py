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
  - ZIP: unpacked recursively (depth-limited) and every supported file
    inside is analysed and listed individually, prefixed with its path
    inside the archive.
"""

import os
import zipfile
import tempfile
import shutil
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
MAX_ZIP_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024  # 2GB guard against zip bombs


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

    def as_dict(self):
        d = asdict(self)
        for k in ("width_mm", "height_mm"):
            if d[k] is not None:
                d[k] = round(d[k], 1)
        return d


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
                notes="" if m.is_standard else "Non-standard page size for a PDF page."
            ))
    except Exception as e:
        rows.append(Row(source_file, location, "PDF", "Whole document",
                         flagged=True, notes=f"Could not read PDF: {e}"))
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
            rows.append(Row(
                source_file, location, "Image", f"{width_px}x{height_px}px",
                width_mm=w_mm, height_mm=h_mm,
                matched_size=m.label, is_standard=m.is_standard,
                flagged=assumed or (not m.is_standard),
                notes=" ".join(notes)
            ))
    except Exception as e:
        rows.append(Row(source_file, location, "Image", "Whole file",
                         flagged=True, notes=f"Could not read image: {e}"))
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
        rows.append(Row(
            source_file, location, "PPTX", f"{slide_count} slide(s) @ deck size",
            width_mm=w_mm, height_mm=h_mm,
            matched_size=m.label, is_standard=m.is_standard,
            flagged=not m.is_standard,
            notes="" if m.is_standard else "Non-standard slide size for the whole deck."
        ))
    except Exception as e:
        rows.append(Row(source_file, location, "PPTX", "Whole file",
                         flagged=True, notes=f"Could not read PPTX: {e}"))
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
                        std_w, std_h = STANDARD_PAGE_SIZES.get(declared.split(" ")[0], (None, None))
                        rows.append(Row(
                            source_file, location, "Excel", f"Sheet '{title}' (declared print setup)",
                            width_mm=std_w, height_mm=std_h,
                            matched_size=LABEL_ALIASES.get(declared, declared), is_standard=True,
                            flagged=False,
                            notes=f"Orientation: {ws['orientation'] or 'not set'}."
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
                           "dimensions, not a true print size. Please sanity-check before quoting.")
                ))
    except Exception as e:
        rows.append(Row(source_file, location, "Excel", "Whole file",
                         flagged=True, notes=f"Could not read spreadsheet: {e}"))
    return rows


DISPATCH = {
    ".pdf": analyze_pdf,
    ".jpg": analyze_image,
    ".jpeg": analyze_image,
    ".png": analyze_image,
    ".pptx": analyze_pptx,
    ".xlsx": analyze_xlsx,
    ".xlsm": analyze_xlsx,
}


def analyze_zip(path, source_file, location="", depth=0):
    rows = []
    if depth >= MAX_ZIP_DEPTH:
        rows.append(Row(source_file, location, "ZIP", "Whole archive",
                         flagged=True, notes="Archive nested too deeply - stopped unpacking for safety."))
        return rows
    tmpdir = tempfile.mkdtemp(prefix="zipscan_")
    try:
        with zipfile.ZipFile(path) as zf:
            total_size = sum(i.file_size for i in zf.infolist())
            if total_size > MAX_ZIP_UNCOMPRESSED_BYTES:
                rows.append(Row(source_file, location, "ZIP", "Whole archive",
                                 flagged=True, notes="Archive too large when uncompressed - not scanned."))
                return rows
            zf.extractall(tmpdir)
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                base = os.path.basename(name)
                if base.startswith(".") or "__MACOSX" in name:
                    continue
                ext = os.path.splitext(base)[1].lower()
                full_path = os.path.join(tmpdir, name)
                inner_location = f"{location}{os.path.basename(path)}/{name}" if not location else f"{location}{name}"
                if ext == ".zip":
                    rows.extend(analyze_zip(full_path, source_file, location=inner_location + " > ", depth=depth + 1))
                elif ext in DISPATCH:
                    rows.extend(DISPATCH[ext](full_path, source_file, location=inner_location))
                else:
                    rows.append(Row(source_file, inner_location, ext.lstrip(".").upper() or "Unknown",
                                     "Whole file", flagged=False,
                                     notes="File type not analysed by this tool (listed for reference only)."))
    except Exception as e:
        rows.append(Row(source_file, location, "ZIP", "Whole archive",
                         flagged=True, notes=f"Could not open archive: {e}"))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return rows


def analyze_file(path, original_filename):
    """Entry point: dispatch on extension. Returns a list of Row objects."""
    ext = os.path.splitext(original_filename)[1].lower()
    if ext == ".zip":
        return analyze_zip(path, original_filename)
    elif ext in DISPATCH:
        return DISPATCH[ext](path, original_filename)
    else:
        return [Row(original_filename, "", ext.lstrip(".").upper() or "Unknown", "Whole file",
                     flagged=True,
                     notes="Not a file type we can count pages for - it's included with the original "
                           "files, please check it manually.")]
