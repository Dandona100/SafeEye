FROM python:3.11-slim

LABEL org.opencontainers.image.source="https://github.com/Dandona100/SafeEye"
LABEL org.opencontainers.image.description="SafeEye Content Safety Scanner"

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git libgl1 libglib2.0-0 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir faiss-cpu || true

COPY nsfw_scanner/ /app/nsfw_scanner/
COPY nsfw_domains.txt /app/nsfw_domains.txt

RUN mkdir -p /app/data /tmp/nsfw_scans \
    && git config --global --add safe.directory /app

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:1985/health || exit 1

CMD ["python", "-m", "nsfw_scanner"]
