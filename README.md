# tubi-agent

Daily scraper for Tubi's carousel and title metadata, designed for ML research into recommender systems and content seasonality.

## How it works

1. Seeds a session cookie on `tubitv.com` to obtain a `deviceId`
2. Authenticates via Tubi's anonymous device token flow (PKCE + TUBI-HMAC-SHA256 signing against `account.production-public.tubi.io`)
3. Calls `tensor-cdn.production-public.tubi.io/api/v1/browse_list` to retrieve all carousel metadata (no personalization `mode` param — most generic request)
4. Paginates each carousel to 50 items via `/api/v7/containers/{id}` using a shared thread pool (20 workers)
5. Writes one JSON and one CSV per day to `data/`, overwriting on same-day reruns

## Running

```bash
# one-off
bash cron_daily.sh

# schedule daily at 7am PT — copy plist to LaunchAgents and load
cp com.tubiagent.daily.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.tubiagent.daily.plist

# test fire immediately
launchctl start com.tubiagent.daily
```

## Output files

```
data/tubi_YYYYMMDD.json
data/tubi_YYYYMMDD.csv
logs/scrape_YYYYMMDD.log
```

---

## Data dictionary

### Top-level (JSON only)

| Field | Type | Description |
|---|---|---|
| `scraped_at` | ISO 8601 string | UTC timestamp of when the scrape ran |
| `carousels` | array | Ordered list of all carousel objects |

---

### Carousel fields

| Field | Type | Description |
|---|---|---|
| `carousel_rank` | int | Position of this carousel on the page (1 = top) |
| `carousel_id` | string | Tubi's internal slug, e.g. `most_popular`, `drama`, `leaving_soon` |
| `carousel_title` | string | Display name shown to users, e.g. `"Most Popular"` |
| `carousel_type` | string | Rendering type — see carousel types below |
| `carousel_tags` | string[] | Tubi's editorial classification — see carousel tags below |
| `carousel_description` | string | Short editorial blurb shown under the carousel title |
| `is_personalized` | bool | `true` if the carousel is user-specific (`recommended_*`, `watch_it_again`, `recommended_linear_channels`). `false` for all editorial carousels. Filter to `false` for non-personalized ML signal. |
| `num_items` | int | Number of items fetched (capped at 50; may be lower if carousel has fewer) |
| `items` | array | Ordered list of title objects within this carousel |

#### Carousel types

| Value | Meaning |
|---|---|
| `regular` | Standard title carousel (68 carousels) |
| `channel` | FAST / linear channel carousel (22 carousels) |
| `genesis` | Editorial collection or curated row (19 carousels) |
| `linear` | Live TV / EPG row (3 carousels) |

#### Carousel tags

| Value | Meaning |
|---|---|
| `Genres` | Genre-based editorial row (Action, Drama, Horror, etc.) — 26 carousels |
| `Channels` | Partner / FAST channel rows (FOX, A&E, FilmRise, etc.) — 22 carousels |
| `Popular` | Popularity or trending signal (Most Popular, Recently Added, etc.) — 9 carousels |
| `Collections` | Seasonal or thematic curations (Wedding Season, Pride, etc.) — 10 carousels |
| `For You` | Tubi's UX label — appears on both personalized rows **and** algorithmic ones (Most Popular, Leaving Soon). Do not use this tag alone to identify personalization; use `is_personalized` instead. |

---

### Item (title) fields

#### Identity

| Field | Type | Population | Description |
|---|---|---|---|
| `id` | string | ~100% | Tubi's internal content ID |
| `title` | string | ~100% | Display title |
| `detailed_type` | string | ~77% | `movie`, `series`, or `linear` (live channel). `unknown` when API returns no metadata for the ID. |
| `gracenote_id` | string | ~80% | Gracenote cross-reference ID for external catalog enrichment |

#### Carousel membership

| Field | Type | Description |
|---|---|---|
| `item_rank` | int | 1-based position of this title within its carousel |
| `carousel_ids` | string[] | All carousel IDs this title appears in across the full scrape (cross-carousel membership). 56% of titles appear in 2+ carousels; max observed is 14. Filter out `is_personalized` carousels before using for clustering. |

#### Content metadata

| Field | Type | Population | Description |
|---|---|---|---|
| `year` | int | ~77% | Production / release year. Range: 1929–2026 |
| `duration_sec` | int | ~77% | Runtime in seconds (movies only; null for series) |
| `num_seasons` | int | ~5% | Number of seasons (series only) |
| `lang` | string | ~77% | Primary audio language, e.g. `"English"` |
| `tags` | string[] | ~77% | Tubi genre tags, e.g. `["Action", "Comedy"]`. Multi-valued, pipe-separated in CSV. |
| `rating` | string | ~77% | MPAA or TV Parental Guidelines code — see rating values below |
| `actors` | string[] | ~77% | Cast members. Pipe-separated in CSV. |
| `directors` | string[] | ~77% | Director(s). Pipe-separated in CSV. |
| `description` | string | ~77% | Short synopsis |

