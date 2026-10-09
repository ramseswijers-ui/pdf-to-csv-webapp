#!/usr/bin/env python3
"""
pdf_to_csv.py - Extract info from a technical PDF (e.g. an engineering
drawing) into CSV files for easy review and double-checking.

It produces up to three CSVs from a single PDF:

  1. <name>_titleblock.csv   - Label/value pairs from the drawing's title
                                block (title, material, scale, doc number,
                                dates, etc.), matched against a list of
                                known field labels.
  2. <name>_callouts.csv     - Every dimension, tolerance, thread callout,
                                GD&T frame, etc. on the drawing, grouped by
                                spatial proximity and listed with its page
                                position, sorted top-to-bottom / left-to-
                                right - handy as a checklist against the
                                physical part.
  3. <name>_full_text.csv    - A raw, position-sorted dump of every text
                                line on the page/document. This is the
                                fallback "just show me everything" view -
                                useful for text-heavy technical PDFs
                                (manuals, reports) rather than drawings.

Usage:
    Just double-click this file, or run it with no arguments, and a
    point-and-click window opens: pick a PDF, pick an output folder,
    click Convert.

        python3 pdf_to_csv.py

    Command-line mode still works for scripting/automation:

        python3 pdf_to_csv.py input.pdf [-o output_folder]

Notes / limitations (read before trusting the output for QA):
- Works on PDFs with a real text layer (not scanned images). Run
  `pdffonts input.pdf` first - if it lists no fonts, this won't work;
  the PDF would need OCR first.
- Title-block extraction is done by matching a list of common field
  labels (English + German, since many CAD/PLM exports use bilingual
  labels). If your drawings use a different template, edit the
  KNOWN_LABELS list near the top of this file to match your labels -
  it's plain Python, no special tooling needed.
- Callout grouping is a spatial-proximity heuristic (nearby text on the
  page gets grouped into one "callout"), not true semantic understanding
  of the drawing. It's meant to make manual review faster, not to replace
  it - always cross-check critical dimensions against the drawing itself.
"""

import argparse
import csv
import re
import sys
from pathlib import Path

import pdfplumber

# ---------------------------------------------------------------------------
# Configuration - edit this list if your drawings use different title-block
# labels. Matching is case-insensitive substring matching against each text
# line pulled from the page.
# ---------------------------------------------------------------------------
KNOWN_LABELS = [
    "benennung", "title",
    "material-nr", "material no", "werkstoff-nr", "werkstoff",
    "maßstab", "scale",
    "dokumenten-nr", "document-no", "document no",
    "gewicht", "weight",
    "bl./sheet", "sheet no", "blätter", "sheets",
    "format",
    "index",
    "drwn", "chkd", "appd", "bearbeitet", "geprüft", "genehmigt",
    "erstanlage", "initial release",
    "änderungsnr", "ecn",
    "konfiguration", "configuration",
    "ersatz für", "replaces doc",
    "modell", "model",
    "oberflächen-behandlung", "finish",
    "toleranzen", "tolerances",
    "größenmaße", "size dimension",
    "winkelgrößenmaße", "angle size dimension",
    "passung", "toleranz",
    "doc.-art", "doc.-type", "doc.-teil", "doc.-part", "version", "status",
    # English / ASML-style title blocks
    "part number", "material number", "description", "checked by",
    "name", "former", "sheet", "sheets", "size", "scale",
    "tolerances on linear dimensions", "tolerances on angles",
    "surface roughness", "first angle projection", "status date",
    "drawing not to scale", "do not scale drawing",
]

# Regex fragments that flag a text token as a "dimension / callout" worth
# pulling into the checklist CSV (numbers, tolerances, threads, GD&T, etc.)
CALLOUT_PATTERNS = [
    r"^\d+([.,]\d+)?$",          # plain numbers: 45,21  110  0,2
    r"\d[±£#]\d",               # value glued to its tolerance: 40±0,2
    r"\(\d+\s?x\)",             # glued multiplicity: 31.5(2x)
    r"^[+\-±£#]\d+([.,]\d+)?$",  # signed/tolerance values (£ and # are common OCR misreads of ±)
    r"^\d+x$",                   # multiplicity: 3x 4x 6x
    r"^[MR]\d+([.,]\d+)?",       # thread/radius callouts: M4, R3,75
    r"^[A-Z]\d+$",               # fit classes: H8, H7, E8
    r"^Rz\s?\d+",                # surface roughness: Rz 6
    r"^DIN|^ISO|^EN",            # standard references
    r"DURCH ALLES",              # "through all" - common drawing note
    r"^\d+°",                    # angles
]
CALLOUT_RE = re.compile("|".join(CALLOUT_PATTERNS), re.IGNORECASE)

