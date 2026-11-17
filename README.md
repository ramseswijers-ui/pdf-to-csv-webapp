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

## Notation in the checklist

- **Requirement** holds symbol + value + multiplicity, without tolerance text,
  e.g. `ø3,3 (4x)`; the tolerances are in **Upper Tol** (always with `+`) and
  **Lower Tol** (`-x`, or `0`).
- Tolerance frames get their symbol and a value: `POS ø0,2 A` with upper `+0,2`
  and lower `0` (recognised: POS, ◎, ○, ▱, ⌓; others are marked "symbol not
  recognised" in the last column). Frames drawn vertically are recognised
  too, and a `(4x)` next to a frame is added to it.
- Boxed dimensions get `(TED)`, also when the box is drawn at an angle.
- A stacked tolerance whose lower value OCR did not read (`+0,3` over `0`) is
  filled in as `0` and marked "lower tol assumed 0 - check" in the last column.
- `ø` is restored where OCR reads it as `Q`; decimals use a comma.
- General info (part number, description, material, surface/heat/after
  treatment, surface roughness, notes such as `A = ... m²`) is listed at
  the top and not numbered. Title block and border grid markers are not numbered.
- The Excel file stores everything as left-aligned text; a CSV opened in
  Excel right-aligns numbers.

## Balloons on the original drawing

Besides the CSV, you get a copy of the original PDF with a red numbered
balloon next to every characteristic. The number is the same as
"5. Char No." in the CSV, so you can find any row on the drawing and
vice versa. Balloons try to avoid covering other text; where a balloon
is some distance from its value, a thin leader line points to it.
Title-block items (dates, sheet numbers, tolerance tables), revision-table
rows and the border zone markers (1-2 digit numbers and letters) are not numbered.

## OCR fallback (for drawings with no text layer)

Some CAD exports flatten all drawing text into vector outlines instead
of real text characters, so there is nothing to read directly. When that
happens the tool automatically switches to OCR, fully offline, using
RapidOCR (PaddleOCR models running on the CPU - no API key, no credits).

- The page is read upright, rotated +/-90 degrees (vertical text) and at
  +/-30, 45 and 60 degrees (slanted dimensions). Vertical text is also read
  in small tiles, which finds it much better than a whole-page read.
- Where the OCR sees a lone diameter symbol but not the number next to it,
  that spot is read again, zoomed in.
- The best reading per place wins; small symbols OCR often drops (degree,
  plus/minus) are kept when any pass saw them.
- Zone references (e.g. `4-C`) come from the sheet's own border markers,
  so they follow each drawing's own grid.
- The page is also read in overlapping tiles, which finds small
  dimensions the full-page read misses. Set the environment variable
  `OCR_TILES=0` to skip this if speed matters more than completeness.
- Conversion runs in the background and the page refreshes itself, so
  slow OCR on a NAS CPU doesn't hit proxy/Cloudflare timeouts.

OCR is still less reliable than a real text layer: stacked tolerances and
GD&T frames are sometimes split or misread. The last column of every row
keeps the raw text that was read, so you can always see what was found.

## Files

- `app.py` — Flask routes (upload, process, preview, download, zip).
- `pdf_to_csv.py` — the extraction engine (shared with the desktop GUI/CLI
  version — safe to update in one place and copy to both).
- `templates/index.html`, `templates/results.html` — pages.
- `static/style.css` — styling.
- `requirements.txt` — Python dependencies.

## Known limits

- Datum triangles on a dimension (such as `ref A`) are not text and are not read.
- Slanted text and very small print can still be misread by the OCR; the last
  column keeps the raw text so you can see what was found.
- On a NAS CPU a large A2 drawing can take a few minutes with OCR; the page
  refreshes itself while it works.
