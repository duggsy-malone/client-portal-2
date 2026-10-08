"""
Works out a job's folder structure: its documents (one divider each), its
folders (one tab each) and the folder tree drawn in the report.

The browser tells the server which folder each uploaded file came from
(e.g. "Archway/Drawings"), because a file's name alone can't be trusted for
that - "Transport - Drawings - plan.pdf" might be a file in two folders, or
just a file with dashes in its name.

`folder_info` maps the uploaded file's name as analysed (Row.source_file) to
{"folders": ["Archway", "Drawings"], "name": "plan.pdf"}. Files missing from
it are treated as loose files (not in a folder), which is also what happens
for the desktop Page Counter.

Inside a ZIP, the ZIP itself counts as a folder (a tab), as does every folder
inside it, and every file inside it is a document (a divider).
"""

import difflib
import re
from collections import OrderedDict


def _natural_key(text):
    """Sorts "2. Design" before "10. Appendix", like a file browser does."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text or "")]


def clean_folder_path(path):
    """'Archway/Drawings/' or 'Archway\\Drawings' -> ['Archway', 'Drawings']."""
    parts = re.split(r"[\\/]+", str(path or ""))
    return [p.strip() for p in parts if p.strip() and p.strip() not in (".", "..")][:50]


def original_name(flat_name, folders):
    """The upload page names a file from a folder "Archway - Drawings - plan.pdf"
    so names don't clash. With the folders known, this gets back "plan.pdf"."""
    prefix = " - ".join(folders) + " - " if folders else ""
    if prefix and flat_name.startswith(prefix):
        return flat_name[len(prefix):] or flat_name
    return flat_name


class Document:
    __slots__ = ("folders", "name", "pages", "source_file", "location", "rows")

    def __init__(self, folders, name, source_file, location):
        self.folders = tuple(folders)
        self.name = name
        self.source_file = source_file
        self.location = location
        self.pages = 0
        self.rows = []

    @property
    def file_bytes(self):
        """How big this document's file is, where we know."""
        for r in self.rows:
            if getattr(r, "source_bytes", None):
                return r.source_bytes
        return None

    @property
    def extension(self):
        m = re.search(r"\.([A-Za-z0-9]{1,5})$", self.name)
        return m.group(1).lower() if m else ""


def build_documents(rows, folder_info=None):
    """One Document per file (including each file inside a ZIP), in upload order."""
    folder_info = folder_info or {}
    docs = OrderedDict()
    for r in rows:
        key = (r.source_file, r.location or "")
        doc = docs.get(key)
        if doc is None:
            info = folder_info.get(r.source_file) or {}
            base_folders = list(info.get("folders") or [])
            top_name = info.get("name") or r.source_file
            if not r.location:
                doc = Document(base_folders, top_name, r.source_file, "")
            else:
                # e.g. "bundle.zip/inside/plan.pdf", or for a ZIP inside a ZIP
                # "bundle.zip/inner.zip > sub/plan.pdf"
                comps = []
                for segment in r.location.split(" > "):
                    comps.extend(p for p in segment.split("/") if p)
                if comps and comps[0] == r.source_file:
                    comps[0] = top_name
                name = comps[-1] if comps else top_name
                doc = Document(base_folders + comps[:-1], name, r.source_file, r.location)
            docs[key] = doc
        doc.rows.append(r)
        if r.width_mm is not None and r.height_mm is not None:
            doc.pages += 1
    return list(docs.values())


def count_tabs(documents):
    """Every folder at every level that holds a document somewhere inside it."""
    folders = set()
    for d in documents:
        for i in range(1, len(d.folders) + 1):
            folders.add(d.folders[:i])
    return len(folders)


class FolderNode:
    def __init__(self, name, path):
        self.name = name
        self.path = path
        self.children = OrderedDict()
        self.documents = []
        self.total_documents = 0
        self.total_pages = 0


def build_tree(documents):
    """A tree of FolderNodes. The root (name None) holds the top-level folders,
    plus any loose files that weren't in a folder. Totals include subfolders."""
    root = FolderNode(None, ())
    for d in documents:
        node = root
        for i, part in enumerate(d.folders):
            if part not in node.children:
                node.children[part] = FolderNode(part, d.folders[:i + 1])
            node = node.children[part]
        node.documents.append(d)

    def finish(node):
        node.children = OrderedDict(sorted(node.children.items(), key=lambda kv: _natural_key(kv[0])))
        node.documents.sort(key=lambda d: _natural_key(d.name))
        node.total_documents = len(node.documents)
        node.total_pages = sum(d.pages for d in node.documents)
        for child in node.children.values():
            finish(child)
            node.total_documents += child.total_documents
            node.total_pages += child.total_pages
    finish(root)
    return root