#### Availability

| Field | Type | Population | Description |
|---|---|---|---|
| `availability_starts` | ISO 8601 string | ~77% | When this title became available on Tubi |
| `availability_ends` | ISO 8601 string | ~60% | License expiry date. Key seasonality signal — titles near expiry surface in `leaving_soon` carousels. |
| `availability_duration_sec` | int | ~60% | Seconds since the title was added (`now - availability_starts`) |
| `days_remaining` | int | 0% | Days until license expiry (populated in SSR only; not returned by the tensor API) |

#### Distributor

| Field | Type | Population | Description |
|---|---|---|---|
| `import_id` | string | ~77% | Studio / distributor slug, e.g. `sony-pictures`, `disney`, `a24`, `lionsgate`. Top distributors: Sony (456), Disney (273), Lionsgate (252), MGM (244). |
| `publisher_id` | string | ~50% | Internal publisher UUID |

#### Quality & editorial signals

| Field | Type | Population | Description |
|---|---|---|---|
| `imdb_highly_rated` | bool | 2% | Tubi badge: IMDB highly rated |
| `rotten_tomatoes_fresh` | bool | 1% | Tubi badge: Rotten Tomatoes Certified Fresh |
| `tubi_most_liked` | bool | 2% | Tubi badge: community most-liked |
| `is_elite` | bool | 0% | Legacy Tubi quality flag (not populated by tensor API) |
| `is_shiny` | bool | 0% | Legacy Tubi quality flag (not populated by tensor API) |
| `imdb_rating` | float | 0% | Raw IMDB score (not populated by tensor API; use `imdb_highly_rated` instead) |
| `rotten_tomatoes_score` | int | 0% | Raw RT score (not populated by tensor API) |
| `poster_labels` | string[] | 24% | Labels overlaid on the poster tile: `Leaving Soon`, `Just Added`, `Top 10 Horror` |
| `vibes` | string[] | 24% | Same values as `poster_labels` — Tubi's internal name for the same signal |
| `has_trailer` | bool | ~77% | Whether a trailer is available |
| `has_subtitle` | bool | ~77% | Whether subtitles are available |
| `needs_login` | bool | ~77% | Whether the title requires a Tubi account to watch |

#### Rating values

| Code | System | Description |
|---|---|---|
| `R` | MPAA | Restricted (1,175 titles) |
| `TV-MA` | TV-PG | Mature audiences (679) |
| `PG-13` | MPAA | Parents strongly cautioned (645) |
| `TV-14` | TV-PG | Parents strongly cautioned (565) |
| `PG` | MPAA | Parental guidance (360) |
| `TV-PG` | TV-PG | Parental guidance (310) |
| `G` / `TV-G` | Both | General audiences (116) |
| `NC-17` | MPAA | Adults only (5) |

---

## Coverage (2026-06-21)

| Metric | Value |
|---|---|
| Total carousels | 112 |
| Editorial carousels (`is_personalized=False`) | 108 |
| Personalized carousels (`is_personalized=True`) | 4 |
| Items per carousel (cap) | 50 |
| Total item rows | 5,020 |
| Unique title IDs | 2,993 |
| Titles in 2+ carousels | 2,790 (56%) |
| Max carousels for one title | 14 (_Percy Jackson & the Olympians: The Lightning Thief_) |
| `detailed_type=movie` | 3,816 (76%) |
| `detailed_type=linear` | 58 (1%) |
| `detailed_type=unknown` | 1,146 (23%) — items where tensor API returned no metadata |
| Year range | 1929 – 2026 |

---

## Notes for ML use

- **Carousel rank × item rank** is the primary recommender signal — a title at rank 1 in `most_popular` is a strong popularity proxy.
- **`carousel_ids`** (cross-carousel membership) is a multi-label feature encoding how Tubi clusters titles editorially. Filter to `is_personalized=False` carousels before using.
- **`availability_ends`** combined with `poster_labels=Leaving Soon` tracks catalog churn — useful for modeling seasonality and licensing cycles.
- **`import_id`** (studio) correlates with content deals and catalog windows. Titles from the same distributor tend to arrive and leave together.
- **`tags`** (genre) + **`carousel_id`** together give two levels of genre signal: Tubi's own genre taxonomy and their editorial clustering.
- **`unknown` detailed_type** rows are live channel IDs or items where the API returned no metadata for that session. Safe to drop for title-level analysis.
