#!/usr/bin/env bash
# OpnDoor installer
#
#   curl -fsSL https://raw.githubusercontent.com/Stevy2191/OpnDoor/main/install.sh | sudo bash
#
# Sets up everything: Docker (if needed), ports, HTTPS, your UniFi Access console,
# optional Microsoft sign-in, and starts OpnDoor. All you do afterwards is open the
# web address it prints and create the main admin account with the setup code.
#
# Run it again any time to update, change settings, or uninstall.
#   sudo bash install.sh            # install, or menu if already installed
#   sudo bash install.sh --update   # update to the newest version, no questions
set -euo pipefail

OPNDOOR_IMAGE="${OPNDOOR_IMAGE:-ghcr.io/stevy2191/opndoor:latest}"
CADDY_IMAGE="${CADDY_IMAGE:-caddy:2}"
GO2RTC_IMAGE="${GO2RTC_IMAGE:-alexxit/go2rtc:1.9.9}"
INSTALL_DIR="${OPNDOOR_DIR:-/srv/docker/opndoor}"
APP_UID=10001

# ---------------------------------------------------------------- output helpers
if [ -t 1 ]; then
  B=$'\e[1m'; DIM=$'\e[2m'; RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; BLU=$'\e[34m'; R=$'\e[0m'
else
  B=""; DIM=""; RED=""; GRN=""; YEL=""; BLU=""; R=""
fi
say()   { printf '%s\n' "$*"; }
step()  { printf '\n%s==> %s%s\n' "$B$BLU" "$*" "$R"; }
ok()    { printf '  %s✓%s %s\n' "$GRN" "$R" "$*"; }
warn()  { printf '  %s!%s %s\n' "$YEL" "$R" "$*"; }
fail()  { printf '\n%sError:%s %s\n' "$RED$B" "$R" "$*" >&2; exit 1; }
note()  { printf '  %s%s%s\n' "$DIM" "$*" "$R"; }

# Questions come from the keyboard even when this script is piped into bash.
if [ "${OPNDOOR_ANSWERS_FROM_STDIN:-}" = "1" ]; then
  TTY=/dev/stdin
elif [ -t 0 ]; then
  TTY=/dev/stdin
elif { : </dev/tty; } 2>/dev/null; then
  TTY=/dev/tty
else
  TTY=/dev/stdin
fi

# ask "Question" "default" -> REPLY
ask() {
  local q="$1" def="${2:-}"
  if [ -n "$def" ]; then printf '%s %s[%s]%s: ' "$q" "$DIM" "$def" "$R"; else printf '%s: ' "$q"; fi
  IFS= read -r REPLY <"$TTY" || REPLY=""
  REPLY="${REPLY#"${REPLY%%[![:space:]]*}"}"; REPLY="${REPLY%"${REPLY##*[![:space:]]}"}"
  [ -z "$REPLY" ] && REPLY="$def"
  return 0
}
ask_secret() {
  printf '%s: ' "$1"
  if [ "$TTY" = /dev/tty ] || [ -t 0 ]; then
    IFS= read -rs REPLY <"$TTY" || REPLY=""; printf '\n'
  else
    IFS= read -r REPLY <"$TTY" || REPLY=""; printf '\n'
  fi
}
# yes_no "Question" Y|N -> returns 0 for yes
yes_no() {
  local def="$2" hint="y/N"; [ "$def" = "Y" ] && hint="Y/n"
  while true; do
    ask "$1 ($hint)" ""
    case "${REPLY:-$def}" in
      [Yy]|[Yy][Ee][Ss]) return 0 ;;
      [Nn]|[Nn][Oo]) return 1 ;;
      *) say "  Please answer y or n." ;;
    esac
  done
}
# choose "1 2 3" default -> REPLY
choose() {
  local valid="$1" def="$2"
  while true; do
    ask "Type a number" "$def"
    for v in $valid; do [ "$REPLY" = "$v" ] && return 0; done
    say "  Please type one of: $valid"
  done
}

