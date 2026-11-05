# Moving the app to the Mac mini

Do this once the app is working the way you want on the laptop. Keep the
existing database: it holds only real data (the sheet history, statements,
rules, accounts, confirmed transactions). Only the temporary `demo` login
needs deleting (step 6).

## Three things not to do

- **Don't copy `venv/`.** It hard-codes the laptop's path; rebuild it in step 2.
- **Don't copy the database with Finder.** It runs in WAL mode, so a plain copy
  can miss recent writes. Use SQLite's backup command (step 1).
- **Don't copy the project folder.** Use `git clone`, so updates are one `git pull`.

## Step by step

### 1. On the laptop: make a safe copy of the database

```bash
cd /Volumes/COMMON/Projects/finance-app && sqlite3 data/finance.sqlite3 ".backup data/finance-transfer.sqlite3"
```

Move these to the mini by AirDrop or USB stick, not a cloud folder:

- `data/finance-transfer.sqlite3`
- `.env`
- `docs/` (your statements, optional)

### 2. On the Mac mini: get the code and Python

```bash
xcode-select --install
```

```bash
brew install python@3.12 sqlite age
```

```bash
git clone git@github.com:saurabhj/finance-app.git ~/finance-app && cd ~/finance-app && python3.12 -m venv venv && source venv/bin/activate && python3 -m pip install -r requirements.txt
```

### 3. Put the data in place

```bash
cd ~/finance-app && mkdir -p data && mv ~/Downloads/finance-transfer.sqlite3 data/finance.sqlite3 && mv ~/Downloads/.env .env
```

### 4. Edit `.env` on the mini

- `APP_BASE_URL=https://<mini-name>.<your-tailnet>.ts.net`
- `BACKUP_DIR=` a Google Drive or Dropbox synced folder
- Encrypted backups: run `age-keygen -o ~/.age/finance.key` and put the public
  key it prints into `BACKUP_AGE_RECIPIENT`. **Keep the key file somewhere safe
  (password manager); without it the backups cannot be restored.**
- Email settings (`EMAIL_HOST_USER`, app password, `REPORT_RECIPIENTS`) if you
  want reviews and reminders mailed.

### 5. Check it and create your logins

```bash
source venv/bin/activate && python3 manage.py migrate && python3 manage.py test
```

```bash
python3 manage.py createsuperuser
```

Run `createsuperuser` twice: once for Saurabh, once for Pritika.

### 6. Start everything

```bash
ops/install.sh
```

```bash
tailscale serve --bg 8765
```

Open `https://<mini>.<tailnet>.ts.net` on any device on the tailnet. Then remove the
temporary dev login:

```bash
python3 manage.py shell -c "from django.contrib.auth.models import User; User.objects.filter(username='demo').delete()"
```

(or delete it at `/admin/` → Users).

### 7. One-off settings

- **Mac mini:** System Settings → Energy → turn on "Prevent automatic sleeping",
  or the 02:30 backup and 09:00 reviews won't run.
- **Zerodha:** set each Kite app's redirect URL to
  `https://<mini>.<tailnet>.ts.net/zerodha/callback/`.
- **Laptop:** stop using its copy from now on, so there aren't two diverging
  databases.

## Afterwards

Updating:

```bash
cd ~/finance-app && git pull && source venv/bin/activate && python3 -m pip install -r requirements.txt && ops/install.sh
```

- Logs: `data/logs/`
- Remove the services: `ops/install.sh --uninstall`
- Preview what would be installed: `ops/install.sh --dry-run`

What runs on its own once installed:

| Job | When |
|---|---|
| Web server (gunicorn on 127.0.0.1:8765) | Always, restarts if it stops |
| Encrypted backup | Daily 02:30 |
| Mutual-fund NAV fetch | Daily 23:15 |
| Month-end reminder email | 1st of the month, 08:00 |
| Weekly AI check | Sundays 09:00 |
| Monthly AI review | 3rd of the month, 09:00 |
