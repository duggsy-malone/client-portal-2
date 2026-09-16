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


@dataclass
class SizeMatch:
    label: str          # e.g. "A4" or "Non-standard"
    is_standard: bool
    width_mm: float
    height_mm: float


def _normalise(width_mm: float, height_mm: float):
    """Return (short_side, long_side) so orientation doesn't affect matching."""
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


def mm_from_points(points: float) -> float:
    """PDF units are points (1/72 inch)."""
    return points / 72.0 * 25.4


def mm_from_emu(emu: float) -> float:
    """PPTX dimensions are English Metric Units (914400 per inch)."""
    return emu / 914400.0 * 25.4


def mm_from_pixels(pixels: float, dpi: float) -> float:
    return pixels / dpi * 25.4
