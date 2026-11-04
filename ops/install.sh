#!/bin/bash
# Install (or remove) the app's background services as launchd agents for this user.
#   ops/install.sh             install / update everything
#   ops/install.sh --dry-run   write the plists to a temp folder and check them, install nothing
#   ops/install.sh --uninstall stop and remove everything
# Services: web server (always on), nightly backup, daily NAVs, month-end reminder,
# weekly and monthly AI reviews. Paths come from where this project lives, so it works
# on any Mac once the project folder is in place.
set -euo pipefail
APP="$(cd "$(dirname "$0")/.." && pwd)"
LOGS="$APP/data/logs"
DEST="$HOME/Library/LaunchAgents"
MODE="${1:-install}"
[ "$MODE" = "--dry-run" ] && DEST="$(mktemp -d)"
mkdir -p "$LOGS" "$DEST"
PREFIX="com.family.finance"
NAMES=(web backup nav reminder review-weekly review-monthly)

if [ "$MODE" = "--uninstall" ]; then
  for n in "${NAMES[@]}"; do
    launchctl bootout "gui/$(id -u)/$PREFIX.$n" 2>/dev/null || true
    rm -f "$DEST/$PREFIX.$n.plist"
  done
  echo "Removed all $PREFIX services."; exit 0
fi

# write_plist <name> <shell command> <schedule xml or KEEPALIVE>
write_plist() {
  local name="$1" cmd="$2" when="$3" file="$DEST/$PREFIX.$1.plist"
  local timing
  if [ "$when" = "KEEPALIVE" ]; then
    timing="<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>"
  else
    timing="<key>StartCalendarInterval</key>$when"
  fi
  cat > "$file" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$PREFIX.$name</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>-lc</string><string>$cmd</string></array>
  <key>WorkingDirectory</key><string>$APP</string>
  $timing
  <key>StandardOutPath</key><string>$LOGS/$name.log</string>
  <key>StandardErrorPath</key><string>$LOGS/$name.log</string>
</dict>
</plist>
PLIST
  plutil -lint -s "$file"
}

MANAGE="cd &quot;$APP&quot; &amp;&amp; source venv/bin/activate &amp;&amp; python3 manage.py"
at() { echo "<dict>$1<key>Hour</key><integer>$2</integer><key>Minute</key><integer>$3</integer></dict>"; }

write_plist web            "&quot;$APP/ops/run_web.sh&quot;"            KEEPALIVE
write_plist backup         "&quot;$APP/ops/backup.sh&quot;"             "$(at '' 2 30)"
write_plist nav            "$MANAGE fetch_nav"                          "$(at '' 23 15)"
write_plist reminder       "$MANAGE send_reminder"                      "$(at '<key>Day</key><integer>1</integer>' 8 0)"
write_plist review-weekly  "$MANAGE run_review --cadence weekly"        "$(at '<key>Weekday</key><integer>0</integer>' 9 0)"
write_plist review-monthly "$MANAGE run_review --cadence monthly"       "$(at '<key>Day</key><integer>3</integer>' 9 0)"

if [ "$MODE" = "--dry-run" ]; then
  echo "Dry run: plists written and checked in $DEST (nothing installed)."; exit 0
fi
for n in "${NAMES[@]}"; do
  launchctl bootout "gui/$(id -u)/$PREFIX.$n" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$DEST/$PREFIX.$n.plist"
done
echo "Installed: ${NAMES[*]}"
echo "Web server: http://127.0.0.1:8765  (logs in $LOGS)"
