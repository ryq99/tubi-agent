#!/usr/bin/env python3
"""
Scrape every Sony Pictures title on Tubi with its full metadata.

Walks Tubi's SEO sitemaps for every movie + series ID (~78k), fetches each
title's HTML page, and parses the `window.__data` hydration blob — that's the
only public path to per-title metadata; Tubi has no JSON content endpoint.

Filters by Sony Pictures' publisher_id (one hash covers Columbia, TriStar,
Screen Gems, Sony Pictures Animation, and Sony Pictures Classics) and writes
data/sony_catalog.csv with every metadata field.

Resumable via data/catalog.jsonl — on re-run, IDs already present are skipped.
Series-level objects have no publisher_id (only their episodes do), so we
stamp `_publisher_id_inferred` from the dominant episode publisher_id.

Usage:
    python src/scrape_sony_catalog.py                # full crawl + export
    python src/scrape_sony_catalog.py --export-only  # re-export CSV only
    python src/scrape_sony_catalog.py --workers 12   # crank concurrency
"""
import argparse, csv, json, random, re, sys, threading, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests

SONY_PUBLISHER_ID = "05eca4c3864a1eef48a92914dcbbd428"

DATA_DIR     = Path(__file__).parent.parent / "data"
CATALOG_PATH = DATA_DIR / "catalog.jsonl"
ERRORS_PATH  = DATA_DIR / "catalog_errors.jsonl"


def _sony_csv_path() -> Path:
    """One CSV per month so we can diff slate changes over time."""
    return DATA_DIR / f"sony_catalog_{datetime.now().strftime('%Y%m')}.csv"

# (sitemap_filename, url_path_kind). Tubi names TV sitemaps "tv-shows-N.xml"
# but the actual title URL path is /series/{id}/{slug}.
SITEMAPS = (
    [(f"movies-{n}",   "movies")  for n in range(1, 8)] +
    [(f"tv-shows-{n}", "series")  for n in (1, 2)]
)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Hydration parser ──────────────────────────────────────────────────────────

def _extract_window_data(html: str) -> dict:
    """Parse the `window.__data=…;` Redux hydration blob from a Tubi page.

    Brace-counts forward from the marker; regex can't slice the blob safely
    because string values embed `;` and `}`. JS `undefined` literals are
    replaced with `null` so json.loads can read it.
    """
    marker = "window.__data="
    i = html.find(marker)
    if i < 0:
        return {}
    start = i + len(marker)
    depth, in_str, esc = 0, False, False
    for j in range(start, len(html)):
        ch = html[j]
        if in_str:
            if esc:           esc = False
            elif ch == "\\":  esc = True
            elif ch == '"':   in_str = False
        else:
            if   ch == '"':   in_str = True
            elif ch == "{":   depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    blob = html[start:j + 1]
                    blob = re.sub(r"(?<![A-Za-z0-9_$])undefined(?![A-Za-z0-9_$])",
                                  "null", blob)
                    return json.loads(blob)
    return {}


# ── Sitemap walk ──────────────────────────────────────────────────────────────

def gather_ids() -> list[tuple[str, str]]:
    """Return [(id, kind), …] for every title published in Tubi's sitemaps."""
    seen: set[str] = set()
    out:  list[tuple[str, str]] = []
    for name, kind in SITEMAPS:
        xml = requests.get(f"https://tubitv.com/sitemaps/{name}.xml",
                           headers=HEADERS, timeout=60).text
        before = len(out)
        for tid in re.findall(rf"tubitv\.com/{kind}/(\d+)/", xml):
            if tid not in seen:
                seen.add(tid)
                out.append((tid, kind))
        print(f"  {name}: +{len(out) - before}  (total {len(out)})", file=sys.stderr)
    return out


# ── Per-title fetch ───────────────────────────────────────────────────────────

def fetch_title(session: requests.Session, tid: str, kind: str) -> dict | None:
    """Fetch one title page and return its hydration object, or None on failure."""
    for attempt in range(3):
        try:
            r = session.get(f"https://tubitv.com/{kind}/{tid}",
                            headers=HEADERS, timeout=25, allow_redirects=True)
        except requests.RequestException:
            time.sleep(1 + random.random())
            continue
        if r.status_code in (429, 502, 503, 504):
            time.sleep(2 ** attempt + random.random())
            continue
        if r.status_code != 200:
            return None

        by_id = (_extract_window_data(r.text).get("video") or {}).get("byId") or {}
        obj   = by_id.get(tid)
        if not obj:
            return None
        # Series have no publisher_id; infer from the dominant episode publisher.
        if kind == "series" and not obj.get("publisher_id"):
            ep_pubs = [v.get("publisher_id") for k, v in by_id.items()
                       if k != tid and v.get("publisher_id")]
            if ep_pubs:
                obj["_publisher_id_inferred"] = Counter(ep_pubs).most_common(1)[0][0]
        return obj
    return None


