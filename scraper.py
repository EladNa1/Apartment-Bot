#!/usr/bin/env python3
"""
Yad2 Apartment Scraper v3.0
============================
Uses Yad2's actual Next.js _next/data endpoint (discovered April 2026).

URL pattern:
  https://www.yad2.co.il/realestate/_next/data/{BUILD_ID}/rent/tel-aviv-area.json
  ?minRooms=2&maxRooms=3&multiCity=5000&minPrice=7000&maxPrice=10000&slug=tel-aviv-area

The BUILD_ID changes on each Yad2 deploy — we extract it from the main page.

Usage:
  python scraper.py --debug    # Test connectivity
  python scraper.py --once     # Single scan
  python scraper.py            # Continuous (every N hours)
  python scraper.py --reset    # Clear DB
"""

import json, logging, os, random, re, smtplib, sqlite3, sys, time
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

from llm import gemini_normalize  # LLM (Gemini) connection helpers

try:
    from curl_cffi import requests as http
    HTTP_LIB = "curl_cffi"
except ImportError:
    try:
        import cloudscraper
        _cs = cloudscraper.create_scraper(browser={"browser":"chrome","platform":"windows","desktop":True})
        class _CS:
            @staticmethod
            def get(url, **kw):
                kw.pop("impersonate", None)
                return _cs.get(url, **kw)
        http = _CS()
        HTTP_LIB = "cloudscraper"
    except ImportError:
        import requests as http
        HTTP_LIB = "requests"

DIR = Path(__file__).parent
DB = DIR / "apartments.db"
OUT = DIR / "apartments.json"

# Windows consoles default to cp1252 and choke on Hebrew / ₪ / ✓ in log lines.
# Force UTF-8 so StreamHandler doesn't raise UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(DIR / "scraper.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("yad2")

# Secrets live in .env (gitignored), NOT in config.json. Each entry maps an env
# var to a dotted path inside config["notifications"]. They are overlaid onto the
# config at load time and stripped again before the config is ever written to disk,
# so config.json stays safe to commit.
ENV_SECRETS = {
    "EMAIL_USER":         "email_user",
    "EMAIL_PASS":         "email_pass",
    "EMAIL_TO":           "email_to",
    "GEMINI_API_KEY":     "gemini_api_key",
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "TELEGRAM_CHAT_ID":   "telegram_chat_id",
    # ALLOWED_SENDERS is comma-separated → a list; handled specially.
}

def _load_env():
    """Parse a simple KEY=VALUE .env file (if present). No external dependency."""
    env = {}
    p = DIR / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    # Process environment overrides the file.
    for k in list(ENV_SECRETS) + ["ALLOWED_SENDERS"]:
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env

