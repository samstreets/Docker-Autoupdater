#!/usr/bin/env python3
"""
Docker Image Auto-Updater
Checks running containers for image updates and optionally restarts them.
"""

import os
import sys
import logging
from datetime import datetime

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
PRUNE_OLD_IMAGES = os.environ.get("PRUNE_OLD_IMAGES", "true").lower() == "true"
LABEL_ENABLE = os.environ.get("LABEL_ENABLE", "")
LABEL_KEY, LABEL_VALUE = LABEL_ENABLE.split("=", 1) if LABEL_ENABLE else ("", "")
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


def check_for_update(client: docker.DockerClient, image_name: str):
    """
    Pull the latest image and compare its ID to what's currently local.
    Returns (has_update: bool, old_image_id: str | None).
    """
    try:
        try:
            local_id = client.images.get(image_name).id
        except docker.errors.ImageNotFound:
            local_id = None

        pulled = client.images.pull(image_name)
        remote_id = pulled.id

        log.debug(f"  Local ID:  {local_id}")
        log.debug(f"  Remote ID: {remote_id}")

        return local_id != remote_id, local_id
    except Exception as e:
        log.error(f"  Failed to check/pull {image_name}: {e}")
        raise


def prune_image(client: docker.DockerClient, old_image_id: str):
    """Remove the old image, skipping if another container still uses it."""
    if not old_image_id:
        return
    if DRY_RUN:
        log.info(f"  [DRY RUN] Would remove old image {old_image_id[:12]}")
        return
    try:
        # Check if any container (running or stopped) still references this image
        all_containers = client.containers.list(all=True)
        still_in_use = any(c.image.id == old_image_id for c in all_containers)
        if still_in_use:
            log.info(f"  ⏭️  Old image {old_image_id[:12]} still in use by another container, skipping removal.")
            return
        client.images.remove(old_image_id, force=False)
        log.info(f"  🗑️  Removed old image {old_image_id[:12]}")
    except docker.errors.ImageNotFound:
        log.debug(f"  Old image {old_image_id[:12]} already gone.")
    except Exception as e:
        log.warning(f"  Could not remove old image {old_image_id[:12]}: {e}")


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
    """Recreate the container using the already-pulled latest image."""
    image_name = container.attrs["Config"]["Image"]
    container_name = container.name

    if DRY_RUN:
        log.info(f"  [DRY RUN] Would restart {container_name} with new {image_name}")
        return True

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
    log.info(f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE'} | Auto-update: {AUTO_UPDATE} | Prune old images: {PRUNE_OLD_IMAGES}")
    log.info("=" * 60)

    # Detect own container ID to avoid self-update
    own_id = os.environ.get("HOSTNAME", "")  # Docker sets HOSTNAME to the short container ID

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
        # Skip self to avoid stopping our own process
        if own_id and container.short_id == own_id[:12] or container.id.startswith(own_id):
            log.info(f"⏭️  Skipping self ({container.name})")
            continue

        image_name = container.attrs["Config"]["Image"]
        container_name = container.name
        log.info(f"\n🔍 Checking: {container_name} ({image_name})")

        try:
            has_update, old_image_id = check_for_update(client, image_name)
        except Exception:
            failed.append(container_name)
            continue

        if not has_update:
            log.info("  ✔ Already up to date.")
            skipped.append(container_name)
            continue

        log.info("  🔄 Update found!")

        if DRY_RUN:
            log.info("  [DRY RUN] Would recreate container.")
            if PRUNE_OLD_IMAGES:
                prune_image(client, old_image_id)
            skipped.append(container_name)
            continue

        if AUTO_UPDATE:
            success = update_container(client, container)
            if success:
                updated.append(container_name)
                if PRUNE_OLD_IMAGES:
                    prune_image(client, old_image_id)
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
    log.info(f"  Prune old images: {PRUNE_OLD_IMAGES}")
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
        )
        log.info(f"\n⏰ Next check in {CHECK_INTERVAL_MINUTES} minutes. Press Ctrl+C to stop.")
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            log.info("Shutting down.")


if __name__ == "__main__":
    main()
