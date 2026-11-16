# OpnDoor

Door scheduling for **UniFi Access**. People sign in, pick the doors they're allowed to use,
and set when they unlock and lock again: one-off events, weekly repeats, or a quick "unlock now".
Connect a calendar and every booking on it unlocks the right doors automatically.

Runs as a single Docker container (FastAPI + SQLite). Images are built by GitHub Actions and published to GHCR.

## Features

- **Sign-in and per-door permissions.** Admins see and manage everything. Other users can only
  schedule the doors an admin grants them.
- **Easy scheduling.** Name it, tap the doors, pick a date and times. Weekly repeats (any weekdays,
  optional end date) and overnight windows are supported.
- **Unlock now** for 15 min to 2 hours, straight from the home page.
- **Calendar automation.** Add any iCal (.ics) link (Google Calendar, Outlook / Microsoft 365,
  booking systems). Rules map bookings to doors by text in the title, location or description
  (e.g. location contains "Fellowship Hall" → Fellowship Hall + Side Door), with unlock-early /
  lock-late padding. Moved or cancelled bookings update automatically. **Preview** shows what a
  calendar will do before it does it.
- **Activity log** of every schedule change, unlock, lock and error.

## How door control works

Every 30 seconds OpnDoor works out which doors should be unlocked right now and for how long.
For each one it sets a UniFi Access *temporary lock rule* (`custom`, N minutes) through the
developer API. The controller counts down and relocks the door itself, so doors still lock on
time if OpnDoor is stopped. Overlapping or back-to-back schedules are merged into one unlock.
If a schedule is deleted or shortened while a door is open, OpnDoor sends `reset` to lock it.
After a restart it catches up on anything that should be open.

API calls used: `GET /api/v1/developer/doors` and `PUT /api/v1/developer/doors/{id}/lock_rule`.

## Deploy

Image: `ghcr.io/stevy2191/opndoor:latest` (published on every push to `main`).

```bash
mkdir -p /srv/docker/opndoor/data && cd /srv/docker/opndoor
sudo chown 10001 data          # the container runs as uid 10001
# copy docker-compose.yml and .env.example here
cp .env.example .env           # fill in the UniFi host and API token
docker compose pull && docker compose up -d
```

Open `http://<server>:8080`. The first visit asks you to create the admin account.
Then add users (Users → Add user) and tick the doors each one may schedule.

Put it behind a reverse proxy with HTTPS if it's reachable from outside your network,
and set `SECURE_COOKIES=true`.

If the package is private, log in first:
`echo <PAT with read:packages> | docker login ghcr.io -u Stevy2191 --password-stdin`

### Try it without a controller

Leave `UNIFI_ACCESS_TOKEN=changeme` (or set `DRY_RUN=true`). OpnDoor runs with four demo
doors and logs what it would have sent.

## Local development

```bash
pip install -r requirements-dev.txt
pytest
DATA_DIR=./data uvicorn app.main:app --reload --port 8080
```

## Layout

| Path | What |
| --- | --- |
| `app/main.py` | App setup, background jobs |
| `app/web.py` | Pages and form handling |
| `app/engine.py` | Reconcile loop (schedules → door commands), door sync, audit log |
| `app/schedules.py` | Repeat expansion, merging overlapping windows, time zones |
| `app/calendars.py` | ICS fetching, rule matching, booking → schedule sync |
| `app/unifi.py` | UniFi Access API client and the dry-run fake |
| `app/models.py` | Database tables |

## Releases

Push to `main` → `latest` + short SHA tags. Tag `v1.0.0` → semver tags.