def load_cfg():
    with open(DIR / "config.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)
    env = _load_env()
    n = cfg.setdefault("notifications", {})
    for env_key, field in ENV_SECRETS.items():
        if env.get(env_key):
            n[field] = env[env_key]
    if env.get("ALLOWED_SENDERS"):
        n["allowed_senders"] = [s.strip() for s in env["ALLOWED_SENDERS"].split(",") if s.strip()]
    return cfg

# config.json fields that must never be persisted (they come from .env).
_SECRET_FIELDS = list(ENV_SECRETS.values()) + ["allowed_senders"]

def save_cfg(cfg):
    """Write config.json with all secret fields stripped back to placeholders."""
    import copy
    safe = copy.deepcopy(cfg)
    n = safe.get("notifications", {})
    for field in _SECRET_FIELDS:
        if field == "allowed_senders":
            n[field] = []
        elif field in n:
            n[field] = ""
    with open(DIR / "config.json", "w", encoding="utf-8") as f:
        json.dump(safe, f, ensure_ascii=False, indent=2)


# ============================================================================
# EMAIL COMMANDS (reply-to-update preferences)
# ============================================================================

def _email_body_text(msg):
    """Pull plain-text body out of an email message (handles multipart)."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
    except Exception:
        return ""

def parse_commands(body, cfg):
    """Apply structured preference commands from an email body.

    Returns a list of human-readable descriptions of the changes applied
    (empty list = nothing changed).

    Commands (one per line): maxprice N | minprice N | maxrooms N | minrooms N |
    minsqm N | exclude_ground on|off | parking on|off | area+ <name> | area- <name> |
    topn N
    """
    s = cfg["search"]; ta = cfg["target_areas"]; nt = cfg["notifications"]
    applied = []
    # Strip quoted reply lines (Gmail prefixes with ">") and stop at the quote header.
    for raw in body.splitlines():
        line = raw.strip().lstrip(">").strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("on ") and "wrote:" in low:  # quoted original — stop parsing
            break
        parts = line.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        desc = None
        try:
            if cmd == "maxprice":
                s["price_max"] = int(arg); desc = f"מחיר מקסימלי → ₪{int(arg):,}"
            elif cmd == "minprice":
                s["price_min"] = int(arg); desc = f"מחיר מינימלי → ₪{int(arg):,}"
            elif cmd == "maxrooms":
                s["rooms_max"] = float(arg); desc = f"חדרים מקסימום → {arg}"
            elif cmd == "minrooms":
                s["rooms_min"] = float(arg); desc = f"חדרים מינימום → {arg}"
            elif cmd == "minsqm":
                s["min_sqm"] = int(arg); desc = f'מ"ר מינימום → {arg}'
            elif cmd in ("exclude_ground", "exclude_ground_floor"):
                v = arg.lower() in ("on", "true", "yes", "1")
                s["exclude_ground_floor"] = v; desc = f"דילוג קומת קרקע → {'כן' if v else 'לא'}"
            elif cmd == "parking":
                v = arg.lower() in ("on", "true", "yes", "1")
                s["prefer_parking"] = v; desc = f"העדפת חנייה → {'כן' if v else 'לא'}"
            elif cmd == "topn":
                nt["email_top_n"] = int(arg); desc = f"מספר דירות במייל → {arg}"
            elif cmd in ("area+", "area_add"):
                lst = ta["english"] if arg.isascii() else ta["hebrew"]
                if arg and arg not in lst:
                    lst.append(arg); desc = f"נוסף אזור → {arg}"
            elif cmd in ("area-", "area_del", "area_remove"):
                for lst in (ta["hebrew"], ta["english"]):
                    if arg in lst:
                        lst.remove(arg); desc = f"הוסר אזור → {arg}"
            else:
                log.info(f"  email cmd ignored (unknown): {line}")
                continue
            if desc:
                applied.append(desc)
                log.info(f"  email cmd applied: {line}")
        except Exception as e:
            log.warning(f"  email cmd bad '{line}': {e}")
    return applied

def apply_email_commands(cfg):
    """Read unseen whitelisted emails, apply any preference commands, save config.

    Returns the (possibly updated) config dict.
    """
    n = cfg["notifications"]
    if not n.get("imap_enabled") or not n.get("email_user") or not n.get("email_pass"):
        return cfg
    import imaplib, email
    allowed = {a.strip().lower() for a in n.get("allowed_senders", [])}
    applied_all = []
    try:
        M = imaplib.IMAP4_SSL(n.get("imap_server", "imap.gmail.com"))
        M.login(n["email_user"], n["email_pass"])
        M.select("INBOX")
        typ, data = M.search(None, "UNSEEN")
        ids = data[0].split() if data and data[0] else []
        log.info(f"Email inbox: {len(ids)} unseen message(s)")
        for num in ids:
            typ, md = M.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(md[0][1])
            sender = email.utils.parseaddr(msg.get("From", ""))[1].lower()
            M.store(num, "+FLAGS", "\\Seen")  # mark read regardless
            if allowed and sender not in allowed:
                log.info(f"  ignoring email from non-whitelisted sender: {sender}")
                continue
            log.info(f"  reading commands from: {sender}")
            body = _email_body_text(msg)
            # Gemini turns free-text into canonical commands; fall back to raw keywords.
            normalized = gemini_normalize(body, cfg)
            applied_all += parse_commands(normalized if normalized is not None else body, cfg)
        M.logout()
    except Exception as e:
        log.warning(f"IMAP/email-command error: {e}")
    if applied_all:
        save_cfg(cfg)
        log.info("Config updated from email commands")
        send_pref_confirmation(applied_all, cfg)
    return cfg


def send_pref_confirmation(applied, cfg):
    """Email back a summary of the preference changes that were applied."""
    n = cfg["notifications"]
    if not n.get("email_enabled") or not n.get("email_smtp") or not applied:
        return
    s = cfg["search"]
    items = "".join(f'<li style="margin:4px 0;">{a}</li>' for a in applied)
    areas = "، ".join(cfg["target_areas"]["hebrew"] + cfg["target_areas"]["english"])
    html = f"""<!DOCTYPE html><html dir="rtl" lang="he"><head><meta charset="utf-8"></head>
    <body style="margin:0;background:#f4f6f8;font-family:Arial,'Segoe UI',sans-serif;padding:24px;">
      <table cellpadding="0" cellspacing="0" width="100%"><tr><td align="center">
        <table width="560" style="max-width:560px;background:#fff;border-radius:10px;border:1px solid #e3e8ee;">
          <tr><td style="padding:20px 24px;">
            <div style="font-size:20px;font-weight:800;color:#137333;">✅ ההעדפות עודכנו</div>
            <ul style="font-size:14px;color:#222;padding-right:18px;">{items}</ul>
            <hr style="border:none;border-top:1px solid #eee;">
            <div style="font-size:13px;color:#555;">
              <b>חיפוש נוכחי:</b><br>
              מחיר: ₪{s['price_min']:,}–{s['price_max']:,}<br>
              חדרים: {s['rooms_min']}–{s['rooms_max']} · מ"ר מינ׳: {s['min_sqm']}<br>
              אזורים: {areas}
            </div>
          </td></tr>
        </table>
      </td></tr></table>
    </body></html>"""
    plain = "ההעדפות עודכנו:\n" + "\n".join(f"• {a}" for a in applied)
    from email.mime.multipart import MIMEMultipart
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "✅ העדפות החיפוש עודכנו"
    msg["From"] = n["email_user"]; msg["To"] = n["email_to"]
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP(n["email_smtp"], n["email_port"]) as srv:
            srv.starttls(); srv.login(n["email_user"], n["email_pass"]); srv.send_message(msg)
        log.info(f"Preference confirmation sent ({len(applied)} change(s))")
    except Exception as e:
        log.warning(f"Confirmation email err: {e}")


# --- Constants ---
YAD2_BASE = "https://www.yad2.co.il"
YAD2_RENT_PAGE = f"{YAD2_BASE}/realestate/rent"
ITEM_URL = f"{YAD2_BASE}/item/{{}}"

UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
]

def hdrs():
    return {
        "User-Agent": random.choice(UAS),
        "Accept": "application/json, text/html, */*",
        "Accept-Language": "he-IL,he;q=0.9,en;q=0.7",
        "Referer": YAD2_RENT_PAGE,
    }

def fetch(url, as_json=True, retries=3):
    """Fetch a URL with TLS impersonation and retries."""
    for attempt in range(retries):
        try:
            kw = {"headers": hdrs(), "timeout": 30}
            if HTTP_LIB == "curl_cffi":
                kw["impersonate"] = "chrome124"
            r = http.get(url, **kw)
            if hasattr(r, "status_code") and r.status_code != 200:
                log.warning(f"HTTP {r.status_code} from {url[:80]}")
                return None
            return r.json() if as_json else r.text
        except Exception as e:
            log.warning(f"Fetch attempt {attempt+1}/{retries} failed: {e}")
            if attempt < retries - 1:
                time.sleep(2 + random.uniform(1, 3))
    return None


# ============================================================================
# BUILD ID EXTRACTION
# ============================================================================

def _extract_build_id(html):
    """Pull the Next.js build ID out of page HTML, trying several patterns."""
    if not html:
        return None
    # Pattern 1: __NEXT_DATA__ JSON (most reliable on current Yad2)
    m = re.search(r'"buildId"\s*:\s*"([^"]+)"', html)
    if m:
        return m.group(1), "__NEXT_DATA__"
    # Pattern 2: _next/data/BUILD_ID/ in script src
    m = re.search(r'/_next/data/([a-zA-Z0-9_-]+)/', html)
    if m:
        return m.group(1), "_next/data"
    # Pattern 3: any _next/static/BUILD_ID
    m = re.search(r'/_next/static/([a-zA-Z0-9_-]{10,})/', html)
    if m:
        return m.group(1), "_next/static"
    return None


def get_build_id(rounds=6):
    """Extract the Next.js build ID from the main Yad2 page.

    The main HTML page is heavily Cloudflare-throttled and resets the connection
    on most attempts, so we retry generously — it succeeds roughly 1 in 6 tries.
    """
    for r in range(rounds):
        html = fetch(YAD2_RENT_PAGE, as_json=False, retries=4)
        found = _extract_build_id(html)
        if found:
            bid, src = found
            log.info(f"Build ID (from {src}): {bid}")
            return bid
        log.warning(f"Build ID round {r+1}/{rounds} failed — retrying")
        time.sleep(2 + random.uniform(0, 2))
    log.error("Could not find build ID after all retries")
    return None


# ============================================================================
# FEED FETCHING
# ============================================================================

def build_feed_url(build_id, cfg, city, page=1):
    """Build the _next/data feed URL for one city in the configured region.

    Note: Yad2 ignores multiNeighborhood server-side and won't combine multiple
    cities in one multiCity param — so we query one city at a time and narrow to
    the wanted neighborhoods client-side via the area-keyword filter.
    """
    s = cfg["search"]
    slug = s["region_slug"]
    params = {
        "maxPrice": str(s["price_max"]),
        "multiCity": str(city),
        "slug": slug,
    }
    if s.get("price_min"):
        params["minPrice"] = str(s["price_min"])
    if s.get("rooms_min"):
        params["minRooms"] = str(s["rooms_min"])
    if s.get("rooms_max") and s["rooms_max"] < 99:
        params["maxRooms"] = str(s["rooms_max"])
    if s.get("min_sqm"):
        params["squareMeterMin"] = str(s["min_sqm"])
    if s.get("require_elevator"):
        params["elevator"] = "1"
    if page > 1:
        params["page"] = str(page)

    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{YAD2_BASE}/realestate/_next/data/{build_id}/rent/{slug}.json?{qs}"


def extract_listings(data):
    """Extract apartment listings from Yad2's Next.js response."""
    items = []
    try:
        queries = data.get("pageProps", {}).get("dehydratedState", {}).get("queries", [])
        for q in queries:
            state_data = q.get("state", {}).get("data", {})
            if not isinstance(state_data, dict):
                continue
            # Listings are in "private" and "agency" arrays
            for key in ("private", "agency", "platinum", "items", "feed_items"):
                lst = state_data.get(key)
                if isinstance(lst, list) and lst:
                    for item in lst:
                        if isinstance(item, dict) and item.get("token"):
                            items.append(item)
    except Exception as e:
        log.warning(f"Extract error: {e}")

    # Deduplicate by token
    seen = set()
    unique = []
    for item in items:
        tok = item.get("token")
        if tok and tok not in seen:
            seen.add(tok)
            unique.append(item)

    return unique


def extract_pagination(data):
    """Check if there are more pages."""
    try:
        queries = data.get("pageProps", {}).get("dehydratedState", {}).get("queries", [])
        for q in queries:
            sd = q.get("state", {}).get("data", {})
            if isinstance(sd, dict):
                pagination = sd.get("pagination", {})
                if pagination:
                    current = pagination.get("currentPage", 1)
                    total = pagination.get("totalPages", 1)
                    return current < total
    except:
        pass
    return False


# ============================================================================
# PARSING
# ============================================================================

def parse_listing(item, cfg):
    """Parse a single Yad2 Next.js listing into our format."""
    s = cfg["search"]
    addr = item.get("address", {})

    # --- ID ---
    token = item.get("token", "")
    if not token:
        return None

    # --- Rooms ---
    details = item.get("additionalDetails", {})
    rooms = details.get("roomsCount")
    if rooms is not None and not (s["rooms_min"] <= rooms <= s["rooms_max"]):
        return None

    # --- Floor ---
    house = addr.get("house", {})
    floor = house.get("floor")
    if s["exclude_ground_floor"] and floor is not None and floor == 0:
        return None

    # --- Price ---
    price = item.get("price")
    if not price or price < s["price_min"] or price > s["price_max"]:
        return None

    # --- Location ---
    street = addr.get("street", {}).get("text", "")
    neighborhood = addr.get("neighborhood", {}).get("text", "")
    city = addr.get("city", {}).get("text", "תל אביב יפו")
    house_num = house.get("number", "")
    address_str = ", ".join(filter(None, [
        f"{street} {house_num}".strip(), neighborhood, city
    ]))

    # --- Area match ---
    area_match = match_area(address_str, neighborhood, street, cfg)

    # --- Size ---
    size = details.get("squareMeter")
    meta = item.get("metaData", {})
    if not size:
        size = meta.get("squareMeterBuild")
    min_sqm = s.get("min_sqm", 0)
    if min_sqm and size is not None and size < min_sqm:
        return None

    # --- Images ---
    images = meta.get("images", [])
    cover = meta.get("coverImage", "")
    if cover and cover not in images:
        images.insert(0, cover)

    # --- Tags / amenities ---
    tags = item.get("tags", [])
    tag_names = " ".join(t.get("name", "") for t in tags if isinstance(t, dict)).lower()

    parking = "חנייה" in tag_names or "חניה" in tag_names or "parking" in tag_names
    elevator = bool("מעלית" in tag_names or "elevator" in tag_names or
                details.get("elevator") or details.get("hasElevator"))
    balcony = bool("מרפסת" in tag_names or "balcony" in tag_names or
               details.get("balcony") or details.get("hasBalcony"))
    ac = "מיזוג" in tag_names or "מזגן" in tag_names
    mamad = 'ממ"ד' in tag_names or "ממד" in tag_names

    # --- Elevator filter (note: Yad2 feed tags don't always include amenities,
    #     so this may filter aggressively. Set require_elevator=false if too strict) ---
    if s.get("require_elevator") and not elevator:
        return None

    # --- Property type ---
    prop_type = details.get("property", {}).get("text", "")

    # --- Ad type ---
    ad_type = item.get("adType", "")
    is_agent = ad_type == "agency" or ad_type == "business"

    # --- Entry date ---
    entry_date = item.get("entryDate", "") or item.get("dateOfEntry", "")

    # --- Coords ---
    coords = addr.get("coords", {})

    return {
        "item_id": token,
        "title": f"{prop_type} {street} {house_num}".strip() or address_str,
        "address": address_str,
        "street": f"{street} {house_num}".strip(),
        "neighborhood": neighborhood,
        "city": city,
        "rooms": rooms,
        "floor": floor,
        "total_floors": None,
        "price": price,
        "price_before": item.get("priceBeforeTag"),
        "size_sqm": size,
        "parking": parking,
        "elevator": elevator,
        "balcony": balcony,
        "ac": ac,
        "mamad": mamad,
        "furnished": "",
        "entry_date": entry_date,
        "description": "",
        "images": images,
        "link": ITEM_URL.format(token),
        "contact_name": "",
        "is_agent": is_agent,
        "area_match": area_match,
        "tags": [t.get("name", "") for t in tags if isinstance(t, dict)],
        "lat": coords.get("lat"),
        "lon": coords.get("lon"),
        "_raw": item,
    }


def match_area(address, neighborhood, street, cfg):
    text = f"{address} {neighborhood} {street}".lower()
    for kw in cfg["target_areas"]["hebrew"] + cfg["target_areas"]["english"]:
        if kw.lower() in text:
            return kw
    return ""


# ============================================================================
# DATABASE
# ============================================================================

def init_db():
    c = sqlite3.connect(DB)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS apartments (
        item_id TEXT PRIMARY KEY, title TEXT, address TEXT, street TEXT,
        neighborhood TEXT, city TEXT, rooms REAL, floor INTEGER,
        total_floors INTEGER, price INTEGER, price_before INTEGER,
        size_sqm INTEGER, parking INT DEFAULT 0, elevator INT DEFAULT 0,
        balcony INT DEFAULT 0, ac INT DEFAULT 0, mamad INT DEFAULT 0,
        furnished TEXT, entry_date TEXT, description TEXT, images TEXT,
        link TEXT, contact_name TEXT, is_agent INT DEFAULT 0,
        first_seen TEXT, last_seen TEXT, is_new INT DEFAULT 1,
        area_match TEXT, tags TEXT, lat REAL, lon REAL, raw_json TEXT,
        notified INT DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS scan_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, total INT, new INT
    )""")
    # Migration: add `notified` to DBs created before this column existed.
    cols = [r[1] for r in c.execute("PRAGMA table_info(apartments)").fetchall()]
    if "notified" not in cols:
        c.execute("ALTER TABLE apartments ADD COLUMN notified INT DEFAULT 0")
    c.commit()
    return c

def upsert(c, a):
    now = datetime.now().isoformat()
    if c.execute("SELECT 1 FROM apartments WHERE item_id=?", (a["item_id"],)).fetchone():
        c.execute("UPDATE apartments SET last_seen=?, price=? WHERE item_id=?",
                  (now, a.get("price"), a["item_id"]))
        c.commit()
        return False
    c.execute(
        """INSERT INTO apartments (item_id,title,address,street,neighborhood,city,
           rooms,floor,total_floors,price,price_before,size_sqm,
           parking,elevator,balcony,ac,mamad,furnished,entry_date,description,
           images,link,contact_name,is_agent,first_seen,last_seen,is_new,
           area_match,tags,lat,lon,raw_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?)""",
        (a["item_id"], a.get("title",""), a.get("address",""), a.get("street",""),
         a.get("neighborhood",""), a.get("city",""), a.get("rooms"), a.get("floor"),
         a.get("total_floors"), a.get("price"), a.get("price_before"), a.get("size_sqm"),
         int(a.get("parking",False)), int(a.get("elevator",False)),
         int(a.get("balcony",False)), int(a.get("ac",False)), int(a.get("mamad",False)),
         a.get("furnished",""), a.get("entry_date",""), a.get("description",""),
         json.dumps(a.get("images",[]),ensure_ascii=False), a.get("link",""),
         a.get("contact_name",""), int(a.get("is_agent",False)),
         now, now,
         a.get("area_match",""), json.dumps(a.get("tags",[]),ensure_ascii=False),
         a.get("lat"), a.get("lon"),
         json.dumps(a.get("_raw",{}),ensure_ascii=False))
    )
    c.commit()
    return True

def export_json(c, cfg):
    cols = [r[1] for r in c.execute("PRAGMA table_info(apartments)").fetchall()]
    rows = c.execute("SELECT * FROM apartments ORDER BY is_new DESC, first_seen DESC").fetchall()
    apts = []
    for r in rows:
        d = dict(zip(cols, r))
        d["images"] = json.loads(d.get("images") or "[]")
        d["tags"] = json.loads(d.get("tags") or "[]")
        for bf in ("parking","elevator","balcony","ac","mamad","is_new","is_agent"):
            d[bf] = bool(d.get(bf))
        d.pop("raw_json", None)
        apts.append(d)
    apts.sort(key=lambda a: (not a["is_new"], not a["parking"], a.get("price") or 99999))
    s = cfg["search"]
    payload = {
        "updated": datetime.now().isoformat(),
        "count": len(apts),
        "config": {
            "rooms": f"{s['rooms_min']}-{s['rooms_max']}",
            "price": f"₪{s['price_min']:,}-{s['price_max']:,}",
            "areas": cfg["target_areas"]["english"],
        },
        "apartments": apts,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log.info(f"Exported {len(apts)} apartments")


# ============================================================================
# MAIN SCRAPE
# ============================================================================

def scrape(cfg):
    s = cfg["search"]
    log.info(f"HTTP: {HTTP_LIB} | rooms={s['rooms_min']}-{s['rooms_max']} | "
             f"price=₪{s['price_min']:,}-{s['price_max']:,}")

    # Step 1: Get build ID
    build_id = get_build_id()
    if not build_id:
        log.error("Cannot get build ID — aborting")
        return []

    seen, apts = set(), []
    delay = cfg["schedule"]["delay_between_requests_sec"]

    # Step 2: Query each configured city separately (Yad2 won't combine cities
    # in one request, and ignores neighborhood filters — we narrow by keyword later).
    for city in s["cities"]:
        log.info(f"Querying city {city} (region {s['region_slug']})")
        page = 1
        while page <= cfg["schedule"]["max_pages"]:
            url = build_feed_url(build_id, cfg, city, page=page)
            time.sleep(delay + random.uniform(0, 1.0))
            data = fetch(url)
            if not data:
                break

            items = extract_listings(data)
            if not items:
                log.info(f"  p{page}: 0 items — stopping")
                break

            n = 0
            for raw in items:
                apt = parse_listing(raw, cfg)
                if apt and apt["item_id"] not in seen:
                    seen.add(apt["item_id"])
                    apts.append(apt)
                    n += 1

            log.info(f"  p{page}: {len(items)} items → {n} new matches")

            if not extract_pagination(data):
                break
            page += 1

        # Extra delay between cities to avoid connection resets
        time.sleep(3 + random.uniform(1, 2))

    # Step 3: Area keyword filter (for broad query results)
    keywords = cfg["target_areas"]["hebrew"] + cfg["target_areas"]["english"]
    filtered = []
    for a in apts:
        if a["area_match"]:
            filtered.append(a)
        else:
            text = f"{a['address']} {a['neighborhood']} {a['street']}".lower()
            for kw in keywords:
                if kw.lower() in text:
                    a["area_match"] = kw
                    filtered.append(a)
                    break

    log.info(f"Total: {len(apts)} → area filtered: {len(filtered)}")
    return filtered


# ============================================================================
# NOTIFICATIONS
# ============================================================================

def notify_tg(new_apts, cfg):
    n = cfg["notifications"]
    if not n.get("telegram_enabled") or not n.get("telegram_bot_token"):
        return
    import requests as rq
    for a in new_apts[:10]:
        p = "🅿️" if a["parking"] else ""
        msg = (f"🏠 <b>דירה חדשה!</b>\n📍 {a['address']}\n"
               f"🛏 {a.get('rooms','?')} חד׳ | קומה {a.get('floor','?')}\n"
               f"💰 ₪{a['price']:,} {p}\n🔗 <a href=\"{a['link']}\">יד2</a>")
        try:
            rq.post(f"https://api.telegram.org/bot{n['telegram_bot_token']}/sendMessage",
                    json={"chat_id":n["telegram_chat_id"],"text":msg,"parse_mode":"HTML"}, timeout=10)
            time.sleep(0.5)
        except: pass

def area_priority(a, cfg):
    """Rank by position in target_areas (earlier = higher priority); unknown last."""
    areas = cfg["target_areas"]["hebrew"] + cfg["target_areas"]["english"]
    am = a.get("area_match", "")
    return areas.index(am) if am in areas else len(areas)

def rank_key(a, cfg):
    """Most-relevant ordering: area priority → cheaper → has parking → bigger."""
    return (
        area_priority(a, cfg),
        a.get("price") or 99999,
        0 if a.get("parking") else 1,
        -(a.get("size_sqm") or 0),
    )

def _card_html(a):
    img = (a.get("images") or [""])[0]
    img_html = (f'<img src="{img}" alt="" width="220" '
                f'style="width:220px;height:160px;object-fit:cover;border-radius:8px 0 0 8px;display:block;">'
                if img else
                '<div style="width:220px;height:160px;background:#e8eef3;border-radius:8px 0 0 8px;"></div>')
    chips = []
    if a.get("parking"):  chips.append("🅿️ חנייה")
    if a.get("elevator"): chips.append("🛗 מעלית")
    if a.get("balcony"):  chips.append("🌿 מרפסת")
    if a.get("mamad"):    chips.append('🛡 ממ"ד')
    chips_html = "".join(
        f'<span style="display:inline-block;background:#eef4ff;color:#2456c4;'
        f'border-radius:12px;padding:2px 10px;margin:2px 4px 2px 0;font-size:12px;">{c}</span>'
        for c in chips)
    size = f"{a['size_sqm']} מ\"ר" if a.get("size_sqm") else ""
    meta = " · ".join(filter(None, [
        f"{a.get('rooms','?')} חד׳", f"קומה {a.get('floor','?')}", size]))
    return f"""
    <tr><td style="padding:0 0 16px 0;">
      <table cellpadding="0" cellspacing="0" width="100%" style="background:#fff;border:1px solid #e3e8ee;border-radius:10px;overflow:hidden;">
        <tr>
          <td width="220" valign="top">{img_html}</td>
          <td valign="top" style="padding:14px 16px;">
            <div style="font-size:18px;font-weight:700;color:#1a73e8;">₪{a.get('price',0):,}</div>
            <div style="font-size:15px;font-weight:600;color:#222;margin:4px 0;">{a.get('address','')}</div>
            <div style="font-size:13px;color:#555;">{meta}</div>
            <div style="margin:8px 0;">{chips_html}</div>
            <a href="{a.get('link','')}" style="display:inline-block;background:#1a73e8;color:#fff;
               text-decoration:none;padding:7px 18px;border-radius:6px;font-size:13px;font-weight:600;">
               צפייה במודעה ←</a>
          </td>
        </tr>
      </table>
    </td></tr>"""

def build_email_html(apts, cfg):
    s = cfg["search"]
    cards = "".join(_card_html(a) for a in apts)
    sub = (f'₪{s["price_min"]:,}–{s["price_max"]:,} · ' +
           " · ".join(cfg["target_areas"]["hebrew"] + cfg["target_areas"]["english"]))
    return f"""<!DOCTYPE html><html dir="rtl" lang="he"><head><meta charset="utf-8"></head>
    <body style="margin:0;padding:0;background:#f4f6f8;font-family:Arial,'Segoe UI',sans-serif;">
      <table cellpadding="0" cellspacing="0" width="100%" style="background:#f4f6f8;padding:24px 0;">
        <tr><td align="center">
          <table cellpadding="0" cellspacing="0" width="620" style="max-width:620px;width:100%;">
            <tr><td style="padding:0 8px 16px;">
              <div style="font-size:22px;font-weight:800;color:#111;">🏠 {len(apts)} דירות חדשות עבורך</div>
              <div style="font-size:13px;color:#667;margin-top:4px;">{sub}</div>
            </td></tr>
            {cards}
            <tr><td style="padding:8px;color:#99a;font-size:11px;text-align:center;">
              נשלח אוטומטית · לשינוי העדפות פשוט השב/י למייל זה בשפה חופשית (לדוגמה: "תוריד את המחיר ל-3200 ותוסיף את אחוזה")
            </td></tr>
          </table>
        </td></tr>
      </table>
    </body></html>"""

def notify_email(apts, cfg):
    n = cfg["notifications"]
    if not n.get("email_enabled") or not n.get("email_smtp") or not apts:
        return
    from email.mime.multipart import MIMEMultipart
    html = build_email_html(apts, cfg)
    plain = "\n".join(f"• {a['address']} | {a.get('rooms','?')} חד׳ | ₪{a.get('price',0):,} | {a['link']}"
                      for a in apts)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🏠 Yad2: {len(apts)} דירות חדשות בחיפה והסביבה"
    msg["From"] = n["email_user"]; msg["To"] = n["email_to"]
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP(n["email_smtp"], n["email_port"]) as s:
            s.starttls(); s.login(n["email_user"], n["email_pass"]); s.send_message(msg)
        log.info(f"Email sent ({len(apts)} listings)")
    except Exception as e:
        log.warning(f"Email err: {e}")


# ============================================================================
# RUN
# ============================================================================

def run_once():
    cfg = load_cfg()
    # Step 0: apply any preference changes mailed in since last run (may rewrite config).
    cfg = apply_email_commands(cfg)
    conn = init_db()
    log.info("=" * 60 + "\nSCAN CYCLE\n" + "=" * 60)
    apts = scrape(cfg)
    new_c = 0
    for a in apts:
        if upsert(conn, a):
            new_c += 1
            p = "✓" if a["parking"] else "✗"
            log.info(f"  🆕 {a['address']} | {a.get('rooms','?')}r | ₪{a['price']:,} | "
                     f"F{a.get('floor','?')} | P:{p} | {a.get('area_match','')}")
    export_json(conn, cfg)
    conn.execute("INSERT INTO scan_log (ts,total,new) VALUES (?,?,?)",
                 (datetime.now().isoformat(), len(apts), new_c))
    conn.commit()
    log.info(f"Done: {len(apts)} found, {new_c} new")

    # Notify: top-N most-relevant listings that were never emailed before.
    if apts:
        ids = [a["item_id"] for a in apts]
        ph = ",".join("?" * len(ids))
        sent = {r[0] for r in conn.execute(
            f"SELECT item_id FROM apartments WHERE notified=1 AND item_id IN ({ph})", ids)}
        unsent = [a for a in apts if a["item_id"] not in sent]
        unsent.sort(key=lambda a: rank_key(a, cfg))
        top = unsent[:cfg["notifications"].get("email_top_n", 10)]
        if top:
            log.info(f"Notifying top {len(top)} of {len(unsent)} never-emailed matches")
            notify_tg(top, cfg)
            notify_email(top, cfg)
            conn.executemany("UPDATE apartments SET notified=1 WHERE item_id=?",
                             [(a["item_id"],) for a in top])
            conn.commit()
    conn.close()
    return new_c

def main():
    cfg = load_cfg()
    hrs = cfg["schedule"]["interval_hours"]
    log.info(f"Yad2 Bot v3.0 | interval={hrs}h | DB={DB}")
    while True:
        try: run_once()
        except KeyboardInterrupt: break
        except Exception as e: log.error(f"Error: {e}", exc_info=True)
        log.info(f"Next in {hrs}h...")
        try: time.sleep(hrs * 3600)
        except KeyboardInterrupt: break

if __name__ == "__main__":
    if "--reset" in sys.argv:
        for p in (DB, OUT): p.unlink(missing_ok=True)
        log.info("Reset done.")
    elif "--debug" in sys.argv:
        log.info("=" * 60)
        log.info(f"DEBUG | HTTP: {HTTP_LIB}")
        log.info("=" * 60)
        cfg = load_cfg()

        # Test build ID
        bid = get_build_id()
        if not bid:
            log.error("FAILED: Cannot get build ID")
            sys.exit(1)

        # Test one feed request (first configured city)
        url = build_feed_url(bid, cfg, cfg["search"]["cities"][0], page=1)
        log.info(f"Feed URL: {url}")
        data = fetch(url)
        if not data:
            log.error("FAILED: Cannot fetch feed")
            sys.exit(1)

        items = extract_listings(data)
        log.info(f"✓ Found {len(items)} raw listings")
        if items:
            first = items[0]
            addr = first.get("address", {})
            log.info(f"Sample: token={first.get('token')}, "
                     f"price={first.get('price')}, "
                     f"rooms={first.get('additionalDetails',{}).get('roomsCount')}, "
                     f"street={addr.get('street',{}).get('text')}, "
                     f"neighborhood={addr.get('neighborhood',{}).get('text')}, "
                     f"floor={addr.get('house',{}).get('floor')}")
            # Parse it
            parsed = parse_listing(first, cfg)
            if parsed:
                log.info(f"Parsed: {parsed['address']} | ₪{parsed['price']:,} | "
                         f"{parsed['rooms']}r | F{parsed['floor']} | "
                         f"imgs={len(parsed['images'])} | area={parsed['area_match']}")
        log.info("=" * 60)
        log.info("✓ Everything works! Run: python scraper.py --once")
    elif "--once" in sys.argv:
        run_once()
    else:
        main()
