FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -U -r requirements.txt
RUN yt-dlp -U || true

COPY app.py .
COPY templates/ templates/
RUN mkdir -p downloads downloads/clients

ENV PORT=10000
EXPOSE 10000

CMD gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 600 app:app
