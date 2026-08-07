"""
Grailed Brand + Size Watcher
-----------------------------
Checks Grailed for new listings across your curated brand list
(brands.json), filtered to your sizes only, and writes them into
docs/listings.json — the data file the feed webpage reads.

This is meant to run on a schedule via GitHub Actions (see
.github/workflows/scrape.yml), but you can also run it locally:

SETUP:
    pip install playwright
    playwright install chromium

USAGE:
    python grailed_scraper.py

Each run merges newly found listings into docs/listings.json and
prunes anything older than PRUNE_AFTER_DAYS, so the feed stays a
rolling window rather than growing forever.

IMPORTANT — please read:
Grailed is a JavaScript-rendered (React) site, so this uses Playwright
(a real headless browser) rather than a simple HTTP request.

This was written without the ability to live-test against Grailed
(sandboxed environment, no network access). The CSS selectors in
SELECTORS below are based on Grailed's known page structure but may
be stale by the time you run this — if a run finds 0 results across
every brand, open a Grailed search page in your own browser,
right-click a listing card -> Inspect, and update the selectors.

This scrapes public search-result pages only (no login, no account
data). Scraping is technically against Grailed's Terms of Service —
this is a personal-use tool, so keep request volume low (the delays
below are intentional) and don't run it constantly or distribute it.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.async_api import async_playwright

# ---------------- CONFIG ----------------

BASE_DIR = Path(__file__).resolve().parent
BRANDS_FILE = BASE_DIR / "brands.json"
LISTINGS_FILE = BASE_DIR.parent / "docs" / "listings.json"

# EDIT ME: include every size label you wear — Grailed labels are
# inconsistent across categories (tops use "M", denim uses "32", etc).
MY_SIZES = ["L", "34"]

# How long a listing stays in the feed before it's pruned out.
# (The feed page itself now has a time-range filter, so this mainly
# exists to cap dead-link buildup from sold/delisted items, not to
# limit what you can browse day-to-day.)
PRUNE_AFTER_DAYS = 30

# Delay between brand searches, in ms — keep this polite/slow.
DELAY_BETWEEN_BRANDS_MS = 1200

SEARCH_URL = "https://www.grailed.com/shop?query={brand}&sold=false"

# Target Grailed's listing-card markup as of when this was written.
# Update if the script stops finding listings — see note above.
#
# The "location" selector is the least certain of these. Grailed shows
# a country flag/indicator on some listing cards for non-domestic
# sellers, but this isn't guaranteed to be present on every card, and
# its exact markup wasn't verifiable without live access to the site.
# Because of that, location filtering is designed to fail open (see
# classify_location below) rather than silently hiding everything if
# this selector turns out to be wrong.
SELECTORS = {
    "listing_card": "div[class*='feed-item']",
    "title": "p[class*='ListingMetadata-module__title']",
    "price": "p[class*='ListingMetadata-module__price']",
    "size": "p[class*='ListingMetadata-module__size']",
    "link": "a[class*='listing-card-link']",
    "image": "img[class*='Image-module__crop']",
    "location": "[class*='Location'], [class*='flag'], [aria-label*='ships from' i]",
}

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


def matches_size(listing_size):
    # Exact token match, not substring — otherwise "L" would also match
    # "XL" / "XXL", and "34" would match "134" etc. Grailed size text is
    # usually short (e.g. "L", "W34", "34x32"), so split on common
    # separators and compare each piece exactly.
    import re
    normalized = re.sub(r"(?<=\d)\s*[xX]\s*(?=\d)", "/", listing_size.strip())
    tokens = re.split(r"[\s/,\-]+", normalized)
    tokens = [t.upper().lstrip("W") for t in tokens if t]  # "W34" -> "34"
    wanted = [s.upper() for s in MY_SIZES]
    return any(t in wanted for t in tokens)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Common ways a US/Canada (or other) location might show up in Grailed's
# markup — flag emoji, alt text, country name, or ISO code.
US_MARKERS = ["🇺🇸", "united states", "usa", "u.s.", " us "]
CA_MARKERS = ["🇨🇦", "canada", " ca "]


def classify_location(raw):
    """
    Returns 'US', 'CA', 'OTHER', or 'UNKNOWN'.
    UNKNOWN means the selector found nothing — this is treated as
    "probably fine, can't confirm" rather than excluded, since an
    unverified selector failing shouldn't silently empty the whole feed.
    Once you've confirmed (via the location tag shown on each card in
    the feed page) that this is actually detecting flags correctly,
    you can tighten this in the frontend filter if you want.
    """
    if not raw:
        return "UNKNOWN"
    text = f" {raw.strip().lower()} "
    if any(m in text for m in US_MARKERS):
        return "US"
    if any(m in text for m in CA_MARKERS):
        return "CA"
    return "OTHER"


# ---------------- SCRAPER ----------------


async def scrape_brand(page, brand, category):
    url = SEARCH_URL.format(brand=brand.replace(" ", "+"))
    await page.goto(url, wait_until="networkidle", timeout=20000)
    await page.wait_for_timeout(1200)  # let React hydrate

    cards = await page.query_selector_all(SELECTORS["listing_card"])
    results = []
    for card in cards:
        try:
            title_el = await card.query_selector(SELECTORS["title"])
            price_el = await card.query_selector(SELECTORS["price"])
            size_el = await card.query_selector(SELECTORS["size"])
            link_el = await card.query_selector(SELECTORS["link"])
            img_el = await card.query_selector(SELECTORS["image"])
            loc_el = await card.query_selector(SELECTORS["location"])

            title = (await title_el.inner_text()).strip() if title_el else ""
            price = (await price_el.inner_text()).strip() if price_el else ""
            size = (await size_el.inner_text()).strip() if size_el else ""
            href = await link_el.get_attribute("href") if link_el else None
            image = await img_el.get_attribute("src") if img_el else None

            location_raw = None
            if loc_el:
                location_raw = (await loc_el.get_attribute("aria-label")) or (await loc_el.get_attribute("alt"))
                if not location_raw:
                    location_raw = (await loc_el.inner_text()).strip()

            if not href:
                continue

            results.append(
                {
                    "brand": brand,
                    "category": category,
                    "title": title,
                    "price": price,
                    "size": size,
                    "link": f"https://www.grailed.com{href}",
                    "image": image,
                    "location": classify_location(location_raw),
                    "location_raw": location_raw,
                }
            )
        except Exception:
            continue
    return results


async def run():
    brand_pairs = load_brands()

    existing = load_json(LISTINGS_FILE, {"updated_at": None, "listings": []})
    existing_by_link = {item["link"]: item for item in existing.get("listings", [])}

    print(f"Checking {len(brand_pairs)} brands for sizes {MY_SIZES}...\n")

    new_count = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        for i, (brand, category) in enumerate(brand_pairs, 1):
            print(f"[{i}/{len(brand_pairs)}] {brand}")
            try:
                results = await scrape_brand(page, brand, category)
            except Exception as e:
                print(f"    ! failed: {e}")
                continue

            for item in results:
                if item["link"] in existing_by_link:
                    continue
                if not matches_size(item["size"]):
                    continue
                item["first_seen"] = now_iso()
                existing_by_link[item["link"]] = item
                new_count += 1

            await page.wait_for_timeout(DELAY_BETWEEN_BRANDS_MS)

        await browser.close()

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

    # Newest first
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
    asyncio.run(run())
