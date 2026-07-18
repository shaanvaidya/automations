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

### concerts — SF/Bay Area venue tracker
`concerts/` · runs weekly Monday 12pm PT · [`concerts.yml`](.github/workflows/concerts.yml)

Tracks 19 specific Bay Area venues (not a fixed artist list — Bandsintown/Songkick both turned out to require partnership API access with no self-serve path). Two notifications: immediately (well, next weekly run) when a new show is found, and a reminder 10 days before a show date in case booking was forgotten. Fully deterministic — no LLM at runtime:
- Live Nation venues (The Fillmore, August Hall, The Masonic): JSON-LD parsed from plain HTML, no Playwright needed
- Another Planet Entertainment venues (The Independent, Bill Graham Civic Auditorium, Fox Theater, Greek Theatre): one combined calendar fetch, filtered by venue name
- Independent venue sites (The Warfield, Great American Music Hall, The Chapel, Rickshaw Stop, Cafe du Nord, Neck of the Woods): bespoke per-site parsers
- Bottom of the Hill: plain RSS feed
- Ticketmaster Discovery API (Chase Center, Frost Amphitheatre, SF Symphony/Davies Symphony Hall, Regency Ballroom, SFJAZZ): venues that are bot-walled on their own sites or AXS-only. SFJAZZ coverage is best-effort via keyword search — no dedicated venue ID exists, so it only catches shows Ticketmaster also lists (marquee/collab bookings), not the full subscription season.

Unlike the stateless movie digest, this needs **state tracking**: `concerts/state.json` records which shows have already triggered a "new" or "reminder" notification, and is committed back to the repo by the workflow after each run (`permissions: contents: write` + a git commit step).

Secrets: `CONCERTS_NTFY_TOPIC`, `TICKETMASTER_API_KEY`.

## Conventions
- One subfolder per automation, one workflow file per automation.
- Deterministic code only — no LLM calls at runtime (an earlier cloud-agent approach for the movie digest proved unreliable).
- Secrets via `gh secret set`, never committed.
- Delivery via ntfy.
