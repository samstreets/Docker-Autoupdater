#!/usr/bin/env python3
"""
Docker Image Auto-Updater
Checks running containers for image updates and optionally restarts them.
"""

import os
import sys
import json
import logging
import subprocess
from datetime import datetime
from typing import Optional

import docker
import requests
from apscheduler.schedulers.blocking import BlockingScheduler

# --- Logging Setup ---
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("docker-autoupdater")

# --- Config ---
CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL_MINUTES", "60"))
AUTO_UPDATE = os.environ.get("AUTO_UPDATE", "true").lower() == "true"
LABEL_ENABLE = os.environ.get("LABEL_ENABLE", "")
LABEL_KEY, LABEL_VALUE = LABEL_ENABLE.split("=", 1)
NOTIFY_WEBHOOK = os.environ.get("NOTIFY_WEBHOOK", "")  # optional webhook URL
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"


def get_docker_client() -> docker.DockerClient:
    try:
        client = docker.from_env()
        client.ping()
        return client
    except Exception as e:
        log.error(f"Cannot connect to Docker daemon: {e}")
        sys.exit(1)


def get_image_digest(image_name: str) -> Optional[str]:
    """Pull manifest to get the remote digest without downloading the full image."""
    try:
        result = subprocess.run(
            ["docker", "manifest", "inspect", "--verbose", image_name],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            # Fallback: try docker pull --dry-run (newer Docker versions)
            return None
        data = json.loads(result.stdout)
        if isinstance(data, list):
            data = data[0]
        return data.get("Descriptor", {}).get("digest") or data.get("digest")
    except Exception as e:
        log.debug(f"manifest inspect failed for {image_name}: {e}")
        return None


def get_local_digest(client: docker.DockerClient, image_name: str) -> Optional[str]:
    """Get the repo digest of the locally cached image."""
    try:
        image = client.images.get(image_name)
        digests = image.attrs.get("RepoDigests", [])
        if digests:
            # RepoDigests look like "nginx@sha256:abc123"
            return digests[0].split("@")[-1]
        return None
    except docker.errors.ImageNotFound:
        return None
    except Exception as e:
        log.debug(f"Could not get local digest for {image_name}: {e}")
        return None


def send_notification(message: str):
    """Send a webhook notification (e.g. Discord, Slack, Gotify)."""
    if not NOTIFY_WEBHOOK:
        return
    try:
        payload = {"content": message, "text": message}
        requests.post(NOTIFY_WEBHOOK, json=payload, timeout=10)
        log.debug(f"Notification sent: {message}")
    except Exception as e:
        log.warning(f"Failed to send notification: {e}")


def update_container(client: docker.DockerClient, container) -> bool:
    """Pull latest image and recreate the container."""
    image_name = container.attrs["Config"]["Image"]
    container_name = container.name

    log.info(f"  Pulling latest image: {image_name}")
    if DRY_RUN:
        log.info(f"  [DRY RUN] Would pull {image_name} and restart {container_name}")
        return True

    try:
        client.images.pull(image_name)
    except Exception as e:
        log.error(f"  Failed to pull {image_name}: {e}")
        return False

    # Capture container config to recreate it
    attrs = container.attrs
    config = attrs["Config"]
    host_config = attrs["HostConfig"]

    log.info(f"  Stopping container: {container_name}")
    try:
        container.stop(timeout=30)
        container.remove()
    except Exception as e:
        log.error(f"  Failed to stop/remove {container_name}: {e}")
        return False

    log.info(f"  Recreating container: {container_name}")
    try:
        new_container = client.containers.run(
            image_name,
            name=container_name,
            detach=True,
            environment=config.get("Env"),
            ports=host_config.get("PortBindings"),
            volumes=host_config.get("Binds"),
            network_mode=host_config.get("NetworkMode"),
            restart_policy=host_config.get("RestartPolicy"),
            labels=config.get("Labels"),
        )
        log.info(f"  ✅ Container {container_name} recreated (ID: {new_container.short_id})")
        return True
    except Exception as e:
        log.error(f"  Failed to recreate {container_name}: {e}")
        return False


def check_and_update(client: docker.DockerClient):
    log.info("=" * 60)
    log.info(f"Starting update check — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE'} | Auto-update: {AUTO_UPDATE}")
    log.info("=" * 60)

    # Collect containers to check
    if LABEL_KEY and LABEL_VALUE:
        containers = client.containers.list(filters={"label": LABEL_ENABLE})
        log.info(f"Checking containers with label '{LABEL_ENABLE}': {len(containers)} found")
    else:
        containers = client.containers.list()
        log.info(f"Checking all running containers: {len(containers)} found")

    if not containers:
        log.info("No containers to check.")
        return

    updated, skipped, failed = [], [], []

    for container in containers:
        image_name = container.attrs["Config"]["Image"]
        container_name = container.name
        log.info(f"\n🔍 Checking: {container_name} ({image_name})")

        local_digest = get_local_digest(client, image_name)
        remote_digest = get_image_digest(image_name)

        if remote_digest is None:
            log.warning(f"  Could not fetch remote digest for {image_name}, trying pull-based check...")
            # Fallback: pull and compare IDs
            try:
                old_id = client.images.get(image_name).id
                if not DRY_RUN:
                    pulled = client.images.pull(image_name)
                    new_id = pulled.id
                    if old_id == new_id:
                        log.info("  ✔ Already up to date.")
                        skipped.append(container_name)
                        continue
                    else:
                        log.info("  🔄 Update found! (image ID changed)")
                        # Container will be recreated below
                else:
                    log.info(f"  [DRY RUN] Would check pull for {image_name}")
                    skipped.append(container_name)
                    continue
            except Exception as e:
                log.error(f"  Fallback check failed: {e}")
                failed.append(container_name)
                continue
        else:
            log.debug(f"  Local digest:  {local_digest}")
            log.debug(f"  Remote digest: {remote_digest}")
            if local_digest and local_digest == remote_digest:
                log.info("  ✔ Already up to date.")
                skipped.append(container_name)
                continue
            log.info("  🔄 Update available!")

        if AUTO_UPDATE:
            success = update_container(client, container)
            if success:
                updated.append(container_name)
                send_notification(f"✅ Updated Docker container `{container_name}` ({image_name})")
            else:
                failed.append(container_name)
                send_notification(f"❌ Failed to update `{container_name}` ({image_name})")
        else:
            log.info("  ⚠️  Update available but AUTO_UPDATE=false. Skipping restart.")
            send_notification(f"⚠️ Update available for `{container_name}` ({image_name}) — manual action required.")
            skipped.append(container_name)

    log.info("\n" + "=" * 60)
    log.info(f"Summary — Updated: {len(updated)} | Skipped: {len(skipped)} | Failed: {len(failed)}")
    if updated:
        log.info(f"  Updated: {', '.join(updated)}")
    if failed:
        log.info(f"  Failed:  {', '.join(failed)}")
    log.info("=" * 60)


def main():
    log.info("🐳 Docker Auto-Updater starting...")
    log.info(f"  Check interval:   {CHECK_INTERVAL_MINUTES} minutes")
    log.info(f"  Auto-update:      {AUTO_UPDATE}")
    log.info(f"  Label filter:     {LABEL_ENABLE or 'None (all containers)'}")
    log.info(f"  Dry run:          {DRY_RUN}")

    client = get_docker_client()

    # Run immediately on startup
    check_and_update(client)

    if CHECK_INTERVAL_MINUTES > 0:
        scheduler = BlockingScheduler()
        scheduler.add_job(
            check_and_update,
            "interval",
            args=[client],
            minutes=CHECK_INTERVAL_MINUTES,
            next_run_time=None,
        )
        log.info(f"\n⏰ Scheduled to run every {CHECK_INTERVAL_MINUTES} minutes. Press Ctrl+C to stop.")
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            log.info("Shutting down.")


if __name__ == "__main__":
    main()
