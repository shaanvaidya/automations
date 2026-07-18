# automations

Personal automations, each running on its own GitHub Actions schedule and pushing results to my phone via [ntfy](https://ntfy.sh).

## Projects

### movies — weekly AMC digest
`movies/` · runs Monday 1pm PT · [`movie-digest.yml`](.github/workflows/movie-digest.yml)

Every movie currently showing at AMC Metreon 16 (SF), with director, cast, genre, a spoiler-free premise, IMDb/RT ratings, an RT critics-consensus line, and showtimes after 5pm. Fully deterministic — no LLM at runtime:
- Playwright renders AMC's showtimes page (JS-rendered, plain HTTP won't work) and the text is regex-parsed
- OMDb API for cast/director/genre/ratings (recency-aware search to avoid matching an old same-titled film)
- Rotten Tomatoes consensus scraped from static HTML by guessing the page slug

Secrets: `OMDB_API_KEY`, `NTFY_TOPIC`.

### concerts — tour date alerts
Status: **not started** — blocked on Bandsintown API partner access (no self-serve key available).

Plan: track a fixed artist list, notify immediately on newly announced shows, remind ~1-2 weeks before a show date. Needs state tracking (JSON committed to the repo) to distinguish new vs. already-seen shows, unlike the stateless movie digest.

## Conventions
- One subfolder per automation, one workflow file per automation.
- Deterministic code only — no LLM calls at runtime (an earlier cloud-agent approach for the movie digest proved unreliable).
- Secrets via `gh secret set`, never committed.
- Delivery via ntfy.