def walk_folders(node, depth=0):
    """(depth, node) for every folder, parents before children."""
    for child in node.children.values():
        yield depth, child
        yield from walk_folders(child, depth + 1)


SPREADSHEET_EXTENSIONS = {"xlsx", "xlsm", "xls", "xlsb"}


def spreadsheet_documents(documents):
    return [d for d in documents if d.extension in SPREADSHEET_EXTENSIONS]


def describe_spreadsheet(doc):
    """What the report's spreadsheet section shows for one workbook."""
    sheets = [r for r in doc.rows if r.unit_label.startswith("Sheet")]
    sized = [r for r in sheets if r.width_mm is not None]
    declared = sum(1 for r in sized if "(declared print setup)" in r.unit_label)
    estimated = len(sized) - declared
    sizes = OrderedDict()
    for r in sized:
        label = r.matched_size or "Non-standard"
        sizes[label] = sizes.get(label, 0) + 1
    if not sheets:
        print_size = "Check manually - couldn't be read"
    elif not sized:
        print_size = "Check manually - no sheets with a size"
    elif estimated == 0:
        print_size = "Set in file"
    elif declared == 0:
        print_size = "Estimated (no print size set)"
    else:
        print_size = f"Mixed: {declared} set in file, {estimated} estimated"
    unsized_notes = [r for r in sheets if r.width_mm is None and "empty" not in (r.notes or "").lower()]
    if unsized_notes and sized:
        print_size += f"; {len(unsized_notes)} sheet(s) to check manually"
    return {
        "name": doc.name,
        "folder": " / ".join(doc.folders),
        "worksheets": len(sheets),
        "pages": len(sized),
        "sizes": ", ".join(f"{label} × {n}" for label, n in sizes.items()),
        "print_size": print_size,
    }


# --------------------------------------------------------------------------
# Possible duplicates
# --------------------------------------------------------------------------

def _duplicate_signature(doc):
    """Two documents look like the same thing if they have the same number of
    pages and those pages are the same sizes in the same order."""
    sizes = tuple((r.matched_size or "", int(round(abs(r.width_mm))) if r.width_mm else 0,
                   int(round(abs(r.height_mm))) if r.height_mm else 0)
                  for r in doc.rows if r.width_mm is not None)
    return sizes


# Documents shorter than this are never called duplicates: a job can easily
# hold sixty single-page A4 drawings that are nothing to do with each other.
MIN_DUPLICATE_PAGES = 3
# How alike two names have to be (0 to 1). Matching page counts and sizes
# aren't enough on their own: a submission can hold 195 seven-page energy
# reports that are all different buildings. Real duplicates nearly always
# have near-identical names - "... High Resolution" and "... Low Resolution".
DUPLICATE_NAME_SIMILARITY = 0.72
# A set bigger than this is a template used many times over, not a document
# printed twice by mistake.
MAX_DUPLICATE_SET = 5


