#!/usr/bin/env python3
"""
Scrape Tubi carousels and title metadata.

- Home page: parses SSR window.__data (fast, no auth needed)
- Movies / TV shows: calls the tensor homescreen API with an anonymous token,
  then paginates each carousel via v7/containers to reach ITEMS_PER_CAROUSEL
"""
import csv, hashlib, hmac, json, re, sys, uuid, base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import requests

OUT_DIR            = Path(__file__).parent.parent / "data"
ITEMS_PER_CAROUSEL = 50
PAGE_SIZE          = 10   # items per v7/containers call
MAX_WORKERS        = 8    # concurrent pagination requests

ACCOUNT  = "https://account.production-public.tubi.io"
TENSOR   = "https://tensor-cdn.production-public.tubi.io"
HOME_URL = "https://tubitv.com/"

PAGES = {
    "home":     ("ssr", HOME_URL),
    "movies":   ("api", "movie"),
    "tv_shows": ("api", "tv"),
}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://tubitv.com",
    "Referer": "https://tubitv.com/",
    "Content-Type": "application/json",
}


# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_anon_token(session: requests.Session, device_id: str) -> str:
    verifier  = uuid.uuid4().hex
    challenge = (base64.b64encode(hashlib.sha256(verifier.encode()).digest())
                 .decode().replace("+", "-").replace("/", "_"))

    sk = session.post(
        f"{ACCOUNT}/device/anonymous/signing_key",
        headers=HEADERS,
        json={"challenge": challenge, "version": "1.0.0",
              "platform": "web", "device_id": device_id},
        timeout=15,
    ).json()

    key_bytes = base64.b64decode(sk["key"])
    body_dict = {"verifier": verifier, "id": sk["id"],
                 "platform": "web", "device_id": device_id}
    body_json = json.dumps(body_dict, separators=(",", ":"))
    now       = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    bh    = hashlib.sha256(body_json.encode()).hexdigest()
    canon = (f"POST\n/device/anonymous/token\n\n"
             f"content-type:application/json\n\ncontent-type\n{bh}")
    ch    = hashlib.sha256(canon.encode()).hexdigest()
    sts   = f"TUBI-HMAC-SHA256\n{now}\n{ch}"

    k1  = hmac.new(b"TUBI" + key_bytes, now[:8].encode(), hashlib.sha256).digest()
    k2  = hmac.new(k1, b"tubi_request", hashlib.sha256).digest()
    sig = hmac.new(k2, sts.encode(), hashlib.sha256).hexdigest()

    params = {
        "X-Tubi-Algorithm": "TUBI-HMAC-SHA256", "X-Tubi-Date": now,
        "X-Tubi-Expires": 30, "X-Tubi-SignedHeaders": "content-type",
        "X-Tubi-Signature": sig,
    }
    resp = session.post(
        f"{ACCOUNT}/device/anonymous/token",
        headers=HEADERS, params=params, data=body_json.encode(), timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("token") or data.get("access_token")


# ── Content extraction ────────────────────────────────────────────────────────

def _extract_content(vid: dict) -> dict:
    ct = vid.get("content_tags") or {}
    return {
        "id":                        vid.get("id"),
        "title":                     vid.get("title"),
        "detailed_type":             vid.get("detailed_type"),
        "year":                      vid.get("year"),
        "duration_sec":              vid.get("duration"),
        "num_seasons":               vid.get("num_seasons"),
        "lang":                      vid.get("lang"),
        "tags":                      vid.get("tags") or [],
        "rating":                    (vid.get("ratings") or [{}])[0].get("code"),
        "actors":                    vid.get("actors") or [],
        "directors":                 vid.get("directors") or [],
        "import_id":                 vid.get("import_id"),
        "publisher_id":              vid.get("publisher_id"),
        "gracenote_id":              vid.get("gracenote_id"),
        "description":               vid.get("description"),
        "availability_starts":       vid.get("availability_starts"),
        "availability_ends":         vid.get("availability_ends"),
        "availability_duration_sec": vid.get("availability_duration"),
        "days_remaining":            ct.get("days_remaining"),
        "imdb_rating":               ct.get("imdb_rating"),
        "rotten_tomatoes_score":     ct.get("rotten_tomatoes_score"),
        "is_elite":                  ct.get("is_elite"),
        "is_shiny":                  ct.get("is_shiny"),
        "imdb_highly_rated":         ct.get("imdb_highly_rated"),
        "rotten_tomatoes_fresh":     ct.get("rotten_tomatoes_certified_fresh"),
        "tubi_most_liked":           ct.get("tubi_most_liked"),
        "poster_labels":             ct.get("poster_labels") or [],
        "vibes":                     vid.get("vibes") or [],
        "has_trailer":               vid.get("has_trailer"),
        "has_subtitle":              vid.get("has_subtitle"),
        "needs_login":               vid.get("needs_login"),
    }


# ── SSR parser (home page) ────────────────────────────────────────────────────

def _parse_ssr(html: str) -> list[dict]:
    m = re.search(r"window\.__data\s*=\s*(\{.*?\});?\s*</script>", html, re.DOTALL)
    if not m:
        m = re.search(r"window\.__data\s*=\s*(\{.*\})\s*;", html, re.DOTALL)
    if not m:
        raise ValueError("window.__data not found")
    blob = re.sub(r":\s*undefined", ": null", m.group(1))
    data = json.loads(blob)

    container = data.get("container", {})
    id_map    = container.get("containerIdMap", {}) or {}
    child_map = container.get("containerChildrenIdMap", {}) or {}
    order     = container.get("containersList") or list(id_map.keys())
    by_id     = (data.get("video", {}) or {}).get("byId", {}) or {}

    title_carousels: dict[str, list[str]] = {}
    for cid in order:
        for iid in (child_map.get(cid) or []):
            title_carousels.setdefault(iid, []).append(cid)

    carousels = []
    for rank, cid in enumerate(order, 1):
        meta     = id_map.get(cid) or {}
        item_ids = child_map.get(cid) or []
        items    = []
        for item_rank, iid in enumerate(item_ids, 1):
            vid   = by_id.get(iid)
            entry: dict = {"item_rank": item_rank,
                           "carousel_ids": title_carousels.get(iid, [cid])}
            if vid:
                entry.update(_extract_content(vid))
            else:
                entry["id"] = iid
            items.append(entry)

        carousels.append({
            "carousel_rank":        rank,
            "carousel_id":          cid,
            "carousel_title":       meta.get("title") or cid,
            "carousel_type":        meta.get("type"),
            "carousel_tags":        meta.get("tags") or [],
            "carousel_description": meta.get("description"),
            "num_items":            len(item_ids),
            "items":                items,
        })
    return carousels


# ── Pagination (v7/containers) ────────────────────────────────────────────────

def _fetch_container_page(
    session: requests.Session,
    auth_headers: dict,
    mode: str,
    carousel_id: str,
    cursor: int,
) -> tuple[list[str], dict[str, dict]]:
    """Fetch one page of a carousel; return (child_ids, by_id mapping)."""
    resp = session.get(
        f"{TENSOR}/api/v7/containers/{carousel_id}",
        headers=auth_headers,
        params={"mode": mode, "limit": PAGE_SIZE, "cursor": cursor},
        timeout=20,
    )
    if resp.status_code != 200:
        return [], {}
    data     = resp.json()
    children = (data.get("container") or {}).get("children") or []
    contents = data.get("contents") or []
    if isinstance(contents, dict):
        contents = list(contents.values())
    by_id = {str(v.get("id") or v.get("content_id", "")): v for v in contents}
    return [str(c) for c in children], by_id


def _paginate_carousel(
    session: requests.Session,
    auth_headers: dict,
    mode: str,
    carousel_id: str,
    already_have: int,
) -> tuple[list[str], dict[str, dict]]:
    """
    Fetch pages beyond the initial homescreen load until we reach
    ITEMS_PER_CAROUSEL total, firing concurrent requests.
    """
    need       = ITEMS_PER_CAROUSEL - already_have
    cursors    = list(range(already_have, already_have + need, PAGE_SIZE))
    all_ids: list[str]        = []
    all_by_id: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_container_page, session, auth_headers,
                        mode, carousel_id, cur): cur
            for cur in cursors
        }
        # collect in cursor order
        ordered: dict[int, tuple] = {}
        for fut in as_completed(futures):
            cur = futures[fut]
            try:
                ordered[cur] = fut.result()
            except Exception:
                ordered[cur] = ([], {})

    for cur in sorted(ordered):
        ids, by_id = ordered[cur]
        all_ids.extend(ids)
        all_by_id.update(by_id)

    return all_ids, all_by_id


