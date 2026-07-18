"""Weekly AMC Metreon 16 movie digest.

Fully deterministic, no LLM involved:
  1. Playwright renders the AMC showtimes page (JS-rendered, plain HTTP won't work)
     and we parse the visible text with regex.
  2. OMDb API gives director/cast/genre/IMDb rating/RT score per movie (deterministic
     JSON lookup) and its short plot doubles as a spoiler-safe premise.
  3. Rotten Tomatoes' critics-consensus line is fetched by guessing the page slug
     (title, then title+year as fallback) and reading the static HTML.
  4. Digest is formatted and pushed to ntfy.

Env vars required: OMDB_API_KEY, NTFY_TOPIC.
Optional: AMC_THEATRE_SLUG (default amc-metreon-16), AMC_THEATRE_CITY_SLUG
(default san-francisco), AMC_THEATRE_NAME (default "AMC Metreon 16").
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

THEATRE_SLUG = os.environ.get("AMC_THEATRE_SLUG", "amc-metreon-16")
CITY_SLUG = os.environ.get("AMC_THEATRE_CITY_SLUG", "san-francisco")
THEATRE_NAME = os.environ.get("AMC_THEATRE_NAME", "AMC Metreon 16")
OMDB_API_KEY = os.environ["OMDB_API_KEY"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]

MPAA_RATINGS = {"G", "PG", "PG-13", "PG13", "R", "NC-17", "NR", "Not Rated"}
NON_MOVIE_PATTERNS = [re.compile(r"screen unseen", re.I)]


def local_today() -> str:
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


def fetch_rendered_text(date_str: str) -> str:
    url = (
        f"https://www.amctheatres.com/movie-theatres/{CITY_SLUG}/{THEATRE_SLUG}"
        f"/showtimes?date={date_str}"
    )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(2000)
        text = page.inner_text("body")
        browser.close()
    return text


def extract_master_titles(text: str) -> list[str]:
    start = text.find("All Movies")
    end = text.find("Premium Offerings", start)
    if start == -1 or end == -1:
        return []
    block = text[start + len("All Movies") : end]
    titles = [t.strip() for t in block.splitlines() if t.strip()]
    return [t for t in titles if not any(p.search(t) for p in NON_MOVIE_PATTERNS)]


def extract_showtime_after_5pm(times: list[tuple[str, str, str]]) -> list[str]:
    out = []
    for h, m, period in times:
        hour = int(h)
        period = period.lower()
        if period == "pm" and hour != 12:
            hour24 = hour + 12
        elif period == "pm" and hour == 12:
            hour24 = 12
        else:
            continue  # am times are matinees, not wanted
        if hour24 >= 17:
            out.append(f"{h}:{m}{period}")
    # de-dupe while preserving order
    seen = set()
    result = []
    for t in out:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


def parse_movies(text: str) -> list[dict]:
    titles = extract_master_titles(text)
    if not titles:
        return []

    anchor = text.find("Movies start 25-30 minutes after showtime.")
    body = text[anchor:] if anchor != -1 else text

    lines = body.splitlines()
    blocks = []  # list of (title, start_line_idx)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped in titles:
            # confirm a runtime pattern shows up in the next few lines
            lookahead = "\n".join(lines[i : i + 4])
            if re.search(r"\d+\s*HR\s*\d+\s*MIN?", lookahead):
                blocks.append((stripped, i))

    movies = []
    for idx, (title, start) in enumerate(blocks):
        end = blocks[idx + 1][1] if idx + 1 < len(blocks) else len(lines)
        block_text = "\n".join(lines[start:end])

        runtime_match = re.search(r"(\d+)\s*HR\s*(\d+)\s*MIN?", block_text)
        runtime = f"{runtime_match.group(1)}h {runtime_match.group(2)}m" if runtime_match else "?"

        rating = "NR"
        for line in block_text.splitlines():
            if line.strip() in MPAA_RATINGS:
                rating = line.strip()
                break

        times = re.findall(r"\b(\d{1,2}):(\d{2})(am|pm)\b", block_text, re.I)
        showtimes = extract_showtime_after_5pm(times)

        movies.append(
            {
                "title": title,
                "runtime": runtime,
                "rating": rating,
                "showtimes": showtimes,
            }
        )
    return movies


def omdb_lookup(title: str) -> dict | None:
    # A bare `t=` lookup returns OMDb's "most popular" match for the title,
    # which for common names is often an older, unrelated film/show (e.g. "The
    # Odyssey" -> a 1997 TV miniseries, "Supergirl" -> the CW series). Since
    # everything on today's AMC schedule is a current theatrical release,
    # search and prefer the most recent match instead.
    current_year = datetime.now().year
    search_resp = requests.get(
        "http://www.omdbapi.com/",
        params={"s": title, "type": "movie", "apikey": OMDB_API_KEY},
        timeout=15,
    )
    search_data = search_resp.json()
    imdb_id = None
    possible_stale_match = False
    if search_data.get("Response") == "True":
        results = search_data.get("Search", [])
        dated = []
        for item in results:
            year_str = re.match(r"\d{4}", item.get("Year", ""))
            if year_str:
                dated.append((int(year_str.group()), item["Title"], item["imdbID"]))

        # Primary: a genuinely recent release (this is what we expect for
        # something currently in first-run theaters).
        recent = [c for c in dated if c[0] >= current_year - 1]
        if recent:
            recent.sort(reverse=True)
            imdb_id = recent[0][2]
        else:
            # Fallback: an exact title match of any age. Covers legitimate
            # theatrical re-releases (e.g. a re-run of the 2016 "Moana").
            # Flagged as possibly-stale since it can also mean the real
            # current release just isn't in OMDb's database yet.
            exact = [c for c in dated if c[1].lower() == title.lower()]
            if exact:
                exact.sort(reverse=True)
                imdb_id = exact[0][2]
                possible_stale_match = True

    if not imdb_id:
        return None
    resp = requests.get(
        "http://www.omdbapi.com/",
        params={"i": imdb_id, "apikey": OMDB_API_KEY},
        timeout=15,
    )
    data = resp.json()
    if data.get("Response") != "True":
        return None
    rt = next(
        (r["Value"] for r in data.get("Ratings", []) if r["Source"] == "Rotten Tomatoes"),
        None,
    )
    return {
        "director": data.get("Director", "N/A"),
        "cast": ", ".join(data.get("Actors", "").split(", ")[:3]),
        "genre": data.get("Genre", "N/A"),
        "imdb_rating": data.get("imdbRating", "N/A"),
        "rt_rating": rt,
        "plot": data.get("Plot", ""),
        "year": data.get("Year", ""),
        "possible_stale_match": possible_stale_match,
    }


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()
    return re.sub(r"\s+", "_", slug)


def rt_consensus(title: str, year: str) -> str | None:
    candidates = [slugify(title)]
    if year:
        candidates.append(f"{slugify(title)}_{year[:4]}")
    for slug in candidates:
        try:
            resp = requests.get(
                f"https://www.rottentomatoes.com/m/{slug}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        m = re.search(r"Critics Consensus.*?<p>(.*?)</p>", resp.text, re.S)
        if m:
            consensus = re.sub("<[^>]+>", "", m.group(1)).strip()
            if consensus:
                return consensus
    return None


def one_line_premise(plot: str) -> str:
    if not plot or plot == "N/A":
        return "No premise available."
    first_sentence = re.split(r"(?<=[.!?])\s", plot.strip())[0]
    return first_sentence


def build_entry(movie: dict) -> str:
    omdb = omdb_lookup(movie["title"])
    lines = [f"{movie['title']} ({movie['rating']}, {movie['runtime']})"]

    if omdb:
        stale_note = f" (matched: {omdb['year']}, unverified year)" if omdb["possible_stale_match"] else ""
        lines.append(f"Dir: {omdb['director']} | Cast: {omdb['cast']}{stale_note}")
        lines.append(f"Genre: {omdb['genre']}")
        lines.append(one_line_premise(omdb["plot"]))
        scores = f"IMDb {omdb['imdb_rating']}/10"
        if omdb["rt_rating"]:
            scores += f" | RT {omdb['rt_rating']}"
        lines.append(scores)
        consensus = rt_consensus(movie["title"], omdb["year"])
        if consensus:
            lines.append(f'"{consensus}"')
    else:
        lines.append("(no verified cast/rating data found)")

    if movie["showtimes"]:
        lines.append(f"Showtimes after 5pm: {', '.join(movie['showtimes'])}")
    else:
        lines.append("No showtimes after 5pm today.")

    return "\n".join(lines)


def send_ntfy(message: str) -> None:
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": f"Movies Today - {THEATRE_NAME}"},
        timeout=15,
    )


def main() -> None:
    date_str = local_today()
    text = fetch_rendered_text(date_str)
    movies = parse_movies(text)

    if not movies:
        send_ntfy(f"Couldn't parse today's ({date_str}) lineup at {THEATRE_NAME}. Check the site manually.")
        sys.exit(1)

    entries = [build_entry(m) for m in movies]
    digest = f"{THEATRE_NAME} — {date_str}\n\n" + "\n\n".join(entries)

    if len(digest) > 4000:
        digest = digest[:3980] + "\n…(truncated)"

    send_ntfy(digest)
    print(digest)


if __name__ == "__main__":
    main()