def _name_similarity(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _cluster_by_name(docs):
    """Splits documents that look alike by size into groups that also look
    alike by name."""
    clusters = []
    for d in docs:
        for cluster in clusters:
            if any(_name_similarity(d.name, other.name) >= DUPLICATE_NAME_SIMILARITY for other in cluster):
                cluster.append(d)
                break
        else:
            clusters.append([d])
    return clusters


def find_possible_duplicates(documents, min_pages=MIN_DUPLICATE_PAGES):
    """Documents that look like copies of each other - the same page count and
    the same sizes in the same order. Catches the likes of "Appendix 6.2 AVR -
    High Resolution" and "... - Low Resolution", which are easy to print twice.

    Returns [{pages, documents: [{name, folder}], extra_pages}], biggest
    duplication first. `extra_pages` is what you'd save by printing one copy.
    """
    groups = OrderedDict()
    for d in documents:
        if d.pages < min_pages:
            continue
        groups.setdefault(_duplicate_signature(d), []).append(d)
    out = []
    for sig, docs in groups.items():
        if len(docs) < 2 or not sig:
            continue
        for cluster in _cluster_by_name(docs):
            if not 2 <= len(cluster) <= MAX_DUPLICATE_SET:
                continue
            sizes = [d.file_bytes for d in cluster]
            out.append({
                "pages": cluster[0].pages,
                "documents": [{"name": d.name, "folder": " / ".join(d.folders),
                               "bytes": d.file_bytes} for d in cluster],
                "extra_pages": cluster[0].pages * (len(cluster) - 1),
                # Same page count, same sizes AND the same number of bytes:
                # almost certainly the very same file twice.
                "identical_files": all(sizes) and len(set(sizes)) == 1,
            })
    # Certain ones first, then the biggest saving.
    out.sort(key=lambda g: (not g["identical_files"], -g["extra_pages"]))
    return out


def sections(documents):
    """The job split by its top-level folder: [(name, [documents])], so sizes
    can be totalled per chapter. Loose files come last."""
    groups = OrderedDict()
    for d in documents:
        name = d.folders[0] if d.folders else None
        groups.setdefault(name, []).append(d)
    named = [(name, docs) for name, docs in groups.items() if name is not None]
    named.sort(key=lambda kv: _natural_key(kv[0]))
    loose = groups.get(None)
    if loose:
        named.append(("(not in a folder)", loose))
    return named


# --------------------------------------------------------------------------
# Separate documents (design & access statements, non-technical summaries)
# --------------------------------------------------------------------------
#
# The Standard produces these as their own bound documents rather than part
# of the binders, so the report lists them on their own and leaves them out
# of the binder count. They're recognised by name - the file's own name, and
# the names of the folders above it, so a plainly named file inside a folder
# called "2. Design and Access Statement" is still caught.
#
# Add to this list as other document types come up. Each entry is
# (what to call it, [patterns]). Patterns are matched without case.
SEPARATE_DOCUMENT_RULES = [
    ("Design and access statement", [r"design\s*(?:and|&|\+)\s*access",
                                     r"(?<![A-Za-z])D\.?A\.?S\.?(?![A-Za-z])"]),
    ("Non-technical summary", [r"non[\s\-_]*technical\s*summar",
                              r"(?<![A-Za-z])N\.?T\.?S\.?(?![A-Za-z])"]),
]

# Separate documents are wiro bound. Anything longer than this gets a note to
# check it, in case it needs handling differently.
SEPARATE_BINDING = "Wiro bound"
CHECK_BINDING_OVER_PAGES = 64

_SEPARATE_PATTERNS = [(label, [re.compile(p, re.I) for p in patterns])
                      for label, patterns in SEPARATE_DOCUMENT_RULES]


def document_kind(doc):
    """"Design and access statement", "Non-technical summary", or None."""
    haystack = " / ".join(list(doc.folders) + [doc.name])
    for label, patterns in _SEPARATE_PATTERNS:
        if any(p.search(haystack) for p in patterns):
            return label
    return None


def separate_documents(documents):
    """The documents produced separately rather than bound into the set:
    [{name, folder, pages, kind, binding, note}], longest first."""
    out = []
    for d in documents:
        kind = document_kind(d)
        if not kind:
            continue
        out.append({
            "name": d.name,
            "folder": " / ".join(d.folders),
            "pages": d.pages,
            "kind": kind,
            "binding": SEPARATE_BINDING,
            "note": (f"over {CHECK_BINDING_OVER_PAGES} pages - check the binding"
                     if d.pages > CHECK_BINDING_OVER_PAGES else ""),
            "document": d,
        })
    out.sort(key=lambda s: (s["kind"], -s["pages"]))
    return out


def find_split_and_combined(documents, tolerance=3, min_pages=10, min_parts=3):
    """One file that looks like a whole document, while the same document is
    also present split into chapters - e.g. a 278-page "ArchwayCampus-DAS-A.pdf"
    alongside "ArchwayCampus-DAS-A-01-Introduction.pdf" and nine more chapters
    totalling 277 pages. Printed twice by accident, that's 278 wasted pages.

    Both things have to line up before it's reported: the chapter files' names
    have to start with the whole file's name, and their pages have to add up to
    roughly the same total. Page counts alone match by coincidence far too
    often in a big submission.

    Returns [{document, folder, pages, parts, part_pages, where}].
    """
    out = []
    by_stem = []
    for d in documents:
        stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", d.name).strip()
        if stem:
            by_stem.append((stem, d))
    for stem, whole in by_stem:
        if whole.pages < min_pages or len(stem) < 6:
            continue
        parts = [d for other_stem, d in by_stem
                 if d is not whole and other_stem.lower().startswith(stem.lower())
                 and len(other_stem) > len(stem)
                 and other_stem[len(stem)] in "-_ ." and d.pages]
        if len(parts) < min_parts:
            continue
        part_pages = sum(d.pages for d in parts)
        if abs(part_pages - whole.pages) > tolerance:
            continue
        where = sorted({" / ".join(d.folders) or "(not in a folder)" for d in parts})
        out.append({
            "document": whole.name,
            "folder": " / ".join(whole.folders),
            "pages": whole.pages,
            "parts": len(parts),
            "part_pages": part_pages,
            "where": where[0] + (f" and {len(where) - 1} other folder(s)" if len(where) > 1 else ""),
        })
    out.sort(key=lambda s: -s["pages"])
    return out
