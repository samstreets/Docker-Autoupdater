FROM python:3.12-slim

LABEL org.opencontainers.image.title="Docker Auto-Updater"
LABEL org.opencontainers.image.description="Checks and updates Docker container images automatically"
LABEL org.opencontainers.image.source="https://github.com/samstreets/docker-autoupdater"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY updater.py .

# Default environment (all overridable)
ENV CHECK_INTERVAL_MINUTES=60 \
    AUTO_UPDATE=true \
    PRUNE_OLD_IMAGES=true \
    LABEL_ENABLE="" \
    DRY_RUN=false \
    LOG_LEVEL=INFO

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "updater.py"]
