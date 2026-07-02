# PLAN — Run the scraper in the cloud (GitHub Actions) for reliable 08:00 email

## Why

The laptop is **Modern Standby only** (no S3 sleep — `powercfg /a` confirms), so
Task Scheduler wake-timers can't reliably pull it out of deep idle at 08:00. The
task's `StartWhenAvailable` only delivers the email on first laptop-use each day,
not at 08:00 sharp. Moving the run to GitHub Actions removes the laptop from the
loop entirely. Now viable because the scraper fetches from `gw.yad2.co.il` with
no BUILD_ID (see `GW_FEED_MIGRATION_PLAN.md`).

## Design

- **Workflow:** `.github/workflows/daily-scrape.yml`, cron `0 5 * * 0-5`
  (05:00 UTC = 08:00 Israel summer, Sun–Fri, skip Sat) + manual `workflow_dispatch`.
- **State persistence:** a dedicated **`bot-state` branch** holds
  `apartments.db` + `apartments.json` + `config.json` between runs.
  - The DB's `notified` column = "emailed once ever", so state MUST survive
    across ephemeral runners or every run re-emails everything.
  - Chose a git branch over `actions/cache` because GitHub **auto-disables
    scheduled workflows after 60 days with no repo commits** — a cache-only
    workflow makes no commits and would silently stop. The daily state-commit
    keeps the schedule alive.
  - `config.json` is persisted too, so **reply-to-change-search email commands
    keep working in the cloud** (cloud reads/writes the branch copy). After
    go-live, change the search by email (or edit `config.json` on `bot-state`),
    not by editing it on `main`.
- **Secrets** come from GitHub Actions secrets, injected as env vars; the
  scraper's `_load_env()` already reads `os.environ` overrides.

## The one real risk

Whether GitHub's **datacenter IP can reach `gw.yad2.co.il`** (Cloudflare may
treat cloud IPs differently than a residential IP). Cannot be tested from the
dev machine. **Validated by the first manual run** (Actions tab → Run workflow →
check log / `Done: N found`). If gw is blocked there, fall back to a residential
VPS or a self-hosted runner.

## Setup steps (done in the GitHub web UI — the dev box has no `gh` CLI)

1. Push this branch (workflow + `config.json` region change) to `main`.
2. Repo **Settings → Secrets and variables → Actions → New repository secret**,
   add (values from local `.env`):
   - `EMAIL_USER`, `EMAIL_PASS`, `EMAIL_TO`, `GEMINI_API_KEY`, `ALLOWED_SENDERS`
   - optional: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
3. **Actions tab → daily-scrape → Run workflow** (manual test). Confirm the log
   shows `Done: N found` (N>0) and the email arrives.
4. Once confirmed, **disable the Windows Task Scheduler task** `Yad2ApartmentFinder`
   to avoid duplicate emails (`schtasks /Change /TN Yad2ApartmentFinder /DISABLE`),
   or keep it as an offline backup (duplicates are suppressed by `notified` only
   if it shares the same DB — it won't in the cloud, so disabling is cleaner).

## Notes / caveats

- **DST:** cron is UTC; winter runs land at 07:00 local. To keep 08:00 year-round,
  adjust the cron in October, or add a second `0 6 * * 0-5` line (harmless — the
  extra run emails only net-new listings).
- Scheduled runs execute the workflow file on the **default branch** (`main`).
- GitHub-hosted minutes: this is a ~1–2 min job once a day → well within free tier.
