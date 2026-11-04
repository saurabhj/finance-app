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

## Loans
**Loans** page (`g o`): every loan with owner (Saurabh, Pritika or **Joint**), lender, fixed or floating, amount, start date, tenure and EMI.
- **Rate history**: each change with its effective date. Optionally record the new EMI; otherwise the EMI stays and the tenure adjusts, as banks do for floating loans.
- **Prepayments**: each one reduces either the tenure or the EMI. The page shows interest and months saved compared with no prepayments.
- **Bank-reported balances**: the schedule re-anchors to the bank's outstanding from that date.
- **Full amortisation schedule** plus totals per financial year (interest and principal for tax), outstanding, % repaid, interest paid and to come, payoff date.
- EMIs found in bank statements (via each loan's keyword) are listed; a payment well above the EMI can be recorded as a prepayment in one click.
- The month-end form pre-fills each loan's outstanding from its schedule; the FI maths uses each loan's current rate and EMI.

## Bank & credit-card statements
**Statements** page: upload CSV, Excel or (password-protected) PDF statements for any bank account or card. Add cards there first.
- CSV/Excel is parsed on the Mac. PDFs (and odd exports) are read by Claude (`claude-haiku-5-5`), checked against the statement's printed totals, and re-read with `claude-sonnet-5-5` if they don't reconcile.
- Built-in keyword rules catch TO ZERODHA, ET MONEY / ICCL, FD bookings, card bill payments (CRED, "payment received"), home-loan EMIs, salary, interest and common merchants. Claude guesses the rest.
- Transfers between your own or each other's accounts, and card payments vs. card statements, are paired automatically so they aren't counted as spending.
- **Rules & notes**: add keyword rules directly (bank/card, money in/out, category, account), and write plain-English *notes for Claude*, which it follows whenever it reads statements, guesses categories or runs a review.
- **Review** page: fix guesses, tick *remember* to teach a rule, confirm. Confirmed investments become "money invested" and are de-duplicated against the Zerodha ledger and CAS entries.
- Bank closing balances pre-fill the month-end form.
- **Spending** page: monthly spending by category and EMI, coverage warnings for months missing a statement, and a 3-month average you can adopt in the Plan with one click.

## Financial independence
Fill in the **Plan** page: rough monthly expenses, income and hours worked, every loan (home, car…), and optionally target allocation. The **Financial independence** page then shows:
- **FI target**: core expenses (excluding EMI) × 12 ÷ safe withdrawal rate, plus the loan outstanding (the loan is paid off at FI, not carried forever).
- **Corpus**: investments + bank cash above the emergency fund.
- **The wall chart**: safe monthly income vs. core spending over time, and the projected **crossover** month.
- **Monte Carlo**: 2,000 simulated futures, giving percentile bands and the chance of reaching FI by your target date.
- **Life energy**: real hourly wage and what a purchase costs in hours of work.
- **Allocation vs. target**: drift per asset class and the amount needed to rebalance.

## AI reviews
Set `ANTHROPIC_API_KEY` in `.env` (and `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` / `REPORT_RECIPIENTS` for email).
- **Weekly check** (Sunday 09:00, `claude-sonnet-5-5`): flags only urgent items, such as idle cash, drift outside bands, stale data, or a self-managed holding falling sharply.
- **Monthly review** (3rd, 09:00, `claude-opus-5-5`, deep thinking): progress to FI, where new money should go, rebalancing, tax-aware ideas, an explicit **exit / reduce / hold / add** verdict with reasons for every *self-managed* holding (trailing returns vs Nifty 50, regular vs direct plan, tax cost of exiting), and strategy-level notes on advisor-managed groups.
- **Ask**: free-form questions answered from the same data.
- Each action can be marked done or dismissed with a note. The next review sees what you did.
- Preview exactly what is sent to Claude, without calling it: `python3 manage.py run_review --cadence monthly --dry-run`.
- Month-end reminder email on the 1st (`send_reminder`).

## Zerodha (Kite Connect)
Create one app per Zerodha login at developers.kite.trade. Set its redirect URL to `http://<host>:8765/zerodha/callback/`, then put the key and secret in `.env` as `KITE_API_KEY_SAURABH`, `KITE_API_SECRET_SAURABH` (and the same for `PRITIKA`). Access is read-only; the app never places orders.

## Scheduled jobs (launchd)
Copy `ops/launchd/*.plist` to `~/Library/LaunchAgents/` and load each one with `launchctl load`.
- `com.family.finance.backup`: 02:30 nightly. Integrity-checked SQLite backup, compressed and encrypted with `age` when `BACKUP_AGE_RECIPIENT` is set, written to `BACKUP_DIR` (a Google Drive or Dropbox folder). Keeps 30 daily and 12 monthly copies. Restore with `ops/restore.sh <file>`.
- `com.family.finance.nav`: 23:15 daily. AMFI NAVs, used to revalue mutual funds between CAS statements.
- `com.family.finance.reminder`: 1st of the month, 08:00. Month-end update reminder email.
- `com.family.finance.review-weekly`: Sundays, 09:00. Weekly AI check.
- `com.family.finance.review-monthly`: 3rd of the month, 09:00. Monthly AI review.

## Tests
```bash
python3 manage.py test
```

## Privacy
`.env`, `data/` (the database), `docs/*.xlsx` and any statements/CSVs are git-ignored. Don't commit financial data.
