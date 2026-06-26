# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Yad2 (yad2.co.il) rental-apartment scraper for Tel Aviv plus a local web dashboard. Three moving parts: a Python scraper that polls Yad2's internal feed, a SQLite store, and a static HTML dashboard served by a tiny stdlib HTTP server.

## Commands

```bash
pip install -r requirements.txt

python run.py             # Start dashboard + continuous scraper together
python scraper.py         # Continuous scrape only (loops every interval_hours)
python scraper.py --once  # Single scan cycle, then exit
python scraper.py --debug # Connectivity test: fetch build ID + one feed page, parse one listing
python scraper.py --reset # Delete apartments.db and apartments.json
python server.py          # Dashboard only → http://localhost:8080
```

There is no test suite, linter, or build step. `--debug` is the de-facto smoke test — run it after changing fetch/extract/parse logic to confirm Yad2's response shape still matches.

## Architecture

Data flows: `scraper.py` → `apartments.db` (SQLite) → `apartments.json` → `server.py` → `dashboard.html`.

- **scraper.py** — the whole pipeline. Stages, in order:
  1. `get_build_id()` scrapes the Next.js `BUILD_ID` out of the Yad2 rent page HTML. **This is the fragile core**: Yad2 is a Next.js app and the feed lives at `/realestate/_next/data/{BUILD_ID}/rent/{region_slug}.json`. The build ID rotates on every Yad2 deploy, so it must be re-extracted each run. The main HTML page is heavily Cloudflare-throttled (resets the connection ~5 of 6 attempts), so `get_build_id()` retries generously (`rounds` × `fetch` retries) and `_extract_build_id()` tries three regex patterns. The JSON feed endpoints are far more reliable than this HTML page.
  2. `scrape()` queries each city in `config.json` `search.cities` separately, paginating each, all under the one `search.region_slug`. **Why per-city:** Yad2 ignores `multiNeighborhood` server-side and won't combine cities in one `multiCity` param — so neighborhood narrowing happens client-side via the area-keyword filter (step below), and each city needs its own request.
  3. `extract_listings()` digs listings out of the React Query `dehydratedState.queries[].state.data` blob — checks several array keys (`private`, `agency`, `platinum`, …) and dedupes by `token`.
  4. `parse_listing()` normalizes one raw listing and applies filters (rooms, floor, price, sqm, elevator). Returns `None` to reject. `match_area()` keyword-matches against `config.json` area lists.
  5. `upsert()` inserts new listings (keyed by `item_id`/token) or refreshes `last_seen`+`price` on existing ones. New = first time the token is seen.
  6. `export_json()` flattens the table to `apartments.json`, sorted new-first → parking → cheapest.
  7. **Notifications** send the top `notifications.email_top_n` (default 10) most-relevant listings that have **never been emailed** — tracked by the `notified` DB column, so a listing goes out once ever and later runs send only the next batch. Relevance = `rank_key()`: area priority (`area_priority()`, by position in `target_areas`) → cheaper → has parking → bigger. `notify_email()` builds an RTL HTML card layout (photo + chips + CTA) via `build_email_html()`; `notify_tg()` is the Telegram variant.
  8. **Inbound email control:** at the start of each `run_once()`, `apply_email_commands()` reads UNSEEN messages from the sender's own inbox over IMAP, and for whitelisted senders (`notifications.allowed_senders`) updates the search and rewrites `config.json` via `save_cfg()` — so users change the search by replying to an alert. Quoted reply text is skipped. Two-stage parsing: if a `gemini_api_key` is set, `gemini_normalize()` first rewrites the free-text email (any language) into canonical command lines via the Gemini REST API (`generativelanguage.googleapis.com`, temperature 0, no SDK dependency); those lines — or the raw body when Gemini is disabled/unavailable — are then applied by the dumb keyword parser `parse_commands()` (which returns human-readable Hebrew descriptions of each change). So Gemini is only a normalizer; the actual config mutation is always the deterministic keyword parser. After applying, `send_pref_confirmation()` emails the sender list a summary of what changed plus the new effective search.

- **HTTP layer** — `fetch()` picks the best available client at import time: `curl_cffi` (preferred, impersonates Chrome TLS to clear Cloudflare) → `cloudscraper` → plain `requests`. The `impersonate="chrome124"` arg is curl_cffi-only and stripped for the others. Without curl_cffi, Cloudflare will block.

- **server.py** — `http.server.SimpleHTTPRequestHandler` subclass. Serves the directory statically, maps `/` → `dashboard.html`, and exposes two JSON endpoints read straight off disk: `/api/apartments` (apartments.json) and `/api/config` (config.json). CORS open to `*`.

- **dashboard.html** — self-contained frontend, fetches `/api/apartments`.

## Config

`config.json` drives everything; `load_cfg()` re-reads it every cycle, so edits apply live without restart. Key coupling points when editing code:

- Geography is set by `search.region_slug` (the Yad2 region landing-page slug, e.g. `coastal-north` = מישור החוף הצפוני, `tel-aviv-area` = TLV) plus `search.cities` (list of numeric Yad2 cityIds, e.g. `4000`=Haifa, `2100`=Tirat Carmel). All listed cities must belong to the same region slug. To find codes for a new area, hit `https://gw.yad2.co.il/address-autocomplete/realestate/v2?text=<URL-encoded Hebrew>` — it returns `cityId`, `hoodId`, and the `regionHeb`; the region slug is the kebab-case English of `regionHeb`.
- `search.*` thresholds are enforced in `parse_listing()`; the URL params are built in `build_feed_url()`. Changing a filter usually means touching both.
- Neighborhood narrowing is **client-side only**: `target_areas.hebrew`/`english` are free-text keyword filters matched against each listing's city/neighborhood/street text after fetch (Yad2's server-side neighborhood param is ignored). To target specific Haifa neighborhoods, put their names here; to take a whole city (e.g. all of טירת כרמל), the city name keyword matches every listing via its city text.
- `require_elevator` is known to over-filter, because Yad2 feed tags don't reliably carry amenities — leave false unless intentionally aggressive.

Generated/runtime files (`apartments.db`, `apartments.json`, `scraper.log`) are created on first run.

## Gotchas

- This is **not** a git repo.
- Respect `schedule.delay_between_requests_sec` and the inter-city sleeps in `scrape()` — they exist to avoid Cloudflare connection resets. Don't strip them when refactoring.
- Connection-reset warnings (`curl: (35) Recv failure`) in the log are normal and expected — the retry logic absorbs them. They only matter if *every* retry for a request fails.
- Runs unattended via a Windows Task Scheduler task named `Yad2ApartmentFinder` (every 3h, headless `pythonw`, survives reboot). It calls `python scraper.py --once`; the continuous-loop `main()` is the alternative for foreground use.
- `parse_listing()` returning `None` is the rejection path, not an error — many listings are dropped by design.
- The raw Yad2 listing is stashed in each apartment under `_raw` (and DB `raw_json`); useful for discovering new fields when the feed shape shifts.
