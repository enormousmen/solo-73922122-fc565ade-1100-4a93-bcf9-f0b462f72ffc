FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV DATA_DIR=/data \
    TRANSCODE_SLOTS=2 \
    SLOW_AFTER_SECONDS=2.0 \
    SLOW_READRATE=0.4

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# In-memory scheduler state requires exactly one worker process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
