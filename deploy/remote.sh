#!/usr/bin/env bash
# Server half of deploy/deploy.ps1. deploy.ps1 copies this file to /tmp on the server and runs
# it with sudo, so everything below runs as root:
#
#   sudo bash remote.sh status   [options]
#   sudo bash remote.sh setup    --package /tmp/sbp-ID.tar.gz --id ID [options]   (setup, then deploy)
#   sudo bash remote.sh deploy   --package /tmp/sbp-ID.tar.gz --id ID [options]
#   sudo bash remote.sh rollback [--id RELEASE] [options]
#
# Layout under --base (default /opt/solar-battery-planner):
#
#   releases/<id>/   backend/ frontend/ VERSION, owned by root and read-only to the app
#   current       -> releases/<id>      what systemd and nginx serve
#   venvs/<hash>/    Python environments, reused while requirements.txt is unchanged
#   shared/.env      the backend's settings: written once by setup_env.py, never by a deploy
#   shared/backups/  a database dump taken before each deploy
#
# Every step checks before it changes anything, so setup is safe to run again.

set -euo pipefail
umask 022
cd /

MODE=${1:-}
[[ $# -gt 0 ]] && shift

PACKAGE=""
ID=""
BASE=/opt/solar-battery-planner
APP_USER=solar
SERVICE=solar-battery-planner
DB_NAME=solar
ORIGINS=""
KEEP=5
PORT=8000
WORKERS=2
CRON=no
SELF=$(readlink -f "$0")

if [[ -t 1 ]]; then
  BOLD=$'\e[1m' GREEN=$'\e[32m' YELLOW=$'\e[33m' RED=$'\e[31m' RESET=$'\e[0m'
else
  BOLD="" GREEN="" YELLOW="" RED="" RESET=""
fi
step() { printf '\n%s== %s%s\n' "$BOLD" "$*" "$RESET"; }
ok() { printf '   %sok%s    %s\n' "$GREEN" "$RESET" "$*"; }
info() { printf '         %s\n' "$*"; }
warn() { printf '   %swarn%s  %s\n' "$YELLOW" "$RESET" "$*"; }
missing() { printf '   %sno%s    %s\n' "$RED" "$RESET" "$*"; }
die() {
  printf '\n%sFAILED:%s %s\n' "$RED" "$RESET" "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --package) PACKAGE=$2; shift 2 ;;
    --id) ID=$2; shift 2 ;;
    --base) BASE=${2%/}; shift 2 ;;
    --user) APP_USER=$2; shift 2 ;;
    --service) SERVICE=$2; shift 2 ;;
    --db) DB_NAME=$2; shift 2 ;;
    --origins) ORIGINS=$2; shift 2 ;;
    --keep) KEEP=$2; shift 2 ;;
    --port) PORT=$2; shift 2 ;;
    --workers) WORKERS=$2; shift 2 ;;
    --cron) CRON=$2; shift 2 ;;
    *) die "unknown option $1" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "run this with sudo"
[[ -z $ID || $ID =~ ^[A-Za-z0-9._-]+$ ]] || die "bad release id '$ID'"
for name in "$APP_USER" "$SERVICE" "$DB_NAME"; do
  [[ $name =~ ^[a-z_][a-z0-9_-]*$ ]] || die "'$name' isn't a safe user, service or database name"
