#!/usr/bin/env bash
set -euo pipefail
umask 077
ACTION=${1:?action required}
ROOT=/opt/grima
NAME=grima-bot
mkdir -p "$ROOT"
exec 9>"$ROOT/deploy.lock"
flock -n 9 || { echo 'Another Grima operation is running.' >&2; exit 1; }
backup() {
  mkdir -p "$ROOT/backups"
  local stamp
  stamp=$(date -u +%Y%m%dT%H%M%S)-$$
  if [[ -f "$ROOT/data/raid_db" ]]; then
    python3 - "$ROOT/data/raid_db" "$ROOT/backups/raid_db-$stamp" <<'PY'
import sqlite3,sys
with sqlite3.connect(sys.argv[1]) as source, sqlite3.connect(sys.argv[2]) as dest:
    source.backup(dest)
from pathlib import Path
backups=sorted(Path(sys.argv[2]).parent.glob('raid_db-*'))
for old in backups[:-14]:old.unlink()
PY
  fi
}
start() {
  docker run -d --name "$NAME" --restart unless-stopped \
    --log-opt max-size=10m --log-opt max-file=3 \
    --mount "type=bind,src=$ROOT/data,dst=/data" \
    --mount "type=bind,src=$ROOT/config.json,dst=/run/grima/config.json,readonly" \
    "$1"
}
case "$ACTION" in
  status) docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$NAME"; exit;;
  logs) docker logs --tail 150 --follow "$NAME"; exit;;
  restart) docker restart "$NAME"; exit;;
  backup) backup; echo "Backup saved in $ROOT/backups"; exit;;
  install|update) ;;
  *) echo 'Unknown action' >&2; exit 2;;
esac
IMAGE=${2:?image required}; UPDATE_OS=${3:-0}; REBOOT=${4:-0}
case "$(uname -m)" in aarch64|x86_64) ;; *) echo 'Requires 64-bit ARM or x86 Linux.' >&2; exit 1;; esac
command -v apt-get >/dev/null || { echo 'Requires Debian/Ubuntu/Raspberry Pi OS.' >&2; exit 1; }
if ! command -v docker >/dev/null; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io python3 ca-certificates
fi
systemctl enable --now docker
command -v python3 >/dev/null || apt-get install -y python3
mkdir -p "$ROOT/data" "$ROOT/backups"
UPLOAD=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ -f "$UPLOAD/config.json" ]]; then
  python3 - "$UPLOAD/config.json" <<'PY'
import json,sys
c=json.load(open(sys.argv[1]))
assert c.get('BOT_TOKEN') and c['BOT_TOKEN']!='Token goes here', 'Set BOT_TOKEN in config'
for k in ('CLASSES','LINEUP','SERVER_TZ'): assert c.get(k), 'Missing '+k
PY
elif [[ ! -f "$ROOT/config.json" ]]; then
  echo 'Missing config; first deployment needs --config.' >&2; exit 1
fi
# Pull and run the candidate's own tests with no token, network or live volume.
docker pull "$IMAGE"
CANDIDATE=$(docker image inspect --format '{{.Id}}' "$IMAGE")
docker run --rm --network none --entrypoint python -w /app "$CANDIDATE" -m unittest discover -s tests -q
backup
if [[ "$UPDATE_OS" == 1 ]]; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get upgrade -y
fi
if [[ -f "$UPLOAD/config.json" ]]; then
  [[ ! -f "$ROOT/config.json" ]] || cp -p "$ROOT/config.json" "$ROOT/config.previous.json"
  install -m 600 "$UPLOAD/config.json" "$ROOT/config.json"
  rm -f "$UPLOAD/config.json"
fi
if [[ "$UPLOAD" != "$ROOT" ]]; then install -m 700 "$UPLOAD/remote.sh" "$ROOT/remote.sh"; fi
cat > /etc/systemd/system/grima-backup.service <<'UNIT'
[Unit]
Description=Back up Grima SQLite database
[Service]
Type=oneshot
ExecStart=/bin/bash /opt/grima/remote.sh backup
UNIT
cat > /etc/systemd/system/grima-backup.timer <<'UNIT'
[Unit]
Description=Daily Grima database backup
[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=15m
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now grima-backup.timer
OLD=$(docker inspect --format '{{.Image}}' "$NAME" 2>/dev/null || true)
if [[ -n "$OLD" ]]; then docker stop --time 30 "$NAME"; backup; docker rm "$NAME"; fi
if ! start "$CANDIDATE"; then
  echo 'Container start failed; inspect Docker errors. Backup retained.' >&2
  exit 1
fi
for ((i=0;i<40;i++)); do
  state=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$NAME")
  if [[ "$state" == healthy ]]; then
    printf '%s\n' "$IMAGE" > "$ROOT/image"
    # Keep only the running image and the one it replaced for rollback.
    repo=${IMAGE%%[:@]*}
    docker images --no-trunc --format '{{.ID}} {{.Repository}}:{{.Tag}}' "$repo" |
      while read -r id ref; do
        [[ "$id" == "$CANDIDATE" || "$id" == "$OLD" ]] || docker rmi "$ref" >/dev/null 2>&1 || true
      done
    docker image prune -f >/dev/null 2>&1 || true
    echo 'Grima connected and ready.'
    if [[ "$REBOOT" == 1 && -f /var/run/reboot-required ]]; then shutdown -r +1; fi
    exit 0
  fi
  sleep 5
done
# Do not automatically roll an older binary onto a possibly migrated database.
docker stop "$NAME"
echo "Candidate failed readiness. Stopped to protect data. Previous image: $OLD" >&2
echo "Inspect docker logs $NAME; database backup is in $ROOT/backups. No automatic schema rollback." >&2
exit 1
