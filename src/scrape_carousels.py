#!/usr/bin/env python3
"""
Scrape the recommendations Tubi shows on its homepage today.

Fetches every carousel row from the anonymous /api/v1/browse_list and paginates
each row's items to ITEMS_PER_CAROUSEL via /api/v7/containers. Output: one JSON
+ one CSV per day in data/, plus a `carousel_ids` membership list per title so
we can see which rows each title appears in. Carousels whose IDs match known
user-specific patterns (recommended_for_you, watch_it_again, …) are flagged as
`is_personalized` for downstream filtering. Runs daily via cron_daily.sh.
"""
import csv, hashlib, hmac, json, sys, uuid, base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import requests

OUT_DIR            = Path(__file__).parent.parent / "data"
ITEMS_PER_CAROUSEL = 50
PAGE_SIZE          = 10
MAX_WORKERS        = 20

ACCOUNT  = "https://account.production-public.tubi.io"
TENSOR   = "https://tensor-cdn.production-public.tubi.io"
HOME_URL = "https://tubitv.com/"

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


# ── Pagination ────────────────────────────────────────────────────────────────

def _fetch_page(
    session: requests.Session,
    auth_headers: dict,
    carousel_id: str,
    cursor: int,
) -> tuple[str, int, list[str], dict[str, dict]]:
    try:
        resp = session.get(
            f"{TENSOR}/api/v7/containers/{carousel_id}",
            headers=auth_headers,
            params={"limit": PAGE_SIZE, "cursor": cursor},
            timeout=20,
        )
        if resp.status_code != 200:
            return carousel_id, cursor, [], {}
        data     = resp.json()
        children = (data.get("container") or {}).get("children") or []
        contents = data.get("contents") or []
        if isinstance(contents, dict):
            contents = list(contents.values())
        by_id = {str(v.get("id") or v.get("content_id", "")): v for v in contents}
        return carousel_id, cursor, [str(c) for c in children], by_id
    except Exception:
        return carousel_id, cursor, [], {}


# ── Main scrape ───────────────────────────────────────────────────────────────

def scrape(session: requests.Session, auth_headers: dict) -> list[dict]:
    # Step 1: all carousel metadata — no mode param = most generic, least personalized
    bl = session.get(
        f"{TENSOR}/api/v1/browse_list",
        headers=auth_headers,
        params={"is_kids_mode": "false"},
        timeout=30,
    )
    bl.raise_for_status()
    carousel_metas = bl.json().get("containers", [])

    # Step 2: fire all page-fetch jobs in one flat pool
    cursors = list(range(0, ITEMS_PER_CAROUSEL, PAGE_SIZE))
    jobs    = [(c["id"], cur) for c in carousel_metas for cur in cursors]

    raw: dict[str, dict[int, tuple[list[str], dict]]] = {c["id"]: {} for c in carousel_metas}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_page, session, auth_headers, cid, cur): (cid, cur)
            for cid, cur in jobs
        }
        for fut in as_completed(futures):
            cid, cursor, ids, by_id = fut.result()
            raw[cid][cursor] = (ids, by_id)

    # Step 3: build cross-carousel membership
    title_carousels: dict[str, list[str]] = {}
    for c in carousel_metas:
        for cursor in sorted(raw[c["id"]]):
            for sid in raw[c["id"]][cursor][0]:
                title_carousels.setdefault(sid, [])
                if c["id"] not in title_carousels[sid]:
                    title_carousels[sid].append(c["id"])

    # Step 4: assemble carousels
    carousels = []
    for rank, c in enumerate(carousel_metas, 1):
        all_ids:   list[str]        = []
        all_by_id: dict[str, dict]  = {}
        for cursor in sorted(raw[c["id"]]):
            ids, by_id = raw[c["id"]][cursor]
            all_ids.extend(ids)
            all_by_id.update(by_id)
        all_ids = all_ids[:ITEMS_PER_CAROUSEL]

        items = []
        for item_rank, sid in enumerate(all_ids, 1):
            vid   = all_by_id.get(sid)
            entry: dict = {"item_rank": item_rank,
                           "carousel_ids": title_carousels.get(sid, [c["id"]])}
            if vid:
                entry.update(_extract_content(vid))
            else:
                entry["id"] = sid
            items.append(entry)

        # "For You" tag also appears on editorial carousels (most_popular,
        # recently_added, leaving_soon) — use ID to identify truly user-specific rows.
        cid = c.get("id") or ""
        is_personalized = any(kw in cid for kw in ("recommended", "watch_it_again", "continue_watching"))
        carousels.append({
            "carousel_rank":        rank,
            "carousel_id":          c.get("id"),
            "carousel_title":       c.get("title") or c.get("name") or c.get("id"),
            "carousel_type":        c.get("type"),
            "carousel_tags":        c.get("tags") or [],
            "carousel_description": c.get("description"),
            "is_personalized":      is_personalized,
            "num_items":            len(all_ids),
            "items":                items,
        })

    return carousels


# ── CSV writer ────────────────────────────────────────────────────────────────

CSV_COLS = [
    "scraped_at",
    "carousel_rank", "carousel_id", "carousel_title", "carousel_type",
    "carousel_tags", "carousel_description", "is_personalized",
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


def _write_csv(path: Path, scraped_at: str, carousels: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        for car in carousels:
            car_base = {
                "scraped_at":           scraped_at,
                "carousel_rank":        car["carousel_rank"],
                "carousel_id":          car["carousel_id"],
                "carousel_title":       car["carousel_title"],
                "carousel_type":        car["carousel_type"] or "",
                "carousel_tags":        _csv_val(car["carousel_tags"]),
                "carousel_description": car["carousel_description"] or "",
                "is_personalized":      car["is_personalized"],
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


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    stamp   = datetime.now().strftime("%Y%m%d")
    session = requests.Session()

    # Seed cookies
    session.get(HOME_URL, headers={k: v for k, v in HEADERS.items()
                                   if k not in ("Origin", "Referer", "Content-Type")},
                timeout=30)
    device_id = dict(session.cookies).get("deviceId", str(uuid.uuid4()))

    try:
        token        = _get_anon_token(session, device_id)
        auth_headers = {**HEADERS, "Authorization": f"Bearer {token}"}
        scraped_at   = datetime.now(timezone.utc).isoformat()
        carousels    = scrape(session, auth_headers)

        n_personalized = sum(1 for c in carousels if c["is_personalized"])
        n_items        = sum(c["num_items"] for c in carousels)
        print(f"{len(carousels)} carousels ({n_personalized} personalized, "
              f"{len(carousels) - n_personalized} editorial), {n_items} items")

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    result = {"scraped_at": scraped_at, "carousels": carousels}

    json_path = OUT_DIR / f"tubi_{stamp}.json"
    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    csv_path = OUT_DIR / f"tubi_{stamp}.csv"
    _write_csv(csv_path, scraped_at, carousels)

    print(f"\nSaved to {OUT_DIR}/:")
    print(f"  {json_path.name}  ({json_path.stat().st_size // 1024} KB)")
    print(f"  {csv_path.name}   ({csv_path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
