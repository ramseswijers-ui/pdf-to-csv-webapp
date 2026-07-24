FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# where uploaded PDFs / generated CSVs live while a job is being processed
RUN mkdir -p /app/jobs

EXPOSE 8000

CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8000", "--timeout", "120", "app:app"]
