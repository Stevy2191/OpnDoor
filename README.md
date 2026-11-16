# OpnDoor

Schedules one-off door unlock windows for UniFi Access (events, rentals, meetings).
Runs as a Docker container; images are built by GitHub Actions and published to GHCR.

## Status

Skeleton only: `/health` endpoint. Next: schedule windows (door/group, date, start, end, label),
UniFi Access client, and the lock/unlock jobs.

## Build and run

Image: `ghcr.io/stevy2191/opndoor:latest` (published on every push to `main`).

On the server:

```bash
mkdir -p /srv/docker/opndoor && cd /srv/docker/opndoor
# copy docker-compose.yml and .env.example here
cp .env.example .env   # fill in UniFi host and token
docker compose pull && docker compose up -d
curl http://localhost:8080/health
```

If the package is private, log in first:
`echo <PAT with read:packages> | docker login ghcr.io -u Stevy2191 --password-stdin`

## Local development

```bash
docker build -t opndoor .
docker run --rm -p 8080:8080 opndoor
```

## Releases

Push to `main` -> `latest` + short SHA tags. Tag `v1.0.0` -> semver tags.
