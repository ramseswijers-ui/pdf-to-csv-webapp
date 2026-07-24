#!/usr/bin/env python3
"""
Web front end for pdf_to_csv.py - upload a technical PDF in the browser,
get title-block / callout / full-text CSVs back.

Run locally:
    pip install -r requirements.txt
    python3 app.py
    -> open http://localhost:5000

Deployment notes are in README.md.
"""
import csv
import io
import shutil
import uuid
import zipfile
from pathlib import Path

from flask import (
    Flask, render_template, request, send_file, redirect,
    url_for, abort, flash,
)

import pdf_to_csv as core

BASE_DIR = Path(__file__).parent
JOBS_DIR = BASE_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)
MAX_UPLOAD_MB = 25
PREVIEW_ROWS = 25

app = Flask(__name__)
app.secret_key = "pdf-to-csv-dev-secret"  # replace with a real secret if you deploy this publicly
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


def job_dir(job_id):
    d = JOBS_DIR / job_id
    if not d.exists():
        abort(404)
    return d


def read_preview(csv_path, limit=PREVIEW_ROWS):
    """Read up to `limit` rows of a CSV for an in-browser preview table."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        rows = []
        total = 0
        for row in reader:
            total += 1
            if len(rows) < limit:
                rows.append(row)
        return header, rows, total


@app.route("/")
def index():
    return render_template("index.html", max_mb=MAX_UPLOAD_MB)


@app.route("/convert", methods=["POST"])
def convert():
    uploaded = request.files.get("pdf")
    if not uploaded or uploaded.filename == "":
        flash("Choose a PDF file first.")
        return redirect(url_for("index"))
    if not uploaded.filename.lower().endswith(".pdf"):
        flash("That doesn't look like a PDF. Please upload a .pdf file.")
        return redirect(url_for("index"))

    job_id = uuid.uuid4().hex[:12]
    work_dir = JOBS_DIR / job_id
    work_dir.mkdir(parents=True)

    pdf_path = work_dir / "input.pdf"
    uploaded.save(pdf_path)

    try:
        result = core.process_pdf(pdf_path, work_dir, log=lambda *_: None)
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        flash(f"Couldn't process that PDF: {e}")
        return redirect(url_for("index"))

    # keep the original name around for nicer download filenames
    (work_dir / "original_name.txt").write_text(uploaded.filename)

    return redirect(url_for("results", job_id=job_id))


@app.route("/results/<job_id>")
def results(job_id):
    d = job_dir(job_id)
    original_name = (d / "original_name.txt").read_text().strip() if (d / "original_name.txt").exists() else "document.pdf"
    base = Path(original_name).stem

    files = {
        "titleblock": d / "input_titleblock.csv",
        "callouts": d / "input_callouts.csv",
        "full_text": d / "input_full_text.csv",
    }

    previews = {}
    for key, path in files.items():
        if path.exists():
            header, rows, total = read_preview(path)
            previews[key] = {"header": header, "rows": rows, "total": total}
        else:
            previews[key] = {"header": [], "rows": [], "total": 0}

    return render_template(
        "results.html",
        job_id=job_id,
        base=base,
        previews=previews,
        preview_rows=PREVIEW_ROWS,
    )


@app.route("/download/<job_id>/<which>")
def download(job_id, which):
    d = job_dir(job_id)
    mapping = {
        "titleblock": "input_titleblock.csv",
        "callouts": "input_callouts.csv",
        "full_text": "input_full_text.csv",
    }
    if which not in mapping:
        abort(404)
    path = d / mapping[which]
    if not path.exists():
        abort(404)

    original_name = (d / "original_name.txt").read_text().strip() if (d / "original_name.txt").exists() else "document.pdf"
    base = Path(original_name).stem
    download_name = f"{base}_{which}.csv"
    return send_file(path, as_attachment=True, download_name=download_name, mimetype="text/csv")


@app.route("/download-zip/<job_id>")
def download_zip(job_id):
    d = job_dir(job_id)
    original_name = (d / "original_name.txt").read_text().strip() if (d / "original_name.txt").exists() else "document.pdf"
    base = Path(original_name).stem

    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        for key, fname in [("titleblock", "input_titleblock.csv"),
                            ("callouts", "input_callouts.csv"),
                            ("full_text", "input_full_text.csv")]:
            path = d / fname
            if path.exists():
                zf.write(path, arcname=f"{base}_{key}.csv")
    mem.seek(0)
    return send_file(mem, as_attachment=True, download_name=f"{base}_csv_export.zip", mimetype="application/zip")


@app.errorhandler(413)
def too_large(e):
    flash(f"That file is over the {MAX_UPLOAD_MB} MB limit for this demo.")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
