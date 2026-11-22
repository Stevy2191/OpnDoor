#!/bin/sh
# OpnDoor's update helper. It waits for the Update button in OpnDoor (which leaves
# data/update.request), then downloads the newest OpnDoor and restarts it.
# install.sh sets it up as the "updater" service; it needs the Docker socket.
cd "$(dirname "$0")" || exit 1
project=$(docker inspect opndoor --format '{{ index .Config.Labels "com.docker.compose.project" }}' 2>/dev/null)
dc() { docker compose ${project:+-p "$project"} "$@"; }
stamp() { date '+%Y-%m-%d %H:%M'; }
while :; do
  touch data/updater.alive
  if [ -f data/update.request ]; then
    rm -f data/update.request
    echo "running $(stamp)" >data/update.status
    image=$(dc config --images opndoor 2>/dev/null | head -n 1)
    before=$(docker image inspect -f '{{.Id}}' "$image" 2>/dev/null)
    if out=$(dc pull opndoor 2>&1); then
      after=$(docker image inspect -f '{{.Id}}' "$image" 2>/dev/null)
      if [ -n "$before" ] && [ "$before" = "$after" ]; then
        echo "current $(stamp)" >data/update.status
      elif out=$(dc up -d --no-deps opndoor 2>&1); then
        echo "updated $(stamp)" >data/update.status
      else
        printf 'failed %s\n%s\n' "$(stamp)" "$out" >data/update.status
      fi
    else
      printf 'failed %s\n%s\n' "$(stamp)" "$out" >data/update.status
    fi
    chmod 644 data/update.status
  fi
  sleep 10
done