# ---------------------------------------------------------------- ports
PORTS_IN_USE=""
scan_ports() {
  local list=""
  if command -v ss >/dev/null 2>&1; then
    list=$(ss -H -ltn 2>/dev/null | awk '{print $4}' | sed -E 's/.*[:.]([0-9]+)$/\1/')
  elif command -v netstat >/dev/null 2>&1; then
    list=$(netstat -ltn 2>/dev/null | awk 'NR>2 {print $4}' | sed -E 's/.*[:.]([0-9]+)$/\1/')
  elif command -v lsof >/dev/null 2>&1; then
    list=$(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | awk 'NR>1 {print $9}' | sed -E 's/.*:([0-9]+)$/\1/')
  else
    warn "Couldn't find ss, netstat or lsof, so I can't check which ports are free."
  fi
  # ports Docker has published (they may not show above on some systems)
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    list="$list
$(docker ps --format '{{.Ports}}' 2>/dev/null | grep -oE ':[0-9]+->' | tr -d ':->' || true)"
  fi
  PORTS_IN_USE=$(printf '%s\n' "$list" | grep -E '^[0-9]+$' | sort -n -u | tr '\n' ' ' || true)
}
port_free() {  # port_free 8080 [ignore-port...]
  local p="$1"; shift
  for ignore in "$@"; do [ "$p" = "$ignore" ] && return 0; done
  case " $PORTS_IN_USE " in *" $p "*) return 1 ;; esac
  return 0
}
next_free() {  # next_free 8080 -> first free port from there
  local p="$1"; shift
  while ! port_free "$p" "$@"; do p=$((p + 1)); done
  printf '%s' "$p"
}
ask_port() {  # ask_port "Question" default [already-chosen...] -> PORT
  local q="$1" def="$2"; shift 2
  while true; do
    ask "$q" "$def"
    if ! [[ "$REPLY" =~ ^[0-9]+$ ]] || [ "$REPLY" -lt 1 ] || [ "$REPLY" -gt 65535 ]; then
      say "  That isn't a port number (1-65535)."; continue
    fi
    for taken in "$@"; do [ "$REPLY" = "$taken" ] && { say "  You already picked $taken for something else."; continue 2; }; done
    if ! port_free "$REPLY" "${KEEP_PORTS[@]}"; then
      say "  Port $REPLY is already in use on this machine. Pick another."; continue
    fi
    PORT="$REPLY"; return 0
  done
}

# ---------------------------------------------------------------- misc helpers
rand() {  # rand N -> N random chars from A-Z2-9 (no look-alikes)
  LC_ALL=C tr -dc 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789' </dev/urandom | head -c "$1" || true
}
detect_ip() {
  local ip=""
  ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<NF;i++) if ($i=="src") {print $(i+1); exit}}') || true
  [ -z "$ip" ] && ip=$(hostname -I 2>/dev/null | awk '{print $1}') || true
  printf '%s' "${ip:-127.0.0.1}"
}
detect_tz() {
  local tz=""
  tz=$(timedatectl show -p Timezone --value 2>/dev/null) || true
  [ -z "$tz" ] && [ -f /etc/timezone ] && tz=$(cat /etc/timezone)
  [ -z "$tz" ] && tz=$(readlink /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||') || true
  printf '%s' "${tz:-America/Chicago}"
}
env_get() {  # env_get KEY -> value from existing .env
  [ -f "$INSTALL_DIR/.env" ] || return 0
  grep -E "^$1=" "$INSTALL_DIR/.env" | tail -1 | cut -d= -f2- || true
}
compose() { (cd "$INSTALL_DIR" && docker compose "$@"); }

# ---------------------------------------------------------------- docker
ensure_root() {
  if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
      say "OpnDoor's installer needs administrator rights. You may be asked for your password."
      if [ -f "$0" ] && [ "$0" != "bash" ]; then exec sudo -E bash "$0" "$@"; fi
      fail "Please run it with sudo:  curl -fsSL <link> | sudo bash"
    fi
    fail "Please run this as root."
  fi
}

ensure_docker() {
  step "Checking Docker"
  if ! command -v docker >/dev/null 2>&1; then
    warn "Docker isn't installed. OpnDoor runs inside Docker."
    if yes_no "Install Docker now (uses Docker's official install script)?" Y; then
      command -v curl >/dev/null 2>&1 || fail "curl is needed to install Docker. Install curl and run this again."
      curl -fsSL https://get.docker.com | sh || fail "Docker didn't install. See the messages above."
    else
      fail "Install Docker first (https://docs.docker.com/engine/install/), then run this again."
    fi
  fi
  if ! docker info >/dev/null 2>&1; then
    if command -v systemctl >/dev/null 2>&1; then systemctl enable --now docker >/dev/null 2>&1 || true; fi
    sleep 2
    docker info >/dev/null 2>&1 || fail "Docker is installed but not running. Start it (systemctl start docker) and try again."
  fi
  ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null) is running"
  if ! docker compose version >/dev/null 2>&1; then
    warn "Docker Compose is missing; installing it."
    if command -v apt-get >/dev/null 2>&1; then apt-get update -qq && apt-get install -y -qq docker-compose-plugin
    elif command -v dnf >/dev/null 2>&1; then dnf install -y -q docker-compose-plugin
    elif command -v yum >/dev/null 2>&1; then yum install -y -q docker-compose-plugin
    fi
    docker compose version >/dev/null 2>&1 || fail "Couldn't install Docker Compose. Install the docker-compose-plugin package and run this again."
  fi
  ok "Docker Compose $(docker compose version --short 2>/dev/null) is ready"
}

