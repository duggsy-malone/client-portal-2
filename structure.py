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
