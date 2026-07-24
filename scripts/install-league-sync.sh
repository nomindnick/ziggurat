#!/usr/bin/env bash
# Install the Ziggurat league-state sync timer (item 3.1) as a systemd USER unit.
#
# User-level on purpose: no root, no system-wide side effects, and it reads the
# repo's own .env and virtualenv. Uninstall is one command (printed at the end).
#
#   scripts/install-league-sync.sh [--season 2026] [--dry-run] [--uninstall]
#
# Requires a graphical/lingering session to fire when logged out:
#   loginctl enable-linger "$USER"     # once, if this box runs headless
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SEASON=""
DRY_RUN=0
UNINSTALL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --season) SEASON="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ $UNINSTALL -eq 1 ]]; then
  systemctl --user disable --now ziggurat-league-sync.timer 2>/dev/null || true
  rm -f "$UNIT_DIR/ziggurat-league-sync.timer" "$UNIT_DIR/ziggurat-league-sync.service"
  systemctl --user daemon-reload
  echo "removed ziggurat-league-sync timer + service"
  exit 0
fi

# Default the season the same way the CLI does (NFL season year, not calendar
# year — January belongs to the previous season).
if [[ -z "$SEASON" ]]; then
  SEASON="$("$REPO/.venv/bin/python" -c \
    'from datetime import date; from ziggurat.data.asof import nfl_season_of; print(nfl_season_of(date.today()))')"
fi

# Fail EARLY and legibly rather than installing a timer that will fail silently
# four times a day.
[[ -x "$REPO/.venv/bin/ziggurat" ]] || { echo "missing $REPO/.venv/bin/ziggurat — create the venv first" >&2; exit 1; }
[[ -f "$REPO/.env" ]] || { echo "missing $REPO/.env (SWID / ESPN_S2 / ESPN_LEAGUE_ID)" >&2; exit 1; }

render() {
  sed -e "s|@REPO@|$REPO|g" -e "s|@SEASON@|$SEASON|g" "$REPO/scripts/systemd/$1"
}

if [[ $DRY_RUN -eq 1 ]]; then
  echo "--- would install to $UNIT_DIR ---"
  render ziggurat-league-sync.service
  render ziggurat-league-sync.timer
  exit 0
fi

mkdir -p "$UNIT_DIR"
render ziggurat-league-sync.service > "$UNIT_DIR/ziggurat-league-sync.service"
render ziggurat-league-sync.timer   > "$UNIT_DIR/ziggurat-league-sync.timer"

systemctl --user daemon-reload
systemctl --user enable --now ziggurat-league-sync.timer

echo "installed: $UNIT_DIR/ziggurat-league-sync.{service,timer}  (season $SEASON)"
echo
systemctl --user list-timers ziggurat-league-sync.timer --no-pager || true
cat <<EOF

next steps
  run one now      : systemctl --user start ziggurat-league-sync.service
  watch it         : journalctl --user -u ziggurat-league-sync.service -n 50 -f
  check coverage   : $REPO/.venv/bin/ziggurat league status
  survive logout   : loginctl enable-linger "\$USER"
  uninstall        : scripts/install-league-sync.sh --uninstall

Cron alternative (if this box has no user systemd):
  15 5,11,17,23 * * * cd $REPO && .venv/bin/ziggurat league sync --season $SEASON >> data/league-sync.log 2>&1
EOF
