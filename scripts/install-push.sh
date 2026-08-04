#!/usr/bin/env bash
# Install the Ziggurat push-layer timers (item 3.6) as systemd USER units:
#   * ziggurat-brief.timer   — Wednesday 06:00 (once/week), the morning briefing
#   * ziggurat-alerts.timer  — every 20 min 06:00-23:00, the event-alert tick
#
# User-level on purpose: no root, reads the repo's own .env + virtualenv.
#
#   scripts/install-push.sh [--season 2026] [--dry-run] [--uninstall]
#
# Requires linger to fire when logged out:  loginctl enable-linger "$USER"
# Requires NTFY_TOPIC in .env for the phone push (a high-entropy string — it is
# the topic's password on a public ntfy.sh topic). Without it the timers still run
# and write the briefing/alert files, but every push is a recorded failure.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNITS=(ziggurat-brief ziggurat-alerts)
SEASON=""
DRY_RUN=0
UNINSTALL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --season) SEASON="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ $UNINSTALL -eq 1 ]]; then
  for u in "${UNITS[@]}"; do
    systemctl --user disable --now "$u.timer" 2>/dev/null || true
    rm -f "$UNIT_DIR/$u.timer" "$UNIT_DIR/$u.service"
  done
  systemctl --user daemon-reload
  echo "removed ${UNITS[*]} timers + services"
  exit 0
fi

# Default the season the same way the CLI does (NFL season year, not calendar year).
if [[ -z "$SEASON" ]]; then
  SEASON="$("$REPO/.venv/bin/python" -c \
    'from datetime import date; from ziggurat.data.asof import nfl_season_of; print(nfl_season_of(date.today()))')"
fi

# Fail EARLY rather than installing a timer that fails silently.
[[ -x "$REPO/.venv/bin/ziggurat" ]] || { echo "missing $REPO/.venv/bin/ziggurat — create the venv first" >&2; exit 1; }
[[ -f "$REPO/.env" ]] || { echo "missing $REPO/.env (SWID / ESPN_S2 / ESPN_LEAGUE_ID)" >&2; exit 1; }
[[ -x "$HOME/.local/bin/claude" ]] || echo "WARNING: no claude CLI at ~/.local/bin/claude — the briefing prose (claude_cli backend) will fail; set ZIGGURAT_CLAUDE_BIN or install it." >&2
grep -qE '^NTFY_TOPIC=.+' "$REPO/.env" || echo "WARNING: NTFY_TOPIC not set (or empty) in .env — timers will run and write files, but every phone push will be a recorded failure until it is set to a non-empty topic." >&2

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
systemctl --user list-timers "ziggurat-brief.timer" "ziggurat-alerts.timer" --no-pager || true

# Linger check (the nfl-ingest installer added this after finding it off): without
# it, every --user timer dies at logout.
if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)" != "yes" ]]; then
  echo
  echo "WARNING: linger is OFF for $USER — the timers will STOP at logout."
  echo "  fix: loginctl enable-linger \"$USER\""
fi

cat <<EOF

next steps
  run briefing now : systemctl --user start ziggurat-brief.service
  run one tick     : systemctl --user start ziggurat-alerts.service
  or without push  : $REPO/.venv/bin/ziggurat brief run --no-push --no-llm
  watch it         : journalctl --user -u ziggurat-brief.service -n 50 -f
  check runs       : $REPO/.venv/bin/ziggurat brief status ; $REPO/.venv/bin/ziggurat alerts status
  survive logout   : loginctl enable-linger "\$USER"
  uninstall        : scripts/install-push.sh --uninstall

phone setup (once): install the ntfy app, subscribe to your NTFY_TOPIC, and put
  NTFY_TOPIC=zig-<high-entropy string>   in .env (the topic name IS the password).
  Privacy upgrade path (self-host behind Tailscale / ntfy.sh Pro reserved topic):
  set NTFY_SERVER + NTFY_TOKEN in .env — no code change. See the 3.6 runbook.
EOF
