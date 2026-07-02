# 🏠 Apartment Finder Bot

Automated scraper + dashboard for finding rental apartments on yad2.co.il.
The search area, price, and filters are fully configurable — and can even be
changed **by replying to the alert emails**.

## Features

- 🔎 Fetches listings from Yad2's API gateway (`gw.yad2.co.il`) — no fragile build-ID, TLS-impersonation to pass Cloudflare
- 🗺️ Targets any city/region + neighborhoods, filtered by price/rooms/size
- 📧 Ranked HTML email alerts (photos, top N most relevant) to multiple recipients
- 🚫 Never emails the same listing twice
- ✉️ **Change the filter by replying to an alert in plain language** (Hebrew/English) — see below
- 🖥️ Local web dashboard
- ☁️ Runs unattended daily in the cloud (GitHub Actions) — laptop-independent; a local Windows Task Scheduler run is also supported

## Quick Start

```bash
pip install -r requirements.txt

# Secrets (email + Gemini key) live in .env, NOT in config.json:
cp .env.example .env   # then edit .env with your real values

python run.py            # dashboard + continuous scraper
# or a single scan:
python scraper.py --once
# dashboard only → http://localhost:8080
python server.py
# reset the database:
python scraper.py --reset
```

**Important:** `curl_cffi` is the key dependency — it impersonates Chrome's TLS
fingerprint to bypass Cloudflare. Without it, Yad2 will block you.

## Secrets — `.env` (never committed)

All credentials live in `.env` (gitignored). Copy `.env.example` and fill in:

| Variable | Purpose |
|----------|---------|
| `EMAIL_USER` / `EMAIL_PASS` | Gmail sender — use a **dedicated account + App Password**, not your login password |
| `EMAIL_TO` | Comma-separated alert recipients |
| `GEMINI_API_KEY` | Google AI Studio key — for free-text email preference parsing (optional) |
| `ALLOWED_SENDERS` | Comma-separated senders allowed to change preferences by email |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Optional Telegram alerts |

`config.json` holds **only non-secret search settings** — secrets are overlaid from
`.env` at runtime and are never written back to `config.json`.

## The filter is not fixed — it can change

There are **two ways** to change what the bot searches for, and the change applies on
the very next scan (`config.json` is re-read every run — no restart needed):

### 1. Edit `config.json` directly

| Setting | Controls |
|---------|----------|
| `search.region` | **Numeric** Yad2 region ID — required by the gw feed (e.g. `5` = coastal-north) |
| `search.region_slug` | Legacy/informational region slug (the feed uses `search.region`, not this) |
| `search.cities` | List of numeric Yad2 city IDs |
| `search.price_min/max` | Rent range in ₪ |
| `search.rooms_min/max` | Room range |
| `search.min_sqm` | Minimum size (m²) |
| `search.exclude_ground_floor` | Skip ground floor |
| `target_areas.hebrew` / `english` | Neighborhood/city keywords — narrows results client-side |
| `notifications.email_top_n` | How many listings per email |
| `dashboard_port` | Dashboard port |

To find the codes for any area, query Yad2's address API:
`https://gw.yad2.co.il/address-autocomplete/realestate/v2?text=<URL-encoded Hebrew>`
It returns each match's `cityId`, `hoodId`, `regionId`, and `regionHeb`. Put the
`cityId`(s) in `search.cities` and the `regionId` in `search.region`. All cities in one
config must belong to the same region.

### 2. Reply to an alert email (no file editing) ⭐

Reply to any alert from a whitelisted address and write what you want — **in plain
Hebrew or English**. Gemini converts it to commands; a deterministic parser applies
them and rewrites `config.json`. You get a **confirmation email** back listing the changes.

Examples (just write naturally):
> raise the budget to 4000 and add the <neighborhood> area

> lower max price and drop the <city> listings

Under the hood it maps to fixed commands (you can also write these directly):
`maxprice N` · `minprice N` · `maxrooms N` · `minrooms N` · `minsqm N` ·
`exclude_ground on|off` · `parking on|off` · `topn N` · `area+ <name>` · `area- <name>`

Only senders in `ALLOWED_SENDERS` can change settings. The LLM only *translates* text
to commands — it never edits the config itself; the deterministic parser does, so an
unrecognized request is safely ignored.

## How It Works

1. **Feed** — fetches from Yad2's API gateway `https://gw.yad2.co.il/realestate-feed/rent/feed?region={id}&city={id}&maxPrice=…&page=N`. No rotating build-ID needed. (Replaced the old www `_next/data` + `BUILD_ID` path, which the main host now Cloudflare-blocks.)
2. **Per-city queries** — Yad2 ignores neighborhood filters server-side and won't combine cities, so each city in `search.cities` is queried separately and paginated.
3. **Filter** — price/rooms/size enforced in `parse_listing()`; neighborhoods narrowed client-side by `target_areas` keyword match.
4. **Dedup + new detection** — SQLite tracks every listing token; a `notified` flag ensures each listing is emailed once ever.
5. **Rank + notify** — top `email_top_n` never-emailed listings, ordered by area priority → cheaper → parking → bigger, sent as an HTML card email.
6. **Inbound email** — before each scan, reads whitelisted reply emails and updates the search (see above).

## Automation

### Cloud — GitHub Actions (recommended, laptop-independent)

`.github/workflows/daily-scrape.yml` runs `scraper.py --once` on a daily cron
(`0 5 * * 0-5` = 08:00 Israel summer time, Sun–Fri). Runs entirely on GitHub's
runners, so the machine can be off/asleep.

- **Secrets:** add the `.env` values as repo **Actions secrets** (Settings →
  Secrets and variables → Actions): `EMAIL_USER`, `EMAIL_PASS`, `EMAIL_TO`,
  `GEMINI_API_KEY`, `ALLOWED_SENDERS` (+ optional Telegram). The scraper reads
  them from the environment.
- **State** (`apartments.db` + `apartments.json` + `config.json`) is persisted
  between runs on a dedicated **`bot-state` branch** — the DB carries the
  "emailed-once" flag, and the daily commit also keeps the schedule alive
  (GitHub disables cron after 60 days with no repo commits). To re-send
  everything, remove `apartments.db` from `bot-state`.
- **DST caveat:** cron is UTC (no DST), so in winter runs land at 07:00 local.
- Trigger a manual test from the **Actions tab → daily-scrape → Run workflow**.

### Local — Windows Task Scheduler (optional / offline backup)

A task named **`Yad2ApartmentFinder`** can run `pythonw scraper.py --once`
headless on a local schedule. **Disable it if the cloud workflow is active**
(`schtasks /Change /TN Yad2ApartmentFinder /DISABLE`) — it uses a separate local
DB and would send duplicate emails.

## Files

```
config.json                        ← non-secret search settings (committed)
.env                               ← secrets (gitignored — copy from .env.example)
scraper.py                         ← main bot
llm.py                             ← Gemini/LLM connection helpers
server.py                          ← dashboard web server
dashboard.html                     ← frontend UI
requirements.txt                   ← Python dependencies
.github/workflows/daily-scrape.yml ← cloud daily run (GitHub Actions)
apartments.db                      ← SQLite database (auto-created, gitignored)
apartments.json                    ← dashboard data (auto-created, gitignored)
scraper.log                        ← timestamped log (gitignored)
```

> Cloud runs persist `apartments.db` / `apartments.json` on the `bot-state` branch
> instead of locally.
