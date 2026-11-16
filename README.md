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
- **Three roles.**
  - **Admin**: everything, at every site, including adding sites (consoles) and other admins.
  - **Site admin**: runs the sites they're given: all doors there, the door operators there,
    calendars that use those doors, and history for those sites. Can't see other sites.
  - **Door operator**: sees and schedules only the doors they're given, either *all doors at a
    site* (including doors added later) or single doors. Never sees other sites.

  If a door operator works at two sites, each site admin can change only that person's access at
  their own site; name, email and sign-in can then only be changed by an admin.
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

## Install

On a Linux machine (Ubuntu, Debian, Raspberry Pi OS, etc.) that can reach your UniFi Access console:

```bash
curl -fsSL https://raw.githubusercontent.com/Stevy2191/OpnDoor/main/install.sh | sudo bash
```

The installer does everything except sign in for you:

1. Installs Docker if it's missing.
2. Checks which ports are already in use and only picks free ones.
3. Asks how people should reach OpnDoor:
   - **Secure (HTTPS) with a built-in certificate** (recommended). Adds Caddy with a self-signed
     certificate. Browsers show a one-time warning; install `opndoor-certificate.crt` from the
     install folder on your computers to remove it.
   - **Your own reverse proxy** (nginx, Traefik, Nginx Proxy Manager, Cloudflare Tunnel...).
     OpnDoor listens on a local port for your proxy to forward to.
   - **No HTTPS**, for testing only.
4. Connects your UniFi Access console (and tests the API token), or skips it so you can add
   consoles later under **Sites**.
5. Optionally sets up Microsoft sign-in.
6. Opens the firewall port if `ufw` or `firewalld` is on, downloads OpnDoor and starts it.

At the end it prints the web address and a **setup code**. Open the address, enter the code and
create your account: that first account is the **main admin**, which nobody else can change or remove.

Everything lives in `/srv/docker/opndoor` (settings in `.env`, data in `data/`).

| To... | Run |
| --- | --- |
| Update to the newest version | `sudo /srv/docker/opndoor/update.sh` |
| Change settings (HTTPS, ports, Microsoft sign-in) | the install command again → *Change settings* |
| Uninstall (keeps data unless you say otherwise) | the install command again → *Uninstall* |
| See logs | `cd /srv/docker/opndoor && sudo docker compose logs -f` |

Re-running the installer never touches your schedules, people or history.

### How the image is built

GitHub Actions (`.github/workflows/docker.yml`) runs the tests and checks the installer on every
push and pull request. On every push to `main` it builds the Docker image for regular PCs and
servers (amd64) and ARM boards like the Raspberry Pi 4/5 (arm64), and publishes it to GitHub's
container registry as `ghcr.io/stevy2191/opndoor:latest` (plus a tag for the commit). Tagging a
release `v1.2.0` also publishes `1.2.0` and `1.2`.

After the first build, check that the package is public: GitHub → your profile → **Packages** →
**opndoor** → **Package settings** → **Change visibility** → Public. (If it's private, the installer
asks for a GitHub token with `read:packages` instead.)

### Manual install

Prefer to do it by hand? Copy `docker-compose.yml` and `.env.example` (as `.env`) into a folder,
fill in `.env`, create `data/` owned by uid 10001 (`sudo chown 10001 data`), then
`docker compose up -d`.

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
5. Run the installer again → *Change settings* and paste them in when it asks. (Or add them to
   `/srv/docker/opndoor/.env` yourself and run `sudo docker compose up -d` in that folder):
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
