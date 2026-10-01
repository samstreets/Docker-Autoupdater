#!/usr/bin/env python3
"""
Docker Image Auto-Updater
Checks running containers for image updates and optionally restarts them.
"""

import os
import re
import sys
import time
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
EXCLUDE_IMAGES_RAW = os.environ.get("EXCLUDE_IMAGES", "")
EXCLUDE_IMAGES = [img.strip() for img in EXCLUDE_IMAGES_RAW.split(",") if img.strip()]


def get_docker_client() -> docker.DockerClient:
    try:
        client = docker.from_env()
        client.ping()
        return client
    except Exception as e:
        log.error(f"Cannot connect to Docker daemon: {e}")
        sys.exit(1)


def is_pinned_image(image_name: str) -> bool:
    """True for references that can never change (digest-pinned or bare image IDs)."""
    return image_name.startswith("sha256:") or "@sha256:" in image_name


def registry_has_update(client: docker.DockerClient, image_name: str, container) -> bool:
    """Compare the registry digest with the running image's digests without pulling."""
    digest = client.images.get_registry_data(image_name).id
    local_digests = container.image.attrs.get("RepoDigests") or []
    if not local_digests:
        log.warning("  No local repo digest (locally built image?); cannot compare without pulling.")
        return False
    return not any(d.endswith("@" + digest) for d in local_digests)


def check_for_update(client: docker.DockerClient, container):
    """
    Compare the image the container is actually running with the latest from the registry.
    Returns (has_update: bool, running_image_id: str).
    In dry-run mode nothing is pulled, so local image state is left untouched.
    """
    image_name = container.attrs["Config"]["Image"]
    running_id = container.attrs["Image"]
    try:
        if DRY_RUN:
            return registry_has_update(client, image_name, container), running_id

        remote_id = client.images.pull(image_name).id
        log.debug(f"  Running ID: {running_id}")
        log.debug(f"  Remote ID:  {remote_id}")
        return running_id != remote_id, running_id
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


def split_image_ref(ref: str):
    """Split an image reference into (normalised repository, tag or None)."""
    ref = ref.split("@", 1)[0]
    tag = None
    # Only a colon in the last path segment is a tag (earlier ones are registry ports)
    if ":" in ref.rsplit("/", 1)[-1]:
        ref, tag = ref.rsplit(":", 1)
    ref = ref.lower()
    for prefix in ("index.docker.io/", "docker.io/"):
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
    if ref.startswith("library/"):
        ref = ref[len("library/"):]
    return ref, tag


def is_image_excluded(image_name: str) -> bool:
    """
    True if the image matches an EXCLUDE_IMAGES entry. An entry without a tag
    excludes every tag of that repository; an entry with a tag matches only that tag.
    """
    repo, tag = split_image_ref(image_name)
    for excluded in EXCLUDE_IMAGES:
        ex_repo, ex_tag = split_image_ref(excluded)
        if repo == ex_repo and (ex_tag is None or ex_tag == (tag or "latest")):
            return True
    return False


def get_own_container_id() -> str:
    """Best-effort ID of the container this process runs in ('' if unknown)."""
    for path, pattern in (
        ("/proc/self/mountinfo", r"/containers/([0-9a-f]{64})/"),
        ("/proc/self/cgroup", r"([0-9a-f]{64})"),
    ):
        try:
            with open(path) as f:
                match = re.search(pattern, f.read())
        except OSError:
            continue
        if match:
            return match.group(1)
    hostname = os.environ.get("HOSTNAME", "")  # Docker sets this to the short ID by default
    return hostname if re.fullmatch(r"[0-9a-f]{12,64}", hostname) else ""


def build_container_spec(client: docker.DockerClient, container) -> dict:
    """
    Translate an inspected container into kwargs for the low-level create_container call,
    keeping its full configuration. Values that merely mirror the old image's defaults
    (env, labels, cmd, entrypoint, ...) are dropped so the new image's defaults apply.
    """
    attrs = container.attrs
    config = attrs["Config"]
    host_config = dict(attrs["HostConfig"])
    try:
        image_config = container.image.attrs.get("Config") or {}
    except docker.errors.ImageNotFound:
        image_config = {}

    network_mode = host_config.get("NetworkMode") or "default"
    shares_network = network_mode == "host" or network_mode.startswith("container:")

    def custom(key):
        value = config.get(key)
        return None if value == image_config.get(key) else value

    image_env = set(image_config.get("Env") or [])
    image_labels = image_config.get("Labels") or {}
    labels = {k: v for k, v in (config.get("Labels") or {}).items() if image_labels.get(k) != v}

    # Preserve anonymous volumes (and their data) by re-attaching them by name
    if not host_config.get("VolumesFrom"):
        binds = list(host_config.get("Binds") or [])
        covered = {b.split(":")[1] for b in binds if ":" in b}
        covered |= {m.get("Target") for m in host_config.get("Mounts") or []}
        for mount in attrs.get("Mounts") or []:
            if mount.get("Type") == "volume" and mount.get("Name") and mount["Destination"] not in covered:
                binds.append(f"{mount['Name']}:{mount['Destination']}{'' if mount.get('RW', True) else ':ro'}")
        host_config["Binds"] = binds or None

    networks = {} if shares_network else dict(attrs["NetworkSettings"].get("Networks") or {})
    primary = network_mode if network_mode in networks else None

    def endpoint(net):
        ipam = net.get("IPAMConfig") or {}
        aliases = [a for a in net.get("Aliases") or [] if a != container.short_id]
        return client.api.create_endpoint_config(
            aliases=aliases or None,
            ipv4_address=ipam.get("IPv4Address"),
            ipv6_address=ipam.get("IPv6Address"),
        )

    exposed = [tuple(p.split("/", 1)) for p in config.get("ExposedPorts") or {}]
    hostname = config.get("Hostname")
    return {
        "create": dict(
            image=config["Image"],
            name=container.name,
            command=custom("Cmd"),
            entrypoint=custom("Entrypoint"),
            environment=[e for e in config.get("Env") or [] if e not in image_env] or None,
            labels=labels or None,
            ports=[(int(port), proto) for port, proto in exposed] or None,
            hostname=None if shares_network or hostname == container.short_id else hostname,
            domainname=config.get("Domainname") or None,
            user=config.get("User") or None,
            working_dir=custom("WorkingDir"),
            tty=config.get("Tty", False),
            stdin_open=config.get("OpenStdin", False),
            stop_signal=config.get("StopSignal"),
            stop_timeout=config.get("StopTimeout"),
            healthcheck=custom("Healthcheck"),
            host_config=host_config,
            networking_config=(
                client.api.create_networking_config({primary: endpoint(networks[primary])}) if primary else None
            ),
        ),
        "extra_networks": {n: endpoint(c) for n, c in networks.items() if n != primary and n != "bridge"},
    }


