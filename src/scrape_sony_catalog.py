#!/usr/bin/env python3
"""
Scrape every Sony Pictures title on Tubi with its full metadata.

Walks Tubi's SEO sitemaps for every movie + series ID (~78k), fetches each
title's HTML page, and parses the `window.__data` hydration blob — the only
public path to per-title metadata; Tubi has no JSON content endpoint.

Filters by Sony Pictures' publisher_id (one hash covers Columbia, TriStar,
Screen Gems, Sony Pictures Animation, and Sony Pictures Classics). Series-
level objects have no publisher_id (only their episodes do), so we stamp
`_publisher_id_inferred` from the dominant episode publisher_id.

Per-month outputs (so monthly snapshots are archived for diffing):
    data/catalog_YYYYMM.jsonl         every title fetched this month
    data/catalog_errors_YYYYMM.jsonl  IDs that failed after retries
    data/sony_catalog_YYYYMM.csv      filtered Sony slate

Default behavior = fresh full crawl for the current month + one retry pass
+ Sony CSV export. If catalog_YYYYMM.jsonl already has rows, the crawl
resumes (skipping IDs already in the file) — so a re-run mid-month picks
up where an interrupted run left off, but the next month starts empty.

Usage:
    python src/scrape_sony_catalog.py                  # crawl + retry + export
    python src/scrape_sony_catalog.py --no-retry       # skip auto-retry
    python src/scrape_sony_catalog.py --retry-only     # only retry + export
    python src/scrape_sony_catalog.py --export-only    # only re-export CSV
    python src/scrape_sony_catalog.py --month 202507   # target a specific month
    python src/scrape_sony_catalog.py --workers 12     # crank concurrency
"""
import argparse, csv, json, random, re, sys, threading, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests

SONY_PUBLISHER_ID = "05eca4c3864a1eef48a92914dcbbd428"
DATA_DIR = Path(__file__).parent.parent / "data"

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


def _current_month() -> str:
    return datetime.now().strftime("%Y%m")


def _catalog_path(month: str) -> Path:
    return DATA_DIR / f"catalog_{month}.jsonl"


def _errors_path(month: str) -> Path:
    return DATA_DIR / f"catalog_errors_{month}.jsonl"


def _sony_csv_path(month: str) -> Path:
    return DATA_DIR / f"sony_catalog_{month}.csv"


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


# ── Parallel runner ───────────────────────────────────────────────────────────

def _load_done_ids(catalog_path: Path) -> set[str]:
    if not catalog_path.exists():
        return set()
    with catalog_path.open() as f:
        return {json.loads(line)["id"] for line in f if line.strip()}


def _parallel_fetch(todo: list[tuple[str, str]], workers: int,
                    catalog_fh, on_fail) -> dict:
    """Fetch every (id, kind) in parallel; append successes to catalog_fh.

    on_fail(tid, kind) is called for each title that fails after retries —
    crawl uses it to write to the errors file; retry uses it to accumulate
    the still-failing list.
    """
    session    = requests.Session()
    write_lock = threading.Lock()
    state      = {"ok": 0, "fail": 0}

    def worker(tid: str, kind: str) -> None:
        time.sleep(random.uniform(0.05, 0.20))   # jitter — be polite
        obj = fetch_title(session, tid, kind)
        with write_lock:
            if obj is None:
                on_fail(tid, kind)
                state["fail"] += 1
            else:
                catalog_fh.write(json.dumps(obj, ensure_ascii=False,
                                            separators=(",", ":")) + "\n")
                catalog_fh.flush()
                state["ok"] += 1

    start = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(worker, tid, kind) for tid, kind in todo]
        for i, _ in enumerate(as_completed(futs), 1):
            if i % 100 == 0:
                rate    = i / (time.time() - start)
                eta_min = (len(todo) - i) / rate / 60
                print(f"  {i}/{len(todo)}  ok={state['ok']}  fail={state['fail']}  "
                      f"rate={rate:.1f}/s  ETA={eta_min:.1f}min", file=sys.stderr)
    state["elapsed_min"] = (time.time() - start) / 60
    return state


# ── Crawl + retry ─────────────────────────────────────────────────────────────

