# OpnDoor

Door scheduling for **UniFi Access**. People sign in, pick the doors they're allowed to use,
and set when they unlock and lock again: one-off events, weekly repeats, or a quick "unlock now".
Connect a calendar and every booking on it unlocks the right doors automatically.

Runs as a single Docker container (FastAPI + SQLite). Images are built by GitHub Actions and published to GHCR.

## Features

- **Regular hours + overrides.** Give each door its normal weekly unlocked hours. Then add
  overrides on top: an event that unlocks it, or a closure that keeps it locked. While an
  override is on, it completely replaces the regular hours; when it ends, the regular hours
  resume. If overrides overlap, the most recently added one wins. Each door's page shows the
  next 7 days with everything applied.
- **Multiple sites / consoles.** Add each UniFi Access console as a site with its own address,
  API token and timezone. Doors are grouped by site everywhere, and schedule times are local to
  each door's site.
- **Two roles.** **Admins** can do everything. **Door operators** only see and schedule the doors
  they're given: either *all doors at a site* (including doors added later) or individual doors.
  Operators never see other sites.
- **Sign in with Microsoft** (work accounts), or with a password.
- **History.** Every change is recorded: who, when, and what changed (before → after), in plain
  English. Filter by person, site, door, type of change or date, and download it as a spreadsheet.
  Each door's page also shows its recent changes.
- **Events at a glance.** Each override says what it's for (🎉 Event, 👥 Meeting, 🔑 Rental, 📚 Class,
  🔧 Maintenance, 🚫 Closure), with an optional contact person.
- **Easy scheduling.** On the Doors page, tick the doors, then **Set regular hours** or **Add override**.
  Weekly repeats (any weekdays, optional end date) and overnight windows are supported.
- **Unlock now** for 15 min to 2 hours, straight from the home page.
- **Calendar automation.** Each booking becomes an unlock override. Add any iCal (.ics) link (Google Calendar, Outlook / Microsoft 365,
  booking systems). Rules map bookings to doors by text in the title, location or description
  (e.g. location contains "Fellowship Hall" → Fellowship Hall + Side Door), with unlock-early /
  lock-late padding. Moved or cancelled bookings update automatically. **Preview** shows what a
  calendar will do before it does it.
- **Activity log** of every schedule change, unlock, lock and error.

## How door control works

For each door, OpnDoor builds a timeline from its schedules:

1. An active **override** decides: unlocked or kept locked (newest override wins).
2. Otherwise, during **regular hours**, the door is unlocked.
3. Otherwise OpnDoor leaves the door on its normal UniFi rule (locked).

Every 30 seconds it compares that with what it last told each console, through the UniFi Access
developer API (`PUT /api/v1/developer/doors/{id}/lock_rule`):

| Door should be | Command | Notes |
| --- | --- | --- |
| Unlocked until T | `custom`, interval = minutes until T | The console relocks by itself at T, even if OpnDoor is down. Back-to-back periods (e.g. regular hours running into an event) are sent as one unlock. |
| Kept locked | `keep_lock` | Also overrides unlock schedules set up inside UniFi Access itself. |
| Back to normal | `reset` | Sent when an override or unlock ends early, is deleted, or a lock override ends. |

After a restart it catches up on whatever should be happening now. Doors are listed with
`GET /api/v1/developer/doors` on each site's console every 10 minutes.

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
The console in `.env` becomes your first site. Add more consoles under **Sites**
(address, API token, timezone). Then add users (Users → Add user) and tick the doors each one may schedule.

The API token is created in UniFi Access under Settings → General → Advanced → API Token,
with permission to view and operate doors. One token per console.

Put it behind a reverse proxy with HTTPS if it's reachable from outside your network,
and set `SECURE_COOKIES=true`.

If the package is private, log in first:
`echo <PAT with read:packages> | docker login ghcr.io -u Stevy2191 --password-stdin`

### Try it without a controller

Leave `UNIFI_ACCESS_TOKEN=changeme`. OpnDoor creates a **Demo site** with four pretend doors
and logs what it would have sent. Add more demo sites by entering `demo` as a site's address.
Remove the demo site once your real consoles are added.

## Sign in with Microsoft

Do this once, as someone who can register apps in your Microsoft 365 tenant.

1. Go to <https://entra.microsoft.com> → **Applications** → **App registrations** → **New registration**.
2. **Name:** `OpnDoor`. **Supported account types:** *Accounts in this organizational directory only*.
   **Redirect URI:** platform *Web*, address `https://<your OpnDoor address>/auth/microsoft/callback`.
   (OpnDoor shows this exact address on its **People** page. Microsoft requires `https://`, so put
   OpnDoor behind a reverse proxy with a certificate first.)
3. On the app's **Overview** page, copy the **Application (client) ID** and **Directory (tenant) ID**.
4. **Certificates & secrets** → **New client secret** → copy the secret's **Value** (not its ID).
   Note when it expires and make a new one before then.
5. Add these to OpnDoor's `.env` and restart it (`docker compose up -d`):
   ```
   MS_TENANT_ID=<Directory (tenant) ID>
   MS_CLIENT_ID=<Application (client) ID>
   MS_CLIENT_SECRET=<secret Value>
   PUBLIC_URL=https://<your OpnDoor address>
   SECURE_COOKIES=true
   ```
6. In OpnDoor, add each person under **People** with the email they use for Microsoft, and choose
   *Microsoft account*. Only people added there can sign in. Accounts outside your tenant can't.

Keep at least one admin with a password, so you can still get in if the Microsoft settings change.

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
| `app/engine.py` | Reconcile loop (timelines → door commands), site/door sync, audit log |
| `app/schedules.py` | Repeat expansion, default/override timeline, time zones |
| `app/calendars.py` | ICS fetching, rule matching, booking → schedule sync |
| `app/unifi.py` | UniFi Access API client (one per site) and the demo console |
| `app/models.py` | Database tables |

## Releases

Push to `main` → `latest` + short SHA tags. Tag `v1.0.0` → semver tags.