# Distance (in PDF points) within which two words are considered part of the
# same visual "callout" cluster.
CLUSTER_GAP_X = 10
CLUSTER_GAP_Y = 12

# Below this many extracted text-layer words, treat the PDF as having no
# usable text layer (common when a CAD export flattens text to vector
# outlines) and fall back to OCR instead.
OCR_FALLBACK_THRESHOLD = 5
OCR_DPI = 450
OCR_CONFIG = "--psm 6"


def _norm_ocr_text(t):
    # OCR engines often return a different glyph for the diameter symbol
    for bad in ("Φ", "φ", "⌀", "∅"):
        t = t.replace(bad, "Ø")
    t = re.sub(r"[\u2e80-\u9fff\uff00-\uffef]", " ", t)  # CJK lookalikes of GD&T symbols
    return re.sub(r"\s+", " ", t).strip()


def _iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter == 0:
        return 0.0
    union = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter
    return inter / union


def _iomin(a, b):
    """Overlap as a share of the SMALLER box: catches a box that is mostly
    inside another one (the same text read twice by overlapping tiles)."""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    smaller = min((a[2]-a[0])*(a[3]-a[1]), (b[2]-b[0])*(b[3]-b[1]))
    return (ix * iy) / smaller if smaller > 0 else 1.0


def ocr_words_rapid(pdf_path):
    """Offline OCR using RapidOCR (PaddleOCR models on ONNX, CPU only).
    Reads each page upright AND rotated +/-90 degrees so vertical
    dimension text is picked up too. Also reads the page in overlapping
    tiles (finds small text the full-page read misses; set OCR_TILES=0 in
    the environment to skip this for speed)."""
    import os
    import subprocess
    import tempfile
    import numpy as np
    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    use_tiles = os.environ.get("OCR_TILES") != "0"   # on by default; OCR_TILES=0 disables
    all_words = []

    with pdfplumber.open(pdf_path) as pdf:
        sizes = [(p.width, p.height) for p in pdf.pages]

    with tempfile.TemporaryDirectory() as tmp:
        for page_num, (pw, ph) in enumerate(sizes, start=1):
            # keep the longest side near 4000px so big sheets don't take forever
            dpi = int(min(300, 4000 / (max(pw, ph) / 72.0)))
            prefix = f"{tmp}/p{page_num}"
            subprocess.run(
                ["pdftoppm", "-png", "-r", str(dpi), "-f", str(page_num),
                 "-l", str(page_num), "-singlefile", str(pdf_path), prefix],
                check=True, capture_output=True)
            img = Image.open(prefix + ".png").convert("RGB")
            W, H = img.size
            scale = 72.0 / dpi

            def run(im):
                res, _ = engine(np.array(im))
                return res or []

            def to_box(pts):
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                return (min(xs), min(ys), max(xs), max(ys))

            found = []  # (box_px_in_original_space, text, upright)

            # pass 1: upright (optionally tiled)
            for pts, txt, conf in run(img):
                if float(conf) >= 0.5:
                    found.append((to_box(pts), txt, True))
            if use_tiles:
                tile, ov = 1400, 200
                for y in range(0, H, tile - ov):
                    for x in range(0, W, tile - ov):
                        for pts, txt, conf in run(img.crop((x, y, min(x+tile, W), min(y+tile, H)))):
                            if float(conf) < 0.5:
                                continue
                            b = to_box(pts)
                            b = (b[0]+x, b[1]+y, b[2]+x, b[3]+y)
                            if all(_iomin(b, f[0]) < 0.4 for f in found):
                                found.append((b, txt, True))

            # pass 2/3: rotated 90 degrees either way (vertical text)
            # PIL ROTATE_90 is counter-clockwise: (x, y) -> (y, W-1-x)
            for rot, back in ((Image.ROTATE_90, "ccw"), (Image.ROTATE_270, "cw")):
                for pts, txt, conf in run(img.transpose(rot)):
                    if float(conf) < 0.85:
                        continue  # stricter: horizontal text read sideways is junk
                    corners = []
                    for (rx, ry) in pts:
                        if back == "ccw":
                            corners.append((W - 1 - ry, rx))
                        else:
                            corners.append((ry, H - 1 - rx))
                    b = to_box(corners)
                    if all(_iomin(b, f[0]) < 0.4 for f in found):
                        found.append((b, txt, False))

            for (x0, y0, x1, y1), txt, upright in found:
                txt = _norm_ocr_text(txt)
                if not txt:
                    continue
                all_words.append({
                    "page": page_num, "text": txt,
                    "x0": x0 * scale, "x1": x1 * scale,
                    "top": y0 * scale, "bottom": y1 * scale,
                    "upright": upright, "source": "ocr",
                })
    return all_words


