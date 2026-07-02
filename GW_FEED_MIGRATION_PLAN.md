# PLAN — Migrate scraper from blocked www `_next/data` to `gw.yad2.co.il` feed

## Problem (diagnosed 2026-07-02)

The daily email stopped arriving. **Not a scheduler problem** — Task Scheduler
`StartWhenAvailable` works (task ran 7/2 09:02 after wake, exit 0). The real cause:

- `www.yad2.co.il` is **TLS connection-reset** from this machine/IP — both the
  build-id HTML page *and* the `_next/data` JSON feed. Every retry fails, so
  `get_build_id()` aborts → `0 found, 0 new` → no email. Confirmed live via
  `scraper.py --debug` and a raw `curl_cffi` probe.
- `gw.yad2.co.il` (Yad2's API gateway) is **NOT blocked** — returns HTTP 200.
- Network itself is fine (google 200, gw autocomplete 200).

## Fix (chosen: replace www→gw + static region id)

Point the scraper at the gw realestate feed, which returns the **same listing
shape** and needs **no BUILD_ID**:

```
GET https://gw.yad2.co.il/realestate-feed/rent/feed?region=5&city=4000&maxPrice=3800&page=1
```

Response: `{"data": {"private":[…], "agency":[…], "platinum":[…], …, "pagination":{"total":N,"totalPages":M}}}`.
Each listing dict is identical to the old `_next/data` item
(`token`, `price`, `address.city.text`, `additionalDetails.roomsCount`,
`metaData.images`, `tags[].name`) — so **`parse_listing()` is unchanged**.

### gw feed param rules (probed)
- **Required:** `region` (numeric regionId). `city` alone → 400 "region is required".
- **Allowed:** `region, city, area, maxPrice, minPrice, minRooms, maxRooms, property, elevator, parking, page`.
- **Rejected (400):** `rooms` (range), `squareMeterMin`, `squareMeter`, `propertyGroup`, `topArea`.
  → sqm filtering stays **client-side** in `parse_listing()` (already there).
- Pagination has no `currentPage`; use `page < data.pagination.totalPages`.

### Region ids (from working gw autocomplete)
- city 4000 (Haifa) → region 5; city 2100 (Tirat Carmel) → region 5.
  Both = "מישור החוף הצפוני" (coastal-north). One static `search.region: 5` fits.

## Changes

1. **config.json** — add `"region": 5` to `search`.
2. **scraper.py**
   - Add `GW_FEED = "https://gw.yad2.co.il/realestate-feed/rent/feed"`; add `Origin` header.
   - `build_feed_url(cfg, city, page)` → build gw URL with allowed params only.
   - `extract_listings(data)` → read listings from `data["data"][…]` container.
   - `extract_pagination(data, page)` → `page < data.data.pagination.totalPages`.
   - `scrape()` → drop `get_build_id()`; loop cities hitting gw; keep inter-request sleeps.
   - `--debug` → hit gw feed directly (no build id).
   - Remove now-dead `get_build_id()` / `_extract_build_id()` (the fragile core).

## Verify
- `python scraper.py --debug` → listings found + one parsed.
- `python scraper.py --once` → `Done: N found` with N>0, email sent.

## Notes / rollback
- If gw ever changes, the old www path is in git history (pre this change).
- Adding a city from another region requires updating `search.region`.
