#!/usr/bin/env python3
"""Scrape Tubi carousels and the titles under each, for home, movies, and tv-shows."""
import csv, json, re, sys
from datetime import datetime, timezone
from pathlib import Path
import requests

PAGES = {
    "home": "https://tubitv.com/",
    "movies": "https://tubitv.com/movies",
    "tv_shows": "https://tubitv.com/tv-shows",
}
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}
OUT_DIR = Path(__file__).parent / "output"


def extract_data(html: str) -> dict:
    """Pull the `window.__data={...}` object out of the page HTML."""
    m = re.search(r"window\.__data\s*=\s*(\{.*?\});?\s*</script>", html, re.DOTALL)
    if not m:
        m = re.search(r"window\.__data\s*=\s*(\{.*\})\s*;", html, re.DOTALL)
    if not m:
        raise ValueError("window.__data not found in page")
    blob = m.group(1)
    blob = re.sub(r":\s*undefined", ": null", blob)
    return json.loads(blob)


def parse_carousels(data: dict) -> list:
    container = data.get("container", {})
    id_map = container.get("containerIdMap", {}) or {}
    child_map = container.get("containerChildrenIdMap", {}) or {}
    order = container.get("containersList") or list(id_map.keys())
    by_id = (data.get("video", {}) or {}).get("byId", {}) or {}

    carousels = []
    for cid in order:
        meta = id_map.get(cid, {})
        item_ids = child_map.get(cid, []) or []
        items = []
        for iid in item_ids:
            vid = by_id.get(iid)
            items.append({
                "id": iid,
                "title": vid.get("title") if vid else None,
                "type": vid.get("type") if vid else None,
            })
        carousels.append({
            "carousel_id": cid,
            "carousel_title": meta.get("title") or cid,
            "num_items": len(item_ids),
            "items": items,
        })
    return carousels


def scrape_page(name: str, url: str, session: requests.Session) -> dict:
    resp = session.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = extract_data(resp.text)
    return {
        "page": name,
        "url": url,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "carousels": parse_carousels(data),
    }


def main():
    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session = requests.Session()
    results = []
    for name, url in PAGES.items():
        try:
            page = scrape_page(name, url, session)
            print(f"[{name}] {len(page['carousels'])} carousels")
            results.append(page)
        except Exception as e:
            print(f"[{name}] ERROR: {e}", file=sys.stderr)

    json_path = OUT_DIR / f"tubi_{stamp}.json"
    json_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))

    csv_path = OUT_DIR / f"tubi_{stamp}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["page", "carousel_title", "carousel_id",
                    "item_position", "item_title", "item_id", "scraped_at"])
        for page in results:
            for car in page["carousels"]:
                if not car["items"]:
                    w.writerow([page["page"], car["carousel_title"],
                                car["carousel_id"], "", "", "", page["scraped_at"]])
                for i, it in enumerate(car["items"], 1):
                    w.writerow([page["page"], car["carousel_title"],
                                car["carousel_id"], i, it["title"],
                                it["id"], page["scraped_at"]])

    print(f"Saved {json_path.name} and {csv_path.name} in {OUT_DIR}")


if __name__ == "__main__":
    main()
