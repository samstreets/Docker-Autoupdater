# 🐳 Docker Auto-Updater

A lightweight Docker container that monitors your running containers for image updates and automatically pulls and recreates them when a newer image is available on the registry.

**Docker Hub:** [`samuelstreets/docker-updater`](https://hub.docker.com/r/samuelstreets/docker-updater)

---

## Features

- ✅ **Automatic updates** — pulls new images and recreates containers in-place
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
| `LABEL_ENABLE` | *(empty)* | Only manage containers with this label (e.g. `autoupdate=true`). Leave empty to check **all** running containers. |
| `DRY_RUN` | `false` | Simulate updates without making any changes. |
| `NOTIFY_WEBHOOK` | *(empty)* | POST notifications here (Discord, Slack, Gotify webhook URL). |
| `LOG_LEVEL` | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

---

## How It Works

1. On startup (and then on each scheduled interval), the updater lists all running containers matching the label filter (or all containers if no filter is set)
2. For each container, it fetches the **remote image digest** via `docker manifest inspect` — no unnecessary full pulls
3. Compares it against the **local image digest**
4. If they differ, it pulls the new image, stops the old container, and recreates it with the same configuration
5. Sends a webhook notification (if configured)

The updater preserves environment variables, port bindings, volumes, network mode, and restart policy when recreating containers.

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
