#!/usr/bin/env python3
"""
Docker Image Auto-Updater
Checks running containers for image updates and optionally restarts them.
"""

import os
import re
import sys
import time
import signal
import logging
import threading
from datetime import datetime

import docker
import requests

# --- Logging Setup ---
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL) if LOG_LEVEL in VALID_LOG_LEVELS else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("docker-autoupdater")
if LOG_LEVEL not in VALID_LOG_LEVELS:
    log.warning(f"Invalid LOG_LEVEL {LOG_LEVEL!r} (expected one of {', '.join(VALID_LOG_LEVELS)}); using INFO")


# --- Config ---
def env_bool(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        log.error(f"Invalid value for {name}: {raw!r} (expected an integer)")
        sys.exit(2)


CHECK_INTERVAL_MINUTES = env_int("CHECK_INTERVAL_MINUTES", 60)
AUTO_UPDATE = env_bool("AUTO_UPDATE", "true")
PRUNE_OLD_IMAGES = env_bool("PRUNE_OLD_IMAGES", "true")
LABEL_ENABLE = os.environ.get("LABEL_ENABLE", "").strip()  # "key" or "key=value"
NOTIFY_WEBHOOK = os.environ.get("NOTIFY_WEBHOOK", "")  # optional webhook URL
DRY_RUN = env_bool("DRY_RUN", "false")
EXCLUDE_IMAGES_RAW = os.environ.get("EXCLUDE_IMAGES", "")
EXCLUDE_IMAGES = [img.strip() for img in EXCLUDE_IMAGES_RAW.split(",") if img.strip()]
# How long a recreated container must stay running before the old one is deleted.
START_GRACE_SECONDS = env_int("START_GRACE_SECONDS", 10)
SWARM_SERVICE_LABEL = "com.docker.swarm.service.id"

# Set by SIGTERM/SIGINT; checked between containers so an update is never cut off mid-way.
STOP = threading.Event()

# container ID -> remote image reference we already sent an "update available" notification for
NOTIFIED = {}


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


def local_repo_digests(container):
    """Registry digests of the image the container runs ([] = never pulled, None = image gone)."""
    try:
        return container.image.attrs.get("RepoDigests") or []
    except docker.errors.ImageNotFound:
        return None


def check_for_update(client: docker.DockerClient, container):
    """
    Compare the image the container is actually running with the latest from the registry.
    Returns (has_update: bool, running_image_id: str, remote_ref: str).
    Nothing is pulled unless an update will actually be applied (not in dry-run mode and
    not with AUTO_UPDATE=false), so local image state is otherwise left untouched.
    """
    image_name = container.attrs["Config"]["Image"]
    running_id = container.attrs["Image"]
    try:
        if DRY_RUN or not AUTO_UPDATE:
            digest = client.images.get_registry_data(image_name).id
            local_digests = local_repo_digests(container) or []
            return not any(d.endswith("@" + digest) for d in local_digests), running_id, digest

        remote_id = client.images.pull(image_name).id
        log.debug(f"  Running ID: {running_id}")
        log.debug(f"  Remote ID:  {remote_id}")
        return running_id != remote_id, running_id, remote_id
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
        # attrs["Image"] rather than c.image, which raises if a container's image was deleted
        still_in_use = any(c.attrs.get("Image") == old_image_id for c in all_containers)
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
        # "content" = Discord, "text" = Slack, "message"/"title" = Gotify
        payload = {"content": message, "text": message, "message": message, "title": "Docker Auto-Updater"}
        requests.post(NOTIFY_WEBHOOK, json=payload, timeout=10).raise_for_status()
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


def resolve_container_ref(client: docker.DockerClient, ref: str, replaced: dict) -> str:
    """
    Turn a reference to another container (ID or name) into its name. Docker stores
    `container:<id>` references, which die when that container is recreated; the name
    survives. `replaced` maps IDs of containers recreated this cycle to their names.
    """
    if ref in replaced:
        return replaced[ref]
    try:
        return client.containers.get(ref).name
    except Exception:
        return ref


def build_container_spec(client: docker.DockerClient, container, replaced=None) -> dict:
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

    replaced = replaced or {}
    network_mode = host_config.get("NetworkMode") or "default"
    joins_container = network_mode.startswith("container:")
    shares_network = network_mode == "host" or joins_container
    if joins_container:
        parent = resolve_container_ref(client, network_mode.split(":", 1)[1], replaced)
        host_config["NetworkMode"] = f"container:{parent}"
    if host_config.get("VolumesFrom"):
        host_config["VolumesFrom"] = [
            ":".join([resolve_container_ref(client, ref.split(":", 1)[0], replaced)] + ref.split(":", 1)[1:])
            for ref in host_config["VolumesFrom"]
        ]

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

    # Ports that only come from the image's EXPOSE are left to the new image. Docker also
    # rejects explicitly exposed ports on containers that join another container's network.
    image_ports = set(image_config.get("ExposedPorts") or {})
    published = set(host_config.get("PortBindings") or {})
    exposed = [] if joins_container else [
        tuple(p.split("/", 1)) for p in config.get("ExposedPorts") or {}
        if p not in image_ports or p in published
    ]
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
    """Roll back: give the original container its name back and make sure it is running."""
    try:
        container.reload()
        if container.name != name:
            container.rename(name)
        if container.status != "running":
            container.start()
        log.warning(f"  ↩️  Rolled back: {name} is running on its previous image.")
    except docker.errors.NotFound:
        log.error(f"  Rollback of {name} failed: the old container no longer exists "
                  f"(started with --rm?). It has to be recreated manually.")
    except Exception as e:
        log.error(f"  Rollback of {name} failed, manual intervention needed "
                  f"(old container is '{container.name}'): {e}")


def wait_until_stable(client: docker.DockerClient, container_id: str):
    """Raise unless the container is still running after START_GRACE_SECONDS."""
    deadline = time.monotonic() + START_GRACE_SECONDS
    while True:
        state = client.api.inspect_container(container_id)["State"]
        if not state.get("Running") or state.get("Restarting"):
            raise RuntimeError(
                f"new container did not stay running "
                f"(status: {state.get('Status')}, exit code: {state.get('ExitCode')})"
            )
        if time.monotonic() >= deadline:
            return
        time.sleep(1)


def update_container(client: docker.DockerClient, container, replaced=None) -> bool:
    """
    Recreate the container on the already-pulled image. The replacement is created
    before the old container is stopped, and the old one is kept (renamed, stopped)
    until the new one has stayed up for START_GRACE_SECONDS, so a failure can be
    rolled back.
    """
    image_name = container.attrs["Config"]["Image"]
    container_name = container.name

    if DRY_RUN:
        log.info(f"  [DRY RUN] Would restart {container_name} with new {image_name}")
        return True

    try:
        spec = build_container_spec(client, container, replaced)
    except Exception as e:
        log.error(f"  Could not read configuration of {container_name}, leaving it untouched: {e}")
        return False

    # --rm containers are deleted by Docker as soon as they stop, so they cannot be
    # rolled back after that point; everything that can fail early is done first.
    auto_remove = bool(container.attrs["HostConfig"].get("AutoRemove"))
    stop_timeout = container.attrs["Config"].get("StopTimeout") or 30
    backup_name = f"{container_name}_old_{int(time.time())}"
    new_id = None
    try:
        container.rename(backup_name)
        log.info(f"  Creating replacement for: {container_name}")
        new_id = client.api.create_container(**spec["create"])["Id"]
        for network, endpoint_config in spec["extra_networks"].items():
            client.api.connect_container_to_network(
                new_id, network,
                aliases=endpoint_config.get("Aliases"),
                ipv4_address=(endpoint_config.get("IPAMConfig") or {}).get("IPv4Address"),
                ipv6_address=(endpoint_config.get("IPAMConfig") or {}).get("IPv6Address"),
            )
        log.info(f"  Stopping container: {container_name}")
        container.stop(timeout=stop_timeout)
        log.info(f"  Starting new container: {container_name}")
        client.api.start(new_id)
        wait_until_stable(client, new_id)
    except Exception as e:
        log.error(f"  Failed to recreate {container_name}: {e}")
        if new_id:
            try:
                client.api.remove_container(new_id, force=True)
            except docker.errors.NotFound:
                pass
            except Exception as cleanup_error:
                log.warning(f"  Could not remove partially created container: {cleanup_error}")
        restore_container(container, container_name)
        return False

    if not auto_remove:
        try:
            container.remove()
        except docker.errors.NotFound:
            pass
        except Exception as e:
            log.warning(f"  Updated, but could not remove old container '{backup_name}': {e}")
    log.info(f"  ✅ Container {container_name} recreated (ID: {new_id[:12]})")
    return True


def find_dependents(client: docker.DockerClient, container, own_id: str) -> list:
    """Running containers that share this container's network namespace (`--network container:X`)."""
    mode = f"container:{container.id}"
    return [
        c for c in client.containers.list()
        if c.attrs["HostConfig"].get("NetworkMode") == mode and not (own_id and c.id.startswith(own_id))
    ]


def started_at(container):
    return (container.attrs.get("State") or {}).get("StartedAt")


def check_and_update(client: docker.DockerClient):
    log.info("=" * 60)
    log.info(f"Starting update check — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    mode = "DRY RUN" if DRY_RUN else "LIVE"
    log.info(f"Mode: {mode} | Auto-update: {AUTO_UPDATE} | Prune old images: {PRUNE_OLD_IMAGES}")
    log.info("=" * 60)

    # Detect own container ID to avoid self-update
    own_id = get_own_container_id()

    if LABEL_ENABLE:
        containers = client.containers.list(filters={"label": LABEL_ENABLE})
        log.info(f"Checking containers with label '{LABEL_ENABLE}': {len(containers)} found")
    else:
        containers = client.containers.list()
        log.info(f"Checking all running containers: {len(containers)} found")

    if not containers:
        log.info("No containers to check.")
        return

    updated, skipped, failed = [], [], []
    replaced = {}  # ID -> name of containers recreated during this cycle

    for container in containers:
        if STOP.is_set():
            log.info("Shutdown requested, stopping before the next container.")
            break

        # Skip self to avoid stopping our own process
        if own_id and container.id.startswith(own_id):
            log.info(f"⏭️  Skipping self ({container.name})")
            continue

        if container.id in replaced:
            log.info(f"⏭️  Skipping {replaced[container.id]}: already recreated during this check")
            continue

        image_name = container.attrs["Config"]["Image"]
        container_name = container.name

        if SWARM_SERVICE_LABEL in (container.attrs["Config"].get("Labels") or {}):
            log.info(f"⏭️  Skipping Swarm service task: {container_name} (update the service instead)")
            skipped.append(container_name)
            continue

        if is_pinned_image(image_name):
            log.info(f"⏭️  Skipping pinned image: {container_name} ({image_name})")
            skipped.append(container_name)
            continue

        # Skip excluded images
        if is_image_excluded(image_name):
            log.info(f"⏭️  Skipping excluded image: {container_name} ({image_name})")
            skipped.append(container_name)
            continue

        if local_repo_digests(container) == []:
            log.info(f"⏭️  Skipping locally built image: {container_name} ({image_name})")
            skipped.append(container_name)
            continue

        log.info(f"\n🔍 Checking: {container_name} ({image_name})")

        try:
            has_update, old_image_id, remote_ref = check_for_update(client, container)
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

        if not AUTO_UPDATE:
            log.info("  ⚠️  Update available but AUTO_UPDATE=false. Skipping restart.")
            if NOTIFIED.get(container.id) != remote_ref:  # once per new image, not every interval
                NOTIFIED[container.id] = remote_ref
                send_notification(
                    f"⚠️ Update available for `{container_name}` ({image_name}) — manual action required.")
            skipped.append(container_name)
            continue

        dependents = find_dependents(client, container, own_id)
        before = started_at(container)
        if update_container(client, container, replaced):
            replaced[container.id] = container_name
            updated.append(container_name)
            if PRUNE_OLD_IMAGES:
                prune_image(client, old_image_id)
            send_notification(f"✅ Updated Docker container `{container_name}` ({image_name})")
            # Containers sharing the old network namespace are cut off; recreate them on the new one
            for dependent in dependents:
                dependent_name = dependent.name
                log.info(f"  🔗 Recreating {dependent_name}, which uses the network of {container_name}")
                if update_container(client, dependent, replaced):
                    replaced[dependent.id] = dependent_name
                else:
                    failed.append(dependent_name)
                    send_notification(f"❌ Failed to recreate `{dependent_name}` after updating `{container_name}`")
        else:
            failed.append(container_name)
            send_notification(f"❌ Failed to update `{container_name}` ({image_name})")
            if started_at(container) != before:  # it was stopped and rolled back
                for dependent in dependents:
                    try:
                        log.info(f"  🔗 Restarting {dependent.name}, which uses the network of {container_name}")
                        dependent.restart()
                    except Exception as e:
                        log.warning(f"  Could not restart {dependent.name}: {e}")

    log.info("\n" + "=" * 60)
    log.info(f"Summary — Updated: {len(updated)} | Skipped: {len(skipped)} | Failed: {len(failed)}")
    if updated:
        log.info(f"  Updated: {', '.join(updated)}")
    if failed:
        log.info(f"  Failed:  {', '.join(failed)}")
    log.info("=" * 60)


def handle_signal(signum, frame):
    if STOP.is_set():
        log.warning("Second signal received, exiting immediately.")
        raise SystemExit(1)
    log.info(f"Received {signal.Signals(signum).name}, finishing the current step then shutting down.")
    STOP.set()


def main():
    log.info("🐳 Docker Auto-Updater starting...")
    log.info(f"  Check interval:   {CHECK_INTERVAL_MINUTES} minutes")
    log.info(f"  Auto-update:      {AUTO_UPDATE}")
    log.info(f"  Prune old images: {PRUNE_OLD_IMAGES}")
    log.info(f"  Label filter:     {LABEL_ENABLE or 'None (all containers)'}")
    log.info(f"  Dry run:          {DRY_RUN}")
    log.info(f"  Start grace:      {START_GRACE_SECONDS} seconds")
    if EXCLUDE_IMAGES:
        log.info(f"  Excluded images:  {', '.join(EXCLUDE_IMAGES)}")

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    client = get_docker_client()

    while True:
        try:
            check_and_update(client)
        except Exception:
            log.exception("Update check failed")  # keep running; try again next interval

        if CHECK_INTERVAL_MINUTES <= 0:
            break
        log.info(f"\n⏰ Next check in {CHECK_INTERVAL_MINUTES} minutes.")
        if STOP.wait(CHECK_INTERVAL_MINUTES * 60):
            break
    log.info("Shutting down.")


if __name__ == "__main__":
    main()
