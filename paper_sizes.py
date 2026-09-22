"""
Reference tables of standard paper / slide sizes, and the matching logic
used to flag anything "unusual".

All physical sizes are stored in millimetres (portrait orientation: width < height).
Matching is orientation-agnostic (a landscape A4 still matches "A4") and allows
a small tolerance to absorb scanning drift / rounding.
"""

from dataclasses import dataclass

# Tolerance for matching a measured size to a standard one, in mm.
TOLERANCE_MM = 3.0

# Standard ISO 216 "A" series + common US/business sizes (width_mm, height_mm), portrait.
STANDARD_PAGE_SIZES = {
    "A0": (841.0, 1189.0),
    "A1": (594.0, 841.0),
    "A2": (420.0, 594.0),
    "A3": (297.0, 420.0),
    "A4": (210.0, 297.0),
    "A5": (148.0, 210.0),
    "A6": (105.0, 148.0),
    "US Letter": (215.9, 279.4),
    "US Legal": (215.9, 355.6),
    "Tabloid/Ledger": (279.4, 431.8),
    "ANSI B": (279.4, 431.8),
    "ANSI C": (431.8, 558.8),
}

# Common presentation slide sizes (width_mm, height_mm), landscape as normally authored.
STANDARD_SLIDE_SIZES = {
    "Standard 4:3 (10in x 7.5in)": (254.0, 190.5),
    "Widescreen 16:9 (13.33in x 7.5in)": (338.67, 190.5),
    "Widescreen 16:9 (10in x 5.63in)": (254.0, 143.0),
    "A4 slide (landscape)": (297.0, 210.0),
    "On-screen 16:10": (254.0, 158.75),
}

# MS Excel built-in paper size codes we recognise (subset of the ECMA-376 enumeration).
EXCEL_PAPER_SIZE_CODES = {
    1: "US Letter",
    3: "Tabloid",
    5: "US Legal",
    8: "A3",
    9: "A4",
    11: "A5",
    27: "Envelope B4",
    28: "Envelope B5",
    66: "A2",
    69: "A2 (rotated)",
}


# Roller banner "notable sizes" - large-format print jobs. Defined as a
# short-side range x long-side range (in mm) rather than one exact size, since
# these are cut with some leeway. Matched orientation-agnostically. These are
# recognised business sizes, so they're deliberately NOT flagged for review
# the way a genuine non-standard size is - just labelled distinctly for quoting.
ROLLER_BANNER_BANDS = [
    ("800 RB", (800.0, 850.0), (2000.0, 2200.0)),
    ("1000 RB", (1000.0, 1150.0), (2000.0, 2200.0)),
    ("1200 RB", (1200.0, 1300.0), (2000.0, 2200.0)),
    ("1500 RB", (1500.0, 1600.0), (2000.0, 2200.0)),
    ("2000 RB", (2000.0, 2200.0), (2000.0, 2200.0)),
]

# "Roll-out" documents: a long strip the height of an A4/A3 page (297mm),
# made of three or more A4 widths side by side - e.g. 297 x 630 (three A4),
# 297 x 840 (four A4, or two A3). A single A4 (297 x 210) or A3 (297 x 420)
# is NOT a roll-out. Orientation doesn't matter.
ROLL_OUT_HEIGHT_MM = 297.0
ROLL_OUT_HEIGHT_TOLERANCE_MM = 3.0
ROLL_OUT_STEP_MM = 210.0            # one A4 width
ROLL_OUT_STEP_TOLERANCE_MM = 5.0
ROLL_OUT_MIN_STEPS = 3              # three A4 widths (630mm) and up


def match_roll_out(width_mm: float, height_mm: float):
    """Returns a label like "Roll-out 297 x 630 mm" if this is a roll-out
    strip, else None."""
    short, long_ = _normalise(width_mm, height_mm)
    if abs(short - ROLL_OUT_HEIGHT_MM) > ROLL_OUT_HEIGHT_TOLERANCE_MM:
        return None
    steps = round(long_ / ROLL_OUT_STEP_MM)
    if steps < ROLL_OUT_MIN_STEPS:
        return None
    if abs(long_ - steps * ROLL_OUT_STEP_MM) > ROLL_OUT_STEP_TOLERANCE_MM:
        return None
    return f"Roll-out {ROLL_OUT_HEIGHT_MM:.0f} \u00d7 {steps * ROLL_OUT_STEP_MM:.0f} mm"


# Square formats recognised by name rather than falling through to "Non-standard".
SQUARE_SIZES = {
    "210 square": (210.0, 210.0),
    "297 square": (297.0, 297.0),
}

