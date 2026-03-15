# 🐳 Docker Auto-Updater

A lightweight Docker container that monitors your running containers for image updates and automatically pulls and recreates them when a newer image is available on the registry.

**Docker Hub:** [`samuelstreets/docker-updater`](https://hub.docker.com/r/samuelstreets/docker-updater)

---

## Features

- ✅ **Automatic updates** — pulls new images and recreates containers in-place
- 🗑️ **Old image cleanup** — automatically removes superseded images after a successful update
- 🏷️ **Label-based opt-in** — optionally restrict to containers with a specific label
- ⏰ **Scheduled checks** — runs on a configurable interval (default: every 60 minutes)
- 🔔 **Webhook notifications** — Discord, Slack, Gotify, or any HTTP endpoint
- 🧪 **Dry-run mode** — preview what *would* be updated without making changes
- 📋 **Detailed logging** — clear, timestamped output of every check

---

## Quick Start

### 1. Using Docker Compose (recommended)

```yaml
services:
  docker-autoupdater:
    image: samuelstreets/docker-updater:latest
    container_name: docker-autoupdater
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    environment:
      CHECK_INTERVAL_MINUTES: "60"
      AUTO_UPDATE: "true"
      PRUNE_OLD_IMAGES: "true"
```

By default all running containers are checked. To restrict to specific containers, add the `LABEL_ENABLE` option (see [Configuration](#configuration)) and label your services:

```yaml
  nginx:
    image: nginx:latest
    labels:
      autoupdate: "true"
```

### 2. Using Docker Run

```bash
docker run -d \
  --name docker-autoupdater \
  --restart unless-stopped \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -e CHECK_INTERVAL_MINUTES=60 \
  -e AUTO_UPDATE=true \
  -e PRUNE_OLD_IMAGES=true \
  samuelstreets/docker-updater:latest
```

### 3. Build Locally

```bash
git clone https://github.com/samuelstreets/docker-updater
cd docker-updater
docker build -t docker-updater .
docker run -d \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  docker-updater
```

---

## Configuration

All configuration is done via environment variables:

| Variable | Default | Description |
|---|---|---|
| `CHECK_INTERVAL_MINUTES` | `60` | How often to check for updates. Set to `0` to run once and exit. |
| `AUTO_UPDATE` | `true` | `true` = pull + recreate containers. `false` = notify only. |
| `PRUNE_OLD_IMAGES` | `true` | `true` = delete the previous image after a successful update. The old image is kept if another container is still using it. |
| `LABEL_ENABLE` | *(empty)* | Only manage containers with this label (e.g. `autoupdate=true`). Leave empty to check **all** running containers. |
| `DRY_RUN` | `false` | Simulate updates without making any changes (including image removal). |
| `NOTIFY_WEBHOOK` | *(empty)* | POST notifications here (Discord, Slack, Gotify webhook URL). |
| `LOG_LEVEL` | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

---

## How It Works

1. On startup (and then on each scheduled interval), the updater lists all running containers matching the label filter (or all containers if no filter is set)
2. For each container, it records the **current local image ID** then pulls the latest image from the registry
3. Compares the old and new image IDs
4. If they differ, it stops the old container and recreates it with the same configuration
5. If `PRUNE_OLD_IMAGES=true` (default), it removes the old image — unless another container is still using it
6. Sends a webhook notification (if configured)

The updater preserves environment variables, port bindings, volumes, network mode, and restart policy when recreating containers.

---

## Old Image Cleanup

By default the updater deletes the previous image after a successful container recreate, keeping your host's disk free of stale layers. It is safe to leave this enabled:

- The old image is only removed **after** the new container is confirmed running
- If any other running or stopped container still references the old image ID, removal is skipped automatically
- In dry-run mode, removal is simulated in logs only — nothing is deleted

To opt out, set `PRUNE_OLD_IMAGES=false`.

---

## Webhook Notifications

Set `NOTIFY_WEBHOOK` to any HTTP endpoint and the updater will POST on update success or failure:

```json
{ "content": "✅ Updated Docker container `my-nginx` (nginx:latest)", "text": "..." }
```

This format works out of the box with **Discord** webhooks. For **Slack**, use an Incoming Webhook URL — Slack picks up the `text` field automatically.

---

## Restricting Which Containers Are Updated

By default **all running containers** are checked. To opt specific containers in instead, set `LABEL_ENABLE`:

```yaml
LABEL_ENABLE: "autoupdate=true"
```

Then label only the containers you want managed:

**Docker Compose:**
```yaml
labels:
  autoupdate: "true"
```

**Docker Run:**
```bash
docker run --label autoupdate=true ...
```

---

## Limitations

- Recreates containers using the Docker SDK — works best for standalone containers. For Swarm services or Compose stacks, consider [Watchtower](https://github.com/containrrr/watchtower) or [Diun](https://github.com/crazy-max/diun).
- Requires access to `/var/run/docker.sock` — only deploy in trusted environments.

---

## License

MIT
