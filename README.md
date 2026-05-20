# UNjobs daily digest

**Listing URL:** use **`https://unjobs.org/new`** for the default “Latest jobs” scrape (`UNJOBS_URL` / `--url`). Do not use `https://unjobs.org/latest` (the server returns “Resource not found”).

Production-style Python job that scrapes [UNjobs](https://unjobs.org/), filters postings for **health-focused** roles (lab, public health, clinical AI / bioinformatics), drops generic IT/software jobs unless they mention **AI evaluation** or **QA engineer**, and applies location rules: **all Asian postings are discarded**; **Africa / Nigeria** only **international** posts (P/D, international consultant; no national/SSA/GS/local-only); **remote roles outside Africa** are kept for **North America, Western Europe, Greece, Portugal, and Australia** when they are **remote/home-based** and **open to Nigerian applicants**. Other regions are excluded. Deduplicates against prior runs and emails an HTML digest via Gmail SMTP. By default it scans **20 listing pages** (~500 vacancies).

Respect [unjobs.org](https://unjobs.org/) terms of service and `robots.txt`. Use this for **personal** job-search automation; do not overload the site (pagination and request delay are configurable).

## 1. Virtual environment (required before running)

From the project root:

```bash
cd /home/ejiaka/UNJOBS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Deactivate when finished:

```bash
deactivate
```

Cron and other schedulers should call the **interpreter inside the venv** so dependencies are stable:

```text
/home/ejiaka/UNJOBS/.venv/bin/python /home/ejiaka/UNJOBS/scripts/unjobs_daily_digest.py
```

## 2. Gmail App Password

1. Google Account → Security → 2-Step Verification (must be on).
2. Security → App passwords → create an app password for "Mail" / "Other".
3. Put credentials in `.env` as `GMAIL_USER` and `GMAIL_APP_PASSWORD` (see [`.env.example`](.env.example)).

## 3. Environment variables (Cursor and shell)

**Cursor:** use a `.env` file at the project root (loaded automatically via `python-dotenv`).

**Shell:**

```bash
cp .env.example .env
chmod 600 .env
```

**Email (required for non–dry-run sends):**

| Variable | Purpose |
|----------|---------|
| `GMAIL_USER` | Sender Gmail address |
| `GMAIL_APP_PASSWORD` | Gmail App Password (16 characters) |
| `NOTIFY_TO_EMAIL` | Where the digest is sent |

Legacy names `SENDER_EMAIL`, `SENDER_PASSWORD`, and `RECEIVER_EMAIL` still work if set instead.

**Optional (stored for future features; not used by the scraper today):** `SERPER_API_KEY`, `GEMINI_API_KEY`.

See [`.env.example`](.env.example) for scraper tuning (`UNJOBS_URL`, `MAX_PAGES`, `STATE_PATH`, etc.).

## 4. Run manually

Dry run (no email; prints matches):

```bash
source .venv/bin/activate
python scripts/unjobs_daily_digest.py --dry-run
```

Normal run (email only **new** vacancies since last run):

```bash
python scripts/unjobs_daily_digest.py
```

## 5. Listing URL note

If you open **`https://unjobs.org/latest`** in a browser, the server returns a **Resource not found** site error. The page may show as a wall of raw HTML because the response is not a normal document for that path.

The working “Latest jobs” list is **`https://unjobs.org/new`**. Set `UNJOBS_URL` to that URL (this is the default in [`.env.example`](.env.example)). The script automatically rewrites a URL whose last path segment is `latest` to `new` and logs a warning, so old bookmarks still work.

You can point `UNJOBS_URL` at other listing paths the site exposes when you use search or duty-station views; pagination appends `/2`, `/3`, … when the path has no query string.

## 6. Automate with GitHub Actions (recommended)

Two workflows live under [`.github/workflows/`](.github/workflows/):

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| [`ci.yml`](.github/workflows/ci.yml) | Push / PR to `main` | Install deps, syntax-check the script |
| [`daily-digest.yml`](.github/workflows/daily-digest.yml) | Daily **06:00 & 20:00 WAT** + manual | Scrape, filter, email new jobs |

### One-time setup

1. Push this repo to GitHub (default branch `main`).
2. Open **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret name | Value |
|-------------|--------|
| `GMAIL_USER` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | Gmail App Password (16 characters) |
| `NOTIFY_TO_EMAIL` | Where to receive the digest |

3. Enable Actions: **Actions** tab → allow workflows if prompted.
4. Test manually: **Actions → Daily UNjobs digest → Run workflow**.

### How state is stored in CI

`data/seen_jobs.json` is **not** committed. The daily workflow uses [Actions cache](https://docs.github.com/en/actions/using-workflows/caching-dependencies-to-speed-up-workflows) so repeat runs do not re-email the same URLs. If the cache is cleared, you may get duplicate digests once.

### Notes

- Schedule is **twice daily in WAT (UTC+1):** 06:00 WAT (`05:00 UTC`) and 20:00 WAT / 8pm (`19:00 UTC`). GitHub cron uses UTC only.
- Scheduled runs execute only on the **default branch** and only while the repo has recent activity (GitHub policy).
- GitHub-hosted runners use datacenter IPs; UNjobs may occasionally block them (HTTP 403). If that happens, use local cron (below) or re-run the workflow later.
- **Cost:** public repos get free Action minutes; private repos use your plan’s included minutes (this job is short, typically a few minutes per day).

## 7. Automate with cron (WSL or Linux, alternative)

```bash
crontab -e
```

Example: run every weekday at 07:00, log to a file:

```cron
0 7 * * 1-5 cd /home/ejiaka/UNJOBS && . .venv/bin/activate && set -a && [ -f .env ] && . ./.env && set +a && /home/ejiaka/UNJOBS/.venv/bin/python /home/ejiaka/UNJOBS/scripts/unjobs_daily_digest.py >> /home/ejiaka/UNJOBS/logs/cron.log 2>&1
```

Create `logs/` first (`mkdir -p logs`) or change the log path.

## 8. Network / bot checks

Some networks or datacenter IPs may receive HTTP 403 from Cloudflare when using simple HTTP clients. If that happens, try from a residential connection, reduce frequency, or run the script on a machine that can browse the site normally. The script uses a desktop-like `User-Agent` and conservative delays only; it does not execute JavaScript in a browser.

## 9. State file

Seen vacancy URLs are stored at `data/seen_jobs.json` by default (see `STATE_PATH`). Delete or edit this file to re-send jobs.
