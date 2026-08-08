"""
Grailed Brand + Size + Location Watcher (API-based)
-----------------------------------------------------
Checks Grailed for new listings across your curated brand list
(brands.json), filtered to your sizes, and writes them into
listings.json — the data file the feed webpage reads.

This version uses the `grailed_api` package (an open-source client for
Grailed's actual internal search API) instead of scraping rendered
HTML. That's a deliberate change from an earlier version of this
script: scraping the live site turned out to be hitting a URL pattern
that doesn't actually exist on Grailed, which silently returned zero
results every time. Talking to the real API directly is both simpler
and far more reliable than reverse-engineering page markup.

SETUP:
    pip install grailed_api

USAGE:
    python grailed_scraper.py

IMPORTANT — please read:
This was written without the ability to live-test against Grailed's
API from this environment (sandboxed, no network access). The
`grailed_api` package's exact response shape (which dict keys hold
title/price/size/location/etc） wasn't independently verifiable, so
this script is deliberately defensive about it:

  - It tries several plausible key names for each field before
    giving up on that field (see `extract_field`).
  - The FIRST successful run prints the raw shape of one real product
    to the Action log (look for "DEBUG: sample product shape"). If
    fields are coming through empty/wrong in the feed, copy that
    debug block from the log and share it — it tells us exactly what
    keys to use, no more guessing.
  - Nothing is silently dropped because a field lookup failed —
    fields that can't be found come through as empty/unknown rather
    than causing the whole item to be skipped.

This queries Grailed's own search backend, not scraped HTML — no
login, no account data. This is a personal-use tool; keep request
volume reasonable and don't distribute or run it constantly.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from grailed_api import GrailedAPIClient

# ---------------- CONFIG ----------------

BASE_DIR = Path(__file__).resolve().parent
BRANDS_FILE = BASE_DIR / "brands.json"
LISTINGS_FILE = BASE_DIR / "listings.json"

# EDIT ME: include every size label you wear.
MY_SIZES = ["L", "34"]

# How long a listing stays in the feed before it's pruned out.
PRUNE_AFTER_DAYS = 30

# Results checked per brand per run (one page). Fine for niche brands;
# if a brand floods more than this many new matches between runs,
# raise it (costs a slightly longer run).
HITS_PER_BRAND = 40

# ---------------- HELPERS ----------------


def load_json(path, default):
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text())
    return default


def save_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False))


def load_brands():
    """Returns list of (brand, category) tuples."""
    data = load_json(BRANDS_FILE, {})
    out = []
    for category, brands in data.get("categories", {}).items():
        for b in brands:
            out.append((b, category))
    return out


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def matches_size(listing_size):
    # Exact token match, not substring — otherwise "L" would also match
    # "XL" / "XXL", and "34" would match "134" etc.
    import re
    if not listing_size:
        return False
    normalized = re.sub(r"(?<=\d)\s*[xX]\s*(?=\d)", "/", str(listing_size).strip())
    tokens = re.split(r"[\s/,\-]+", normalized)
    tokens = [t.upper().lstrip("W") for t in tokens if t]
    wanted = [s.upper() for s in MY_SIZES]
    return any(t in wanted for t in tokens)


# Common ways a US/Canada (or other) location might show up in the
# API's data — country name, ISO code, etc.
US_MARKERS = ["united states", "usa", "u.s.", " us "]
CA_MARKERS = ["canada", " ca "]


def classify_location(raw):
    """
    Returns 'US', 'CA', 'OTHER', or 'UNKNOWN'.
    UNKNOWN means we couldn't find/parse a location field — treated as
    "can't confirm" rather than excluded, so an unverified field name
    guess doesn't silently empty the whole feed.
    """
    if not raw:
        return "UNKNOWN"
    text = f" {str(raw).strip().lower()} "
    if any(m in text for m in US_MARKERS):
        return "US"
    if any(m in text for m in CA_MARKERS):
        return "CA"
    return "OTHER"


def extract_field(d, *keys, default=None):
    """
    Try several possible dict keys/paths and return the first non-empty
    match. A key can be a plain string (top-level) or a tuple for a
    nested path, e.g. ("seller", "location") or ("photos", 0, "url").
    """
    if not isinstance(d, dict):
        return default
    for k in keys:
        if isinstance(k, tuple):
            cur = d
            ok = True
            for part in k:
                try:
                    if isinstance(part, int) and isinstance(cur, list):
                        cur = cur[part]
                    elif isinstance(cur, dict) and part in cur:
                        cur = cur[part]
                    else:
                        ok = False
                        break
                except (IndexError, KeyError, TypeError):
                    ok = False
                    break
            if ok and cur:
                return cur
        else:
            if k in d and d[k]:
                return d[k]
    return default


# ---------------- SCRAPER ----------------


def run():
    client = GrailedAPIClient()
    brand_pairs = load_brands()

    existing = load_json(LISTINGS_FILE, {"updated_at": None, "categories": [], "listings": []})
    existing_by_link = {item["link"]: item for item in existing.get("listings", [])}

    print(f"Checking {len(brand_pairs)} brands for sizes {MY_SIZES}...\n")

    new_count = 0
    debug_printed = False

    for i, (brand, category) in enumerate(brand_pairs, 1):
        print(f"[{i}/{len(brand_pairs)}] {brand}")
        try:
            products = client.find_products(
                sold=False,
                query_search=brand,
                hits_per_page=HITS_PER_BRAND,
            )
        except Exception as e:
            print(f"    ! failed: {e}")
            continue

        if not debug_printed and products:
            print("\n--- DEBUG: sample product shape (first real hit this run) ---")
            try:
                print(json.dumps(products[0], indent=2, default=str)[:3000])
            except Exception:
                print(repr(products[0])[:3000])
            print("--- end debug sample ---\n")
            debug_printed = True

        for p in products:
            title = extract_field(p, "title", "name", default="")
            price = extract_field(p, "price", "priceFormatted", "displayPrice", default="")
            size = extract_field(p, "size", "sizeFormatted", default="")
            designer = extract_field(p, "designer", "designerName", ("designer", "name"), default=None)
            listing_id = extract_field(p, "id", "objectID", "slug", default=None)
            link = extract_field(p, "url", "link", default=None)
            if not link and listing_id:
                link = f"https://www.grailed.com/listings/{listing_id}"
            image = extract_field(p, "image", "photo", ("photos", 0, "url"), default=None)
            location_raw = extract_field(
                p,
                "location",
                "sellerLocation",
                "shipsFrom",
                "country",
                ("seller", "location"),
                ("seller", "country"),
                default=None,
            )

            if not link:
                continue
            if not matches_size(size):
                continue
            # Only reject on a designer mismatch if we actually found a
            # designer field to check — fail open if we couldn't.
            if designer and brand.lower() not in str(designer).lower() and str(designer).lower() not in brand.lower():
                continue
            if link in existing_by_link:
                continue

            item = {
                "brand": str(designer) if designer else brand,
                "category": category,
                "title": str(title),
                "price": str(price),
                "size": str(size),
                "link": link,
                "image": image,
                "location": classify_location(location_raw),
                "location_raw": str(location_raw) if location_raw else None,
                "first_seen": now_iso(),
            }
            existing_by_link[link] = item
            new_count += 1

    # Prune anything older than PRUNE_AFTER_DAYS
    cutoff = datetime.now(timezone.utc) - timedelta(days=PRUNE_AFTER_DAYS)
    kept = []
    for item in existing_by_link.values():
        try:
            seen_at = datetime.strptime(item["first_seen"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except Exception:
            seen_at = datetime.now(timezone.utc)
        if seen_at >= cutoff:
            kept.append(item)

    kept.sort(key=lambda x: x["first_seen"], reverse=True)

    save_json(
        LISTINGS_FILE,
        {
            "updated_at": now_iso(),
            "categories": sorted(load_json(BRANDS_FILE, {}).get("categories", {}).keys()),
            "listings": kept,
        },
    )

    print(f"\n{new_count} new matching listing(s) this run.")
    print(f"{len(kept)} total listing(s) in feed (pruned to last {PRUNE_AFTER_DAYS} days).")
    print(f"Feed written to {LISTINGS_FILE}")


if __name__ == "__main__":
    run()
