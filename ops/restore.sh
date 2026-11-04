#!/bin/bash
# Restore a backup into a target path (default: a temp file, so you can inspect before replacing).
#   ops/restore.sh <backup file> [target.sqlite3]
# For encrypted backups set AGE_IDENTITY to your age key file.
set -euo pipefail
SRC="${1:?usage: restore.sh <backup file> [target]}"
TARGET="${2:-$(mktemp -d)/finance.sqlite3}"
case "$SRC" in
  *.age) age -d -i "${AGE_IDENTITY:?Set AGE_IDENTITY to your age key file}" "$SRC" | gunzip > "$TARGET" ;;
  *.gz)  gunzip -c "$SRC" > "$TARGET" ;;
  *)     cp "$SRC" "$TARGET" ;;
esac
sqlite3 "$TARGET" "PRAGMA integrity_check;" | grep -qx ok
echo "Restored to $TARGET"
sqlite3 "$TARGET" "SELECT 'snapshots', COUNT(*) FROM core_snapshot UNION ALL SELECT 'balances', COUNT(*) FROM core_balance UNION ALL SELECT 'cash flows', COUNT(*) FROM core_cashflow;"
