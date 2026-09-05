#!/usr/bin/env bash
# Install the Ziggurat decision-archive timer (item 4.2b) as a systemd USER unit:
#   * ziggurat-decisions.timer — Tuesday 18:30 local, freeze the waiver plan
#
# User-level on purpose: no root, reads the repo's own .env + virtualenv.
#
#   scripts/install-decisions.sh [--season 2026] [--dry-run] [--uninstall]
#
# Requires linger to fire when logged out:  loginctl enable-linger "$USER"
#
# This timer is a SECOND capture, never the only one: every `ziggurat waivers`
# run already archives itself. What the timer adds is the Tuesday the operator
# never opened — and a missed Tuesday is unrecoverable (league_player_state
# accumulates forward only, item 3.1). No network: it reads the local database.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNITS=(ziggurat-decisions)
SEASON=""
DRY_RUN=0
UNINSTALL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --season) SEASON="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ $UNINSTALL -eq 1 ]]; then
  for u in "${UNITS[@]}"; do
    systemctl --user disable --now "$u.timer" 2>/dev/null || true
    rm -f "$UNIT_DIR/$u.timer" "$UNIT_DIR/$u.service"
  done
  systemctl --user daemon-reload
  echo "removed ${UNITS[*]} timer + service"
  echo 'NOTE: `ziggurat waivers` still captures every run — removing this timer only'
  echo '      gives up the Tuesday you never open.'
  exit 0
fi

# Default the season the same way the CLI does (NFL season year, not calendar year).
if [[ -z "$SEASON" ]]; then
  SEASON="$("$REPO/.venv/bin/python" -c \
    'from datetime import date; from ziggurat.data.asof import nfl_season_of; print(nfl_season_of(date.today()))')"
fi

# Fail EARLY rather than installing a timer that fails silently.
[[ -x "$REPO/.venv/bin/ziggurat" ]] || { echo "missing $REPO/.venv/bin/ziggurat — create the venv first" >&2; exit 1; }
[[ -f "$REPO/.env" ]] || { echo "missing $REPO/.env (SWID / ESPN_S2 / ESPN_LEAGUE_ID) — the freeze resolves your team from SWID" >&2; exit 1; }

render() {
  sed -e "s|@REPO@|$REPO|g" -e "s|@SEASON@|$SEASON|g" "$REPO/scripts/systemd/$1"
}

if [[ $DRY_RUN -eq 1 ]]; then
  echo "--- would install to $UNIT_DIR ---"
  for u in "${UNITS[@]}"; do render "$u.service"; render "$u.timer"; done
  exit 0
fi

mkdir -p "$UNIT_DIR"
for u in "${UNITS[@]}"; do
  render "$u.service" > "$UNIT_DIR/$u.service"
  render "$u.timer"   > "$UNIT_DIR/$u.timer"
done

systemctl --user daemon-reload
for u in "${UNITS[@]}"; do systemctl --user enable --now "$u.timer"; done

echo "installed: $UNIT_DIR/{$(IFS=,; echo "${UNITS[*]}")}.{service,timer}  (season $SEASON)"
echo
systemctl --user list-timers "ziggurat-decisions.timer" --no-pager || true

# Linger check (the nfl-ingest installer added this after finding it off): without
# it, every --user timer dies at logout.
if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)" != "yes" ]]; then
  echo
  echo "WARNING: linger is OFF for $USER — the timer will STOP at logout."
  echo "  fix: loginctl enable-linger \"$USER\""
fi

cat <<EOF

next steps
  freeze now      : systemctl --user start ziggurat-decisions.service
  or by hand      : $REPO/.venv/bin/ziggurat decisions freeze --claim-budget 10
  watch it        : journalctl --user -u ziggurat-decisions.service -n 50 -f
  check captures  : $REPO/.venv/bin/ziggurat decisions status
  verify the bytes: $REPO/.venv/bin/ziggurat decisions verify
  survive logout  : loginctl enable-linger "\$USER"
  uninstall       : scripts/install-decisions.sh --uninstall

An EMPTY \`decisions status\` is NOT healthy-empty: it means no Tuesday has been
archived on this box. Captures land under $REPO/data/decisions/ (gitignored, and
matched by repo_guard — they hold the whole free-agent pool and rival rosters).
EOF
