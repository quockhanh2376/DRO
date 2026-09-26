#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd)"
DEST_DIR="${DRO_INSTALL_DIR:-/opt/dro}"
SERVICE_NAME="${DRO_SERVICE_NAME:-dro}"
DATABASE_PATH="${DRO_DATABASE_PATH:-/var/lib/dro/dro.db}"
LOG_FILE="${DRO_LOG_FILE:-/var/log/dro/dro.log}"
LOGROTATE_NAME="${DRO_LOGROTATE_NAME:-dro}"
LISTEN_PORT="${DRO_LISTEN_PORT:-8000}"
ENV_FILE="${DRO_ENV_FILE:-/etc/dro/dro.env}"
PYTHON="${DRO_PYTHON:-python3.12}"
DATABASE_URL="sqlite:////${DATABASE_PATH#/}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi
[[ "$DEST_DIR" == /opt/dro || "$DEST_DIR" =~ ^/opt/dro/[A-Za-z0-9._/-]+$ ]] || { echo "DRO_INSTALL_DIR must be under /opt/dro." >&2; exit 1; }
[[ "$DATABASE_PATH" =~ ^/var/lib/dro/[A-Za-z0-9._/-]+$ ]] || { echo "DRO_DATABASE_PATH must be under /var/lib/dro." >&2; exit 1; }
[[ "$LOG_FILE" =~ ^/var/log/dro/[A-Za-z0-9._/-]+$ ]] || { echo "DRO_LOG_FILE must be under /var/log/dro." >&2; exit 1; }
[[ "$ENV_FILE" =~ ^/etc/dro/[A-Za-z0-9._/-]+$ ]] || { echo "DRO_ENV_FILE must be under /etc/dro." >&2; exit 1; }
[[ "$SERVICE_NAME" =~ ^[A-Za-z0-9_.@-]+$ && "$LOGROTATE_NAME" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "Invalid unit or logrotate name." >&2; exit 1; }
[[ "$LISTEN_PORT" =~ ^[0-9]{1,5}$ ]] && (( LISTEN_PORT >= 1024 && LISTEN_PORT <= 65535 )) || { echo "DRO_LISTEN_PORT must be between 1024 and 65535." >&2; exit 1; }
command -v "$PYTHON" >/dev/null || { echo "$PYTHON is required (Python 3.12+)." >&2; exit 1; }
"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' || { echo "Python 3.12 or newer is required." >&2; exit 1; }

getent group dro >/dev/null || groupadd --system dro
id -u dro >/dev/null 2>&1 || useradd --system --gid dro --home-dir /var/lib/dro --shell /usr/sbin/nologin dro
install -d -o root -g root -m 0755 "$DEST_DIR"
install -d -o dro -g dro -m 0750 /var/lib/dro /var/log/dro
install -d -o dro -g dro -m 0750 "$(dirname "$DATABASE_PATH")"
install -d -o dro -g dro -m 0750 "$(dirname "$LOG_FILE")"
if [[ ! -e "$LOG_FILE" ]]; then install -o dro -g dro -m 0640 /dev/null "$LOG_FILE"; fi
chown dro:dro "$LOG_FILE"
chmod 0640 "$LOG_FILE"
install -d -o root -g root -m 0700 /etc/dro
if [[ ! -e "$ENV_FILE" ]]; then install -o root -g root -m 0600 /dev/null "$ENV_FILE"; fi

for item in app migrations scripts systemd tests pyproject.toml alembic.ini README.md; do
  if [[ -d "$SOURCE_DIR/$item" ]]; then
    install -d "$DEST_DIR/$item"
    cp -a "$SOURCE_DIR/$item/." "$DEST_DIR/$item/"
  else
    install -m 0644 "$SOURCE_DIR/$item" "$DEST_DIR/$item"
  fi
done
chown -R root:root "$DEST_DIR"
chmod -R u=rwX,go=rX "$DEST_DIR"
"$PYTHON" -m venv "$DEST_DIR/.venv"
"$DEST_DIR/.venv/bin/pip" install "$DEST_DIR"

render_template() {
  sed \
    -e "s|@DRO_INSTALL_DIR@|$DEST_DIR|g" \
    -e "s|@DRO_SERVICE_NAME@|$SERVICE_NAME|g" \
    -e "s|@DRO_DATABASE_URL@|$DATABASE_URL|g" \
    -e "s|@DRO_ENV_FILE@|$ENV_FILE|g" \
    -e "s|@DRO_LISTEN_PORT@|$LISTEN_PORT|g" \
    -e "s|@DRO_LOG_FILE@|$LOG_FILE|g" "$1"
}
unit_tmp="$(mktemp)"
logrotate_tmp="$(mktemp)"
trap 'rm -f "$unit_tmp" "$logrotate_tmp"' EXIT
render_template "$DEST_DIR/systemd/dro.service.in" > "$unit_tmp"
render_template "$DEST_DIR/scripts/dro-logrotate.in" > "$logrotate_tmp"
install -o root -g root -m 0644 "$unit_tmp" "/etc/systemd/system/$SERVICE_NAME.service"
install -o root -g root -m 0644 "$logrotate_tmp" "/etc/logrotate.d/$LOGROTATE_NAME"
chown root:root "$ENV_FILE"
chmod 0600 "$ENV_FILE"

if ! grep -q '^DRO_ADMIN_PASSWORD=.' "$ENV_FILE" || ! grep -q '^DRO_SESSION_SECRET=.' "$ENV_FILE"; then
  echo "Set DRO_ADMIN_USER, DRO_ADMIN_PASSWORD, and DRO_SESSION_SECRET in $ENV_FILE, then rerun this installer."
  exit 0
fi

cd "$DEST_DIR"
runuser -u dro -- env DRO_DATABASE_URL="$DATABASE_URL" "$DEST_DIR/.venv/bin/alembic" -c "$DEST_DIR/alembic.ini" upgrade head
systemctl daemon-reload
systemctl enable "$SERVICE_NAME.service"
systemctl restart "$SERVICE_NAME.service"
echo "DRO installed under $DEST_DIR using $SERVICE_NAME.service. Existing /opt/dns-optimizer was not touched."
