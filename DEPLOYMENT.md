# Deploy Grima from Linux to a Pi

Pattern based on `nayberhq/the-network/bin/deploy.sh` and
`nayberhq/the-network-install/scripts/update.sh`: build a registry image, then
pull and test it on the destination before replacing the bot. Only Grima is
managed; other Docker projects and caches are not pruned.

## Prerequisites

- Linux controller with Python 3.12+, SSH, Git, and project requirements installed.
- 64-bit Raspberry Pi OS/Debian/Ubuntu target with SSH enabled, systemd, apt,
  and a user with passwordless sudo. 32-bit Pi OS is rejected.
- Authenticate with SSH keys and verify the host fingerprint first. The script
  requires the verified key in known_hosts and does not disable host checks.
- Publish an ARM64 image (AMD64 is also supported). Registry login on the
  controller is needed to publish. Public GHCR packages need no target login;
  private packages require `sudo docker login ghcr.io` on the Pi after Docker
  installation. Never put registry or bot credentials into flags or Git.
- Create your Discord application and a private local config copied from
  `source/example-config.json`. Set BOT_TOKEN, timezone and emoji host settings.
  Enable the required Gateway intents in Discord. Invite the bot to at least
  one server before first start (the upstream bot exits if it belongs to none).
  After first start, the application owner runs `!sync` in Discord to register
  global slash commands; repeat when deploying command changes. SSH username/IP alone cannot
  supply the Discord token.

## Controller setup and image publication

```bash
python3.12 -m venv .deploy-venv
source .deploy-venv/bin/activate
pip install -r requirements.txt
./deploy.sh --test
# Requires Docker buildx with ARM64 build support and GHCR login:
./deploy.sh --publish --image ghcr.io/YOUR_ACCOUNT/grima-bot:v1
```

Alternatively, push this fork to your own Grima repository and run the Grima
image workflow (manual or `grima-v*` tag). It tests before publishing an
AMD64/ARM64 image tagged `sha-FULL_COMMIT_SHA`. No repository, image or release
has been published by this implementation. The image references here are
examples, not existing packages. The script never pushes Git to upstream.

## Install remotely

```bash
./deploy.sh --deploy --user pi --host 192.168.1.50 \
  --image ghcr.io/YOUR_ACCOUNT/grima-bot:v1 \
  --config /home/YOU/private/grima-config.json
```

Optional SSH flags: `--identity ~/.ssh/id_ed25519 --port 22`.

The command runs local tests before SSH mutations, provisions Docker from the
OS repositories if missing, enables Docker at boot, uploads runtime scripts
and config, pulls the requested image, and runs that image's tests with
`--network none` and no token or production volume. Only after those pass does
it stop the old bot, take a final database backup and replace the container.
The new container must report Discord readiness within approximately 200 seconds.
A failed candidate is stopped and reported; database schema rollback is never
performed automatically. Previous image IDs and database backups remain available.

Runtime data: `/opt/grima/data/raid_db`; secrets: `/opt/grima/config.json` (0600).
The runtime mounts these paths separately from image code. Restart policy is
`unless-stopped`. A systemd timer takes daily SQLite-consistent backups in
`/opt/grima/backups`, keeping the latest 14 backup files (including deployment
backups). Copy backups off the Pi separately for protection from disk failure.
Docker stdout/stderr logs rotate; application file logs are kept in the data
folder and should be included in your host log rotation policy.

## Update bot, deployment scripts, and optionally OS

```bash
./deploy.sh --update --pull-git --update-os \
  --user pi --host 192.168.1.50 \
  --image ghcr.io/YOUR_ACCOUNT/grima-bot:v2
```

`--pull-git` fast-forwards the **local controller checkout** from its own origin,
then restarts the controller with the updated script and runs its tests. It
requires a clean worktree and refuses an original Saruman origin. The Pi runs
an image, not a source checkout; updated runtime scripts are uploaded via SSH.
An image must already be published; pulling Git does not build/publish it.
Use `--publish` or the GitHub workflow first when making a new release.

`--update-os` runs apt update/upgrade (not a distribution upgrade). Omit it to
update only Grima. Add `--reboot` to schedule a reboot after successful bot
readiness if the OS creates `/var/run/reboot-required`. Pi kernel updates do
not always create that marker; check reboot requirements after maintenance.
OS upgrades may restart Docker or other services; use a maintenance window.
The script does not change firewall/SSH settings or migrate the OS release.

## Operations through the same entry point

```bash
./deploy.sh --status  --user pi --host 192.168.1.50
./deploy.sh --logs    --user pi --host 192.168.1.50
./deploy.sh --restart --user pi --host 192.168.1.50
./deploy.sh --backup  --user pi --host 192.168.1.50
./deploy.sh --help
```

No inbound bot port or domain is needed. Server administrators invite the same
Discord application regardless of where the container runs.

## Verification limits

53 Python tests pass, including injection-resistant argument validation,
mandatory pre-SSH test gating, verified SSH behavior and OS-upgrade flag checks.
Bash syntax and changed Python modules compile. Docker is unavailable in the
implementation environment: image builds, ARM64 dependency installation,
remote apt/systemd/SSH operation and live Discord readiness require validation
on the actual Pi. This is implemented deployment tooling, not a completed deployment.
