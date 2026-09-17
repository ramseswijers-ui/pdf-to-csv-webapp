# Technical PDF → CSV (web version)

A small Flask app: upload a technical PDF, get back three CSVs
(title block, dimension/tolerance callouts, full text) with in-browser
previews and downloads. Same extraction engine as the desktop tool
(`pdf_to_csv.py`), just wrapped in a web UI.

## Run it locally

```bash
pip install -r requirements.txt
python3 app.py
```

Then open http://localhost:5000

## Put it on a real website

### Option A: Your QNAP NAS (Docker) + your own domain

This repo includes a `Dockerfile` and `docker-compose.yml` made for this.

**1. Get the files onto the NAS**
- Open **File Station** on the NAS, create a folder e.g. `Container/pdf-to-csv-webapp`
- Upload this whole project folder into it (drag & drop, or via SMB share
  from your computer)

**2. Build & run with Container Station**
- Open **Container Station** → **Applications** (or **Create** on older
  versions) → **Create Application**
- Choose "Create from docker-compose.yml"
- Point it at the `docker-compose.yml` you just uploaded (or paste its
  contents in)
- Deploy. Container Station will build the image (a few minutes the
  first time) and start the container.

**3. Test it on your local network first**
- Find your NAS's local IP (Control Panel → Network)
- Visit `http://<nas-ip>:8000` from a browser on the same network
- Upload a PDF, confirm it works, *before* exposing it to the internet

**4. Point your domain at it**
You have two ways to do this on QNAP:

- **QNAP Reverse Proxy (recommended — gives you HTTPS)**
  Control Panel → **Network & File Services → Reverse Proxy** → Add a
  rule: source = your domain (e.g. `pdf.yourdomain.com`), destination =
  `localhost:8000`. QNAP can issue a free Let's Encrypt certificate for
  it in the same Control Panel (**Security → Certificate & Private Key**).
  Then in your domain's DNS settings, add an **A record** pointing
  `pdf.yourdomain.com` to your home's public IP address.

- **Plain port forward (simpler, no HTTPS)**
  On your router, forward external port 8000 (or 80) to the NAS's port
  8000. Point your domain's A record at your public IP. Works, but the
  site is unencrypted (http, not https) unless you add the reverse
  proxy step above.

**5. If your home IP changes (most home internet does)**
Use QNAP's built-in **myQNAPcloud** (Dynamic DNS) or set up DDNS through
your domain registrar/Cloudflare, so your domain keeps pointing at your
NAS even after your ISP changes your IP.

**Updating later:** after editing files, in Container Station just
**Recreate** / **Rebuild** the application — it rebuilds from the same
`docker-compose.yml`.

### Option B: Render.com (no NAS/networking needed)
1. Push this folder to a GitHub repo.
2. On Render: **New → Web Service**, connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app`
5. Deploy. Render gives you a `https://your-app.onrender.com` URL.

### Option C: Railway.app
1. Push to GitHub, then **New Project → Deploy from GitHub repo** on Railway.
2. Railway auto-detects Python. Set the start command to `gunicorn app:app`
   if it doesn't pick one automatically.
3. Deploy — Railway gives you a public URL.

### Option D: Your own server / VPS
```bash
pip install -r requirements.txt
gunicorn -w 2 -b 0.0.0.0:8000 app:app
```
Put nginx or Caddy in front of it for HTTPS.

**Do not** use `python3 app.py` (the Flask dev server) in production —
that's fine for testing locally, but for a real deployment always run it
through `gunicorn` (included in requirements.txt) as shown above.

## Before you make it public

- **Change `app.secret_key`** in `app.py` to a real random value — it's
  currently a placeholder.
- **Upload size limit**: capped at 25 MB by default (`MAX_UPLOAD_MB` in
  `app.py`). Adjust if needed.
- **Job cleanup**: each upload gets its own folder under `jobs/`, which
  isn't automatically deleted. For a public site, add a simple cron job
  or scheduled task that deletes folders in `jobs/` older than a day, e.g.:

  ```bash
  find jobs/ -maxdepth 1 -mtime +1 -exec rm -rf {} \;
  ```

- **Privacy**: uploaded PDFs and generated CSVs sit unencrypted in the
  `jobs/` folder while a job exists. Don't point this at sensitive
  drawings unless the server itself is private/trusted, or you add
  encryption/access control.

## Output format

One CSV per upload, laid out like Form 3 (First Article Inspection -
Characteristic Accountability): a part-identification block, then one
row per dimension/tolerance found on the drawing, with blank
Results/Designed Tooling/Non-Conf. Number/Deviation/Error columns ready
to fill in during a physical check. The last column keeps the original
extracted text for every row, so you can always see exactly what the
tool read off the page.

## OCR fallback (for drawings with no text layer)

Some CAD exports flatten all drawing text into vector outlines instead
of real text characters - pdfplumber then finds zero words, since
there's nothing to read as text. When that happens, the tool
automatically falls back to OCR (rasterizing the page and running
Tesseract) instead of producing an empty result.

This works, but is noticeably less reliable than reading a real text
layer: line-art and hatching sometimes get misread as stray characters,
and multi-part dimensions (like `14 ±0,2`) can end up split across two
rows. Every extracted row still keeps its raw OCR text in the last
column, so nothing is silently dropped - just budget more time for
manual review on these PDFs specifically. If you have any control over
how a drawing is exported, exporting with an embedded text layer (rather
than "flatten text to outlines" or similar) will give much cleaner
results here.

## Files

- `app.py` — Flask routes (upload, process, preview, download, zip).
- `pdf_to_csv.py` — the extraction engine (shared with the desktop GUI/CLI
  version — safe to update in one place and copy to both).
- `templates/index.html`, `templates/results.html` — pages.
- `static/style.css` — styling.
- `requirements.txt` — Python dependencies.
