#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   SSH_KEY=~/.ssh/jomnaik_do ./deploy_remote.sh
#
# The private key stays on the local computer. It is only passed to ssh/rsync
# and is never copied into the repository or the DigitalOcean server.
REMOTE_HOST="${REMOTE_HOST:-root@152.42.181.141}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/jomnaik_do}"
REMOTE_DIR="${REMOTE_DIR:-/root/jomnaik_backend}"
MOTIS_CONTAINER="${MOTIS_CONTAINER:-motis-engine}"
DOCKER_NETWORK="${DOCKER_NETWORK:-jomnaik_net}"

if [[ ! -f "$SSH_KEY" ]]; then
  echo "SSH private key not found: $SSH_KEY" >&2
  echo "Set SSH_KEY=/path/to/key and run this script again." >&2
  exit 1
fi

SSH=(ssh -i "$SSH_KEY" -o IdentitiesOnly=yes)
RSYNC=(rsync -avP -e "ssh -i $SSH_KEY -o IdentitiesOnly=yes")

"${RSYNC[@]}" \
  klang_valley.sheltered.osm.pbf gtfs-rail.zip gtfs-bus.zip \
  gtfs-mrtfeeder.zip gtfs-ktmb.zip server.py \
  build_station_transfers.py build_station_pathways.py \
  rebuild_rail_frequencies.py motis_adapter.py Dockerfile.motis docker-compose.yml \
  "$REMOTE_HOST:$REMOTE_DIR/"

"${SSH[@]}" "$REMOTE_HOST" "cd '$REMOTE_DIR' && \
  docker build -t motis-local:latest -f Dockerfile.motis . && \
  docker build -t jomnaik-fastapi:latest -f DockerFile . && \
  docker network create '$DOCKER_NETWORK' 2>/dev/null || true && \
  docker rm -f '$MOTIS_CONTAINER' 2>/dev/null || true && \
  docker run -d --name '$MOTIS_CONTAINER' --restart always \
    --network '$DOCKER_NETWORK' motis-local:latest && \
  docker rm -f fastapi_middleware_container 2>/dev/null || true && \
  docker run -d --name fastapi_middleware_container --restart always \
    -p 80:8000 --env-file '$REMOTE_DIR/.env' \
    -e MOTIS_URL=http://$MOTIS_CONTAINER:8080 \
    --network '$DOCKER_NETWORK' jomnaik-fastapi:latest"

echo "Deployment completed."