# Sizes that should be grouped/displayed under a different, more familiar label
# in reports. The measured mm dimensions are unaffected - only the label shown
# and used for grouping changes (e.g. a US Legal page is reported as "A4").
LABEL_ALIASES = {
    "US Legal": "A4",
    "US Letter": "A4",
}


# Suggested scaling for items that don't match a recognised size. Anything
# shorter than this on its long side (small labels, stickers, cropped
# artwork) isn't a scale-up candidate and is listed for a manual look.
MIN_SCALE_LONG_MM = 100.0
SCALE_TOO_SMALL = "Too small to scale - check manually"
SCALE_UP_A4 = "Scale up to A4"
SCALE_UP_A3 = "Scale up to A3"
SCALE_DOWN_A3 = "Scale down to A3"


def suggest_scale(width_mm: float, height_mm: float, tolerance: float = TOLERANCE_MM) -> str:
    """What to do with an odd-sized item: bring it up to A4, up to A3, or
    down to A3. Kept separate from the size totals so it can be checked."""
    short, long_ = _normalise(abs(width_mm), abs(height_mm))
    if long_ < MIN_SCALE_LONG_MM:
        return SCALE_TOO_SMALL
    a4_w, a4_h = STANDARD_PAGE_SIZES["A4"]
    a3_w, a3_h = STANDARD_PAGE_SIZES["A3"]
    if short <= a4_w + tolerance and long_ <= a4_h + tolerance:
        return SCALE_UP_A4
    if short <= a3_w + tolerance and long_ <= a3_h + tolerance:
        return SCALE_UP_A3
    return SCALE_DOWN_A3


@dataclass
class SizeMatch:
    label: str          # e.g. "A4" or "Non-standard"
    is_standard: bool
    width_mm: float
    height_mm: float


def _normalise(width_mm: float, height_mm: float):
    """Return (short_side, long_side) so orientation doesn't affect matching.
    Negative measurements (from PDFs with flipped page corners) count as positive."""
    width_mm, height_mm = abs(width_mm), abs(height_mm)
    return (min(width_mm, height_mm), max(width_mm, height_mm))


def match_size(width_mm: float, height_mm: float, table: dict, tolerance: float = TOLERANCE_MM) -> SizeMatch:
    """Compare a physical size against a reference table and return the closest match,
    or mark it as non-standard if nothing is within tolerance."""
    w, h = _normalise(width_mm, height_mm)
    for label, (std_w, std_h) in table.items():
        sw, sh = _normalise(std_w, std_h)
        if abs(w - sw) <= tolerance and abs(h - sh) <= tolerance:
            return SizeMatch(label=label, is_standard=True, width_mm=width_mm, height_mm=height_mm)
    return SizeMatch(label="Non-standard", is_standard=False, width_mm=width_mm, height_mm=height_mm)


def match_roller_banner(width_mm: float, height_mm: float):
    """Check a physical size against the roller-banner bands. Returns the band's
    label (e.g. "1000 RB") if it falls within one, else None. Orientation-agnostic:
    the short side is checked against the band's first range, the long side
    against its second."""
    short, long_ = _normalise(width_mm, height_mm)
    for label, (lo1, hi1), (lo2, hi2) in ROLLER_BANNER_BANDS:
        if lo1 <= short <= hi1 and lo2 <= long_ <= hi2:
            return label
    return None


def match_page_size(width_mm: float, height_mm: float, tolerance: float = TOLERANCE_MM) -> SizeMatch:
    """The combined matcher used for PDF pages, images, and estimated Excel print
    sizes. Checks the roller-banner bands first (large format - always treated as
    a recognised, non-flagged size), then the standard page sizes plus the square
    formats, and finally applies any display-label aliasing (e.g. US Legal -> A4).
    """
    rb_label = match_roller_banner(width_mm, height_mm)
    if rb_label:
        return SizeMatch(label=rb_label, is_standard=True, width_mm=width_mm, height_mm=height_mm)

    roll_out = match_roll_out(width_mm, height_mm)
    if roll_out:
        return SizeMatch(label=roll_out, is_standard=True, width_mm=width_mm, height_mm=height_mm)

    combined_table = {**STANDARD_PAGE_SIZES, **SQUARE_SIZES}
    m = match_size(width_mm, height_mm, combined_table, tolerance)
    if m.is_standard and m.label in LABEL_ALIASES:
        m.label = LABEL_ALIASES[m.label]
    return m


def mm_from_points(points: float) -> float:
    """PDF units are points (1/72 inch)."""
    return points / 72.0 * 25.4


def mm_from_emu(emu: float) -> float:
    """PPTX dimensions are English Metric Units (914400 per inch)."""
    return emu / 914400.0 * 25.4


def mm_from_pixels(pixels: float, dpi: float) -> float:
    return pixels / dpi * 25.4
