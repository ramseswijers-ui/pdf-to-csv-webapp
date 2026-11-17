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
import math
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
    r"^[øØ∅Φ]\d",                # diameter glued to its value: ø37
    r"\d[xX]\d",                 # chamfers: 1x45°
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
OCR_FALLBACK_THRESHOLD = 30
OCR_DPI = 450
OCR_CONFIG = "--psm 6"


def _norm_ocr_text(t):
    # OCR engines often return a different glyph for the diameter symbol
    for bad in ("Φ", "φ", "⌀", "∅"):
        t = t.replace(bad, "Ø")
    t = t.replace("（", "(").replace("）", ")")
    t = re.sub(r"(?<![\w*])\*(?=\d)", "+", t)                   # a plus read as an asterisk
    t = re.sub(r"[\u2e80-\u9fff\uff00-\uffef]", " ", t)  # CJK lookalikes of GD&T symbols
    t = re.sub(r"\(\s?[x×]\s?\+\s?\)?", "(4x)", t)       # "(4x)" read upside down
    t = re.sub(r"[({\[]\s?(\d)\s?[x×X]\s?[)}\]]", r"(\1x)", t)
    return re.sub(r"\s+", " ", t).strip()


_DIMLIKE_RE = re.compile(
    r"(\d\s?[xX\u00d7]\s?\)|\(\s?\d+\s?[xX\u00d7]|[\u00b1+\-]\s?\d|\u00b0|^[RrQ\u00d8]\s?\d|"
    r"\d\s?[xX\u00d7]\s?\d|^\d+([.,]\d+)?$|^M\d|\bref\b)")


def _dimlike(t):
    """Does this OCR text look like a dimension callout (not a random word)?"""
    return bool(_DIMLIKE_RE.search(t.strip()))


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

            found = []  # (box_px_in_original_space, text, upright, conf)
            rx, ry = int(W * 0.5), int(H * 0.7)   # title-block corner (re-read below)

            def _score(t, c):
                # OCR drops small symbols far more often than it invents them
                return c + 0.05 * len(t.replace(" ", "")) + (0.08 if ("°" in t or "±" in t) else 0)

            GLYPH_CH = ("Q", "Ø", "Φ", "φ")

            def _chars(t):
                return re.sub(r"[^\w°±.,]", "", t)      # letters, digits, ° ± . , only

            def _subseq(small, big):
                it = iter(big)
                return all(ch in it for ch in small)

            def add(b, txt, upright, conf, strict=False):
                """Keep the best reading per place. A reading that overlaps an
                existing one only wins if it scores clearly higher, is not just
                a shortened version of it, and (for risky slanted reads) really
                extends what is already there."""
                ov = [i for i, f in enumerate(found) if _iomin(b, f[0]) >= 0.4]
                if not ov:
                    found.append((b, txt, upright, conf))
                    return
                new_c = _chars(txt)
                if strict and not all(_subseq(_chars(found[i][1]), new_c) for i in ov):
                    return
                if len(ov) == 1:
                    old_c = _chars(found[ov[0]][1])
                    if len(new_c) < len(old_c) and _subseq(new_c, old_c):
                        return                    # "37" must not replace "Q 37"
                    # same text, but with a small symbol OCR usually drops (° ± Ø) added
                    if len(new_c) > len(old_c) and _subseq(old_c, new_c) and conf >= 0.75:
                        i = j = 0
                        extra = []
                        while i < len(new_c):
                            if j < len(old_c) and new_c[i] == old_c[j]:
                                j += 1
                            else:
                                extra.append(new_c[i])
                            i += 1
                        if extra and all(ch in "°±Ø" for ch in extra):
                            rest = [f for k_, f in enumerate(found) if k_ != ov[0]]
                            found[:] = rest + [(b, txt, upright, conf)]
                            return
                old_best = max(_score(found[i][1], found[i][3]) for i in ov)
                old_len = sum(len(_chars(found[i][1])) for i in ov)
                if _score(txt, conf) > old_best + 0.02 and (len(ov) == 1 or len(new_c) >= old_len):
                    keepers = []
                    if not any(g in new_c for g in GLYPH_CH):
                        keepers = [found[i] for i in ov if _chars(found[i][1]) in GLYPH_CH]
                    rest = [f for i, f in enumerate(found) if i not in ov]
                    found[:] = rest + keepers + [(b, txt, upright, conf)]

            # pass 1: upright (optionally tiled)
            for pts, txt, conf in run(img):
                if float(conf) >= 0.5:
                    add(to_box(pts), txt, True, float(conf))
            tiles = []
            if use_tiles:
                tile, ov_px = 1400, 200
                for y in range(0, H, tile - ov_px):
                    for x in range(0, W, tile - ov_px):
                        tiles.append((x, y, min(x + tile, W), min(y + tile, H)))
                for (x, y, x2, y2) in tiles:
                    for pts, txt, conf in run(img.crop((x, y, x2, y2))):
                        if float(conf) < 0.5:
                            continue
                        b = to_box(pts)
                        add((b[0]+x, b[1]+y, b[2]+x, b[3]+y), txt, True, float(conf))

            rtiles = []
            if use_tiles:      # small tiles: vertical text is found much better in a small picture
                rt, rov = 700, 150
                for y in range(0, H, rt - rov):
                    for x in range(0, W, rt - rov):
                        rtiles.append((x, y, min(x + rt, W), min(y + rt, H)))

            def floor_for(txt):
                # horizontal text read sideways is junk, so be strict - unless
                # it looks like a dimension, which is very unlikely by chance
                return 0.7 if _dimlike(_norm_ocr_text(txt)) else 0.85

            # pass 2/3: rotated 90 degrees either way (vertical text), whole page + tiles
            # PIL ROTATE_90 is counter-clockwise: (x, y) -> (y, W-1-x)
            for rot, back in ((Image.ROTATE_90, "ccw"), (Image.ROTATE_270, "cw")):
                def unrot(pts, w_, h_, ox=0, oy=0):
                    cs = []
                    for (px, py) in pts:
                        cs.append((w_ - 1 - py, px) if back == "ccw" else (py, h_ - 1 - px))
                    b = to_box(cs)
                    return (b[0]+ox, b[1]+oy, b[2]+ox, b[3]+oy)
                for pts, txt, conf in run(img.transpose(rot)):
                    if float(conf) >= floor_for(txt):
                        add(unrot(pts, W, H), txt, False, float(conf))
                for (x, y, x2, y2) in rtiles:
                    for pts, txt, conf in run(img.crop((x, y, x2, y2)).transpose(rot)):
                        if float(conf) >= floor_for(txt):
                            add(unrot(pts, x2 - x, y2 - y, x, y), txt, False, float(conf))

            # pass 4: slanted text (+/-30, 45, 60 degrees), whole page, only
            # dimension-like results and never in the title-block corner
            import math
            for th in (30, -30, 45, -45, 60, -60):
                rim = img.rotate(th, expand=True, fillcolor=(255, 255, 255))
                Wp, Hp = rim.size
                t_ = math.radians(th)
                for pts, txt, conf in run(rim):
                    if float(conf) < 0.75 or not _dimlike(_norm_ocr_text(txt)):
                        continue
                    cs = []
                    for (qx, qy) in pts:
                        dx, dy = qx - Wp / 2, qy - Hp / 2
                        cs.append((W / 2 + math.cos(t_) * dx - math.sin(t_) * dy,
                                   H / 2 + math.sin(t_) * dx + math.cos(t_) * dy))
                    b = to_box(cs)
                    if (b[0] + b[2]) / 2 >= rx and (b[1] + b[3]) / 2 >= ry:
                        continue
                    add(b, txt, False, float(conf), strict=True)

            # lone ø glyphs: the number next to them was not read - look again, zoomed in
            pad = int(40 / scale)
            for f in [f for f in found if _chars(f[1]) in GLYPH_CH]:
                gx0, gy0, gx1, gy1 = f[0]
                ox, oy = max(0, int(gx0 - pad)), max(0, int(gy0 - pad))
                sub_im = img.crop((ox, oy, min(W, int(gx1 + pad)), min(H, int(gy1 + pad))))
                big = sub_im.resize((sub_im.width * 2, sub_im.height * 2), Image.LANCZOS)
                bw, bh = big.size
                for rot in (None, Image.ROTATE_90, Image.ROTATE_270):
                    im2 = big if rot is None else big.transpose(rot)
                    for pts, txt, conf in run(im2):
                        if float(conf) < 0.7 or len(_chars(txt)) < 2 or not _dimlike(_norm_ocr_text(txt)):
                            continue
                        if rot is None:
                            cs = pts
                        elif rot == Image.ROTATE_90:
                            cs = [(bw - 1 - py, px) for px, py in pts]
                        else:
                            cs = [(py, bh - 1 - px) for px, py in pts]
                        bb = to_box(cs)
                        add((bb[0] / 2 + ox, bb[1] / 2 + oy, bb[2] / 2 + ox, bb[3] / 2 + oy),
                            txt, rot is None, float(conf))

            # small print in the title block (material, treatment, description)
            # is misread at page scale: re-read that corner at double size
            crop = img.crop((rx, ry, W, H)).resize(((W - rx) * 2, (H - ry) * 2), Image.LANCZOS)
            fresh = []
            for pts, txt, conf in run(crop):
                if float(conf) >= 0.5:
                    bx = to_box(pts)
                    fresh.append(((bx[0] / 2 + rx, bx[1] / 2 + ry, bx[2] / 2 + rx, bx[3] / 2 + ry), txt, True, float(conf)))
            found = [f for f in found
                     if not ((f[0][0] + f[0][2]) / 2 >= rx and (f[0][1] + f[0][3]) / 2 >= ry)] + fresh

            for (x0, y0, x1, y1), txt, upright, _conf in found:
                if re.fullmatch(r"\s+\d+(?:[.,]\d+)?", txt):
                    txt = "Ø" + txt          # OCR leaves a blank where it can't read the ø symbol
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


