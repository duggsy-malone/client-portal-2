"""
Everything about how Flightpath LOOKS and what it SAYS, in one file.

The values below come from the Flight Path brand guide v1.0 (September 2026).
Nothing else in the app hard-codes a colour, a font or a line of copy - it all
comes from here, so the brand can be changed without touching the working
parts.

Two rules from the guide that are easy to break by accident:

  1. Button text on orange is ALWAYS navy, never white. White on orange only
     passes contrast at 28px and above, so it is reserved for display type.
  2. Runway Cyan is iPrintManage's colour, not Flightpath's. It is used only
     for focus rings and "in progress", and never as a fill.

The email header is the one image that can't live in static/brand/: email
programs won't load images from a page's own files, so it needs a public web
address. Emails look right without it - they fall back to a navy bar with the
wordmark set as text - so EMAIL_HEADER_URL is left blank by default.
"""

# --------------------------------------------------------------------------
# Colour - "orange leads, navy anchors" (guide page 4)
# --------------------------------------------------------------------------
COLOUR_PRIMARY = "#FF7A1A"     # Flight Orange. Buttons, the app tile, the drop zone.
COLOUR_BEACON = "#FFA125"      # Beacon. Hovers, highlights, text on navy.
COLOUR_NAVY = "#1C0072"        # Night Navy. Shared with iPrintManage. Type, the board.
COLOUR_PAPER = "#FFF8F2"       # Paper. Page background - warmer than white.
COLOUR_CYAN = "#00BFF9"        # Runway Cyan. Focus rings and "in progress" ONLY.
COLOUR_EMBER = "#C2410C"       # Ember. Small text and links on light backgrounds.

# Status colours. Cleared is a green that sits beside the navy board without
# fighting it; "needs a look" is Beacon, because the guide asks, never accuses.
COLOUR_CLEARED = "#1E7A54"
COLOUR_CLEARED_ON_NAVY = "#4ADE9B"
COLOUR_ERROR = "#C2410C"       # Ember doubles as the error colour.

# Names the rest of the app still asks for, mapped onto the real palette.
COLOUR_ACCENT = COLOUR_PRIMARY
COLOUR_AMBER = COLOUR_BEACON
COLOUR_SUCCESS = COLOUR_CLEARED
COLOUR_TEXT = COLOUR_NAVY
COLOUR_MUTED = "#6B6480"       # Body text that needs to step back from navy.
COLOUR_LINE = "#F0E2D6"        # Hairlines and card borders on Paper.
COLOUR_CARD = "#FFFFFF"
COLOUR_TILE = "#FDEDE1"        # The peach fill behind the batch figures.

# --------------------------------------------------------------------------
# Type - one family from the parent, two for the job (guide page 5)
# --------------------------------------------------------------------------
# Loaded from Google Fonts on the pages. Emails fall back to the stacks below,
# because mail programs can't be relied on to fetch a web font.
FONT_GOOGLE_URL = ("https://fonts.googleapis.com/css2?"
                   "family=Work+Sans:wght@400;500;600;700;800&"
                   "family=Barlow+Condensed:wght@600&"
                   "family=JetBrains+Mono:wght@500&display=swap")
FONT_BODY = "'Work Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif"
FONT_LABEL = "'Barlow Condensed', 'Arial Narrow', Arial, sans-serif"
FONT_MONO = "'JetBrains Mono', ui-monospace, 'SF Mono', Consolas, monospace"
FONT_EMAIL = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif"
FONT_EMAIL_MONO = "Consolas, Menlo, monospace"

# --------------------------------------------------------------------------
# Images. Files live in static/brand/. Blank means "don't show it".
# --------------------------------------------------------------------------
MARK = "brand/mark.svg"              # the orange tile - header, favicon, avatar
MARK_NAVY = "brand/mark-navy.svg"    # when orange is already busy
MARK_MONO = "brand/mark-mono.svg"    # single-colour print
DART = "brand/dart.svg"              # the loose dart and trail, for hero areas
DART_ON_NAVY = "brand/dart-on-navy.svg"
FAVICON = "brand/favicon.png"
APPLE_ICON = "brand/apple-touch-icon.png"
EMAIL_HEADER_URL = ""                # public https:// link, see the note above
MARK_HEIGHT_PX = 34                  # how tall the tile is drawn in the header