def ocr_words_tesseract(pdf_path, dpi=OCR_DPI):
    """Older/lighter fallback, used only if RapidOCR isn't installed."""
    import subprocess
    import tempfile
    import pytesseract
    from PIL import Image

    all_words = []
    scale = 72.0 / dpi
    with tempfile.TemporaryDirectory() as tmp:
        prefix = f"{tmp}/page"
        subprocess.run(["pdftoppm", "-png", "-r", str(dpi), str(pdf_path), prefix],
                       check=True, capture_output=True)
        for page_num, img_path in enumerate(sorted(Path(tmp).glob("page*.png")), start=1):
            data = pytesseract.image_to_data(Image.open(img_path), config=OCR_CONFIG,
                                             output_type=pytesseract.Output.DICT)
            for i, text in enumerate(data["text"]):
                text = text.strip()
                if not text:
                    continue
                x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
                all_words.append({
                    "page": page_num, "text": text,
                    "x0": x*scale, "x1": (x+w)*scale, "top": y*scale, "bottom": (y+h)*scale,
                    "upright": True, "source": "ocr",
                })
    return all_words


def ocr_words_for_pdf(pdf_path):
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        return ocr_words_tesseract(pdf_path)
    return ocr_words_rapid(pdf_path)


def load_words(pdf_path):
    """Return list of dicts: page, text, x0, x1, top, bottom, upright for
    every word. Technical drawings often mix horizontal (title block) and
    rotated/vertical text (dimension callouts along vertical lines) - we
    keep the 'upright' flag so callers can avoid merging across the two.
    If the PDF has no real text layer (text flattened to vector outlines,
    or a scan), falls back to OCR automatically."""
    all_words = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            for w in page.extract_words(use_text_flow=False, extra_attrs=["upright"]):
                all_words.append({
                    "page": page_num,
                    "text": w["text"],
                    "x0": w["x0"], "x1": w["x1"],
                    "top": w["top"], "bottom": w["bottom"],
                    "upright": w.get("upright", True),
                })

    if len(all_words) < OCR_FALLBACK_THRESHOLD:
        ocr_words = ocr_words_for_pdf(pdf_path)
        if len(ocr_words) > len(all_words):
            return ocr_words
    return all_words


def cluster_words(words):
    """Group nearby words (same page) into callout clusters via union-find
    on expanded bounding-box overlap."""
    n = len(words)
    parent = list(range(n))

    # OCR-derived words come from noisier bounding boxes (line-art/hatching
    # often gets misread as stray characters) - use a tighter merge distance
    # for them so garbage doesn't get pulled into real dimension clusters.
    is_ocr = any(w.get("source") == "ocr" for w in words)
    gap_x = 8 if is_ocr else CLUSTER_GAP_X
    gap_y = 6 if is_ocr else CLUSTER_GAP_Y

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    # Only compare words on the same page, use simple O(n^2) - fine for
    # single-drawing pages (hundreds of words). For huge multi-page PDFs,
    # this could be optimized with a spatial index.
    by_page = {}
    for idx, w in enumerate(words):
        by_page.setdefault(w["page"], []).append(idx)

    for page, idxs in by_page.items():
        for a in range(len(idxs)):
            i = idxs[a]
            wi = words[i]
            for b in range(a + 1, len(idxs)):
                j = idxs[b]
                wj = words[j]
                if wi.get("upright", True) != wj.get("upright", True):
                    continue  # never merge horizontal and rotated text
                # expanded bbox overlap test
                if (wi["x0"] - gap_x <= wj["x1"] and
                        wj["x0"] - gap_x <= wi["x1"] and
                        wi["top"] - gap_y <= wj["bottom"] and
                        wj["top"] - gap_y <= wi["bottom"]):
                    union(i, j)

    groups = {}
    for idx in range(n):
        root = find(idx)
        groups.setdefault(root, []).append(idx)

    clusters = []
    for idxs in groups.values():
        ws = [words[i] for i in idxs]
        ws.sort(key=lambda w: (round(w["top"] / 5), w["x0"]))  # reading order
        text = " ".join(w["text"] for w in ws)
        x0 = min(w["x0"] for w in ws)
        x1 = max(w["x1"] for w in ws)
        top = min(w["top"] for w in ws)
        bottom = max(w["bottom"] for w in ws)
        clusters.append({
            "page": ws[0]["page"],
            "text": text,
            "x0": x0, "x1": x1, "top": top, "bottom": bottom,
        })
    clusters.sort(key=lambda c: (c["page"], round(c["top"] / 10), c["x0"]))
    return clusters