_XFRAG = re.compile(r"^[+\-±]\s?\d+(?:[.,]\d+)?$|^0$|^\(\d+\s?[xX×]\)$|^[QØø]$")


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
                    # never merge horizontal and rotated text - except small
                    # OCR fragments (a stacked tolerance, "(4x)", the ø glyph)
                    if not (is_ocr and (_XFRAG.match(wi["text"].strip()) or _XFRAG.match(wj["text"].strip()))):
                        continue
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
            "members": ws,
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
    position -> label lines. Works for any direction/count. Only markers
    that line up in one border strip are used, and two are enough: the
    rest of the grid is extrapolated from their spacing.
    Returns (col_fit, row_fit, digit_strip_y, letter_strip_x) or None."""
    digits, letters = [], []
    for w in words:
        t = w["text"].strip()
        cx, cy = (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2
        if re.fullmatch(r"\d{1,2}", t) and 1 <= int(t) <= 20 and (cy < page_h * 0.07 or cy > page_h * 0.94):
            digits.append((int(t), cx, cy))
        elif re.fullmatch(r"[A-L]", t) and (cx < page_w * 0.06 or cx > page_w * 0.96):
            letters.append((ord(t) - 64, cy, cx))

    def best_strip(items):
        strips = {}
        for label, along, cross in items:
            strips.setdefault(round(cross / 10), []).append((label, along, cross))
        if not strips:
            return {}, None
        key = max(strips, key=lambda k: (len({l for l, _, _ in strips[k]}), -k))
        d = {}
        for label, along, _ in strips[key]:
            d.setdefault(label, []).append(along)
        return d, sum(c for _, _, c in strips[key]) / len(strips[key])

    def fit(d, extent, cap):
        if len(d) < 2:
            return None
        pts = [(sum(v) / len(v), k) for k, v in d.items()]
        n = len(pts)
        mx = sum(p for p, _ in pts) / n
        mk = sum(k for _, k in pts) / n
        var = sum((p - mx) ** 2 for p, _ in pts)
        if var == 0:
            return None
        slope = sum((p - mx) * (k - mk) for p, k in pts) / var
        if slope == 0:
            return None
        k0 = mk + slope * (extent * 0.045 - mx)
        k1 = mk + slope * (extent * 0.955 - mx)
        return (mx, mk, slope, 1, min(cap, max(max(d), round(max(k0, k1)))))

    dd, dcross = best_strip([(l, x, y) for l, x, y in digits])
    ld, lcross = best_strip([(l, y, x) for l, y, x in letters])
    cf, rf = fit(dd, page_w, 20), fit(ld, page_h, 12)
    return (cf, rf, dcross, lcross) if cf and rf else None


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



# ---------------------------------------------------------------------------
# Boxes drawn on the sheet: TED boxes and tolerance (GD&T) frames
# ---------------------------------------------------------------------------
def _render_gray(pdf_path, page_no, page_w, page_h):
    """Render one page to a grayscale array (longest side ~4000 px).
    Returns (image, points_per_pixel)."""
    import subprocess
    import tempfile
    import cv2
    dpi = int(min(300, 4000 / (max(page_w, page_h) / 72.0)))
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["pdftoppm", "-png", "-gray", "-r", str(dpi), "-f", str(page_no),
                        "-l", str(page_no), "-singlefile", str(pdf_path), f"{tmp}/g"],
                       check=True, capture_output=True)
        img = cv2.imread(f"{tmp}/g.png", cv2.IMREAD_GRAYSCALE)
    return img, 72.0 / dpi


def _is_parallelogram(approx):
    """4 corners with equal opposite sides (tells ▱ from a half circle)."""
    import math
    p = [tuple(pt[0]) for pt in approx]
    d = [math.dist(p[i], p[(i + 1) % 4]) for i in range(4)]
    return abs(d[0] - d[2]) <= 0.2 * max(d[0], d[2]) and abs(d[1] - d[3]) <= 0.2 * max(d[1], d[3])


def _symbol_from_image(gray, cell, scale, rotate=False):
    """Name the geometric-tolerance symbol in a frame's first cell from the
    picture: how many separate shapes it has and how many holes in them.
    Returns POS / ◎ / ○ / ▱, or "" if unsure."""
    import cv2
    import math
    x0, y0 = int(cell["x0"] / scale), int(cell["top"] / scale)
    x1, y1 = int(cell["x1"] / scale), int(cell["bottom"] / scale)
    crop = gray[y0 + 3:y1 - 3, x0 + 3:x1 - 3]
    if crop.size == 0 or min(crop.shape) < 8:
        return ""
    if rotate:                                   # a frame drawn vertically
        import numpy as np
        crop = np.ascontiguousarray(np.rot90(crop))
    ink = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    cont, hier = cv2.findContours(ink, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hier is None:
        return ""
    cell_area = crop.shape[0] * crop.shape[1]
    blobs, holes = [], {}
    for i, c in enumerate(cont):
        parent = hier[0][i][3]
        if parent == -1:
            if cv2.contourArea(c) >= 0.04 * cell_area:
                blobs.append(i)
        elif cv2.contourArea(c) >= 12:
            holes[parent] = holes.get(parent, 0) + 1
    if len(blobs) >= 2 and all(holes.get(b, 0) >= 1 for b in blobs[:2]):
        return "◎"
    if len(blobs) == 1:
        b = blobs[0]
        if holes.get(b, 0) >= 3:
            return "POS"
        if holes.get(b, 0) == 1:
            c = cont[b]
            per = cv2.arcLength(c, True)
            area = cv2.contourArea(c)
            w, h = cv2.boundingRect(c)[2:]
            asp = max(w, h) / max(min(w, h), 1)
            circ = 4 * math.pi * area / per ** 2 if per > 0 else 0
            approx = cv2.approxPolyDP(c, 0.04 * per, True)
            if len(approx) == 4 and asp >= 1.3 and _is_parallelogram(approx):
                return "▱"
            if asp >= 1.5 and circ > 0.6:        # dome on a flat base
                return "⌓"
            if circ > 0.75:
                return "○"
    return ""


FORCE_IMAGE_CELLS = False


def find_cells(pdf_path):
    """Rectangles drawn on each page. A rectangle that touches another one is
    part of a tolerance frame (a chain of cells); a lone one is a box around a
    dimension (TED / basic dimension). Uses the vector lines when the PDF has
    them, otherwise finds the lines in the rendered picture (some exports
    contain the whole drawing as one image). Returns {page: [cell dicts]}."""
    from collections import defaultdict
    import cv2
    result = {}
    with pdfplumber.open(pdf_path) as pdf:
        for pn, page in enumerate(pdf.pages, start=1):
            gray, scale = None, None
            vector = len(page.edges) >= 200 and not FORCE_IMAGE_CELLS
            if vector:
                H = [e for e in page.horizontal_edges if 4 <= abs(e["x1"] - e["x0"]) <= 260]
                V = [e for e in page.vertical_edges if 4 <= abs(e["bottom"] - e["top"]) <= 90]
                hmap = defaultdict(list)
                for e in H:
                    hmap[round(e["top"])].append((e["x0"], e["x1"]))
                vmap = defaultdict(list)
                for e in V:
                    vmap[round(e["top"])].append(e)

                def has_h(y, xa, xb):
                    return any(hx0 <= xa + 1.5 and hx1 >= xb - 1.5
                               for k in (round(y) - 1, round(y), round(y) + 1)
                               for hx0, hx1 in hmap.get(k, []))

                cands = {}
                for v1 in V:
                    for k in (round(v1["top"]) - 1, round(v1["top"]), round(v1["top"]) + 1):
                        for v2 in vmap.get(k, []):
                            dx = v2["x0"] - v1["x0"]
                            if not (6 <= dx <= 260):
                                continue
                            if abs(v2["top"] - v1["top"]) > 1.5 or abs(v2["bottom"] - v1["bottom"]) > 1.5:
                                continue
                            if has_h(v1["top"], v1["x0"], v2["x0"]) and has_h(v1["bottom"], v1["x0"], v2["x0"]):
                                key = (round(v1["x0"]), round(v2["x0"]), round(v1["top"]), round(v1["bottom"]))
                                cands[key] = {"x0": v1["x0"], "x1": v2["x0"], "top": v1["top"], "bottom": v1["bottom"]}
                cells = []
                for c in cands.values():   # keep only the smallest cells
                    crossed = any(c["x0"] + 2 < v["x0"] < c["x1"] - 2
                                  and abs(v["top"] - c["top"]) <= 1.5 and abs(v["bottom"] - c["bottom"]) <= 1.5
                                  for k in (round(c["top"]) - 1, round(c["top"]), round(c["top"]) + 1)
                                  for v in vmap.get(k, []))
                    if not crossed:
                        cells.append(c)
                adj_tol = 1.5
            def image_cells():
                """Rectangles found in the rendered picture (line strokes only)."""
                nonlocal gray, scale
                if gray is None:
                    gray, scale = _render_gray(pdf_path, pn, page.width, page.height)
                ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
                k = max(10, int(8 / scale))          # keep only strokes longer than ~8 pt
                hor = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (k, 1)))
                ver = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, k)))
                mask = cv2.dilate(cv2.bitwise_or(hor, ver), cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
                cont, hier = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
                found = []
                if hier is not None:
                    for i, c in enumerate(cont):
                        if hier[0][i][3] == -1:      # only the holes = inside of a box
                            continue
                        x, y, w, h = cv2.boundingRect(c)
                        if not (6 <= w * scale <= 260 and 6 <= h * scale <= 90):
                            continue
                        if cv2.contourArea(c) / float(w * h) < 0.9:
                            continue                 # not rectangular
                        found.append({"x0": x * scale, "x1": (x + w) * scale,
                                      "top": y * scale, "bottom": (y + h) * scale})
                return found

            if not vector:
                cells = image_cells()
                adj_tol = 3.0

            def tilted_boxes():
                """Closed four-sided boxes drawn at an angle (a boxed dimension
                turned with its dimension line)."""
                import numpy as np
                nonlocal gray, scale
                if gray is None:
                    gray, scale = _render_gray(pdf_path, pn, page.width, page.height)
                ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
                cont, hier = cv2.findContours(ink, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
                out = []
                if hier is None:
                    return out
                for i, c in enumerate(cont):
                    if hier[0][i][3] == -1:
                        continue                      # only holes: the inside of a box
                    (cx_, cy_), (rw, rh), ang = cv2.minAreaRect(c)
                    wpt, hpt = rw * scale, rh * scale
                    if not (8 <= min(wpt, hpt) and max(wpt, hpt) <= 90 and min(wpt, hpt) <= 60):
                        continue
                    a90 = ang % 90
                    if a90 < 8 or a90 > 82:
                        continue                      # upright boxes are handled above
                    per = cv2.arcLength(c, True)
                    if per == 0 or len(cv2.approxPolyDP(c, 0.04 * per, True)) != 4:
                        continue
                    if cv2.contourArea(c) / max(rw * rh, 1) < 0.9:
                        continue
                    box = cv2.boxPoints(((cx_, cy_), (rw, rh), ang)) * scale
                    out.append({"x0": float(box[:, 0].min()), "x1": float(box[:, 0].max()),
                                "top": float(box[:, 1].min()), "bottom": float(box[:, 1].max()),
                                "multi": False, "tilted": True, "poly": box.tolist()})
                return out

            def symbol_of(cell, rotate=False):
                nonlocal gray, scale
                glyph = ""
                for ch in page.chars:
                    if (ch["x0"] >= cell["x0"] - 1 and ch["x1"] <= cell["x1"] + 1
                            and ch["top"] >= cell["top"] - 1 and ch["bottom"] <= cell["bottom"] + 1):
                        glyph = {"⌖": "POS", "◎": "◎", "⏥": "▱", "▱": "▱", "○": "○", "⌓": "⌓"}.get(ch["text"], glyph)
                if not glyph:
                    if gray is None:
                        gray, scale = _render_gray(pdf_path, pn, page.width, page.height)
                    glyph = _symbol_from_image(gray, cell, scale, rotate)
                return glyph

            parent = list(range(len(cells)))

            def find(i):
                while parent[i] != i:
                    parent[i] = parent[parent[i]]
                    i = parent[i]
                return i

            for i, a in enumerate(cells):
                for j, b in enumerate(cells):
                    if (i != j and 0 <= b["x0"] - a["x1"] <= adj_tol
                            and abs(a["top"] - b["top"]) <= adj_tol
                            and abs(a["bottom"] - b["bottom"]) <= adj_tol):
                        parent[find(i)] = find(j)
            groups = defaultdict(list)
            for i in range(len(cells)):
                groups[find(i)].append(i)
            for root, idxs in groups.items():
                multi = len(idxs) > 1
                first = min(idxs, key=lambda i: cells[i]["x0"])
                for i in idxs:
                    cells[i]["multi"] = multi
                    cells[i]["chain"] = (pn, root)
                    cells[i]["first"] = multi and i == first
                    cells[i]["symbol"] = ""
                if multi:
                    cells[first]["symbol"] = symbol_of(cells[first])

            # frames drawn vertically: cells stacked on top of each other
            def mark_vertical(cl, tol, chain_base):
                vparent = list(range(len(cl)))

                def vfind(i):
                    while vparent[i] != i:
                        vparent[i] = vparent[vparent[i]]
                        i = vparent[i]
                    return i

                for i, a_ in enumerate(cl):
                    for j, b_ in enumerate(cl):
                        if (i != j and 0 <= b_["top"] - a_["bottom"] <= tol
                                and abs(a_["x0"] - b_["x0"]) <= tol and abs(a_["x1"] - b_["x1"]) <= tol
                                and a_["x1"] - a_["x0"] <= 32):
                            vparent[vfind(i)] = vfind(j)
                vgroups = defaultdict(list)
                for i in range(len(cl)):
                    vgroups[vfind(i)].append(i)
                made = []
                for root, idxs in vgroups.items():
                    if not (2 <= len(idxs) <= 5) or any(cl[i].get("multi") for i in idxs):
                        continue
                    top_i = min(idxs, key=lambda i: cl[i]["top"])
                    bot_i = max(idxs, key=lambda i: cl[i]["bottom"])
                    sym_top = symbol_of(cl[top_i], True)
                    sym_bot = symbol_of(cl[bot_i], True)
                    first = top_i if (sym_top and not sym_bot) else bot_i   # text reads bottom to top
                    sym = sym_top if first == top_i else sym_bot
                    for i in idxs:
                        cl[i].update({"multi": True, "vertical": True, "chain": (pn, chain_base + root),
                                      "first": i == first, "symbol": ""})
                    cl[first]["symbol"] = sym
                    made.append((idxs, bool(sym)))
                return made

            for c_ in cells:
                c_.setdefault("multi", False)
            mark_vertical(cells, adj_tol, 100000)
            if vector:
                # vector pass misses frames whose side is one long line: take
                # those from the picture, and drop vector cells they replace
                extra = image_cells()
                for c_ in extra:
                    c_["multi"] = False
                for idxs, _ in mark_vertical(extra, 3.0, 200000):
                    chain_cells = [extra[i] for i in idxs]
                    clash = [c_ for c_ in cells if any(_iomin_cell(c_, e) > 0.3 for e in chain_cells)]
                    if any(c_["multi"] and not c_.get("vertical") for c_ in clash):
                        for i in idxs:                     # horizontal frame wins
                            extra[i]["multi"] = False
                        continue
                    for c_ in clash:
                        cells.remove(c_)
                    cells.extend(chain_cells)
            cells.extend(tilted_boxes())
            result[pn] = cells
    return result


def _iomin_cell(a, b):
    ix = max(0, min(a["x1"], b["x1"]) - max(a["x0"], b["x0"]))
    iy = max(0, min(a["bottom"], b["bottom"]) - max(a["top"], b["top"]))
    sm = min((a["x1"] - a["x0"]) * (a["bottom"] - a["top"]), (b["x1"] - b["x0"]) * (b["bottom"] - b["top"]))
    return ix * iy / sm if sm > 0 else 0.0


def _overlap_frac(w, cell):
    ix = max(0, min(w["x1"], cell["x1"]) - max(w["x0"], cell["x0"]))
    iy = max(0, min(w["bottom"], cell["bottom"]) - max(w["top"], cell["top"]))
    area = max((w["x1"] - w["x0"]) * (w["bottom"] - w["top"]), 0.01)
    return ix * iy / area


def build_items(words, cells):
    """Turn the page's words into characteristics:
      - tolerance frames (symbol cell + value cells): their own item ("frame")
      - every word boxed on its own becomes a "ted" item (one per box)
      - everything else is grouped by proximity ("plain")
    A multiplicity left on its own, like (2x), is attached to its neighbour."""
    # a chain only counts as a frame when its first (symbol) cell holds no number;
    # "0 | 14,5" side by side are two boxed dimensions, not a frame
    chains = {}
    for pn, cl in cells.items():
        for c in cl:
            if c["multi"]:
                chains.setdefault(c["chain"], []).append(c)
    for chain, ck in chains.items():
        first = next(k for k in ck if k["first"])
        numeric_first = any(w["page"] == chain[0] and _overlap_frac(w, first) >= 0.4
                            and re.fullmatch(_NUM, w["text"].strip()) for w in words)
        if numeric_first:
            for k in ck:
                k["multi"], k["first"] = False, False
    for chain, ck in chains.items():
        # a vertical chain only counts as a frame when it has a recognised symbol or a number
        if ck[0].get("vertical") and ck[0]["multi"]:
            has_sym = any(k["symbol"] for k in ck)
            has_num = any(w["page"] == chain[0] and re.search(r"\d", w["text"]) and
                          any(_overlap_frac(w, k) >= 0.4 for k in ck) for w in words)
            if not (has_sym or has_num):
                for k in ck:
                    k["multi"], k["first"] = False, False
    by_chain = {ch: ck for ch, ck in chains.items() if ck[0]["multi"]}

    items, used = [], set()
    for chain, ck in by_chain.items():
        pn = chain[0]
        vals = []
        for i, w in enumerate(words):
            if w["page"] != pn:
                continue
            hit = max(ck, key=lambda k: _overlap_frac(w, k))
            if _overlap_frac(w, hit) >= 0.4:
                used.add(i)                      # symbol-cell junk is dropped too
                if not hit["first"]:
                    vals.append(w)
        if vals:
            if ck[0].get("vertical"):
                fc = next(k for k in ck if k["first"])
                sign = -1 if fc["top"] >= max(k["top"] for k in ck) else 1   # symbol at the bottom: read upwards
                vals.sort(key=lambda w: sign * (w["top"] + w["bottom"]) / 2)
            else:
                vals.sort(key=lambda w: (round(w["top"] / 5), w["x0"]))
            items.append({
                "kind": "frame", "page": pn, "text": " ".join(w["text"] for w in vals),
                "symbol": next((k["symbol"] for k in ck if k["first"]), ""),
                "x0": min(k["x0"] for k in ck), "x1": max(k["x1"] for k in ck),
                "top": min(k["top"] for k in ck), "bottom": max(k["bottom"] for k in ck),
            })

    GLYPHS = {"Q", "Ø", "ø", "O", "Φ"}
    import numpy as np
    import cv2
    for pn, cl in cells.items():                  # one TED item per box
        for k in cl:
            if k["multi"]:
                continue
            if k.get("tilted"):
                poly = np.array(k["poly"], dtype=np.float32)
                ins = [i for i, w in enumerate(words)
                       if w["page"] == pn and i not in used
                       and cv2.pointPolygonTest(poly, ((w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2), False) >= 0]
            else:
                ins = [i for i, w in enumerate(words)
                       if w["page"] == pn and i not in used and _overlap_frac(w, k) >= 0.5]
            if not ins or len(ins) > 3:
                continue
            ws = [words[i] for i in ins]
            ux0, ux1 = min(w["x0"] for w in ws), max(w["x1"] for w in ws)
            ut, ub = min(w["top"] for w in ws), max(w["bottom"] for w in ws)
            if not k.get("tilted") and ((k["x1"] - k["x0"]) > (ux1 - ux0) + 14 or (k["bottom"] - k["top"]) > (ub - ut) + 14):
                continue
            glyph = [w for w in ws if w["text"].strip() in GLYPHS]
            body = [w for w in ws if w not in glyph]
            if len(body) != 1:
                continue                          # a box holds one value; leave odd ones to the per-word rule
            body.sort(key=lambda w: (round(w["top"] / 5), w["x0"]))
            text = " ".join(w["text"] for w in body)
            if glyph and re.fullmatch(_NUM, text.strip()):
                text = "ø" + text.strip()
            used.update(ins)
            items.append({"kind": "ted", "page": pn, "text": text,
                          "x0": k["x0"], "x1": k["x1"], "top": k["top"], "bottom": k["bottom"]})

    def hugs(w, k):
        return (_overlap_frac(w, k) >= 0.3
                and (k["x1"] - k["x0"]) <= (w["x1"] - w["x0"]) + 12
                and (k["bottom"] - k["top"]) <= (w["bottom"] - w["top"]) + 10)

    for i, w in enumerate(words):                 # a box around a single word, the plain way
        if i in used:
            continue
        if any(hugs(w, k) for k in cells.get(w["page"], []) if not k["multi"] and not k.get("tilted")):
            used.add(i)
            items.append({"kind": "ted", "page": w["page"], "text": w["text"],
                          "x0": w["x0"], "x1": w["x1"], "top": w["top"], "bottom": w["bottom"]})

    rest = [w for i, w in enumerate(words) if i not in used]
    for c in cluster_words(rest):
        items.append({"kind": "plain", "page": c["page"], "text": c["text"],
                      "x0": c["x0"], "x1": c["x1"], "top": c["top"], "bottom": c["bottom"]})

    # a lone "(2x)" belongs to the dimension next to it
    mult = re.compile(r"\(?\d+\s?[xX]\)?")
    for orphan in [it for it in items if mult.fullmatch(it["text"].strip())]:
        ox, oy = (orphan["x0"] + orphan["x1"]) / 2, (orphan["top"] + orphan["bottom"]) / 2
        best = None
        # the nearest piece of text wins; only if there is none close by does a frame get it
        for want_frame, limit in ((False, 40.0), (True, 25.0)):
            bd = limit
            for it in items:
                if it is orphan or it["page"] != orphan["page"] or (it["kind"] == "frame") != want_frame:
                    continue
                d = math.hypot(max(it["x0"] - ox, 0, ox - it["x1"]), max(it["top"] - oy, 0, oy - it["bottom"]))
                if d < bd:
                    best, bd = it, d
            if best is not None:
                break
        if best is not None:
            best["text"] += " " + orphan["text"]
            items.remove(orphan)

    items.sort(key=lambda it: (it["page"], round(it["top"] / 10), it["x0"]))
    return items


# ---------------------------------------------------------------------------
# Writing a requirement the same way every time
# ---------------------------------------------------------------------------
_NUM = r"\d+(?:[.,]\d+)?"


def _dec(s):
    """Dutch decimal comma: 12.5 -> 12,5"""
    return re.sub(r"(?<=\d)\.(?=\d)", ",", s)


def _is_zero(v):
    return re.fullmatch(r"0+(?:,0+)?", v) is not None


def _fmt_upper(v):
    v = _dec(v.lstrip("+"))
    return "0" if _is_zero(v) else "+" + v


def _fmt_lower(v):
    v = _dec(v.lstrip("-–+"))
    return "0" if _is_zero(v) else "-" + v


def _tidy(t):
    t = re.sub(r"ø\s+(?=\d)", "ø", t)                       # "ø 3" -> "ø3"
    toks = [x for x in t.split()
            if (re.search(r"[\wø°]", x) or x in ("-", "–")) and not re.fullmatch(r"\d\.", x)]
    return " ".join(toks)


def normalize_requirement(text):
    """Split a callout into (requirement, upper tol, lower tol):
    requirement = symbol + value + (Nx), WITHOUT the tolerance text;
    tolerances in their own fields, upper always with a +, lower with a -
    (or 0). Example: '2x 8,5 ±0,5' -> ('8,5 (2x)', '+0,5', '-0,5')"""
    t = re.sub(r"[\[\]|]", " ", text).strip()                 # box edges read as brackets
    t = re.sub(r"[ØΦφ⌀∅]", "ø", t)
    if "ø" in t:
        t = re.sub(r"(?<![\w.,])Q(?![\w.,])", " ", t)           # the ø symbol, read twice
    t = re.sub(rf"\b({_NUM})Q\b", r"ø\1", t)                 # OCR reads ø as Q
    t = re.sub(rf"\bQ\s*({_NUM})\b", r"ø\1", t)
    t = re.sub(rf"\b({_NUM})\s+Q\b", r"ø\1", t)
    t = re.sub(r"(?<=\d)\s*[±£#]\s*(?=\d)", " ± ", t)        # 40±0,2 -> 40 ± 0,2
    t = re.sub(rf"[£#](?=\s*{_NUM})", "±", t)

    chamfers = []

    def keep(m):
        chamfers.append(m.group(0).replace(" ", ""))
        return f" §{len(chamfers) - 1}§ "
    t = re.sub(rf"{_NUM}\s?[xX]\s?{_NUM}\s?°", keep, t)       # 1x45° is not "1 times"

    mult = ""
    m = re.search(r"\(?\b(\d+)\s?[xX]\b\)?", t)
    if m:
        mult = f"({m.group(1)}x)"
        t = t[:m.start()] + " " + t[m.end():]

    upper = lower = ""
    m = re.search(rf"±\s*({_NUM})", t)
    if m:
        upper, lower = _fmt_upper(m.group(1)), _fmt_lower(m.group(1))
        t = t[:m.start()] + " " + t[m.end():]
    else:
        m = re.search(rf"\+\s*({_NUM})\s*/\s*[-–]?\s*({_NUM})", t)
        if m:
            upper, lower = _fmt_upper(m.group(1)), _fmt_lower(m.group(2))
            t = t[:m.start()] + " " + t[m.end():]
        else:
            toks = t.split()
            ups = [x for x in toks if re.fullmatch(rf"\+{_NUM}", x)]
            lows = [x for x in toks if re.fullmatch(rf"[-–]{_NUM}|0+(?:[.,]0+)?", x)]
            if len(ups) == 1 and len(lows) == 1:              # stacked: +1 over 0
                upper, lower = _fmt_upper(ups[0]), _fmt_lower(lows[0])
                toks.remove(ups[0])
                toks.remove(lows[0])
                t = " ".join(toks)
            elif len(ups) == 1 and not lows:                  # "+0,3" whose lower value was not read
                upper = _fmt_upper(ups[0])
                toks.remove(ups[0])
                t = " ".join(toks)

    t = re.sub(r"§(\d+)§", lambda mm: chamfers[int(mm.group(1))], t)
    t = _tidy(_dec(t))
    if re.search(r"DURCH ALLES|THROUGH ALL", t, re.I) and "ø" not in t:
        t = re.sub(rf"^({_NUM})", r"ø\1", t)
    if mult:
        t = (t + " " + mult).strip()
    return t, upper, lower


def frame_requirement(text):
    """(requirement, value) for the cells of a tolerance frame, e.g. 'ø0,2 A'."""
    t = re.sub(r"[ØΦφ⌀∅]", "ø", text)
    t = re.sub(r"[\u2460-\u24ff]\s*(?=\d)", "ø", t)          # ø read as a circled digit
    t = re.sub(r"[\u2460-\u27ff\u2900-\u2bff]", " ", t)
    t = re.sub(rf"\bQ\s*(?={_NUM})", "ø", t)
    t = re.sub(r"(?<!\d)00(?=[.,]\d)", "0", t)                 # "Ø00,2": the ø read as "Ø0"
    t = re.sub(r"(?<=\S)\((?=\d+\s?[xX])", " (", t)
    t = _tidy(_dec(t))
    t = re.sub(r"(\d)([A-Z])\b", r"\1 \2", t)               # 0,05A -> 0,05 A
    m = re.search(_NUM, t)
    return t, (m.group(0) if m else "")


GENERAL_NOTE = re.compile(r"m²|mm²|^\s*A\s*=", re.I)

# title-block labels -> the row they fill in at the top of the sheet
META_FIELDS = [
    ("1. Part Number:", ("partnumber", "dokumentennrdocumentno"), 1),
    ("2. Part Name:", ("description", "benennungtitle"), 1),
    ("Material:", ("materialnumber", "werkstoffbezeichnung"), 2),
    ("Material number:", ("werkstoffnr",), 1),
    ("Surface / heat / after treatment:", ("surfaceheataftertreatment", "oberflchenbehandlung"), 3),
    ("Surface roughness:", ("surfaceroughness",), 3),
]
OTHER_LABEL_KEYS = ("scale", "sheet", "status", "checkedby", "fordrawing", "firstangle", "former",
                    "tolerancesonlineardimensions", "tolerancesonangles",
                    "massstab", "gewicht", "format", "index", "bearbeitet", "geprft",
                    "genehmigt", "passung")


def _norm_key(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _is_label(text, keys):
    """Does this line start with one of the label keys? Tolerant of the odd
    misread letter OCR makes in small caps."""
    import difflib
    nk = _norm_key(text)
    return any(nk.startswith(k) or
               difflib.SequenceMatcher(None, nk[:len(k)], k).ratio() >= 0.88
               for k in keys)


def lines_from_words(words):
    """Group words into text lines (same row, no big gap) - used to read the
    title block, where each cell's label sits above its value."""
    rows = {}
    for w in words:
        if w.get("upright", True):
            rows.setdefault((w["page"], round(w["top"] / 4)), []).append(w)
    lines = []
    for (page, _), ws in rows.items():
        ws.sort(key=lambda w: w["x0"])
        cur = [ws[0]]
        for w in ws[1:]:
            if w["x0"] - cur[-1]["x1"] > 9:
                lines.append(cur)
                cur = []
            cur.append(w)
        lines.append(cur)
    return [{"page": g[0]["page"], "text": " ".join(w["text"] for w in g),
             "x0": min(w["x0"] for w in g), "x1": max(w["x1"] for w in g),
             "top": min(w["top"] for w in g), "bottom": max(w["bottom"] for w in g)}
            for g in lines]