# Kept so older templates and the desktop build keep working.
LOGO = MARK
LOGO_MONO = MARK_MONO
HERO = ""
LOGO_HEIGHT_PX = MARK_HEIGHT_PX

# --------------------------------------------------------------------------
# Words - name, tagline and the endorsement (guide pages 6 and 9)
# --------------------------------------------------------------------------
# The wordmark is set in two tones: the first part navy, the second orange.
WORDMARK_LEAD = "Flight"
WORDMARK_TAIL = "path"
ENDORSEMENT = "by iPrintManage"      # always under the wordmark, except on the tile
PARENT_NAME = "iPrintManage"
PARENT_URL = "https://iprintmanage.com"
PARENT_STRAPLINE = "iPrintManage - We Print Everything"

TAGLINE = "Cleared for print."       # with the full stop - it's part of it
ONE_LINER = ("Flightpath is the iPrintManage upload portal. Drop in your artwork and it counts "
             "your pages and measures every size before a single sheet is printed.")
BOILERPLATE = ("Flightpath is the client upload portal from iPrintManage. Clients check in PDFs "
               "and images, and Flightpath counts every page, measures every trim size and "
               "matches it to a standard format, from A4 leaflets to 8x4 hoarding panels. "
               "Anything unusual is flagged in plain English before it reaches the studio, so "
               "jobs go to press right the first time.")

# --------------------------------------------------------------------------
# Portal microcopy (guide page 7). The words the client actually reads.
# --------------------------------------------------------------------------
PAGE_TITLE = "Check in your artwork"
DROP_ZONE = "Drop PDFs or images here. We'll count every page and measure every size."
DROP_BUTTON = "Choose files"
DROP_BUTTON_FOLDER = "Choose a folder"
CLEAR_BUTTON = "Clear board"
ACCEPTED_LINE = "PDF / JPG / PNG / ZIP / XLSX / PPTX - up to 5 GB in total"
PROCESSING = "Running pre-flight..."
CLEARED_LABEL = "Cleared"
NEEDS_LOOK_LABEL = "Needs a look"
CANT_READ = ("We can't open this file. It may be password-protected. Remove the password and "
             "check it in again.")
EMPTY_BOARD = "No files checked in yet."
PRIMARY_ACTION = "Send to studio"
BOARD_TITLE = "Departures"
BATCH_TITLE = "This batch"
SEND_TITLE = "Send to studio"
CONFIRMATION = "Sent to the studio. Your reference is {reference}."
EMAIL_SUBJECT = "Cleared for print: {reference} ({pages} pages)"
NEED_A_HAND = ("Your iPrintManage account manager can see everything on this board.")

UPLOAD_INTRO = DROP_ZONE
REASSURANCE = ("Your files are used only to prepare your quote, are stored securely and are "
               "deleted once the job has been passed on.")

# Who to chase. Plain text, one item per line - shown on the client pages.
# Taken from iprintmanage.com, so they match what clients see on the main site.
CONTACT_LINES = [
    "iPrintManage",
    "7 Bell Yard, London, WC2A 2JR",
    "Telephone: 020 7126 8454",
    "sales@iprintmanage.com",
]

# The small print at the very bottom. Blank lines are ignored, so a line that
# isn't ready can simply be left out rather than showing a gap.
#
# The company number and VAT number aren't on iprintmanage.com, so they aren't
# guessed at here - send them over and they go back in, one line each. (A UK
# limited company is generally required to show its registered name, number
# and registered office on its websites, so they're worth adding.)
# Left empty for now: with only the trading name in it, this block just said
# "iPrintManage" a third time under the contact details.
FOOTER_LEGAL = [
    # "iPrintManage Limited",
    # "Registered in England & Wales, company number 00000000",
    # "VAT registration number GB000000000",
]

