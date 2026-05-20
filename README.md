# UNjobs daily digest

**Listing URL:** use **`https://unjobs.org/new`** for the default “Latest jobs” scrape (`UNJOBS_URL` / `--url`). Do not use `https://unjobs.org/latest` (the server returns “Resource not found”).

Production-style Python job that scrapes [UNjobs](https://unjobs.org/), filters postings by a lab / public-health / AI-evaluation keyword matrix, applies a Nigerian-eligibility guardrail for locally restricted posts, deduplicates against prior runs, and emails an HTML digest via Gmail SMTP.

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
3. Put the 16-character password in `SENDER_PASSWORD` (see [`.env.example`](.env.example)).

## 3. Environment variables (Cursor and shell)

**Cursor:** use Project Settings or a `.env` file at the repo root. The script loads `.env` automatically if `python-dotenv` is installed.

**Shell:** copy the example file and edit:

```bash
cp .env.example .env
chmod 600 .env
```

Required variables: `SENDER_EMAIL`, `SENDER_PASSWORD`, `RECEIVER_EMAIL`. See [`.env.example`](.env.example) for optional tuning (`UNJOBS_URL`, `MAX_PAGES`, `STATE_PATH`, etc.).

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

## 6. Automate with cron (WSL or Linux)

```bash
crontab -e
```

Example: run every weekday at 07:00, log to a file:

```cron
0 7 * * 1-5 cd /home/ejiaka/UNJOBS && . .venv/bin/activate && set -a && [ -f .env ] && . ./.env && set +a && /home/ejiaka/UNJOBS/.venv/bin/python /home/ejiaka/UNJOBS/scripts/unjobs_daily_digest.py >> /home/ejiaka/UNJOBS/logs/cron.log 2>&1
```

Create `logs/` first (`mkdir -p logs`) or change the log path.

## 7. Network / bot checks

Some networks or datacenter IPs may receive HTTP 403 from Cloudflare when using simple HTTP clients. If that happens, try from a residential connection, reduce frequency, or run the script on a machine that can browse the site normally. The script uses a desktop-like `User-Agent` and conservative delays only; it does not execute JavaScript in a browser.

## 8. State file

Seen vacancy URLs are stored at `data/seen_jobs.json` by default (see `STATE_PATH`). Delete or edit this file to re-send jobs.