# ── API scraper (movies / tv) ─────────────────────────────────────────────────

def _scrape_api_page(
    session: requests.Session,
    auth_headers: dict,
    mode: str,
) -> list[dict]:
    resp = session.get(
        f"{TENSOR}/api/v8/homescreen",
        headers=auth_headers,
        params={"mode": mode, "is_kids_mode": "false"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    api_containers = data.get("containers", [])
    contents_raw   = data.get("contents", [])
    if isinstance(contents_raw, dict):
        contents_raw = list(contents_raw.values())
    global_by_id: dict[str, dict] = {
        str(v.get("id") or v.get("content_id", "")): v for v in contents_raw
    }

    # Build cross-carousel membership from initial load
    title_carousels: dict[str, list[str]] = {}
    for c in api_containers:
        for iid in (c.get("children") or []):
            title_carousels.setdefault(str(iid), []).append(c["id"])

    carousels = []
    for rank, c in enumerate(api_containers, 1):
        initial_ids = [str(i) for i in (c.get("children") or [])]

        # Paginate if we need more items
        extra_ids: list[str] = []
        extra_by_id: dict[str, dict] = {}
        if len(initial_ids) < ITEMS_PER_CAROUSEL:
            extra_ids, extra_by_id = _paginate_carousel(
                session, auth_headers, mode, c["id"], len(initial_ids)
            )

        all_ids = (initial_ids + extra_ids)[:ITEMS_PER_CAROUSEL]
        by_id   = {**global_by_id, **extra_by_id}

        items = []
        for item_rank, sid in enumerate(all_ids, 1):
            vid   = by_id.get(sid)
            entry: dict = {"item_rank": item_rank,
                           "carousel_ids": title_carousels.get(sid, [c["id"]])}
            if vid:
                entry.update(_extract_content(vid))
            else:
                entry["id"] = sid
            items.append(entry)

        carousels.append({
            "carousel_rank":        rank,
            "carousel_id":          c.get("id"),
            "carousel_title":       c.get("title") or c.get("name") or c.get("id"),
            "carousel_type":        c.get("type"),
            "carousel_tags":        c.get("tags") or [],
            "carousel_description": c.get("description"),
            "num_items":            len(all_ids),
            "items":                items,
        })

    return carousels


# ── CSV writer ────────────────────────────────────────────────────────────────

CSV_COLS = [
    "scraped_at", "page",
    "carousel_rank", "carousel_id", "carousel_title", "carousel_type",
    "carousel_tags", "carousel_description",
    "item_rank", "carousel_ids",
    "id", "title", "detailed_type", "year", "duration_sec", "num_seasons",
    "lang", "tags", "rating", "actors", "directors",
    "import_id", "publisher_id", "gracenote_id",
    "availability_starts", "availability_ends", "availability_duration_sec",
    "days_remaining", "imdb_rating", "rotten_tomatoes_score",
    "is_elite", "is_shiny", "imdb_highly_rated", "rotten_tomatoes_fresh",
    "tubi_most_liked", "poster_labels", "vibes",
    "has_trailer", "has_subtitle", "needs_login",
    "description",
]


def _csv_val(v) -> str:
    if isinstance(v, list):
        return "|".join(str(x) for x in v)
    return "" if v is None else str(v)


def _write_csv(path: Path, results: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        for page in results:
            base = {"scraped_at": page["scraped_at"], "page": page["page"]}
            for car in page["carousels"]:
                car_base = {
                    **base,
                    "carousel_rank":        car["carousel_rank"],
                    "carousel_id":          car["carousel_id"],
                    "carousel_title":       car["carousel_title"],
                    "carousel_type":        car["carousel_type"] or "",
                    "carousel_tags":        _csv_val(car["carousel_tags"]),
                    "carousel_description": car["carousel_description"] or "",
                }
                if not car["items"]:
                    w.writerow(car_base)
                    continue
                for item in car["items"]:
                    row = {**car_base}
                    for col in CSV_COLS:
                        if col not in row:
                            row[col] = _csv_val(item.get(col))
                    w.writerow(row)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    stamp   = datetime.now().strftime("%Y%m%d")
    session = requests.Session()

    session.get(HOME_URL, headers={k: v for k, v in HEADERS.items()
                                   if k not in ("Origin", "Referer", "Content-Type")},
                timeout=30)
    device_id = dict(session.cookies).get("deviceId", str(uuid.uuid4()))
    token     = None

    results = []
    for name, (mode, target) in PAGES.items():
        try:
            scraped_at = datetime.now(timezone.utc).isoformat()
            if mode == "ssr":
                resp = session.get(target, headers={k: v for k, v in HEADERS.items()
                    if k not in ("Origin", "Referer", "Content-Type")}, timeout=30)
                resp.raise_for_status()
                carousels = _parse_ssr(resp.text)
            else:
                if token is None:
                    token = _get_anon_token(session, device_id)
                auth_headers = {**HEADERS, "Authorization": f"Bearer {token}"}
                carousels = _scrape_api_page(session, auth_headers, target)

            n_items = sum(c["num_items"] for c in carousels)
            print(f"[{name}] {len(carousels)} carousels, {n_items} items")
            results.append({"page": name, "scraped_at": scraped_at, "carousels": carousels})

        except Exception as e:
            print(f"[{name}] ERROR: {e}", file=sys.stderr)

    json_path = OUT_DIR / f"tubi_{stamp}.json"
    json_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))

    csv_path = OUT_DIR / f"tubi_{stamp}.csv"
    _write_csv(csv_path, results)

    print(f"\nSaved to {OUT_DIR}/:")
    print(f"  {json_path.name}  ({json_path.stat().st_size // 1024} KB)")
    print(f"  {csv_path.name}   ({csv_path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
