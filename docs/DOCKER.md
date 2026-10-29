# Run the bridge in Docker

The container runs **the bridge only**. The OpenCode server and the agent stay
on the host; the bridge reaches them over HTTP with `OPENCODE_URL`.

The image is published to GitHub Container Registry for `linux/amd64` and
`linux/arm64`:

```text
ghcr.io/hix-pro/opencode-telegram:2.4.0
```

## Quick start

1. Create the bot and `.env` as described in the [README](../README.md#setup).
   Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `OPENCODE_PASSWORD` (or
   mount `~/.config/opencode/service.json` and leave the password unset).

2. Export the absolute path of the project the agent should work in **on the
   host**. The bridge mounts it at the same path inside the container because
   `/read`, `/find`, `/write`, and `/ls` run in the bridge process.

   ```bash
   export PROJECT_DIR="$HOME/projects"
   ```

3. Start it:

   ```bash
   docker compose up -d
   docker compose logs -f
   ```

4. Send `/start` or `/doctor` in Telegram.

State, schedules, and attachment caches live in the `bridge-data` volume at
`/data`. `docker compose down` keeps them; `docker compose down -v` deletes
them.

## Networking

The bundled `compose.yaml` uses `network_mode: host` on Linux, so
`OPENCODE_URL=http://127.0.0.1:49374` works exactly as it does outside Docker.
The bridge only makes outbound calls; it opens no port.

On macOS or Windows, remove `network_mode: host` and use Docker Desktop's host
alias:

```yaml
services:
  bridge:
    environment:
      OPENCODE_URL: http://host.docker.internal:49374
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

If the OpenCode server binds only to loopback and Docker Desktop cannot reach
it, make the server listen on an address the VM can reach, or run the container
with host networking on a Linux host.

## Configuration

Everything in [CONFIGURATION.md](CONFIGURATION.md) applies. The image sets
these defaults:

| Variable | Image default | Why |
| --- | --- | --- |
| `PROJECT_DIR` | `/workspace` | Placeholder; set it to a host path in `compose.yaml` |
| `STATE_FILE` | `/data/state.json` | Persist sessions and schedules in the volume |
| `LOG_FILE` | `/data/bridge.log` | Keep the rotating log with the state |

`PROJECT_DIR` must exist both in the container and on the host at the same
absolute path. The compose file mounts it that way. `ALLOWED_ROOTS`, when set,
must use those same host paths.

## Security

Containerizing the bridge does **not** sandbox the agent. The OpenCode server
still runs on the host and executes commands there with your account. The
container isolates the bot process and its Python dependencies, not the work
the agent does.

- Keep `.env` at mode `0600`; it holds the bot token.
- Keep `ALLOWED_ROOTS` set and mount only the directories the agent needs.
- Mount the project read-only (`:ro`) if you only need inspection. `/write`
  and agent edits will fail by design.
- The image runs as UID `10001` with all capabilities dropped and
  `no-new-privileges` set in the compose file.
- Pin a version tag or digest in production rather than `latest`.

## Upgrades

```bash
docker compose pull
docker compose up -d
```

Migrations of the state file happen on startup, and the previous format is
read before it is rewritten. Back up the `bridge-data` volume before a major
upgrade.

## Building locally

```bash
docker build -t opencode-telegram:dev .
docker run --rm --entrypoint python opencode-telegram:dev \
  -c 'import bridge, bridgekit; print("ok")'
```

The release workflow builds both architectures and pushes version, minor, and
`latest` tags for `v*` tags, plus `edge` from `master`.