def restore_container(container, name: str):
    """Roll back: give the original container its name back and start it again."""
    try:
        container.reload()
        if container.name != name:
            container.rename(name)
        container.start()
        log.warning(f"  ↩️  Rolled back: {name} restarted on its previous image.")
    except Exception as e:
        log.error(f"  Rollback of {name} failed, manual intervention needed "
                  f"(old container is '{container.name}'): {e}")


def update_container(client: docker.DockerClient, container) -> bool:
    """
    Recreate the container on the already-pulled image. The old container is kept
    (renamed, stopped) until the new one has started, so a failure can be rolled back.
    """
    image_name = container.attrs["Config"]["Image"]
    container_name = container.name

    if DRY_RUN:
        log.info(f"  [DRY RUN] Would restart {container_name} with new {image_name}")
        return True

    try:
        spec = build_container_spec(client, container)
    except Exception as e:
        log.error(f"  Could not read configuration of {container_name}, leaving it untouched: {e}")
        return False

    backup_name = f"{container_name}_old_{int(time.time())}"
    log.info(f"  Stopping container: {container_name}")
    try:
        container.stop(timeout=30)
        container.rename(backup_name)
    except Exception as e:
        log.error(f"  Failed to stop/rename {container_name}: {e}")
        restore_container(container, container_name)
        return False

    log.info(f"  Recreating container: {container_name}")
    new_id = None
    try:
        new_id = client.api.create_container(**spec["create"])["Id"]
        for network, endpoint_config in spec["extra_networks"].items():
            client.api.connect_container_to_network(
                new_id, network,
                aliases=endpoint_config.get("Aliases"),
                ipv4_address=(endpoint_config.get("IPAMConfig") or {}).get("IPv4Address"),
                ipv6_address=(endpoint_config.get("IPAMConfig") or {}).get("IPv6Address"),
            )
        client.api.start(new_id)
    except Exception as e:
        log.error(f"  Failed to recreate {container_name}: {e}")
        if new_id:
            try:
                client.api.remove_container(new_id, force=True)
            except Exception as cleanup_error:
                log.warning(f"  Could not remove partially created container: {cleanup_error}")
        restore_container(container, container_name)
        return False

    try:
        container.remove()
    except Exception as e:
        log.warning(f"  Updated, but could not remove old container '{backup_name}': {e}")
    log.info(f"  ✅ Container {container_name} recreated (ID: {new_id[:12]})")
    return True


def check_and_update(client: docker.DockerClient):
    log.info("=" * 60)
    log.info(f"Starting update check — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    mode = "DRY RUN" if DRY_RUN else "LIVE"
    log.info(f"Mode: {mode} | Auto-update: {AUTO_UPDATE} | Prune old images: {PRUNE_OLD_IMAGES}")
    log.info("=" * 60)

    # Detect own container ID to avoid self-update
    own_id = get_own_container_id()

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
        if own_id and container.id.startswith(own_id):
            log.info(f"⏭️  Skipping self ({container.name})")
            continue

        image_name = container.attrs["Config"]["Image"]
        container_name = container.name

        if is_pinned_image(image_name):
            log.info(f"⏭️  Skipping pinned image: {container_name} ({image_name})")
            skipped.append(container_name)
            continue

        # Skip excluded images
        if is_image_excluded(image_name):
            log.info(f"⏭️  Skipping excluded image: {container_name} ({image_name})")
            skipped.append(container_name)
            continue

        log.info(f"\n🔍 Checking: {container_name} ({image_name})")

        try:
            has_update, old_image_id = check_for_update(client, container)
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
    if EXCLUDE_IMAGES:
        log.info(f"  Excluded images:  {', '.join(EXCLUDE_IMAGES)}")

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
