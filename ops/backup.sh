#!/bin/bash
# Nightly SQLite backup -> gzip -> (age-encrypt) -> BACKUP_DIR (a Google Drive/Dropbox synced folder).
# Keeps 30 daily copies and one copy per month for 12 months.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; [ -f .env ] && source .env; set +a

DB="${DB_PATH:-data/finance.sqlite3}"
DEST="${BACKUP_DIR:?Set BACKUP_DIR in .env (e.g. ~/Library/CloudStorage/GoogleDrive-.../My Drive/finance-backups)}"
mkdir -p "$DEST/daily" "$DEST/monthly"
STAMP=$(date +%F)
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

sqlite3 "$DB" ".backup '$TMP/finance.sqlite3'"          # consistent copy even while the app runs
sqlite3 "$TMP/finance.sqlite3" "PRAGMA integrity_check;" | grep -qx ok
gzip -9 "$TMP/finance.sqlite3"

if [ -n "${BACKUP_AGE_RECIPIENT:-}" ] && command -v age >/dev/null; then
  OUT="$DEST/daily/finance-$STAMP.sqlite3.gz.age"
  age -r "$BACKUP_AGE_RECIPIENT" -o "$OUT" "$TMP/finance.sqlite3.gz"
else
  echo "WARNING: backup is NOT encrypted (install 'age' and set BACKUP_AGE_RECIPIENT)." >&2
  OUT="$DEST/daily/finance-$STAMP.sqlite3.gz"
  cp "$TMP/finance.sqlite3.gz" "$OUT"
fi

# First backup of each month is also kept as the monthly copy.
MONTH=$(date +%Y-%m)
ls "$DEST/monthly" | grep -q "finance-$MONTH" || cp "$OUT" "$DEST/monthly/"

# Rotate
ls -1t "$DEST/daily"   | tail -n +31 | while read -r f; do rm -f "$DEST/daily/$f"; done
ls -1t "$DEST/monthly" | tail -n +13 | while read -r f; do rm -f "$DEST/monthly/$f"; done
echo "Backup written: $OUT"