pull_images() {
  step "Downloading OpnDoor"
  if [ "${OPNDOOR_NO_PULL:-}" = "1" ]; then note "Skipping download (OPNDOOR_NO_PULL=1)."; return 0; fi
  local out
  while true; do
    if out=$(compose pull 2>&1); then ok "Downloaded the newest version"; return 0; fi
    if printf '%s' "$out" | grep -qiE 'denied|unauthorized|authentication required|forbidden'; then
      warn "GitHub wouldn't let me download the OpnDoor image. The package is probably private."
      note "Either make it public (GitHub → your profile → Packages → opndoor → Package settings),"
      note "or sign in here with a GitHub token that has the 'read:packages' permission."
      if yes_no "Sign in to GitHub's package registry now?" Y; then
        ask "GitHub username" ""
        local gh_user="$REPLY"
        ask_secret "GitHub token (read:packages)"
        printf '%s' "$REPLY" | docker login ghcr.io -u "$gh_user" --password-stdin >/dev/null 2>&1 \
          && ok "Signed in to ghcr.io" || warn "That sign-in didn't work."
        continue
      fi
    fi
    printf '%s\n' "$out" | tail -5
    fail "Couldn't download OpnDoor. Check this machine's internet connection and try again."
  done
}

start_and_wait() {
  step "Starting OpnDoor"
  compose up -d --remove-orphans >/dev/null 2>&1 || { compose up -d --remove-orphans; fail "OpnDoor didn't start."; }
  printf '  Waiting for OpnDoor to be ready'
  local i status
  for i in $(seq 1 60); do
    status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' opndoor 2>/dev/null || echo "")
    if [ "$status" = "healthy" ]; then printf '\n'; ok "OpnDoor is running"; return 0; fi
    if [ "$status" = "exited" ] || [ "$status" = "dead" ]; then break; fi
    printf '.'; sleep 2
  done
  printf '\n'
  compose logs --tail 30 opndoor || true
  fail "OpnDoor didn't become ready. The log above should say why."
}

# ---------------------------------------------------------------- existing install
update_only() {
  [ -f "$INSTALL_DIR/docker-compose.yml" ] || fail "OpnDoor isn't installed in $INSTALL_DIR."
  ensure_docker
  pull_images
  start_and_wait
  docker image prune -f >/dev/null 2>&1 || true
  ok "OpnDoor is up to date. Your schedules, people and settings were kept."
}

uninstall() {
  step "Uninstall OpnDoor"
  [ -f "$INSTALL_DIR/docker-compose.yml" ] || fail "OpnDoor isn't installed in $INSTALL_DIR."
  warn "This stops OpnDoor. Doors go back to their normal UniFi rules once any running unlock times out."
  yes_no "Stop and remove OpnDoor?" N || { say "Nothing changed."; exit 0; }
  compose down --remove-orphans || true
  ok "OpnDoor is stopped and removed."
  if yes_no "Also delete all OpnDoor data (schedules, people, history) in $INSTALL_DIR? This can't be undone." N; then
    rm -rf "$INSTALL_DIR"
    ok "Deleted $INSTALL_DIR"
  else
    note "Your data is still in $INSTALL_DIR/data. Run the installer again to bring OpnDoor back."
  fi
  exit 0
}