done
[[ $KEEP =~ ^[0-9]+$ && $KEEP -ge 2 ]] || die "--keep must be 2 or more"
[[ $PORT =~ ^[0-9]+$ && $WORKERS =~ ^[0-9]+$ ]] || die "--port and --workers must be numbers"
[[ $BASE == /* ]] || die "--base must be an absolute path"

RELEASES=$BASE/releases
VENVS=$BASE/venvs
SHARED=$BASE/shared
ENV_FILE=$SHARED/.env
BACKUPS=$SHARED/backups
CURRENT=$BASE/current
LEGACY=$BASE/app # the git checkout the README's manual steps create
UNIT=/etc/systemd/system/$SERVICE.service
DEPLOY_LOG=$BASE/deploys.log

# The uploaded files are one-shot: remove them however this run ends.
cleanup() {
  [[ -n $PACKAGE && $PACKAGE == /tmp/sbp-* ]] && rm -f "$PACKAGE"
  [[ $SELF == /tmp/sbp-* ]] && rm -f "$SELF"
  return 0
}
trap cleanup EXIT

as_app() { runuser -u "$APP_USER" -- "$@"; }

user_exists() { id -u "$APP_USER" >/dev/null 2>&1; }

pkg_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'install ok installed'; }

# What the app needs, checked by function rather than package name, so a Postgres or Python
# installed some other way counts too. Ubuntu's python3 can't make venvs without python3-venv.
have() {
  case $1 in
    python3-venv) python3 -c 'import ensurepip' 2>/dev/null ;;
    postgresql) command -v pg_ctlcluster >/dev/null || command -v pg_ctl >/dev/null ;;
    *) pkg_installed "$1" ;;
  esac
}

# Python of at least 3.12 (Ubuntu 24.04), which the pinned requirements need.
python_ok() { python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; }

psql_value() { runuser -u postgres -- psql -tAc "$1" 2>/dev/null || true; }

# A setting from shared/.env, without quotes. Empty if unset.
env_value() {
  [[ -f $ENV_FILE ]] || return 0
  sed -n "s/^[[:space:]]*\(export[[:space:]]\+\)\?$1=//p" "$ENV_FILE" | tail -n1 |
    sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

current_release() { [[ -L $CURRENT ]] && basename "$(readlink -f "$CURRENT")" || true; }

health_ok() {
  python3 - "$PORT" <<'PY' 2>/dev/null
import sys, urllib.request
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/health", timeout=3) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
}

wait_healthy() {
  local i
  for i in $(seq 1 40); do
    if health_ok; then return 0; fi
    sleep 1
  done
  return 1
}

unit_text() {
  cat <<EOF
# Written by deploy/remote.sh. Re-running setup rewrites it (keeping a .bak copy).
[Unit]
Description=Solar Battery Planner API
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$CURRENT/backend
ExecStart=$CURRENT/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $PORT --workers $WORKERS --proxy-headers
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
}

CRON_LINE="*/5 * * * * cd $CURRENT/backend && .venv/bin/python -m app.control.nightly"

# ------------------------------------------------------------------------------------ status

do_status() {
  step "Server"
  info "$(. /etc/os-release && echo "$PRETTY_NAME"), Python $(python3 -c 'import platform; print(platform.python_version())' 2>/dev/null || echo 'not found')"
  python_ok && ok "Python 3.12 or newer" || missing "Python 3.12 or newer (Ubuntu 24.04 has it)"
  for p in python3-venv postgresql nginx; do
    have "$p" && ok "package $p" || missing "package $p"
  done

  step "App"
  user_exists && ok "service user $APP_USER" || missing "service user $APP_USER"
  if [[ -d $RELEASES ]]; then
    ok "release folders in $BASE ($(find "$RELEASES" -mindepth 1 -maxdepth 1 -type d | wc -l) releases)"
  else
    missing "release folders in $BASE"
  fi
  local cur
  cur=$(current_release)
  [[ -n $cur ]] && ok "current release $cur" || missing "no release deployed yet"
  if [[ -d $LEGACY ]]; then
    warn "an older manual install (README steps) is at $LEGACY"
  fi

  step "Settings and database"
  if [[ -f $ENV_FILE ]]; then
    ok "settings file $ENV_FILE"
    local url
    url=$(env_value SBP_DATABASE_URL)
    case $url in
      postgresql*) ok "settings use Postgres" ;;
      "") warn "no SBP_DATABASE_URL set, so the backend would use SQLite in the release folder" ;;
      *) warn "settings use ${url%%:*}, not Postgres" ;;
    esac
  elif [[ -f $LEGACY/backend/.env ]]; then
    warn "settings only in the old install ($LEGACY/backend/.env); setup will copy them"
  else
    missing "settings file (setup runs setup_env.py to write it)"
  fi
  if have postgresql; then
    [[ $(psql_value "select 1 from pg_roles where rolname='$APP_USER'") == 1 ]] &&
      ok "Postgres role $APP_USER" || missing "Postgres role $APP_USER"
    [[ $(psql_value "select 1 from pg_database where datname='$DB_NAME'") == 1 ]] &&
      ok "Postgres database $DB_NAME" || missing "Postgres database $DB_NAME"
  fi

  step "Service"
  if [[ -f $UNIT ]]; then
    ok "systemd unit $UNIT"
    grep -q "WorkingDirectory=$CURRENT/backend" "$UNIT" ||
      warn "the unit doesn't run from $CURRENT (setup will update it)"
    info "enabled: $(systemctl is-enabled "$SERVICE" 2>/dev/null || true), state: $(systemctl is-active "$SERVICE" 2>/dev/null || true)"
  else
    missing "systemd unit $UNIT"
  fi
  health_ok && ok "health check on 127.0.0.1:$PORT" || missing "health check on 127.0.0.1:$PORT"
  if user_exists && crontab -u "$APP_USER" -l 2>/dev/null | grep -q 'app.control.nightly'; then
    info "nightly auto-apply cron: on"
  else
    info "nightly auto-apply cron: off"
  fi
  if have nginx; then
    info "nginx: $(systemctl is-active nginx 2>/dev/null || true) (configured by you)"
  fi

  step "Summary"
  if [[ -n $cur && -f $ENV_FILE && -f $UNIT ]]; then
    ok "set up: deploy with .\\deploy\\deploy.ps1"
  else
    info "not fully set up: run .\\deploy\\deploy.ps1 -Setup (it skips whatever already exists)"
  fi
}

