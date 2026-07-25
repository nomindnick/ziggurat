#!/usr/bin/env bash
# Install the Ziggurat NFL data refresh timers (item 3.1b) as systemd USER units.
#
# Three units, one per cadence group — see scripts/systemd/*.timer for why each
# hour was chosen (they are pinned to MEASURED upstream publish times):
#   daily   07:20      projections (perishable), ADP (perishable), players,
#                      schedules, ESPN board (preseason), odds + injuries (in-season)
#   weekly  08:20      weekly_stats, snap_counts, team_defense, ngs_* — fires daily
#                      but the 7d interval gate means upstream is hit once a week,
#                      anchored on Thursday (stat corrections land Mon-Wed) and
#                      self-healing: a failed Thursday retries on Friday
#   gameday 16:20      weather forecasts for weeks inside Open-Meteo's ~16d wall
#
#   scripts/install-nfl-ingest.sh [--season 2026] [--dry-run] [--uninstall]
#
# Requires a lingering session to fire when logged out:
#   loginctl enable-linger "$USER"     # once, if this box runs headless
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNITS=(ziggurat-nfl-ingest ziggurat-nfl-ingest-weekly ziggurat-nfl-ingest-gameday)
SEASON=""
DRY_RUN=0
UNINSTALL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --season) SEASON="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ $UNINSTALL -eq 1 ]]; then
  for unit in "${UNITS[@]}"; do
    systemctl --user disable --now "$unit.timer" 2>/dev/null || true
    rm -f "$UNIT_DIR/$unit.timer" "$UNIT_DIR/$unit.service"
  done
  systemctl --user daemon-reload
  echo "removed the ziggurat nfl-ingest timers + services"
  exit 0
fi

# Default the season the same way the CLI does (NFL season year, not calendar
# year — January belongs to the previous season). Deliberately NOT
# nflreadpy.get_current_season(), which returns the PREVIOUS season until the
# Thursday after Labor Day and would quietly refresh 2025 all summer.
if [[ -z "$SEASON" ]]; then
  SEASON="$("$REPO/.venv/bin/python" -c \
    'from datetime import date; from ziggurat.data.asof import nfl_season_of; print(nfl_season_of(date.today()))')"
fi

# Fail EARLY and legibly rather than installing timers that fail silently daily.
[[ -x "$REPO/.venv/bin/ziggurat" ]] || { echo "missing $REPO/.venv/bin/ziggurat — create the venv first" >&2; exit 1; }
# .env is needed only by the espn_ranks source; without it that ONE source
# records 'skipped' and the rest of the refresh still runs. Warn, do not abort.
[[ -f "$REPO/.env" ]] || echo "warning: no $REPO/.env — the espn_ranks source will be skipped" >&2

render() {
  sed -e "s|@REPO@|$REPO|g" -e "s|@SEASON@|$SEASON|g" "$REPO/scripts/systemd/$1"
}

if [[ $DRY_RUN -eq 1 ]]; then
  echo "--- would install to $UNIT_DIR (season $SEASON) ---"
  for unit in "${UNITS[@]}"; do
    render "$unit.service"
    render "$unit.timer"
  done
  exit 0
fi

mkdir -p "$UNIT_DIR"
for unit in "${UNITS[@]}"; do
  render "$unit.service" > "$UNIT_DIR/$unit.service"
  render "$unit.timer"   > "$UNIT_DIR/$unit.timer"
done

systemctl --user daemon-reload
for unit in "${UNITS[@]}"; do
  systemctl --user enable --now "$unit.timer"
done

echo "installed: $UNIT_DIR/{$(IFS=,; echo "${UNITS[*]}")}.{service,timer}  (season $SEASON)"
echo
systemctl --user list-timers 'ziggurat-nfl-ingest*' --no-pager || true

# CHECK linger rather than only mentioning it: the item-3.1 installer printed the
# hint and it was evidently not acted on — Linger=no on the box that runs the
# league timer today, so every timer there dies at logout.
if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)" != "yes" ]]; then
  cat <<'EOF'

  !! LINGER IS OFF for this user. These timers STOP when you log out, and a
  !! missed daily run costs a projections/ADP snapshot that cannot be re-pulled.
  !! Fix it once:   loginctl enable-linger "$USER"
EOF
fi

cat <<EOF

next steps
  see the plan     : $REPO/.venv/bin/ziggurat ingest run --dry-run --season $SEASON
  run one now      : systemctl --user start ziggurat-nfl-ingest.service
  watch it         : journalctl --user -u ziggurat-nfl-ingest.service -n 50 -f
  check staleness  : $REPO/.venv/bin/ziggurat ingest status
  the registry     : $REPO/.venv/bin/ziggurat ingest sources
  uninstall        : scripts/install-nfl-ingest.sh --uninstall

Cron alternative (if this box has no user systemd) — keep the timeout, cron has
no equivalent of TimeoutStartSec and a hung pull would otherwise never end:
  20 7  * * *   cd $REPO && timeout 1800 .venv/bin/ziggurat ingest run --group daily   --season $SEASON >> data/nfl-ingest.log 2>&1
  20 8  * * *   cd $REPO && timeout 1800 .venv/bin/ziggurat ingest run --group weekly  --season $SEASON >> data/nfl-ingest.log 2>&1
  20 16 * * *   cd $REPO && timeout 1800 .venv/bin/ziggurat ingest run --group gameday --season $SEASON >> data/nfl-ingest.log 2>&1
EOF