def extract_titleblock(words):
    """Best-effort label:value extraction from title-block-like text lines.
    Only considers upright (horizontal) words, grouped into rows by
    similar y-position, so vertical/rotated dimension callouts elsewhere on
    the drawing can't bleed into and garble a title-block row."""
    rows = []
    by_page = {}
    for w in words:
        if not w.get("upright", True):
            continue
        by_page.setdefault(w["page"], []).append(w)

    for page_num, ws in by_page.items():
        # bucket into rows by quantizing the top coordinate - simple and
        # robust for the small-font, densely packed tables typical of
        # title blocks (avoids chained merging across unrelated rows).
        buckets = {}
        for w in ws:
            key = round(w["top"] / 2.0)
            buckets.setdefault(key, []).append(w)

        row_texts = []
        for key in sorted(buckets):
            group = sorted(buckets[key], key=lambda w: w["x0"])
            text = " ".join(w["text"] for w in group).strip()
            if text:
                row_texts.append(text)

        for i, text in enumerate(row_texts):
            lower = text.lower()
            if any(lbl in lower for lbl in KNOWN_LABELS):
                rows.append({"page": page_num, "row_type": "label", "line_text": text})
                # many title blocks put the value on the very next line
                # rather than "Label: value" on one line - include it too
                if i + 1 < len(row_texts):
                    nxt = row_texts[i + 1]
                    nxt_lower = nxt.lower()
                    if not any(lbl in nxt_lower for lbl in KNOWN_LABELS):
                        rows.append({"page": page_num, "row_type": "value_below", "line_text": nxt})
    return rows


def parse_requirement(text):
    """Split a callout's text into (requirement, upper_tol, lower_tol).
    Handles the common tolerance notations seen on drawings: '±0,5',
    '+0,2/-0,0', '+0,2 -0,0'. Falls back to blank tolerances (with the
    full text kept as the requirement) when no clear pattern is found -
    still useful as a checklist line, just without split-out tolerances."""
    t = text.strip()

    m = re.search(r'[±£#]\s*(\d+[.,]\d+|\d+)', t)
    if m:
        v = m.group(1)
        return t, f"+{v}", f"-{v}"

    m = re.search(r'\+\s*(\d+[.,]\d+|\d+)\s*/\s*-\s*(\d+[.,]\d+|\d+)', t)
    if m:
        return t, f"+{m.group(1)}", f"-{m.group(2)}"

    nums = re.findall(r'[+-]\s*\d+[.,]?\d*', t)
    pos = next((n.replace(' ', '') for n in nums if n.strip().startswith('+')), None)
    neg = next((n.replace(' ', '') for n in nums if n.strip().startswith('-')), None)
    if pos and neg:
        return t, pos, neg

    # stacked tolerance read top-to-bottom, e.g. "10 +1 0" or "+1 10 0" or
    # "+0,5 6 (2x) 0": one +upper token, one bare 0 / -lower token, a nominal
    toks = [x for x in re.split(r"\s+", t) if x]
    ups = [x for x in toks if re.fullmatch(r"\+\d+(?:[.,]\d+)?", x)]
    lows = [x for x in toks if re.fullmatch(r"-?\d+(?:[.,]\d+)?", x) and re.fullmatch(r"-?0+(?:[.,]0+)?|-\d+(?:[.,]\d+)?", x)]
    if len(ups) == 1 and len(lows) == 1:
        low = lows[0]
        return t, ups[0], (low if low.startswith("-") else "-" + low)

    return t, "", ""


# Default ISO 5457-style zoning grid, matching the column/row reference
# marks printed on the border of most engineering drawings (numbers along
# the top/bottom, letters down the sides). Edit these if your drawing
# template uses a different sheet size / zone count.
ZONE_COLS = 6   # labeled COLS..1, left to right
ZONE_ROWS = 4   # labeled A..(last letter), bottom to top