# ------------------------------------------------------------------------------------- setup

do_setup() {
  step "Packages"
  command -v apt-get >/dev/null || die "this script expects Ubuntu (apt-get not found)"
  python_ok || die "Python 3.12 or newer is needed (Ubuntu 24.04 ships it); found $(python3 --version 2>&1)"
  local want=() p
  for p in python3-venv postgresql; do have "$p" || want+=("$p"); done
  if ((${#want[@]})); then
    info "installing ${want[*]}"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${want[@]}"
  fi
  ok "python3-venv and postgresql installed"

  step "Service user and folders"
  if user_exists; then
    ok "user $APP_USER exists"
  else
    useradd --system --create-home --home-dir "$BASE" --shell /usr/sbin/nologin "$APP_USER"
    ok "created system user $APP_USER (home $BASE)"
  fi
  install -d -o "$APP_USER" -g "$APP_USER" -m 755 "$BASE"
  install -d -o root -g root -m 755 "$RELEASES"
  install -d -o "$APP_USER" -g "$APP_USER" -m 755 "$VENVS"
  install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$SHARED" "$BACKUPS"
  ok "$BASE/{releases,venvs,shared}"
  unpack # setup_env.py below comes from the new release

  step "Postgres"
  systemctl is-active --quiet postgresql || systemctl enable --now postgresql
  if [[ $(psql_value "select 1 from pg_roles where rolname='$APP_USER'") == 1 ]]; then
    ok "role $APP_USER exists"
  else
    runuser -u postgres -- createuser "$APP_USER"
    ok "created role $APP_USER"
  fi
  if [[ $(psql_value "select 1 from pg_database where datname='$DB_NAME'") == 1 ]]; then
    ok "database $DB_NAME exists"
  else
    runuser -u postgres -- createdb -O "$APP_USER" "$DB_NAME"
    ok "created database $DB_NAME owned by $APP_USER"
  fi

  step "Settings ($ENV_FILE)"
  if [[ ! -f $ENV_FILE && -f $LEGACY/backend/.env ]]; then
    cp "$LEGACY/backend/.env" "$ENV_FILE"
    ok "copied the settings from the older install ($LEGACY/backend/.env)"
  fi
  if [[ -f $ENV_FILE ]]; then
    ok "settings file exists; leaving it as it is"
  else
    # Seed what this layout needs; setup_env.py offers these as its defaults and asks the rest.
    {
      echo "SBP_DATABASE_URL=postgresql+psycopg://$APP_USER@/$DB_NAME"
      if [[ -n $ORIGINS ]]; then
        python3 -c 'import json, sys; print("SBP_CORS_ORIGINS=" + json.dumps([o.strip().rstrip("/") for o in sys.argv[1].split(",") if o.strip()]))' "$ORIGINS"
      fi
      echo "SBP_AUTO_APPLY=false"
    } >"$ENV_FILE"
    chown "$APP_USER:$APP_USER" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    info "Now running setup_env.py as $APP_USER. The database URL and origins are filled in;"
    info "press Enter to accept the suggestions, and answer y to \"Overwrite it when done?\"."
    info "Leave auto-apply off: the service runs $WORKERS workers (see the README)."
    echo
    if ! (cd "$RELEASE_DIR/backend" && as_app python3 setup_env.py --output "$ENV_FILE"); then
      rm -f "$ENV_FILE"
      die "setup_env.py didn't finish, so no settings were written. Run -Setup again."
    fi
    grep -q '^SBP_JWT_SECRET=' "$ENV_FILE" || {
      rm -f "$ENV_FILE"
      die "settings weren't written (did you answer n to overwrite?). Run -Setup again."
    }
    echo
    ok "settings written. Copy SBP_CREDENTIALS_SECRET into your password manager:"
    info "sudo grep CREDENTIALS $ENV_FILE"
  fi
  chown "$APP_USER:$APP_USER" "$ENV_FILE"
  chmod 600 "$ENV_FILE"

  step "systemd unit ($UNIT)"
  local tmp
  tmp=$(mktemp)
  unit_text >"$tmp"
  if [[ -f $UNIT ]] && cmp -s "$tmp" "$UNIT"; then
    rm -f "$tmp"
    ok "unit is up to date"
  else
    if [[ -f $UNIT ]]; then
      cp "$UNIT" "$UNIT.bak-$(date +%Y%m%d%H%M%S)"
      info "kept a copy of the old unit as $UNIT.bak-*"
    fi
    install -m 644 "$tmp" "$UNIT"
    rm -f "$tmp"
    systemctl daemon-reload
    ok "unit written"
  fi
  systemctl enable --quiet "$SERVICE"
  ok "service enabled at boot"

  step "Nightly auto-apply cron"
  local crontab_now
  crontab_now=$(crontab -u "$APP_USER" -l 2>/dev/null || true)
  if [[ $CRON == yes ]]; then
    if grep -qxF "$CRON_LINE" <<<"$crontab_now"; then
      ok "cron line present"
    else
      { grep -v 'app.control.nightly' <<<"$crontab_now" || true; echo "$CRON_LINE"; } |
        crontab -u "$APP_USER" -
      ok "cron line installed for $APP_USER (every 5 minutes)"
    fi
  elif grep -q 'app.control.nightly' <<<"$crontab_now"; then
    if grep -qxF "$CRON_LINE" <<<"$crontab_now"; then
      info "cron line present (NightlyCron is off in your config, but it's left alone)"
    else
      warn "$APP_USER's crontab runs app.control.nightly from an old path; check it with"
      info "sudo crontab -u $APP_USER -e"
    fi
  else
    info "off (set NightlyCron = \$true in deploy.config.psd1 to turn it on)"
  fi

  if [[ -d $LEGACY ]]; then
    echo
    warn "the older install at $LEGACY is no longer used once this deploy works."
    info "Its database is the same Postgres one; delete the folder when you're happy."
  fi
}

# ------------------------------------------------------------------------------------ deploy

RELEASE_DIR=""

unpack() {
  [[ -n $PACKAGE && -f $PACKAGE ]] || die "package '$PACKAGE' not found"
  [[ -n $ID ]] || die "--id is needed"
  install -d -o root -g root -m 755 "$RELEASES"
  RELEASE_DIR=$RELEASES/$ID
  [[ ! -e $RELEASE_DIR ]] || die "release $ID already exists"
  mkdir "$RELEASE_DIR"
  tar -xzf "$PACKAGE" -C "$RELEASE_DIR" --no-same-owner
  [[ -f $RELEASE_DIR/backend/requirements.txt && -f $RELEASE_DIR/frontend/index.html ]] ||
    die "the package is missing backend/requirements.txt or frontend/index.html"
  chown -R root:root "$RELEASE_DIR"
  chmod -R u=rwX,go=rX "$RELEASE_DIR"
}

# A release that never went live is just deleted when something fails.
discard_unswitched() {
  if [[ -n $RELEASE_DIR && -d $RELEASE_DIR && $(current_release) != "$ID" ]]; then
    rm -rf "$RELEASE_DIR"
  fi
}

switch_to() {
  ln -sfn "$RELEASES/$1" "$BASE/.current.new"
  mv -Tf "$BASE/.current.new" "$CURRENT"
}

restart_and_check() {
  systemctl restart "$SERVICE" || return 1
  wait_healthy
}

do_deploy() {
  step "Checking the server is set up"
  [[ -f $ENV_FILE ]] || die "no $ENV_FILE yet: run .\\deploy\\deploy.ps1 -Setup"
  [[ -f $UNIT ]] || die "no systemd unit yet: run .\\deploy\\deploy.ps1 -Setup"
  grep -q "WorkingDirectory=$CURRENT/backend" "$UNIT" ||
    die "$UNIT doesn't run from $CURRENT: run .\\deploy\\deploy.ps1 -Setup to update it"
  user_exists || die "user $APP_USER is missing: run .\\deploy\\deploy.ps1 -Setup"
  ok "settings, unit and user present"

  [[ -n $RELEASE_DIR ]] || unpack
  trap 'discard_unswitched; cleanup' EXIT
  ok "release unpacked to $RELEASE_DIR"
  [[ -f $RELEASE_DIR/VERSION ]] && sed 's/^/         /' "$RELEASE_DIR/VERSION"

  step "Python environment"
  local hash venv
  hash=$( { cat "$RELEASE_DIR/backend/requirements.txt"; python3 --version; echo 'psycopg[binary]'; } |
    sha256sum | cut -c1-12)
  venv=$VENVS/$hash
  if [[ -f $venv/.complete ]]; then
    ok "requirements unchanged; reusing venvs/$hash"
  else
    rm -rf "$venv"
    install -d -o "$APP_USER" -g "$APP_USER" -m 755 "$VENVS"
    info "installing requirements into venvs/$hash (a minute or two)"
    as_app python3 -m venv "$venv"
    as_app "$venv/bin/pip" install --quiet --disable-pip-version-check \
      -r "$RELEASE_DIR/backend/requirements.txt" 'psycopg[binary]' ||
      { rm -rf "$venv"; die "pip install failed"; }
    as_app touch "$venv/.complete"
    ok "venvs/$hash ready"
  fi
  ln -sfn "$venv" "$RELEASE_DIR/backend/.venv"
  ln -sfn "$ENV_FILE" "$RELEASE_DIR/backend/.env"

  step "Checking settings and database from the new release"
  local db_url
  db_url=$(cd "$RELEASE_DIR/backend" && as_app .venv/bin/python - <<'PY'
import sys
try:
    from app.config import Settings
    settings = Settings()
except Exception as exc:  # unknown or invalid keys in .env
    print(f"The new release can't read the settings file:\n{exc}", file=sys.stderr)
    sys.exit(1)
url = settings.database_url
if url.startswith("sqlite") and not url.startswith("sqlite:////"):
    print("SBP_DATABASE_URL is a relative SQLite path, which would start an empty database in "
          "every release folder. Use Postgres (or an absolute sqlite:////path).", file=sys.stderr)
    sys.exit(1)
from sqlalchemy import create_engine, text
try:
    with create_engine(url).connect() as conn:
        conn.execute(text("SELECT 1"))
except Exception as exc:
    print(f"Can't connect to the database: {exc}", file=sys.stderr)
    sys.exit(1)
print(url)
PY
  ) || die "settings check failed; the running release is untouched. Fix $ENV_FILE (cd $CURRENT/backend && sudo -u $APP_USER python3 setup_env.py --output $ENV_FILE) and deploy again."
  ok "settings load and the database answers"

  step "Backing up the database"
  local backup
  case $db_url in
    postgresql*)
      backup=$BACKUPS/$ID.sql.gz
      # libpq doesn't know SQLAlchemy's "+driver" suffix.
      as_app pg_dump --no-owner "$(sed 's/^postgresql+[a-z0-9]*:/postgresql:/' <<<"$db_url")" |
        gzip >"$backup" || die "pg_dump failed; nothing has been changed"
      ;;
    sqlite*)
      backup=$BACKUPS/$ID.db
      local file=${db_url#sqlite:///}
      if [[ -f $file ]]; then cp "$file" "$backup"; else backup=""; fi
      ;;
    *) backup="" ;;
  esac
  if [[ -n $backup ]]; then
    chown "$APP_USER:$APP_USER" "$backup"
    chmod 600 "$backup"
    ok "$backup ($(du -h "$backup" | cut -f1))"
    ls -1t "$BACKUPS" | tail -n +11 | sed "s|^|$BACKUPS/|" | xargs -r rm -f
  else
    info "nothing to back up yet"
  fi

  step "Switching to $ID"
  local previous
  previous=$(current_release)
  switch_to "$ID"
  ok "current -> releases/$ID"
  if restart_and_check; then
    ok "service restarted and /api/health answers"
    echo "$(date -Is) deployed $ID (was ${previous:-none})" >>"$DEPLOY_LOG"
  else
    echo
    warn "the new release didn't come up healthy. Last log lines:"
    journalctl -u "$SERVICE" -n 40 --no-pager 2>/dev/null | sed 's/^/         /' || true
    echo "$(date -Is) FAILED $ID, rolled back to ${previous:-nothing}" >>"$DEPLOY_LOG"
    if [[ -n $previous && -d $RELEASES/$previous ]]; then
      step "Rolling back to $previous"
      switch_to "$previous"
      if restart_and_check; then
        ok "back on $previous and healthy"
      else
        warn "$previous isn't healthy either; see: journalctl -u $SERVICE -n 100"
      fi
      rm -rf "$RELEASE_DIR"
    fi
    if [[ -n $backup && $db_url == postgresql* ]]; then
      info "The app updates tables itself on startup. If the failed start changed them, restore with:"
      info "  sudo -u postgres dropdb $DB_NAME && sudo -u postgres createdb -O $APP_USER $DB_NAME"
      info "  gunzip -c $backup | sudo -u $APP_USER psql -q $DB_NAME"
    fi
    die "deploy of $ID failed"
  fi

  step "Tidying up"
  local keep_list rel
  keep_list=$(ls -1 "$RELEASES" | sort -r | head -n "$KEEP")
  for rel in $(ls -1 "$RELEASES"); do
    if ! grep -qxF "$rel" <<<"$keep_list" && [[ $rel != "$ID" && $rel != "$previous" ]]; then
      rm -rf "${RELEASES:?}/$rel"
      info "removed old release $rel"
    fi
  done
  local used
  used=$(for rel in "$RELEASES"/*/backend/.venv; do readlink "$rel" || true; done)
  for venv in "$VENVS"/*/; do
    venv=${venv%/}
    [[ -d $venv ]] || continue
    grep -qxF "$venv" <<<"$used" || { rm -rf "$venv"; info "removed unused $(basename "$venv")"; }
  done
  ok "keeping $(ls -1 "$RELEASES" | wc -l) releases"
}

# ---------------------------------------------------------------------------------- rollback

do_rollback() {
  local cur target
  cur=$(current_release)
  [[ -n $cur ]] || die "nothing is deployed yet"
  if [[ -n $ID ]]; then
    target=$ID
  else
    # The newest complete release older than the current one.
    target=$(ls -1 "$RELEASES" | sort | awk -v cur="$cur" '$0 < cur' | while read -r rel; do
      [[ -L $RELEASES/$rel/backend/.venv ]] && echo "$rel"
    done | tail -n1)
  fi
  [[ -n $target ]] || die "there's no earlier release to go back to"
  [[ -L $RELEASES/$target/backend/.venv ]] || die "release $target doesn't exist or never finished"
  [[ $target != "$cur" ]] || die "$target is already current"

  step "Rolling back from $cur to $target"
  info "Code only: the database stays as it is (backups are in $BACKUPS)."
  switch_to "$target"
  if restart_and_check; then
    ok "current -> releases/$target, healthy"
    echo "$(date -Is) rolled back $cur -> $target" >>"$DEPLOY_LOG"
  else
    journalctl -u "$SERVICE" -n 40 --no-pager 2>/dev/null | sed 's/^/         /' || true
    die "$target didn't come up healthy; switch back with: -Rollback -Release $cur"
  fi
}

case $MODE in
  status) do_status ;;
  setup)
    trap 'discard_unswitched; cleanup' EXIT
    do_setup
    do_deploy
    ;;
  deploy) do_deploy ;;
  rollback) do_rollback ;;
  *) die "usage: remote.sh status|setup|deploy|rollback [options]" ;;
esac
echo
printf '%sDone.%s\n' "$GREEN" "$RESET"
