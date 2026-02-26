FROM python:3.12-slim

LABEL org.opencontainers.image.title="Docker Auto-Updater"
LABEL org.opencontainers.image.description="Checks and updates Docker container images automatically"
LABEL org.opencontainers.image.source="https://github.com/youruser/docker-autoupdater"

# Install Docker CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
       https://download.docker.com/linux/debian bookworm stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY updater.py .

# Default environment (all overridable)
ENV CHECK_INTERVAL_MINUTES=60 \
    AUTO_UPDATE=true \
    LABEL_ENABLE=autoupdate=true \
    DRY_RUN=false \
    LOG_LEVEL=INFO

ENTRYPOINT ["python", "-u", "updater.py"]