def read_general_info(words):
    """Fill the sheet's general-info rows from the title block: each value
    sits under its label, in the same column."""
    lines = lines_from_words(words)
    all_keys = tuple(k for _, keys, _ in META_FIELDS for k in keys) + OTHER_LABEL_KEYS
    label_lines = [o for o in lines if o["page"] == 1 and _is_label(o["text"], all_keys)]
    out = {}
    for label, keys, maxlines in META_FIELDS:
        found = ""
        for L in lines:
            if L["page"] != 1 or not _is_label(L["text"], keys):
                continue
            # the cell ends where the next label to the right begins
            right = [o["x0"] for o in label_lines
                     if o is not L and o["x0"] > L["x1"] - 5
                     and L["top"] - 10 <= o["top"] <= L["bottom"] + 60]
            x_hi = (min(right) - 2) if right else L["x0"] + 280
            vals, last = [], L["bottom"]
            for v in sorted((o for o in lines if o["page"] == 1 and o["top"] >= L["bottom"] - 1),
                            key=lambda o: (round(o["top"] / 6), o["x0"])):
                if v["top"] - last > 16:
                    break
                if v["x0"] < L["x0"] - 40 or v["x0"] > x_hi:
                    continue
                if _is_label(v["text"], all_keys):
                    if vals:
                        break
                    continue
                vals.append(v["text"])
                last = v["bottom"]
                if len(vals) >= maxlines:
                    break
            found = " ".join(vals).strip(" -–")
            if not found:                         # value centred under a wide label
                for v in sorted((o for o in lines if o["page"] == 1 and o["top"] >= L["bottom"] - 1
                                 and o["top"] - L["bottom"] < 20),
                                key=lambda o: (round(o["top"] / 6), o["x0"])):
                    if v["x1"] >= L["x0"] and v["x0"] <= x_hi and not _is_label(v["text"], all_keys):
                        found = v["text"].strip(" -–")
                        break
            if found:
                break
        if found:
            if label == "2. Part Name:":
                found = re.sub(r"^ASML\s*(?:I(?=NXE))?\s*", "", found)
            if label == "Surface roughness:":
                found = re.sub(r"\bUNITS\s+mm\b\s*", "", found).replace("[", "(")
            out[label] = found
    return out


