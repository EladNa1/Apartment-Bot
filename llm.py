# -*- coding: utf-8 -*-
"""LLM helpers — Gemini connection.

Isolates the only place an LLM is used: turning a free-text preference email
(any language) into canonical command lines. The deterministic keyword parser in
scraper.py then applies those lines. Gemini is purely a normalizer here; it never
edits the config directly. No SDK dependency — plain REST via `requests`.
"""
import logging
import requests as rq

log = logging.getLogger("yad2")

GEMINI_PROMPT = """You translate an apartment-search preference message (any language, e.g. Hebrew) \
into command lines for a search bot. Output ONLY command lines, one per line — no explanation, \
no markdown, no code fences. If the message asks for no change, output exactly: NONE

Allowed commands (use these EXACT keywords):
  maxprice <integer>        maximum monthly rent in shekels
  minprice <integer>        minimum monthly rent
  maxrooms <number>         maximum rooms (e.g. 4 or 3.5)
  minrooms <number>         minimum rooms
  minsqm <integer>          minimum size in square meters
  exclude_ground on|off     skip ground-floor apartments
  parking on|off            prefer parking
  topn <integer>            how many listings per email
  area+ <name>              add a neighborhood or city to search (keep original language)
  area- <name>              remove a neighborhood or city

Current settings: max price {price_max}, min price {price_min}, rooms {rooms_min}-{rooms_max}, \
min sqm {min_sqm}, areas: {areas}.

User message:
\"\"\"
{body}
\"\"\"
"""

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

def gemini_normalize(body, cfg):
    """Use Gemini to rewrite a free-text email into canonical command lines.

    Returns command-line text (to feed parse_commands), or None on failure/disabled
    so the caller can fall back to raw keyword parsing.
    """
    n = cfg["notifications"]
    if not n.get("gemini_enabled") or not n.get("gemini_api_key"):
        return None
    model = n.get("gemini_model", "gemini-flash-latest")
    url = GEMINI_ENDPOINT.format(model=model, key=n["gemini_api_key"])
    s = cfg["search"]
    prompt = GEMINI_PROMPT.format(
        price_max=s.get("price_max"), price_min=s.get("price_min"),
        rooms_min=s.get("rooms_min"), rooms_max=s.get("rooms_max"),
        min_sqm=s.get("min_sqm"),
        areas=", ".join(cfg["target_areas"]["hebrew"] + cfg["target_areas"]["english"]),
        body=body[:4000],
    )
    try:
        r = rq.post(url, json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0},
        }, timeout=30)
        if r.status_code != 200:
            log.warning(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
            return None
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        # Strip any stray markdown fences.
        text = text.replace("```", "").strip()
        log.info(f"  Gemini → commands:\n{text}")
        return text
    except Exception as e:
        log.warning(f"Gemini error: {e}")
        return None
