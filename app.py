#!/usr/bin/env python3
"""
Web front end for pdf_to_csv.py - upload a technical PDF, get back one
FAI-style checklist CSV: a metadata block plus one row per dimension/
tolerance callout, ready to fill in Results during a physical check.
"""
import csv
import io
import shutil
import uuid
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
PREVIEW_ROWS = 30

app = Flask(__name__)
app.secret_key = "pdf-to-csv-dev-secret"  # replace with a real secret if you deploy this publicly
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


def job_dir(job_id):
    d = JOBS_DIR / job_id
    if not d.exists():
        abort(404)
    return d


def read_preview(csv_path, limit=PREVIEW_ROWS):
    """Read the metadata block + up to `limit` characteristic rows for an
    in-browser preview table."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = list(csv.reader(f))

    # find the header row (the FAI_HEADER line) to split metadata from table
    header_idx = next((i for i, row in enumerate(reader) if row and row[0] == "Char No."), None)
    if header_idx is None:
        return {"meta": [], "header": [], "rows": [], "total": 0}

    meta = [row for row in reader[1:header_idx] if row]
    header = reader[header_idx]
    data_rows = reader[header_idx + 1:]
    total = len(data_rows)
    return {"meta": meta, "header": header, "rows": data_rows[:limit], "total": total}


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

    (work_dir / "original_name.txt").write_text(uploaded.filename)

    return redirect(url_for("results", job_id=job_id))


@app.route("/results/<job_id>")
def results(job_id):
    d = job_dir(job_id)
    original_name = (d / "original_name.txt").read_text().strip() if (d / "original_name.txt").exists() else "document.pdf"
    base = Path(original_name).stem

    csv_path = d / "input_checklist.csv"
    preview = read_preview(csv_path) if csv_path.exists() else {"meta": [], "header": [], "rows": [], "total": 0}

    return render_template(
        "results.html",
        job_id=job_id,
        base=base,
        preview=preview,
        preview_rows=PREVIEW_ROWS,
    )


@app.route("/download/<job_id>")
def download(job_id):
    d = job_dir(job_id)
    path = d / "input_checklist.csv"
    if not path.exists():
        abort(404)

    original_name = (d / "original_name.txt").read_text().strip() if (d / "original_name.txt").exists() else "document.pdf"
    base = Path(original_name).stem
    return send_file(path, as_attachment=True, download_name=f"{base}_checklist.csv", mimetype="text/csv")


@app.errorhandler(413)
def too_large(e):
    flash(f"That file is over the {MAX_UPLOAD_MB} MB limit for this demo.")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