def detect_zone_grid(words, page_w, page_h):
    """Find the zone markers printed in the sheet border (digits along the
    top/bottom edge, letters down the left/right edge) and fit
    position -> label lines. Works for any direction/count (1..8
    left-to-right, 6..1, A at top or bottom). Only markers that line up
    in one border strip are used (so stray numbers elsewhere are ignored),
    and as few as two are enough: OCR misses some, and the rest of the
    grid is extrapolated from their spacing. Returns (col_fit, row_fit)
    or None."""
    digits, letters = [], []
    for w in words:
        t = w["text"].strip()
        cx, cy = (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2
        if re.fullmatch(r"[1-9]", t) and (cy < page_h * 0.07 or cy > page_h * 0.94):
            digits.append((int(t), cx, cy))
        elif re.fullmatch(r"[A-H]", t) and (cx < page_w * 0.06 or cx > page_w * 0.96):
            letters.append((ord(t) - 64, cy, cx))

    def best_strip(items):
        """items = (label, along_axis_pos, cross_axis_pos); keep the strip
        (same cross-axis position) containing the most distinct labels."""
        strips = {}
        for label, along, cross in items:
            strips.setdefault(round(cross / 10), []).append((label, along))
        if not strips:
            return {}
        key = max(strips, key=lambda k: (len({l for l, _ in strips[k]}), -k))
        d = {}
        for label, along in strips[key]:
            d.setdefault(label, []).append(along)
        return d

    def fit(d, extent, cap):
        if len(d) < 2:
            return None
        pts = [(sum(v) / len(v), k) for k, v in d.items()]   # (position, index)
        n = len(pts)
        mx = sum(p for p, _ in pts) / n
        mk = sum(k for _, k in pts) / n
        var = sum((p - mx) ** 2 for p, _ in pts)
        if var == 0:
            return None
        slope = sum((p - mx) * (k - mk) for p, k in pts) / var
        if slope == 0:
            return None
        # extrapolate to the inner edges of the drawing frame
        k0 = mk + slope * (extent * 0.045 - mx)
        k1 = mk + slope * (extent * 0.955 - mx)
        hi = min(cap, max(max(d), round(max(k0, k1))))
        return (mx, mk, slope, 1, hi)

    cf = fit(best_strip([(l, x, y) for l, x, y in digits]), page_w, 9)
    rf = fit(best_strip([(l, y, x) for l, y, x in letters]), page_h, 8)
    return (cf, rf) if cf and rf else None


def zone_from_fit(fit, pos):
    mx, mk, slope, lo, hi = fit
    k = round(mk + slope * (pos - mx))
    return max(lo, min(hi, k))


def zone_for_position(x, top, page_width, page_height, grid=None):
    if grid:
        c = zone_from_fit(grid[0], x)
        r = zone_from_fit(grid[1], top)
        return f"{c}-{chr(64 + r)}"
    col_idx = min(int(x / (page_width / ZONE_COLS)), ZONE_COLS - 1)
    col_label = str(ZONE_COLS - col_idx)
    row_idx = min(int(top / (page_height / ZONE_ROWS)), ZONE_ROWS - 1)
    row_label = chr(ord('A') + (ZONE_ROWS - 1 - row_idx))
    return f"{col_label}-{row_label}"


FAI_HEADER = [
    "5. Char No.:", "6. Reference Location:", "7. Operation:",
    "8. Requirement:", "Upper Tol", "Lower Tol",
    "9. Results:", "10. Designed Tooling:", "11. Non-Conf. Number:",
    "14. Deviation", "15. Error", "16. (Insert columns as required by the Customer)",
]


def build_fai_checklist(pdf_path, words, clusters, tb_rows):
    """Build one checklist CSV laid out exactly like Form 3: Characteristic
    Accountability, Verification and Compatibility Evaluation (the standard
    First Article Inspection form) - a part-identification block using the
    form's own field numbers/wording, then one row per dimension/tolerance
    callout under the same numbered column headers, ready to fill in
    Results during a physical check."""
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        page_w, page_h = page.width, page.height

    # pull a few useful fields out of the title block for the metadata rows
    pairs = []
    for i, r in enumerate(tb_rows):
        if r["row_type"] == "label" and i + 1 < len(tb_rows) and tb_rows[i + 1]["row_type"] == "value_below":
            pairs.append((r["line_text"], tb_rows[i + 1]["line_text"]))

    def find_pair(*keywords):
        for label, value in pairs:
            low = label.lower()
            if any(k in low for k in keywords):
                return value
        return ""

    def value_below(*labels, wide=False):
        """Text printed directly under a label like PART NUMBER. With
        wide=True, join everything on that row to the right of the label."""
        for w in words:
            key = re.sub(r"\W", "", w["text"]).lower()
            if not any(re.sub(r"\W", "", l).lower() == key for l in labels):
                continue
            best = None
            for v in words:
                dy = v["top"] - w["bottom"]
                if v is w or dy < -2 or dy > 40:
                    continue
                if v["x1"] < w["x0"] - 5 or v["x0"] > w["x1"] + (400 if wide else 60):
                    continue
                if best is None or dy < best[0]:
                    best = (dy, v["text"], v["top"])
            if best and wide:
                row = sorted((v for v in words if abs(v["top"] - best[2]) < 6
                              and v["x1"] >= w["x0"] - 5 and v["x0"] <= w["x1"] + 400),
                             key=lambda v: v["x0"])
                text = " ".join(v["text"] for v in row)
                return re.sub(r"^ASML\s+", "", text)
            if best:
                return best[1]
        return ""

    part_number = value_below("PART NUMBER", "Document-No.") or find_pair("part number", "dokumenten", "document", "material-nr", "material no")
    part_name = value_below("DESCRIPTION", wide=True) or find_pair("benennung", "title", "description")
    material = value_below("MATERIAL NUMBER") or find_pair("werkstoff", "material (", "material number")

    meta_rows = [
        ["1. Part Number:", part_number],
        ["Rev. Level:", ""],
        ["2. Part Name:", part_name],
        ["3. Serial Number:", ""],
        ["4. FAI Report:", ""],
        ["Material:", material],
        ["Source File:", Path(pdf_path).name],
    ]

    grid = detect_zone_grid(words, page_w, page_h)
    tb_region = title_block_region(words, page_w, page_h)

    rows = []
    placements = []   # where to draw each numbered balloon on the PDF
    skipped_title_block = 0
    char_no = 1
    for c in clusters:
        if len(c["text"]) > 60:
            continue
        if not is_callout(c["text"]):
            continue
        cx, cy = (c["x0"] + c["x1"]) / 2, (c["top"] + c["bottom"]) / 2
        # the zone markers printed in the sheet border (1..8, A..F) are not characteristics
        if re.fullmatch(r"[1-9]", c["text"].strip()) and (cy < page_h * 0.07 or cy > page_h * 0.94):
            continue
        if re.fullmatch(r"[A-H]", c["text"].strip()) and (cx < page_w * 0.06 or cx > page_w * 0.96):
            continue
        if c["page"] == 1 and tb_region and cx >= tb_region[0] and cy >= tb_region[1]:
            skipped_title_block += 1   # dates, sheet numbers, tolerance table...
            continue
        requirement, upper, lower = parse_requirement(c["text"])
        zone = zone_for_position(cx, cy, page_w, page_h, grid)
        rows.append([
            char_no, zone, "", requirement, upper, lower,
            "", "", "", "", "", c["text"],
        ])
        placements.append({"char_no": char_no, "page": c["page"], "x0": c["x0"],
                           "x1": c["x1"], "top": c["top"], "bottom": c["bottom"]})
        char_no += 1

    return meta_rows, rows, placements


def write_fai_csv(path, meta_rows, table_rows):
    # write to a temp file first so a reader never sees a half-written CSV
    final_path, path = path, str(path) + ".tmp"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Form 3: Characteristic Accountability, Verification and Compatibility Evaluation"])
        writer.writerow([])
        for label, value in meta_rows:
            writer.writerow([label, value])
        writer.writerow([])
        writer.writerow(["Characteristic Accountability", "", "", "", "", "", "Inspection / Test Results"])
        writer.writerow(FAI_HEADER)
        for row in table_rows:
            writer.writerow(row)
        writer.writerow([])
        writer.writerow(["Signature indicates that all characteristics are accounted for and meet drawing requirements or are properly documented for disposition."])
        writer.writerow(["12. Prepared By:", "", "13. Date:", ""])
        writer.writerow(["Supporting Data Provided:", ""])
    import os
    os.replace(path, final_path)




def is_callout(text):
    # a cluster counts as a callout if ANY token within it matches a pattern
    for token in text.split():
        if CALLOUT_RE.search(token):
            return True
    return False


def extract_full_text_rows(pdf_path):
    rows = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            for line in text.split("\n"):
                line = line.strip()
                if line:
                    rows.append({"page": page_num, "text": line})
    return rows


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


TITLE_BLOCK_WORDS = (
    "part number", "material number", "description", "tolerances", "scale",
    "sheet", "status", "checked", "passung", "benennung", "werkstoff",
    "maßstab", "gewicht", "dokumenten", "oberfl", "bearbeitet", "format",
)


def title_block_region(words, page_w, page_h):
    """(x_min, y_min) of the title block: everything right of and below this
    corner is title block, not a characteristic to inspect. Found from the
    title-block label words in the lower-right of page 1; None if unsure."""
    hits = [w for w in words
            if w["page"] == 1
            and w["x0"] > page_w * 0.4 and w["top"] > page_h * 0.55
            and any(k in w["text"].lower() for k in TITLE_BLOCK_WORDS)]
    if len(hits) < 3:
        return None
    return (min(w["x0"] for w in hits) - 10, min(w["top"] for w in hits) - 10)


def place_balloons(placements, obstacles, page_w, page_h):
    """Choose a spot for each balloon next to its callout, avoiding other
    text and other balloons. Returns {char_no: (cx, cy, r, leader_target)}."""
    r = max(6.0, min(12.0, page_w / 140.0))
    placed, result = [], {}

    def hits(box, others):
        return sum(1 for o in others
                   if box[0] < o[2] and box[2] > o[0] and box[1] < o[3] and box[3] > o[1])

    dirs = [(0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1)]
    for p in placements:
        own = (p["x0"], p["top"], p["x1"], p["bottom"])
        hw, hh = (own[2] - own[0]) / 2, (own[3] - own[1]) / 2
        mx, my = (own[0] + own[2]) / 2, (own[1] + own[3]) / 2
        others = [o for o in obstacles if o != own]
        best = None
        for gap in (r * 0.4, r * 1.5, r * 3.0):
            for dx, dy in dirs:
                cx = mx + dx * (hw + r + gap) if dx else mx
                cy = my + dy * (hh + r + gap) if dy else my
                box = (cx - r, cy - r, cx + r, cy + r)
                score = (hits(box, others) * 10 + hits(box, placed) * 12
                         + (100 if box[0] < 0 or box[1] < 0 or box[2] > page_w or box[3] > page_h else 0)
                         + gap * 0.05 + (0.3 if dx and dy else 0))
                if best is None or score < best[0]:
                    best = (score, cx, cy, box)
        _, cx, cy, box = best
        placed.append(box)
        # leader line target: nearest point on the callout's box
        tx = min(max(cx, own[0]), own[2])
        ty = min(max(cy, own[1]), own[3])
        result[p["char_no"]] = (cx, cy, r, (tx, ty))
    return result


def annotate_pdf(pdf_path, out_path, placements, clusters):
    """Copy the original PDF and draw a red numbered balloon at every
    characteristic, using the same numbers as the checklist."""
    import io
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase.pdfmetrics import stringWidth

    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()
    for page_idx, page in enumerate(reader.pages, start=1):
        mine = [p for p in placements if p["page"] == page_idx]
        rot = page.get("/Rotate", 0) or 0
        if mine and rot % 360 == 0:
            mb = page.mediabox
            pw, ph = float(mb.width), float(mb.height)
            ox, oy = float(mb.left), float(mb.bottom)
            obstacles = [(c["x0"], c["top"], c["x1"], c["bottom"])
                         for c in clusters if c["page"] == page_idx]
            spots = place_balloons(mine, obstacles, pw, ph)

            buf = io.BytesIO()
            cv = canvas.Canvas(buf, pagesize=(pw + ox, ph + oy))
            red = (0.85, 0.08, 0.08)
            for num, (cx, cy, r, (tx, ty)) in spots.items():
                X, Y = ox + cx, oy + ph - cy
                cv.setStrokeColorRGB(*red)
                cv.setLineWidth(max(0.6, r / 10))
                if (tx - cx) ** 2 + (ty - cy) ** 2 > (r * 1.2) ** 2:   # leader line
                    cv.line(X, Y, ox + tx, oy + ph - ty)
                cv.setFillColorRGB(1, 1, 1)
                cv.circle(X, Y, r, stroke=1, fill=1)
                label = str(num)
                size = r * (1.15 if len(label) <= 2 else 0.9)
                cv.setFillColorRGB(*red)
                cv.setFont("Helvetica-Bold", size)
                cv.drawString(X - stringWidth(label, "Helvetica-Bold", size) / 2,
                              Y - size * 0.35, label)
            cv.save()
            buf.seek(0)
            page.merge_page(PdfReader(buf).pages[0])
        writer.add_page(page)
    with open(out_path, "wb") as f:
        writer.write(f)


def process_pdf(pdf_path, outdir, log=print):
    """Run the full extraction on one PDF and write a single FAI-style
    checklist CSV: a small metadata block, then one row per dimension/
    tolerance callout, laid out like a First Article Inspection
    Characteristic Accountability form.
    `log` is a callable used for progress messages (print, or a GUI callback).
    Returns a dict with the output path and row count."""
    pdf_path = Path(pdf_path)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    base = pdf_path.stem

    log(f"Reading {pdf_path.name} ...")
    words = load_words(pdf_path)
    log(f"  {len(words)} words found across the document.")

    tb_rows = extract_titleblock(words)
    clusters = cluster_words(words)

    meta_rows, table_rows, placements = build_fai_checklist(pdf_path, words, clusters, tb_rows)

    out_path = outdir / f"{base}_checklist.csv"
    write_fai_csv(out_path, meta_rows, table_rows)
    log(f"  Checklist: {len(table_rows)} characteristics -> {out_path.name}")

    balloon_path = outdir / f"{base}_ballooned.pdf"
    annotate_pdf(pdf_path, balloon_path, placements, clusters)
    log(f"  Balloons: {len(placements)} numbered -> {balloon_path.name}")

    log("Done.")
    return {"checklist": out_path, "checklist_rows": len(table_rows),
            "ballooned": balloon_path}


def main_cli():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf", help="Path to the input PDF")
    parser.add_argument("-o", "--outdir", default=".", help="Output folder (default: current folder)")
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        sys.exit(f"File not found: {pdf_path}")

    process_pdf(pdf_path, args.outdir)


def main_gui():
    """Simple point-and-click window: pick a PDF, pick an output folder,
    click Convert. No command line needed - just double-click this file
    (or run `python3 pdf_to_csv.py` with no arguments)."""
    import tkinter as tk
    from tkinter import filedialog, messagebox
    import threading
    import os
    import platform
    import subprocess

    root = tk.Tk()
    root.title("Technical PDF -> CSV")
    root.geometry("560x420")
    root.resizable(False, False)

    pdf_var = tk.StringVar()
    outdir_var = tk.StringVar()
    status_var = tk.StringVar(value="Choose a PDF to get started.")

    def choose_pdf():
        path = filedialog.askopenfilename(
            title="Choose a technical PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        if path:
            pdf_var.set(path)
            if not outdir_var.get():
                outdir_var.set(str(Path(path).parent))

    def choose_outdir():
        path = filedialog.askdirectory(title="Choose an output folder")
        if path:
            outdir_var.set(path)

    def log(msg):
        status_box.configure(state="normal")
        status_box.insert("end", msg + "\n")
        status_box.see("end")
        status_box.configure(state="disabled")
        root.update_idletasks()

    def open_folder(path):
        path = str(path)
        try:
            if platform.system() == "Windows":
                os.startfile(path)  # noqa
            elif platform.system() == "Darwin":
                subprocess.run(["open", path])
            else:
                subprocess.run(["xdg-open", path])
        except Exception:
            pass  # not critical if this fails

    def run_conversion():
        pdf_path = pdf_var.get()
        outdir = outdir_var.get()
        if not pdf_path:
            messagebox.showwarning("No PDF selected", "Please choose a PDF file first.")
            return
        if not outdir:
            outdir = str(Path(pdf_path).parent)
            outdir_var.set(outdir)

        convert_btn.configure(state="disabled")
        status_box.configure(state="normal")
        status_box.delete("1.0", "end")
        status_box.configure(state="disabled")

        def work():
            try:
                result = process_pdf(pdf_path, outdir, log=log)
                log("")
                log("All done! Files saved to:")
                log(str(Path(outdir).resolve()))
                root.after(0, lambda: open_folder_btn.configure(state="normal"))
            except Exception as e:
                log(f"ERROR: {e}")
                messagebox.showerror("Something went wrong", str(e))
            finally:
                root.after(0, lambda: convert_btn.configure(state="normal"))

        threading.Thread(target=work, daemon=True).start()

    # --- layout ---
    pad = {"padx": 12, "pady": 6}

    tk.Label(root, text="Technical PDF -> CSV", font=("Helvetica", 16, "bold")).pack(**pad)

    frame1 = tk.Frame(root)
    frame1.pack(fill="x", **pad)
    tk.Button(frame1, text="1. Choose PDF...", command=choose_pdf, width=18).pack(side="left")
    tk.Label(frame1, textvariable=pdf_var, fg="#555", anchor="w", wraplength=380).pack(side="left", padx=8)

    frame2 = tk.Frame(root)
    frame2.pack(fill="x", **pad)
    tk.Button(frame2, text="2. Output folder...", command=choose_outdir, width=18).pack(side="left")
    tk.Label(frame2, textvariable=outdir_var, fg="#555", anchor="w", wraplength=380).pack(side="left", padx=8)

    convert_btn = tk.Button(root, text="3. Convert to CSV", command=run_conversion,
                             bg="#2d6cdf", fg="white", font=("Helvetica", 12, "bold"), height=2)
    convert_btn.pack(fill="x", **pad)

    status_box = tk.Text(root, height=10, state="disabled", bg="#f5f5f5")
    status_box.pack(fill="both", expand=True, **pad)

    open_folder_btn = tk.Button(root, text="Open output folder", state="disabled",
                                 command=lambda: open_folder(outdir_var.get()))
    open_folder_btn.pack(**pad)

    root.mainloop()


def main():
    # No arguments -> friendly point-and-click window.
    # Any arguments -> classic command-line mode (for scripting/automation).
    if len(sys.argv) > 1:
        main_cli()
    else:
        try:
            main_gui()
        except ImportError:
            sys.exit(
                "No PDF given and tkinter isn't available for the GUI.\n"
                "Either install tkinter, or run from the command line:\n"
                "  python3 pdf_to_csv.py input.pdf [-o output_folder]"
            )


if __name__ == "__main__":
    main()
