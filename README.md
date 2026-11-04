# Family Finance

A personal finance app for Saurabh and Pritika. It tracks net worth across banks, FDs, Zerodha (Smallcase / Weekend Investing, SGB, GOLDBEES) and mutual funds (ET Money index funds and older active funds). It separates **money invested** from **real market gain** each month and, in later phases, runs AI reviews against the *Your Money or Your Life* financial-independence plan.

Runs on the Mac mini as Django + SQLite and is reached over Tailscale.

## Setup
```bash
python3 -m venv venv
source venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env          # fill in keys
python3 manage.py migrate
python3 manage.py createsuperuser
python3 manage.py import_sheet docs/net_worth.xlsx   # one-time history import (file not in git)
python3 manage.py runserver 0.0.0.0:8765
```

## Monthly routine
1. **Holdings → Zerodha → Log in & pull**, once for each person.
2. **Holdings → upload the Zerodha ledger CSV** (Console → Funds → Statement → Ledger). This records money added and withdrawn.
3. **Holdings → upload the CAS PDF** from mfcentral.com. This brings in MF holdings plus every SIP and redemption.
4. **Month-end entry**: Zerodha and MF values and "invested" amounts come pre-filled. Type the bank and FD balances and save.

## Zerodha (Kite Connect)
Create one app per Zerodha login at developers.kite.trade. Set its redirect URL to `http://<host>:8765/zerodha/callback/`, then put the key and secret in `.env` as `KITE_API_KEY_SAURABH`, `KITE_API_SECRET_SAURABH` (and the same for `PRITIKA`). Access is read-only; the app never places orders.

## Scheduled jobs (launchd)
Copy `ops/launchd/*.plist` to `~/Library/LaunchAgents/` and load each one with `launchctl load`.
- `com.family.finance.backup`: 02:30 nightly. Integrity-checked SQLite backup, compressed and encrypted with `age` when `BACKUP_AGE_RECIPIENT` is set, written to `BACKUP_DIR` (a Google Drive or Dropbox folder). Keeps 30 daily and 12 monthly copies. Restore with `ops/restore.sh <file>`.
- `com.family.finance.nav`: 23:15 daily. AMFI NAVs, used to revalue mutual funds between CAS statements.

## Tests
```bash
python3 manage.py test
```

## Privacy
`.env`, `data/` (the database), `docs/*.xlsx` and any statements/CSVs are git-ignored. Don't commit financial data.