def crawl(workers: int, month: str, limit: int = 0) -> None:
    """Fresh full crawl for the given month; resume if catalog_<month>.jsonl
    is already partially populated."""
    catalog_path = _catalog_path(month)
    errors_path  = _errors_path(month)

    print(f"Walking sitemaps for month {month}...", file=sys.stderr)
    ids  = gather_ids()
    done = _load_done_ids(catalog_path)
    todo = [(tid, kind) for tid, kind in ids if tid not in done]
    if limit:
        todo = todo[:limit]
    print(f"Sitemap total: {len(ids)}; in catalog_{month}: {len(done)}; "
          f"todo: {len(todo)}", file=sys.stderr)
    if not todo:
        return

    DATA_DIR.mkdir(exist_ok=True)
    catalog_fh = catalog_path.open("a", encoding="utf-8")
    err_fh     = errors_path.open("a", encoding="utf-8")
    def on_fail(tid: str, kind: str) -> None:
        err_fh.write(json.dumps({"id": tid, "kind": kind}) + "\n")
        err_fh.flush()
    try:
        state = _parallel_fetch(todo, workers, catalog_fh, on_fail)
    finally:
        catalog_fh.close()
        err_fh.close()
    print(f"Crawl done: ok={state['ok']}, fail={state['fail']}, "
          f"elapsed={state['elapsed_min']:.1f} min")


def retry_errors(workers: int, month: str) -> None:
    """Re-attempt every ID in catalog_errors_<month>.jsonl at lower concurrency.

    Crawl-time failures are usually transient (connection-pool contention,
    brief 429s). One sequential retry recovered 50/50 in diagnosis, so a
    halved-concurrency pass recovers ~97%+ in practice. The remaining
    failures are stable 4xx (delisted titles).
    """
    catalog_path = _catalog_path(month)
    errors_path  = _errors_path(month)

    if not errors_path.exists():
        print(f"No errors file for {month} — nothing to retry.", file=sys.stderr)
        return
    with errors_path.open() as f:
        errs = [json.loads(l) for l in f if l.strip()]
    if not errs:
        print(f"Errors file for {month} is empty.", file=sys.stderr)
        return

    # Skip IDs already in catalog (e.g. recovered by a prior retry).
    done = _load_done_ids(catalog_path)
    todo = [(e["id"], e["kind"]) for e in errs if e["id"] not in done]
    print(f"Retrying {len(todo)} failed IDs from {month} with {workers} workers...",
          file=sys.stderr)

    still_failing: list[tuple[str, str]] = []
    catalog_fh = catalog_path.open("a", encoding="utf-8")
    try:
        state = _parallel_fetch(todo, workers, catalog_fh,
                                lambda tid, kind: still_failing.append((tid, kind)))
    finally:
        catalog_fh.close()

    with errors_path.open("w", encoding="utf-8") as f:
        for tid, kind in still_failing:
            f.write(json.dumps({"id": tid, "kind": kind}) + "\n")

    print(f"Retry done: recovered={state['ok']}, still failing={state['fail']}, "
          f"elapsed={state['elapsed_min']:.1f} min")


# ── Sony CSV export ───────────────────────────────────────────────────────────

def _cell(v) -> str:
    if v is None:                       return ""
    if isinstance(v, (dict, list)):     return json.dumps(v, ensure_ascii=False)
    return str(v)


def export_sony(month: str) -> None:
    """Filter catalog_<month>.jsonl by Sony's publisher_id; write sony_catalog_<month>.csv."""
    catalog_path = _catalog_path(month)
    if not catalog_path.exists():
        print(f"ERROR: {catalog_path} not found — run crawl first.", file=sys.stderr)
        sys.exit(1)

    sony:      list[dict] = []
    columns:   list[str]  = []
    seen_cols: set[str]   = set()
    with catalog_path.open() as f:
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

    out = _sony_csv_path(month)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for t in sorted(sony, key=lambda x: -(x.get("year") or 0)):
            w.writerow({k: _cell(t.get(k)) for k in columns})

    n_movies = sum(1 for t in sony if t.get("detailed_type") == "movie")
    n_series = len(sony) - n_movies
    print(f"Sony ({month}): {len(sony)} titles ({n_movies} movies, {n_series} series) "
          f"→ {out.name} ({out.stat().st_size // 1024} KB)")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers",     type=int, default=8)
    ap.add_argument("--month",       default=_current_month(),
                    help="YYYYMM to crawl into (default: current month)")
    ap.add_argument("--limit",       type=int, default=0,
                    help="cap titles for smoke testing")
    ap.add_argument("--export-only", action="store_true",
                    help="skip crawl + retry; just re-export the CSV")
    ap.add_argument("--retry-only",  action="store_true",
                    help="skip crawl; only retry errors + export")
    ap.add_argument("--no-retry",    action="store_true",
                    help="skip the auto-retry pass after crawl")
    args = ap.parse_args()

    if args.export_only:
        export_sony(args.month)
        return

    if args.retry_only:
        # Halve concurrency for retries — main-crawl failures were caused by
        # connection-pool contention at full concurrency.
        retry_errors(workers=max(1, args.workers // 2), month=args.month)
        export_sony(args.month)
        return

    crawl(args.workers, args.month, args.limit)
    if not args.no_retry:
        retry_errors(workers=max(1, args.workers // 2), month=args.month)
    export_sony(args.month)


if __name__ == "__main__":
    main()