# ---------------------------------------------------------------- main install
main() {
  local arg="${1:-}"
  ensure_root "$@"
  [ "$(uname -s)" = "Linux" ] || fail "This installer is for Linux."

  printf '\n%s  OpnDoor installer%s\n' "$B" "$R"
  say "  Door scheduling for UniFi Access."

  case "$arg" in
    --update) update_only; exit 0 ;;
    --uninstall) uninstall ;;
    "") ;;
    *) fail "Unknown option $arg. Use --update or --uninstall, or nothing to install." ;;
  esac

  RECONFIGURE=0
  KEEP_PORTS=()
  if [ -f "$INSTALL_DIR/.env" ]; then
    step "OpnDoor is already installed in $INSTALL_DIR"
    say "  1) Update to the newest version (keeps everything)"
    say "  2) Change settings (HTTPS, ports, Microsoft sign-in, ...). Keeps your data."
    say "  3) Uninstall"
    say "  4) Cancel"
    choose "1 2 3 4" 1
    case "$REPLY" in
      1) update_only; exit 0 ;;
      2) RECONFIGURE=1 ;;
      3) uninstall ;;
      4) exit 0 ;;
    esac
  fi

  ensure_docker

  # -------- ports
  step "Checking which ports are in use on this machine"
  if [ "$RECONFIGURE" = 1 ]; then
    # OpnDoor's own current ports count as free when changing settings
    read -r -a KEEP_PORTS <<<"$(env_get INSTALL_PORTS | tr ',' ' ')"
  fi
  scan_ports
  if [ -n "$PORTS_IN_USE" ]; then
    say "  In use: ${YEL}${PORTS_IN_USE}${R}"
  else
    say "  No ports in use."
  fi
  note "OpnDoor will only use ports that are free."

  # -------- HTTPS mode
  step "How should people reach OpnDoor?"
  say "  1) ${B}Secure (HTTPS) with a built-in certificate${R}  ${DIM}(recommended; uses Caddy, a self-signed certificate)${R}"
  say "  2) ${B}I have my own reverse proxy${R}  ${DIM}(nginx, Traefik, Nginx Proxy Manager, Cloudflare Tunnel, ...)${R}"
  say "  3) ${B}No HTTPS, for testing only${R}  ${DIM}(passwords travel unencrypted; Microsoft sign-in won't work)${R}"
  local cur_mode; cur_mode=$(env_get INSTALL_MODE)
  local def_mode=1; [ "$cur_mode" = "proxy" ] && def_mode=2; [ "$cur_mode" = "http" ] && def_mode=3
  choose "1 2 3" "$def_mode"
  case "$REPLY" in 1) MODE=caddy ;; 2) MODE=proxy ;; 3) MODE=http ;; esac

  local ip; ip=$(detect_ip)
  local host; host=$(hostname -s 2>/dev/null || hostname)
  HTTPS_PORT=""; HTTP_PORT=""; APP_PORT=""; BIND="0.0.0.0"
  case "$MODE" in
    caddy)
      say ""
      say "  This machine's address is ${B}$ip${R}."
      ask "  Address people will type to reach OpnDoor (an IP address or a name like doors.local)" "$(env_get INSTALL_ADDRESS | grep . || echo "$ip")"
      ADDRESS="$REPLY"
      local def443; def443=$(port_free 443 "${KEEP_PORTS[@]}" && echo 443 || next_free 8443 "${KEEP_PORTS[@]}")
      [ "$def443" != 443 ] && warn "Port 443 (the usual HTTPS port) is taken, so I suggest $def443."
      ask_port "  HTTPS port" "$def443"; HTTPS_PORT="$PORT"
      if port_free 80 "${KEEP_PORTS[@]}" && [ "$HTTPS_PORT" != 80 ]; then
        HTTP_PORT=80
        note "Port 80 is free: plain http:// visits will be sent to the secure address."
      else
        note "Port 80 is taken, so plain http:// visits won't be redirected. People need to type https://."
      fi
      PUBLIC_URL="https://$ADDRESS"; [ "$HTTPS_PORT" != 443 ] && PUBLIC_URL="$PUBLIC_URL:$HTTPS_PORT"
      SECURE=true
      ;;
    proxy)
      say ""
      ask_port "  Port OpnDoor should listen on (your proxy forwards to it)" "$(next_free "$(env_get INSTALL_APP_PORT | grep . || echo 8080)" "${KEEP_PORTS[@]}")"
      APP_PORT="$PORT"
      if yes_no "  Is your reverse proxy on this same machine?" Y; then
        BIND="127.0.0.1"; note "OpnDoor will only accept connections from this machine (your proxy)."
      else
        note "Make sure only your proxy can reach port $APP_PORT (firewall)."
      fi
      while true; do
        ask "  The https:// address people will use (what your proxy serves, e.g. https://doors.example.org)" "$( [ "$cur_mode" = proxy ] && env_get PUBLIC_URL)"
        case "$REPLY" in https://*|http://*) break ;; *) say "  Please start it with https://" ;; esac
      done
      PUBLIC_URL="${REPLY%/}"; ADDRESS="$PUBLIC_URL"
      SECURE=true; [[ "$PUBLIC_URL" == http://* ]] && SECURE=false
      note "Point your proxy at http://$( [ "$BIND" = 127.0.0.1 ] && echo 127.0.0.1 || echo "$ip" ):$APP_PORT"
      ;;
    http)
      say ""
      ask_port "  Port for OpnDoor" "$(next_free "$(env_get INSTALL_APP_PORT | grep . || echo 8080)" "${KEEP_PORTS[@]}")"
      APP_PORT="$PORT"
      ADDRESS="$ip"; PUBLIC_URL="http://$ip:$APP_PORT"; SECURE=false
      warn "Testing only: don't use this for real doors on a shared network."
      ;;
  esac

  # -------- timezone
  step "Timezone"
  ask "  Timezone for schedules (you can set a different one for each site later)" "$(env_get TZ | grep . || detect_tz)"
  TZ_NAME="$REPLY"
  [ -f "/usr/share/zoneinfo/$TZ_NAME" ] || [ -n "$(env_get TZ)" ] || warn "I don't recognise \"$TZ_NAME\". OpnDoor will check it; you can fix it later under Sites."

  # -------- UniFi console
  step "Your UniFi Access console"
  UNIFI_HOST="$(env_get UNIFI_ACCESS_HOST)"; UNIFI_TOKEN="$(env_get UNIFI_ACCESS_TOKEN)"; SITE_NAME="$(env_get UNIFI_SITE_NAME)"
  if [ "$RECONFIGURE" = 1 ]; then
    note "Consoles are managed in OpnDoor under Sites now; skipping."
  else
    say "  OpnDoor needs your console's address and an API token to see and control doors."
    note "Make the token in UniFi Access: Settings → General → Advanced → API Token"
    note "(Locations: Edit, Devices: View; add Users: View and Access Policies: View to let"
    note "people's doors follow their UniFi Access permissions). You can also skip this and add consoles later"
    note "in OpnDoor: it asks for them right after you first sign in."
    if yes_no "  Connect a console now?" Y; then
      while true; do
        ask "  Console IP address or name (e.g. 192.168.1.1)" "${UNIFI_HOST#https://}"
        local h="${REPLY#https://}"; h="${h#http://}"; h="${h%/}"
        [[ "$h" == *:* ]] || h="$h:12445"
        UNIFI_HOST="https://$h"
        ask_secret "  API token"
        UNIFI_TOKEN="$REPLY"
        ask "  Name for this site" "${SITE_NAME:-Main site}"
        SITE_NAME="$REPLY"
        printf '  Testing the connection... '
        local resp
        resp=$(curl -sk --noproxy '*' -m 10 -H "Authorization: Bearer $UNIFI_TOKEN" "$UNIFI_HOST/api/v1/developer/doors" 2>&1 || true)
        if printf '%s' "$resp" | grep -q '"code":"SUCCESS"'; then
          local n; n=$(printf '%s' "$resp" | grep -o '"full_name"' | wc -l | tr -d ' ')
          printf '%sworks%s, found %s doors\n' "$GRN" "$R" "$n"
          break
        fi
        printf '%sdidn'"'"'t work%s\n' "$RED" "$R"
        if [ -z "$resp" ]; then note "No answer from $UNIFI_HOST. Check the address, and that this machine can reach the console."
        elif printf '%s' "$resp" | grep -qi 'unauthor\|401\|token'; then note "The console said the token isn't valid. Use a token from the UniFi Access app"
          note "(Settings → General → Advanced → API Token), not a UniFi OS API key."
        else note "Unexpected answer: $(printf '%s' "$resp" | head -c 150)"; fi
        if ! yes_no "  Try again?" Y; then
          if yes_no "  Keep these details anyway (you can fix them later under Sites)?" N; then break; fi
          UNIFI_HOST=""; UNIFI_TOKEN=""; break
        fi
      done
    fi
  fi

  # -------- Microsoft sign-in
  step "Sign in with Microsoft (optional)"
  MS_TENANT="$(env_get MS_TENANT_ID)"; MS_CLIENT="$(env_get MS_CLIENT_ID)"; MS_SECRET="$(env_get MS_CLIENT_SECRET)"
  local redirect="$PUBLIC_URL/auth/microsoft/callback"
  if [ "$MODE" = http ]; then
    note "Needs HTTPS, so it's skipped in testing mode."
    MS_TENANT=""; MS_CLIENT=""; MS_SECRET=""
  else
    say "  Lets people sign in with their work Microsoft account. You can set it up later too."
    local def_ms=N; [ -n "$MS_CLIENT" ] && def_ms=Y
    if yes_no "  Set up Microsoft sign-in now?" "$def_ms"; then
      say ""
      say "  In ${B}https://entra.microsoft.com${R} → Applications → App registrations → New registration:"
      say "    Name: OpnDoor · Accounts in this organizational directory only"
      say "    Redirect URI: Web → ${B}$redirect${R}"
      say "  Then Certificates & secrets → New client secret."
      say ""
      ask "  Directory (tenant) ID" "$MS_TENANT"; MS_TENANT="$REPLY"
      ask "  Application (client) ID" "$MS_CLIENT"; MS_CLIENT="$REPLY"
      ask_secret "  Client secret value${MS_SECRET:+ (press Enter to keep the current one)}"
      [ -n "$REPLY" ] && MS_SECRET="$REPLY"
      if [ -z "$MS_TENANT" ] || [ -z "$MS_CLIENT" ] || [ -z "$MS_SECRET" ]; then
        warn "Something was left blank, so Microsoft sign-in stays off for now."
        MS_TENANT=""; MS_CLIENT=""; MS_SECRET=""
      fi
    else
      MS_TENANT=""; MS_CLIENT=""; MS_SECRET=""
    fi
  fi

  # -------- live video
  step "Live camera video (optional)"
  say "  Lets people watch a door's UniFi Protect camera live in OpnDoor, for example a bay door"
  say "  going up after they open it. Adds a small helper container (go2rtc). You also paste a"
  say "  UniFi OS API key for Protect into each site in OpnDoor. Without this, cameras show a"
  say "  picture that refreshes every second."
  local def_live=N; [ -n "$(env_get LIVE_VIDEO_URL)" ] && def_live=Y
  LIVE=""
  if yes_no "  Turn on live video?" "$def_live"; then LIVE=1; fi

  # -------- write files
  step "Setting up $INSTALL_DIR"
  mkdir -p "$INSTALL_DIR/data"
  chown "$APP_UID:$APP_UID" "$INSTALL_DIR/data"
  local setup_code secret
  setup_code="$(env_get SETUP_CODE)"; [ -z "$setup_code" ] && setup_code="$(rand 4)-$(rand 4)"
  secret="$(env_get SECRET_KEY)"; [ -z "$secret" ] && secret="$(rand 48)"
  local ports_used
  ports_used=$(printf '%s,' $HTTPS_PORT $HTTP_PORT $APP_PORT | sed 's/,$//')

  umask 077
  cat >"$INSTALL_DIR/.env" <<EOF
# OpnDoor settings, written by install.sh on $(date '+%Y-%m-%d %H:%M').
# Run the installer again to change them, or edit and run: docker compose up -d

# One-time code for creating the main admin account at first sign-in
SETUP_CODE=$setup_code
SECRET_KEY=$secret
TZ=$TZ_NAME
PUBLIC_URL=$PUBLIC_URL
SECURE_COOKIES=$SECURE

# First UniFi Access console (only read on first start; manage consoles under Sites)
UNIFI_ACCESS_HOST=${UNIFI_HOST}
UNIFI_ACCESS_TOKEN=${UNIFI_TOKEN:-changeme}
UNIFI_VERIFY_TLS=false
UNIFI_SITE_NAME=${SITE_NAME:-Main site}

# Sign in with Microsoft (blank = off)
MS_TENANT_ID=$MS_TENANT
MS_CLIENT_ID=$MS_CLIENT
MS_CLIENT_SECRET=$MS_SECRET

# Live camera video through the go2rtc helper (blank = off)
LIVE_VIDEO_URL=${LIVE:+http://go2rtc:1984}

# Used by the installer
INSTALL_MODE=$MODE
INSTALL_ADDRESS=$ADDRESS
INSTALL_APP_PORT=$APP_PORT
INSTALL_PORTS=$ports_used
EOF
  chmod 600 "$INSTALL_DIR/.env"
  umask 022
  ok "Settings saved in $INSTALL_DIR/.env"

  {
    printf '# OpnDoor, written by install.sh. Re-run the installer to change it.\n'
    printf 'services:\n'
    printf '  opndoor:\n'
    printf '    image: %s\n' "$OPNDOOR_IMAGE"
    printf '    container_name: opndoor\n'
    printf '    restart: unless-stopped\n'
    printf '    env_file: .env\n'
    printf '    environment:\n      TZ: "%s"\n' "$TZ_NAME"
    printf '    volumes:\n      - ./data:/data\n'
    if [ "$MODE" != caddy ]; then
      printf '    ports:\n      - "%s:%s:8080"\n' "$BIND" "$APP_PORT"
    fi
    if [ -n "$LIVE" ]; then
      printf '\n  go2rtc:\n'
      printf '    image: %s\n' "$GO2RTC_IMAGE"
      printf '    container_name: opndoor-go2rtc\n'
      printf '    restart: unless-stopped\n'
      printf '    # no ports: only OpnDoor talks to it, and it checks who may watch which door\n'
      printf '    volumes:\n      - ./go2rtc.yaml:/config/go2rtc.yaml:ro\n'
    fi
    if [ "$MODE" = caddy ]; then
      printf '\n  caddy:\n'
      printf '    image: %s\n' "$CADDY_IMAGE"
      printf '    container_name: opndoor-caddy\n'
      printf '    restart: unless-stopped\n'
      printf '    depends_on: [opndoor]\n'
      printf '    ports:\n      - "%s:%s"\n' "$HTTPS_PORT" "$HTTPS_PORT"
      [ -n "$HTTP_PORT" ] && printf '      - "%s:%s"\n' "$HTTP_PORT" "$HTTP_PORT"
      printf '    volumes:\n'
      printf '      - ./Caddyfile:/etc/caddy/Caddyfile:ro\n'
      printf '      - ./caddy/data:/data\n'
      printf '      - ./caddy/config:/config\n'
    fi
  } >"$INSTALL_DIR/docker-compose.yml"
  ok "Wrote docker-compose.yml"

  if [ -n "$LIVE" ]; then
    cat >"$INSTALL_DIR/go2rtc.yaml" <<'EOF2'
# go2rtc, the live video helper for OpnDoor. Written by install.sh.
# OpnDoor adds each door camera's stream when someone watches it.
api:
  listen: ":1984"
rtsp:
  listen: ""
webrtc:
  listen: ""
log:
  level: warn
EOF2
    ok "Wrote go2rtc.yaml (live video on)"
  else
    rm -f "$INSTALL_DIR/go2rtc.yaml"
  fi

  if [ "$MODE" = caddy ]; then
    local sites="https://$ADDRESS"
    [ "$ADDRESS" != "$ip" ] && sites="$sites, https://$ip"
    [ "$ADDRESS" != "$host" ] && [ "$ip" != "$host" ] && sites="$sites, https://$host"
    {
      printf '# HTTPS for OpnDoor with a certificate Caddy makes itself (self-signed).\n'
      printf '{\n'
      printf '\tlocal_certs\n\tskip_install_trust\n'
      printf '\thttps_port %s\n' "$HTTPS_PORT"
      if [ -n "$HTTP_PORT" ]; then printf '\thttp_port %s\n' "$HTTP_PORT"; else printf '\tauto_https disable_redirects\n'; fi
      printf '\tdefault_sni %s\n' "$ADDRESS"
      printf '}\n\n'
      printf '%s {\n\ttls internal\n\treverse_proxy opndoor:8080\n}\n' "$sites"
    } >"$INSTALL_DIR/Caddyfile"
    mkdir -p "$INSTALL_DIR/caddy/data" "$INSTALL_DIR/caddy/config"
    ok "Wrote Caddyfile (HTTPS on port $HTTPS_PORT)"
  else
    rm -f "$INSTALL_DIR/Caddyfile"
  fi

  cat >"$INSTALL_DIR/update.sh" <<EOF
#!/usr/bin/env bash
# Update OpnDoor to the newest version. Keeps all your data and settings.
set -e
cd "\$(dirname "\$0")"
docker compose pull
docker compose up -d --remove-orphans
docker image prune -f >/dev/null
echo "OpnDoor is up to date."
EOF
  chmod +x "$INSTALL_DIR/update.sh"

  # -------- firewall
  local open_ports="$ports_used"
  [ "$BIND" = 127.0.0.1 ] && open_ports=""
  if [ -n "$open_ports" ]; then
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
      step "Firewall"
      if yes_no "  The firewall (ufw) is on. Allow OpnDoor's port(s) $open_ports?" Y; then
        for p in ${open_ports//,/ }; do ufw allow "$p/tcp" >/dev/null && ok "Allowed port $p"; done
      fi
    elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
      step "Firewall"
      if yes_no "  The firewall (firewalld) is on. Allow OpnDoor's port(s) $open_ports?" Y; then
        for p in ${open_ports//,/ }; do firewall-cmd -q --permanent --add-port="$p/tcp" && ok "Allowed port $p"; done
        firewall-cmd -q --reload
      fi
    fi
  fi

  # -------- go
  pull_images
  start_and_wait

  if [ "$MODE" = caddy ]; then
    printf '  Checking HTTPS'
    local i
    for i in $(seq 1 20); do
      if curl -sk --noproxy '*' -m 3 --resolve "$ADDRESS:$HTTPS_PORT:127.0.0.1" "https://$ADDRESS:$HTTPS_PORT/health" 2>/dev/null | grep -q '"ok"'; then
        printf '\n'; ok "HTTPS is working"; break
      fi
      printf '.'; sleep 2
    done
    [ "$i" = 20 ] && { printf '\n'; warn "HTTPS isn't answering yet. Check: cd $INSTALL_DIR && docker compose logs caddy"; }
    local ca="$INSTALL_DIR/caddy/data/caddy/pki/authorities/local/root.crt"
    [ -f "$ca" ] && cp "$ca" "$INSTALL_DIR/opndoor-certificate.crt" && chmod 644 "$INSTALL_DIR/opndoor-certificate.crt"
  fi

  # -------- done
  local line="────────────────────────────────────────────────────────────────"
  printf '\n%s%s%s\n' "$GRN" "$line" "$R"
  printf '%s  OpnDoor is installed and running!%s\n' "$B$GRN" "$R"
  printf '%s%s%s\n\n' "$GRN" "$line" "$R"
  if [ "$RECONFIGURE" = 1 ]; then
    say "  Open:        ${B}$PUBLIC_URL${R}"
    say "  Your accounts and schedules are unchanged."
  else
    say "  1. Open      ${B}$PUBLIC_URL${R}"
    say "  2. Enter the setup code:  ${B}${YEL}$setup_code${R}"
    say "  3. Create your main admin account. That's it."
  fi
  say ""
  if [ "$MODE" = caddy ]; then
    say "  ${B}Your browser will warn that the connection isn't private.${R} That's expected:"
    say "  OpnDoor made its own certificate. Click ${B}Advanced → Proceed${R} (Chrome/Edge) or"
    say "  ${B}Advanced → Accept the risk${R} (Firefox). To stop the warning on your computers, install"
    say "  $INSTALL_DIR/opndoor-certificate.crt as a trusted certificate."
    say ""
  fi
  if [ "$MODE" = proxy ]; then
    say "  Point your reverse proxy at ${B}http://$( [ "$BIND" = 127.0.0.1 ] && echo 127.0.0.1 || echo "$ip" ):$APP_PORT${R}"
    say ""
  fi
  if [ -z "$UNIFI_HOST" ] && [ "$RECONFIGURE" = 0 ]; then
    say "  No console connected yet: after you sign in, OpnDoor will ask for your console's"
    say "  address and API token."
    say ""
  fi
  if [ -n "$MS_CLIENT" ]; then
    say "  Microsoft sign-in is on. Redirect URL registered in Entra: $redirect"
    say ""
  fi
  say "  ${DIM}Files:      $INSTALL_DIR${R}"
  say "  ${DIM}Update:     sudo $INSTALL_DIR/update.sh${R}"
  say "  ${DIM}Settings:   run this installer again${R}"
  say "  ${DIM}Logs:       cd $INSTALL_DIR && sudo docker compose logs -f${R}"
  say ""
}

main "$@"