def build_fai_checklist(pdf_path, words, clusters, tb_rows, source_name=None):
    """Build the checklist laid out like Form 3 (First Article Inspection -
    Characteristic Accountability): general info first, then one numbered
    row per characteristic found on the drawing."""
    with pdfplumber.open(pdf_path) as pdf:
        sizes = {i: (p.width, p.height) for i, p in enumerate(pdf.pages, start=1)}
    page_w, page_h = sizes[1]

    cells = find_cells(pdf_path)
    grid = detect_zone_grid(words, page_w, page_h)
    tb_region = title_block_region(words, page_w, page_h)
    items = build_items(words, cells)

    rows, placements, notes = [], [], []
    char_no = 1
    for it in items:
        text = it["text"]
        if it["kind"] != "frame" and (len(text) > 60 or not is_callout(text)):
            continue
        cx, cy = (it["x0"] + it["x1"]) / 2, (it["top"] + it["bottom"]) / 2
        pw, ph = sizes.get(it["page"], (page_w, page_h))
        t1 = text.strip()
        # zone markers printed in the sheet border are not characteristics
        if grid and re.fullmatch(r"\d{1,2}", t1) and grid[2] is not None and abs(cy - grid[2]) <= 14:
            continue
        if grid and re.fullmatch(r"[A-L]", t1) and grid[3] is not None and abs(cx - grid[3]) <= 14:
            continue
        if re.fullmatch(r"\d{1,2}", t1) and (cy < ph * 0.07 or cy > ph * 0.94):
            continue
        if re.fullmatch(r"[A-L]", t1) and (cx < pw * 0.06 or cx > pw * 0.96):
            continue
        # revision-table rows ("16 10-07 MRQY") are not characteristics either
        if re.search(r"\b\d{1,2}-\d{2}\b", t1) and re.search(r"[A-Z]{3,}", t1):
            continue
        if it["page"] == 1 and tb_region and cx >= tb_region[0] and cy >= tb_region[1]:
            continue                                  # title block, not a characteristic
        if GENERAL_NOTE.search(text):
            notes.append(text)                        # e.g. "A = 0,0315 m²": general info
            continue

        note = text
        if it["kind"] == "frame":
            req, val = frame_requirement(text)
            sym = it["symbol"]
            requirement = ((sym + " ") if sym else "") + req
            upper, lower = (_fmt_upper(val), "0") if val else ("", "")
            note = f"[tolerance frame{'' if sym else ', symbol not recognised'}] {text}"
        elif it["kind"] == "ted":
            requirement = normalize_requirement(text)[0] + " (TED)"
            upper = lower = ""
            note = f"[boxed = TED] {text}"
        else:
            requirement, upper, lower = normalize_requirement(text)
            if upper.startswith("+") and not lower:
                lower = "0"                       # stacked "+x over 0": the 0 is often lost by OCR
                note = f"[lower tol assumed 0 - check] {text}"
            if re.match(r"^\s*[\[|]", text) or re.search(r"[\]|]\s*$", text):
                requirement += " (TED)"           # box side read as a bracket
                note = f"[boxed = TED] {text}"
        if not requirement and not upper:
            continue
        if it["kind"] == "plain" and re.fullmatch(r"\d", requirement.strip()) and not upper:
            continue                                  # a stray digit, not a dimension

        zone = zone_for_position(cx, cy, pw, ph, grid)
        rows.append([char_no, zone, "", requirement, upper, lower,
                     "", "", "", "", "", note])
        placements.append({"char_no": char_no, "page": it["page"], "x0": it["x0"],
                           "x1": it["x1"], "top": it["top"], "bottom": it["bottom"]})
        char_no += 1

    info = read_general_info(words)
    meta_rows = [
        ["1. Part Number:", info.get("1. Part Number:", "")],
        ["Rev. Level:", ""],
        ["2. Part Name:", info.get("2. Part Name:", "")],
        ["3. Serial Number:", ""],
        ["4. FAI Report:", ""],
    ]
    for label, _, _ in META_FIELDS[2:]:
        if label in info:
            meta_rows.append([label, info[label]])
    if notes:
        meta_rows.append(["Notes:", "; ".join(notes)])
    meta_rows.append(["Source File:", source_name or Path(pdf_path).name])
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


