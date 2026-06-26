# Haifa Migration PLAN

Switch the scraper from Tel Aviv to the Haifa coastal-north region.

## Target search
- **Cities:** Haifa (cityId 4000) + Tirat Carmel (cityId 2100)
- **Region slug:** `coastal-north` (מישור החוף הצפוני), topArea 25, area 5
- **Haifa neighborhoods wanted:** נווה דוד (hood 606), נאות פרס (hood 607)
- **Tirat Carmel:** whole city
- **Max rent:** ₪3,800. Min rent: 0 (none).
- **Other filters:** permissive to start (no room limit, no min sqm, ground floor allowed). Tighten later.

## Yad2 API facts discovered
- Feed URL: `/realestate/_next/data/{BUILD_ID}/rent/coastal-north.json?maxPrice=3800&multiCity={CITY}&slug=coastal-north`
- `multiNeighborhood` param is ignored server-side → neighborhood narrowing must be client-side (keyword match on listing text).
- `multiCity` does NOT accept comma-combined cities → query each city in its own request.

## Code changes
1. **config.json** — restructure `search`:
   - add `cities: [4000, 2100]`, `region_slug: "coastal-north"`
   - `price_max: 3800`, `price_min: 0`
   - relax `rooms_min/max`, `min_sqm`, `exclude_ground_floor`
   - `target_areas.hebrew: ["נווה דוד", "נאות פרס", "טירת כרמל"]`, english: []
2. **scraper.py**
   - `build_feed_url(build_id, cfg, city, page)` — slug + per-city from config (drop hardcoded `tel-aviv-area`).
   - `scrape()` — loop over `cfg["search"]["cities"]` instead of TLV `neighborhood_groups`; drop hardcoded `NEIGHBORHOODS` dict.
   - Area keyword filter at end stays (Haifa hoods narrowed by נווה דוד/נאות פרס; Tirat Carmel matched via city text).
3. Reset DB (old TLV listings irrelevant), run `--once`, verify matches + email.

## Verification
- `--debug` style probe already confirmed: Haifa 43 listings, Tirat Carmel 40 listings via coastal-north slug.
- After refactor: `python scraper.py --reset` then `--once`; expect matches only from נווה דוד / נאות פרס / טירת כרמל under ₪3,800.
