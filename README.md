# 🏠 Apartment Finder Bot

Automated scraper + dashboard for finding rental apartments on yad2.co.il.
The search area, price, and filters are fully configurable — and can even be
changed **by replying to the alert emails**.

## Features

- 🔎 Scrapes Yad2's internal Next.js feed (TLS-impersonation to pass Cloudflare)
- 🗺️ Targets any city/region + neighborhoods, filtered by price/rooms/size
- 📧 Ranked HTML email alerts (photos, top N most relevant) to multiple recipients
- 🚫 Never emails the same listing twice
- ✉️ **Change the filter by replying to an alert in plain language** (Hebrew/English) — see below
- 🖥️ Local web dashboard
- ⏰ Runs unattended on a schedule via Windows Task Scheduler

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
| `search.region_slug` | Yad2 region landing-page slug |
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
It returns each match's `cityId`, `hoodId`, and `regionHeb`. Put the `cityId`(s) in
`search.cities`; the `region_slug` is the kebab-case English of `regionHeb`. All cities
in one config should belong to the same region slug.

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

1. **Build ID** — extracts Yad2's Next.js `BUILD_ID` from the page HTML (retried hard; the page is Cloudflare-throttled). The feed lives at `/realestate/_next/data/{BUILD_ID}/rent/{region_slug}.json`.
2. **Per-city queries** — Yad2 ignores neighborhood filters server-side and won't combine cities, so each city in `search.cities` is queried separately and paginated.
3. **Filter** — price/rooms/size enforced in `parse_listing()`; neighborhoods narrowed client-side by `target_areas` keyword match.
4. **Dedup + new detection** — SQLite tracks every listing token; a `notified` flag ensures each listing is emailed once ever.
5. **Rank + notify** — top `email_top_n` never-emailed listings, ordered by area priority → cheaper → parking → bigger, sent as an HTML card email.
6. **Inbound email** — before each scan, reads whitelisted reply emails and updates the search (see above).

## Automation (Windows Task Scheduler)

Runs unattended via a task named **`Yad2ApartmentFinder`** — headless (`pythonw`),
survives reboot (catches up if the PC was off). It runs `python scraper.py --once`
on whatever schedule you set. Edit the task in Task Scheduler to change the time or
frequency.

## Files

```
config.json       ← non-secret search settings (committed)
.env              ← secrets (gitignored — copy from .env.example)
scraper.py        ← main bot
server.py         ← dashboard web server
dashboard.html    ← frontend UI
requirements.txt  ← Python dependencies
apartments.db     ← SQLite database (auto-created, gitignored)
apartments.json   ← dashboard data (auto-created, gitignored)
scraper.log       ← timestamped log (gitignored)
```