# ── Crawl ─────────────────────────────────────────────────────────────────────

def _load_done_ids() -> set[str]:
    if not CATALOG_PATH.exists():
        return set()
    with CATALOG_PATH.open() as f:
        return {json.loads(line)["id"] for line in f if line.strip()}


def crawl(workers: int, limit: int = 0) -> None:
    """Walk sitemaps, fetch each new title, append to catalog.jsonl."""
    print("Walking sitemaps...", file=sys.stderr)
    ids  = gather_ids()
    done = _load_done_ids()
    todo = [(tid, kind) for tid, kind in ids if tid not in done]
    if limit:
        todo = todo[:limit]
    print(f"Sitemap total: {len(ids)}; in catalog: {len(done)}; todo: {len(todo)}",
          file=sys.stderr)
    if not todo:
        return

    DATA_DIR.mkdir(exist_ok=True)
    session    = requests.Session()
    write_lock = threading.Lock()
    state      = {"ok": 0, "fail": 0}
    catalog_fh = CATALOG_PATH.open("a", encoding="utf-8")
    err_fh     = ERRORS_PATH.open("a", encoding="utf-8")

    def worker(tid: str, kind: str) -> None:
        time.sleep(random.uniform(0.05, 0.20))   # jitter — be polite
        obj = fetch_title(session, tid, kind)
        with write_lock:
            if obj is None:
                err_fh.write(json.dumps({"id": tid, "kind": kind}) + "\n")
                err_fh.flush()
                state["fail"] += 1
            else:
                catalog_fh.write(json.dumps(obj, ensure_ascii=False,
                                            separators=(",", ":")) + "\n")
                catalog_fh.flush()
                state["ok"] += 1

    start = time.time()
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(worker, tid, kind) for tid, kind in todo]
            for i, _ in enumerate(as_completed(futs), 1):
                if i % 100 == 0:
                    rate    = i / (time.time() - start)
                    eta_min = (len(todo) - i) / rate / 60
                    print(f"  {i}/{len(todo)}  ok={state['ok']}  fail={state['fail']}  "
                          f"rate={rate:.1f}/s  ETA={eta_min:.1f}min", file=sys.stderr)
    finally:
        catalog_fh.close()
        err_fh.close()
    print(f"Done: ok={state['ok']}, fail={state['fail']}, "
          f"elapsed={(time.time() - start) / 60:.1f} min")


# ── Sony CSV export ───────────────────────────────────────────────────────────

def _cell(v) -> str:
    if v is None:                       return ""
    if isinstance(v, (dict, list)):     return json.dumps(v, ensure_ascii=False)
    return str(v)


def export_sony() -> None:
    """Filter catalog.jsonl by Sony's publisher_id; write sony_catalog.csv."""
    if not CATALOG_PATH.exists():
        print(f"ERROR: {CATALOG_PATH} not found — run crawl first.", file=sys.stderr)
        sys.exit(1)

    sony:      list[dict] = []
    columns:   list[str]  = []
    seen_cols: set[str]   = set()
    with CATALOG_PATH.open() as f:
        for line in f:
            t = json.loads(line)
            pid = t.get("publisher_id") or t.get("_publisher_id_inferred")
            if pid != SONY_PUBLISHER_ID:
                continue
            sony.append(t)
            for k in t.keys():
                if k not in seen_cols:
                    seen_cols.add(k)
                    columns.append(k)

    # Surface useful fields first
    for lead in ("year", "title", "id"):
        if lead in columns:
            columns.remove(lead)
            columns.insert(0, lead)

    out = _sony_csv_path()
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for t in sorted(sony, key=lambda x: -(x.get("year") or 0)):
            w.writerow({k: _cell(t.get(k)) for k in columns})

    n_movies = sum(1 for t in sony if t.get("detailed_type") == "movie")
    n_series = len(sony) - n_movies
    print(f"Sony: {len(sony)} titles ({n_movies} movies, {n_series} series) "
          f"→ {out.name} ({out.stat().st_size // 1024} KB)")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers",     type=int, default=8)
    ap.add_argument("--limit",       type=int, default=0,
                    help="cap titles for smoke testing")
    ap.add_argument("--export-only", action="store_true",
                    help="skip crawl; just re-export from catalog.jsonl")
    args = ap.parse_args()

    if not args.export_only:
        crawl(args.workers, args.limit)
    export_sony()


if __name__ == "__main__":
    main()
