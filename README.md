# CO-08 Independent Expenditure Tracker

Watches fec.gov for new independent expenditures (Schedule E) in the Colorado 8th
congressional race between Manny Rutinel and Gabe Evans, and emails a digest whenever
something new is filed.

## What it fixes

- **Missing dates.** Form 5 filers often omit the dissemination date, so fec.gov's
  date sort drops their spending to the bottom. Every item here gets a `best_date`:
  dissemination date, else expenditure date, else filing date. `date_source` says which.
- **Re-reports.** A 24/48-hour notice (Form 24) is re-reported on the quarterly
  Form 3X, and Form 5 filers re-report on amendments. Items are fingerprinted on
  spender, candidate, support/oppose, whole-dollar amount, date, and purpose so a
  re-report is not "new".
- **Double listing.** Several PACs file one ad twice, as "Support Evans" and again as
  "Oppose Rutinel", full amount both times. Those twins are paired: the CSV keeps both
  lines (matching fec.gov's per-candidate figures) but the email and the headline
  pro-Rutinel / pro-Evans totals count them once. Filers who split the cost in half
  (AFP does, a penny apart) are left as two real items.
- **Speed.** Raw e-filings are polled alongside processed data, so a 24-hour notice
  shows up within minutes of hitting the FEC, not after nightly processing.

## Files

- `tracker.py`: the whole thing, standard library only.
- `data/ie_spending.csv`: the running spreadsheet, newest first. Open in Sheets/Excel.
- `data/totals.json`: running totals by candidate and by spender, general election only
  (items dated July 1, 2026 or later; the primary was June 30). Change `TOTALS_START` to move it.
- `data/state.json`: fingerprints already alerted on. Delete it to re-baseline.
- `.github/workflows/track.yml`: runs every 10 minutes on GitHub Actions and commits
  data changes back to the repo.

## Running locally

```bash
set -a; source .env; set +a
python3 tracker.py --dry-run     # fetch and print, change nothing
python3 tracker.py               # normal run (first run baselines quietly)
python3 tracker.py --test-email  # send a test digest to the recipients
```

## Configuration

Secrets in the GitHub repo (Settings > Secrets and variables > Actions):

| Secret | Value |
| --- | --- |
| `FEC_API_KEY` | key from api.data.gov |
| `SMTP_USER` | the Google Workspace address that sends the digest |
| `SMTP_PASSWORD` | a Google app password for that address (needs 2-Step Verification on) |

Repository variable `RECIPIENTS` (Settings > Secrets and variables > Actions > Variables):
comma-separated list of addresses that receive the digest.

Manual runs: Actions > "Track CO-08 independent expenditures" > Run workflow, with
mode `normal`, `test-email`, or `dry-run`.

## Known limits

- If a filer amends an item and changes its amount or purpose text, it looks like a
  new item and will be alerted (and the old amount stays in the totals).
- Amounts on 24-hour notices are often estimates; the quarterly report may differ.
- GitHub's scheduler can run a few minutes late during busy periods.
