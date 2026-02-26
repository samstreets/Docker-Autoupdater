# 🐳 Docker Auto-Updater

A lightweight Docker container that monitors your running containers for image updates and automatically pulls and recreates them when a newer image is available on the registry.

## Features

- ✅ **Automatic updates** — pulls new images and recreates containers in-place
- 🏷️ **Label-based opt-in** — only updates containers with `autoupdate=true` (configurable)
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
    image: ghcr.io/youruser/docker-autoupdater:latest
    container_name: docker-autoupdater
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    environment:
      CHECK_INTERVAL_MINUTES: "60"
      AUTO_UPDATE: "true"
      LABEL_ENABLE: "autoupdate=true"
```

Then label the containers you want auto-updated:

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
  -e LABEL_ENABLE=autoupdate=true \
  ghcr.io/youruser/docker-autoupdater:latest
```

### 3. Build Locally

```bash
git clone https://github.com/youruser/docker-autoupdater
cd docker-autoupdater
docker build -t docker-autoupdater .
docker run -d \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  docker-autoupdater
```

---

## Configuration

All configuration is done via environment variables:

| Variable | Default | Description |
|---|---|---|
| `CHECK_INTERVAL_MINUTES` | `60` | How often to check for updates. Set to `0` to run once and exit. |
| `AUTO_UPDATE` | `true` | `true` = pull + recreate containers. `false` = notify only. |
| `LABEL_ENABLE` | `autoupdate=true` | Only manage containers with this label. Leave empty to check all containers. |
| `DRY_RUN` | `false` | Simulate updates without making any changes. |
| `NOTIFY_WEBHOOK` | *(empty)* | POST notifications here (Discord, Slack, Gotify webhook URL). |
| `LOG_LEVEL` | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

---

## How It Works

1. On startup (and then on each scheduled interval), the updater:
2. Lists all running containers matching the label filter
3. For each container, fetches the **remote image digest** via `docker manifest inspect`
4. Compares it against the **local image digest**
5. If they differ → pulls the new image, stops the old container, and recreates it with the same configuration
6. Sends a webhook notification (if configured)

> **Note:** The updater preserves environment variables, port bindings, volumes, network mode, and restart policy when recreating containers. For complex configs (e.g. `docker swarm` or `docker stack`), prefer Watchtower or manual orchestration.

---

## Webhook Notifications

The updater POSTs a JSON payload to `NOTIFY_WEBHOOK` on update success or failure:

```json
{ "content": "✅ Updated Docker container `my-nginx` (nginx:latest)", "text": "..." }
```

This format is compatible with **Discord** webhooks out of the box. For Slack, point it at an Incoming Webhook URL — Slack will use the `text` field.

---

## Opting Containers In/Out

By default, only containers with the label `autoupdate=true` are managed.

**Docker Compose:**
```yaml
labels:
  autoupdate: "true"
```

**Docker Run:**
```bash
docker run --label autoupdate=true ...
```

To manage **all** running containers, set `LABEL_ENABLE` to an empty string:
```yaml
LABEL_ENABLE: ""
```

---

## Limitations

- Recreates containers using the Docker SDK; works best for standalone containers. For Swarm services or Compose stacks, you may prefer [Watchtower](https://github.com/containrrr/watchtower) or [Diun](https://github.com/crazy-max/diun).
- Requires access to `/var/run/docker.sock` — grant only to trusted environments.

---

## License

MIT