# Optional links shown next to the footer. (label, url)
FOOTER_LINKS = [
    ("How we produce planning sets", "/standard"),
    ("Terms", "https://iprintmanage.com/terms-conditions/"),
    ("Privacy", "https://iprintmanage.com/privacy-policy/"),
]


def as_dict():
    """Everything the templates need, in one go."""
    return {
        # colour
        "colour_primary": COLOUR_PRIMARY,
        "colour_beacon": COLOUR_BEACON,
        "colour_navy": COLOUR_NAVY,
        "colour_paper": COLOUR_PAPER,
        "colour_cyan": COLOUR_CYAN,
        "colour_ember": COLOUR_EMBER,
        "colour_cleared": COLOUR_CLEARED,
        "colour_cleared_on_navy": COLOUR_CLEARED_ON_NAVY,
        "colour_accent": COLOUR_ACCENT,
        "colour_amber": COLOUR_AMBER,
        "colour_success": COLOUR_SUCCESS,
        "colour_error": COLOUR_ERROR,
        "colour_page": COLOUR_PAPER,
        "colour_text": COLOUR_TEXT,
        "colour_muted": COLOUR_MUTED,
        "colour_line": COLOUR_LINE,
        "colour_card": COLOUR_CARD,
        "colour_tile": COLOUR_TILE,
        # type
        "font_google_url": FONT_GOOGLE_URL,
        "font_body": FONT_BODY,
        "font_label": FONT_LABEL,
        "font_mono": FONT_MONO,
        "font_email": FONT_EMAIL,
        "font_email_mono": FONT_EMAIL_MONO,
        # images
        "mark": MARK,
        "mark_navy": MARK_NAVY,
        "mark_mono": MARK_MONO,
        "dart": DART,
        "dart_on_navy": DART_ON_NAVY,
        "favicon": FAVICON,
        "apple_icon": APPLE_ICON,
        "mark_height_px": MARK_HEIGHT_PX,
        "logo": LOGO,
        "logo_mono": LOGO_MONO,
        "hero": HERO,
        "logo_height_px": LOGO_HEIGHT_PX,
        # words
        "wordmark_lead": WORDMARK_LEAD,
        "wordmark_tail": WORDMARK_TAIL,
        "endorsement": ENDORSEMENT,
        "parent_name": PARENT_NAME,
        "parent_url": PARENT_URL,
        "parent_strapline": PARENT_STRAPLINE,
        "tagline": TAGLINE,
        "one_liner": ONE_LINER,
        "boilerplate": BOILERPLATE,
        # microcopy
        "page_title": PAGE_TITLE,
        "drop_zone": DROP_ZONE,
        "drop_button": DROP_BUTTON,
        "drop_button_folder": DROP_BUTTON_FOLDER,
        "clear_button": CLEAR_BUTTON,
        "accepted_line": ACCEPTED_LINE,
        "processing": PROCESSING,
        "cleared_label": CLEARED_LABEL,
        "needs_look_label": NEEDS_LOOK_LABEL,
        "cant_read": CANT_READ,
        "empty_board": EMPTY_BOARD,
        "primary_action": PRIMARY_ACTION,
        "board_title": BOARD_TITLE,
        "batch_title": BATCH_TITLE,
        "send_title": SEND_TITLE,
        "need_a_hand": NEED_A_HAND,
        "upload_intro": UPLOAD_INTRO,
        "reassurance": REASSURANCE,
        "contact_lines": [line for line in CONTACT_LINES if line.strip()],
        "footer_legal": [line for line in FOOTER_LEGAL if line.strip()],
        "footer_links": FOOTER_LINKS,
    }


def confirmation(reference):
    """"Sent to the studio. Your reference is FP-24091." """
    return CONFIRMATION.format(reference=reference)


def email_subject(reference, pages):
    """"Cleared for print: FP-24091 (16 pages)" """
    return EMAIL_SUBJECT.format(reference=reference, pages=f"{pages:,}")
