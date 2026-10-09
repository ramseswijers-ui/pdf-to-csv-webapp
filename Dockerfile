FROM python:3.11-slim

WORKDIR /app

# poppler-utils (pdftoppm) renders pages for OCR; libgl1 + libglib2.0-0 are
# needed by OpenCV, which the offline OCR engine (RapidOCR) depends on
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# where uploaded PDFs / generated CSVs live while a job is being processed
RUN mkdir -p /app/jobs

EXPOSE 8000

CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8000", "--timeout", "300", "app:app"]