def write_fai_xlsx(path, meta_rows, table_rows):
    """Same content as the CSV, as an Excel sheet laid out like the FAI form:
    every cell stored as text and left aligned (a CSV opened in Excel
    right-aligns anything that looks like a number)."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "FAI"
    left = Alignment(horizontal="left", vertical="center")
    yellow = PatternFill("solid", fgColor="FFFF99")
    thin = Side(style="thin", color="888888")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    ncol = len(FAI_HEADER)

    ws.append(["Form 3: Characteristic Accountability, Verification and Compatibility Evaluation"])
    ws["A1"].font = Font(bold=True, size=12)
    ws.append([])
    for label, value in meta_rows:
        ws.append([label, value])
        r = ws.max_row
        ws.cell(r, 1).font = Font(bold=True)
        ws.cell(r, 1).fill = yellow
        for c in (1, 2):
            ws.cell(r, c).border = box
    ws.append([])
    ws.append(["Characteristic Accountability"] + [""] * 5 + ["Inspection / Test Results"])
    for c in (1, 7):
        ws.cell(ws.max_row, c).font = Font(bold=True)
    ws.append(FAI_HEADER)
    header_row = ws.max_row
    for c in range(1, ncol + 1):
        cell = ws.cell(header_row, c)
        cell.font = Font(bold=True)
        cell.fill = yellow
        cell.border = box
        cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    for row in table_rows:
        ws.append([str(v) for v in row])
        for c in range(1, ncol + 1):
            ws.cell(ws.max_row, c).border = box
    ws.append([])
    ws.append(["Signature indicates that all characteristics are accounted for and meet drawing requirements or are properly documented for disposition."])
    ws.append(["12. Prepared By:", "", "13. Date:", ""])
    ws.append(["Supporting Data Provided:", ""])
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is not None:
                if not isinstance(cell.value, str):
                    cell.value = str(cell.value)
                cell.number_format = "@"
            if cell.alignment.wrap_text:
                continue
            cell.alignment = left
    for i, w in enumerate([22, 20, 14, 34, 11, 11, 14, 18, 18, 12, 10, 60], start=1):
        ws.column_dimensions[chr(64 + i)].width = w
    ws.freeze_panes = ws.cell(header_row + 1, 1)
    wb.save(path)


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
            and any(re.sub(r"[^a-z0-9]", "", k) in re.sub(r"[^a-z0-9]", "", w["text"].lower())
                    for k in TITLE_BLOCK_WORDS)]
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
        others = [o for o in obstacles
                  if not (own[0] - 1 <= (o[0] + o[2]) / 2 <= own[2] + 1
                          and own[1] - 1 <= (o[1] + o[3]) / 2 <= own[3] + 1)]
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


_BALLOON_SIG = re.compile(
    rb"\nq\n0\.0 0\.0 [\d.]+ [\d.]+ re\nW\nn\n1 0 0 1 0 0 cm\nBT\n/F\d+(?:-\d+)? 12 Tf\n14\.4 TL\nET\n0\.85 0\.08 0\.08 RG")


def strip_previous_balloons(pdf_path, out_path):
    """If this PDF is an earlier output of this tool (red numbered balloons
    appended to every page), write a copy without them and return True.
    Otherwise do nothing and return False. This keeps the numbering stable
    when someone uploads a ballooned drawing again."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject
    reader = PdfReader(str(pdf_path))
    writer = PdfWriter(clone_from=reader)
    stripped = False
    for page in writer.pages:
        contents = page.get_contents()
        if contents is None:
            continue
        data = contents.get_data()
        hits = list(_BALLOON_SIG.finditer(data))
        if not hits:
            continue
        stream = DecodedStreamObject()
        stream.set_data(data[:hits[-1].start() + 1])     # keep the original, drop the overlay
        page[NameObject("/Contents")] = writer._add_object(stream)
        stripped = True
    if stripped:
        with open(out_path, "wb") as f:
            writer.write(f)
    return stripped


