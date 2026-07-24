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
]

# Regex fragments that flag a text token as a "dimension / callout" worth
# pulling into the checklist CSV (numbers, tolerances, threads, GD&T, etc.)
CALLOUT_PATTERNS = [
    r"^\d+([.,]\d+)?$",          # plain numbers: 45,21  110  0,2
    r"^[+\-±]\d+([.,]\d+)?$",    # signed values: +0,2  -0,3  ±0,1
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


def load_words(pdf_path):
    """Return list of dicts: page, text, x0, x1, top, bottom, upright for
    every word. Technical drawings often mix horizontal (title block) and
    rotated/vertical text (dimension callouts along vertical lines) - we
    keep the 'upright' flag so callers can avoid merging across the two."""
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
    return all_words


def cluster_words(words):
    """Group nearby words (same page) into callout clusters via union-find
    on expanded bounding-box overlap."""
    n = len(words)
    parent = list(range(n))

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
                if (wi["x0"] - CLUSTER_GAP_X <= wj["x1"] and
                        wj["x0"] - CLUSTER_GAP_X <= wi["x1"] and
                        wi["top"] - CLUSTER_GAP_Y <= wj["bottom"] and
                        wj["top"] - CLUSTER_GAP_Y <= wi["bottom"]):
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


def process_pdf(pdf_path, outdir, log=print):
    """Run the full extraction on one PDF and write the 3 CSVs.
    `log` is a callable used for progress messages (print, or a GUI callback).
    Returns a dict with the output paths and row counts."""
    pdf_path = Path(pdf_path)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    base = pdf_path.stem

    log(f"Reading {pdf_path.name} ...")
    words = load_words(pdf_path)
    log(f"  {len(words)} words found across the document.")

    # 1) Title block
    tb_rows = extract_titleblock(words)
    tb_path = outdir / f"{base}_titleblock.csv"
    write_csv(tb_path, tb_rows, ["page", "row_type", "line_text"])
    log(f"  Title block: {len(tb_rows)} lines -> {tb_path.name}")

    # 2) Callouts (dimensions/tolerances/etc.)
    clusters = cluster_words(words)
    callout_rows = []
    for c in clusters:
        if len(c["text"]) > 120:
            continue  # oversized cluster = merged title-block text, not a real callout
        if is_callout(c["text"]):
            callout_rows.append({
                "page": c["page"],
                "callout_text": c["text"],
                "x": round(c["x0"], 1),
                "y": round(c["top"], 1),
            })
    callout_path = outdir / f"{base}_callouts.csv"
    write_csv(callout_path, callout_rows, ["page", "callout_text", "x", "y"])
    log(f"  Callouts: {len(callout_rows)} groups -> {callout_path.name}")

    # 3) Full text dump (fallback overview, good for text-heavy PDFs too)
    full_rows = extract_full_text_rows(pdf_path)
    full_path = outdir / f"{base}_full_text.csv"
    write_csv(full_path, full_rows, ["page", "text"])
    log(f"  Full text: {len(full_rows)} lines -> {full_path.name}")

    log("Done.")
    return {
        "titleblock": tb_path, "titleblock_rows": len(tb_rows),
        "callouts": callout_path, "callouts_rows": len(callout_rows),
        "full_text": full_path, "full_text_rows": len(full_rows),
    }


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
