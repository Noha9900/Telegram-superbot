FROM python:3.11-slim

# ffmpeg is required by utils/ffmpeg_utils.py for screenshots & thumbnails
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p downloads

# Runs as a non-root user for VPS hardening
RUN useradd -m botuser && chown -R botuser:botuser /app
USER botuser

CMD ["python3", "bot.py"]