def process_pdf(pdf_path, outdir, log=print, source_name=None):
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
    source_name = source_name or pdf_path.name

    clean_path = outdir / f"_{base}_without_balloons.pdf"
    if strip_previous_balloons(pdf_path, clean_path):
        log("  Balloons from an earlier run were removed first.")
        pdf_path = clean_path

    log(f"Reading {source_name} ...")
    words = load_words(pdf_path)
    log(f"  {len(words)} words found across the document.")

    tb_rows = extract_titleblock(words)
    clusters = cluster_words(words)

    meta_rows, table_rows, placements = build_fai_checklist(pdf_path, words, clusters, tb_rows, source_name)

    out_path = outdir / f"{base}_checklist.csv"
    write_fai_csv(out_path, meta_rows, table_rows)
    xlsx_path = outdir / f"{base}_checklist.xlsx"
    write_fai_xlsx(xlsx_path, meta_rows, table_rows)
    log(f"  Checklist: {len(table_rows)} characteristics -> {out_path.name}")

    balloon_path = outdir / f"{base}_ballooned.pdf"
    annotate_pdf(pdf_path, balloon_path, placements, words)
    log(f"  Balloons: {len(placements)} numbered -> {balloon_path.name}")

    log("Done.")
    return {"checklist": out_path, "xlsx": xlsx_path, "checklist_rows": len(table_rows),
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
