"""Weekly SF/Bay Area concert venue tracker.

Fully deterministic, no LLM involved. Tracks a fixed list of venues (not a
fixed artist list — Bandsintown/Songkick both require partnership API access
with no self-serve path) via each venue's own public calendar:
  1. Live Nation venues (The Fillmore, August Hall, The Masonic): JSON-LD
     MusicEvent blocks embedded in plain HTML.
  2. Another Planet Entertainment venues (The Independent, Bill Graham Civic
     Auditorium, Fox Theater, Greek Theatre, The Castro): one combined
     calendar fetch, filtered by venue name, using schema.org microdata.
  3. Independent venue sites (The Warfield, Great American Music Hall, The
     Chapel, Rickshaw Stop, Cafe du Nord, Neck of the Woods, Bimbo's 365
     Club): bespoke per-site HTML parsers.
  4. Bottom of the Hill: a plain RSS feed.
  5. Ticketmaster Discovery API (Chase Center, Frost Amphitheatre, Regency
     Ballroom): venues that are bot-walled on their own sites.
  6. SFJAZZ, The Midway: both behind a Cloudflare managed challenge that
     blocks plain HTTP requests, so Playwright drives a real browser past it
     and reads each site's own internal JSON API. SFJAZZ needs one fresh
     page load per lookahead month (a reused session with an injected fetch
     call doesn't pass the challenge); The Midway's single page load +
     WordPress REST endpoint is cheaper.
  7. SF Symphony/Davies Symphony Hall: sfsymphony.org itself sits behind a
     Queue-it virtual waiting room, but its calendar is powered by a public
     Algolia search index with a client-side search-only key - a plain HTTP
     request, no Playwright needed, and covers the full season (including
     box-office-only shows that never cross-list on Ticketmaster).

State (which shows have already triggered a notification) is tracked in
concerts/state.json, committed back to the repo by the GitHub Actions
workflow after each run — unlike the stateless movies digest.

Env vars required: CONCERTS_NTFY_TOPIC, TICKETMASTER_API_KEY.
"""

from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import os
import requests
from bs4 import BeautifulSoup

CONCERTS_NTFY_TOPIC = os.environ["CONCERTS_NTFY_TOPIC"]
TICKETMASTER_API_KEY = os.environ["TICKETMASTER_API_KEY"]

STATE_PATH = Path(__file__).parent / "state.json"
REMINDER_WINDOW_DAYS = 10

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT}

MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

# Some venue calendars mix in non-concert filler: private room rentals with
# no public performer, and sports-screening "watch parties" on the venue's
# TV/big screen. Filtered by title pattern since they come through the same
# feeds as real shows and have no separate event type field to key off of.
NOISE_TITLE_PATTERNS = [
    re.compile(r"^private event$", re.I),
    re.compile(r"^watch\b", re.I),
    re.compile(r"\bwatch party$", re.I),
]


def is_noise_title(title: str) -> bool:
    return any(p.search(title.strip()) for p in NOISE_TITLE_PATTERNS)

LIVE_NATION_VENUES = {
    "The Fillmore": "https://www.livenation.com/venue/KovZpZAE6eeA/the-fillmore-events",
    "August Hall": "https://www.livenation.com/venue/KovZ917ALXF/august-hall-events",
    "The Masonic": "https://www.livenation.com/venue/KovZpZAJ6nlA/the-masonic-events",
}

APE_CALENDAR_URL = "https://apeconcerts.com/calendar/"
APE_VENUES = {"The Independent", "Bill Graham Civic Auditorium", "Fox Theater", "Greek Theatre", "The Castro"}

WARFIELD_URL = "https://www.thewarfieldtheatre.com/events"
GAMH_URL = "https://gamh.com/calendar/"
CHAPEL_URL = "https://thechapelsf.com/calendar/"
RICKSHAW_URL = "https://rickshawstop.com/calendar/"
CAFE_DU_NORD_URL = "https://cafedunord.com/calendar/"
NECK_OF_THE_WOODS_URL = "https://www.neckofthewoodssf.com/calendar/"
BIMBOS_365_URL = "https://bimbos365club.com/shows/"
BOTTOM_OF_THE_HILL_RSS_URL = "https://bottomofthehill.com/RSS.xml"

# These must be the Discovery API's own alphanumeric venue IDs (as seen in
# _embedded.venues[].id on a real API response), not the numeric IDs in
# ticketmaster.com's website URLs (e.g. .../venue/230012) - those are a
# different, legacy ID scheme and silently return zero events if used here.
# Resolve a new venue's real ID via a one-off keyword search first.
TICKETMASTER_VENUE_IDS = {
    "Chase Center": "KovZ917Ah1H",
    "Frost Amphitheatre": "ZFr9jZdA76",
    "Regency Ballroom": "ZFr9jZ7kv6",
}

def local_today() -> date:
    return datetime.now(ZoneInfo("America/Los_Angeles")).date()


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def show_id(show: dict) -> str:
    return f"{slugify(show['venue'])}:{show['date']}:{slugify(show['title'])}"


def roll_year_if_past(month: int, day: int, today: date) -> date:
    """Venue pages often print dates with no year ("Sat Jul 18"). Pick the
    nearest occurrence: this year, or next year if that date already passed."""
    year = today.year
    try:
        d = date(year, month, day)
    except ValueError:
        d = date(year, month, min(day, 28))
    if d < today - timedelta(days=1):
        d = date(year + 1, month, day)
    return d


def get(url: str, **kwargs) -> requests.Response:
    resp = requests.get(url, headers=HEADERS, timeout=20, **kwargs)
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# Per-source parsers. Each returns a list of {"venue", "title", "date", "url"}.
# A page that fetches fine but yields zero recognizable events is treated as
# a parse failure (raised), not "no shows this week" — otherwise a broken
# selector after a site redesign looks identical to a quiet week.
# ---------------------------------------------------------------------------


def fetch_livenation_events(venue_name: str, url: str) -> list[dict]:
    html = get(url).text
    blocks = re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)
    shows = []
    for b in blocks:
        try:
            data = json.loads(b)
        except json.JSONDecodeError:
            continue
        if data.get("@type") != "MusicEvent":
            continue
        start_date = data.get("startDate", "")[:10]
        name = data.get("name")
        if not (start_date and name):
            continue
        shows.append({"venue": venue_name, "title": name, "date": start_date, "url": data.get("url", url)})
    if not shows:
        raise ValueError(f"no MusicEvent JSON-LD found for {venue_name}")
    return shows


def fetch_ape_events() -> list[dict]:
    html = get(APE_CALENDAR_URL).text
    soup = BeautifulSoup(html, "html.parser")
    articles = soup.select("article.event")
    if not articles:
        raise ValueError("no APE calendar events found - page structure may have changed")

    shows = []
    for art in articles:
        venue_el = art.select_one('[itemprop="location"] [itemprop="name"]')
        venue = venue_el.get_text(strip=True) if venue_el else None
        if venue not in APE_VENUES:
            continue
        title_el = art.select_one("h2.attraction_title")
        title = title_el.get_text(strip=True) if title_el else None
        date_el = art.select_one('[itemprop="startDate"]')
        raw_date = date_el.get("content") if date_el else None
        url_el = art.select_one("header a[href]")
        url = url_el["href"] if url_el else APE_CALENDAR_URL
        if not (title and raw_date):
            continue
        shows.append({"venue": venue, "title": title, "date": raw_date[:10], "url": url})

    if not shows:
        raise ValueError("APE calendar fetched but no events matched our 4 tracked venue names")
    return shows


def fetch_warfield_events() -> list[dict]:
    html = get(WARFIELD_URL).text
    soup = BeautifulSoup(html, "html.parser")
    entries = soup.select("div.entry.warfield")
    if not entries:
        raise ValueError("no Warfield events found - page structure may have changed")

    shows = []
    for entry in entries:
        title_el = entry.select_one("h3.carousel_item_title_small a")
        title = title_el.get_text(strip=True) if title_el else None
        date_el = entry.select_one("span.date")
        date_text = date_el.get_text(" ", strip=True) if date_el else ""
        m = re.search(r"([A-Za-z]{3})[a-z]*\s+(\d{1,2}),\s+(\d{4})", date_text)
        if not (title and m):
            continue
        month = MONTHS.get(m.group(1)[:3])
        if not month:
            continue
        url = title_el["href"] if title_el.has_attr("href") else WARFIELD_URL
        iso = date(int(m.group(3)), month, int(m.group(2))).isoformat()
        shows.append({"venue": "The Warfield", "title": title, "date": iso, "url": url})
    return shows


def fetch_seetickets_list_events(venue_name: str, url: str) -> list[dict]:
    """See Tickets 'list view' layout, used by Great American Music Hall."""
    html = get(url).text
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select(".seetickets-list-event-content-container")
    if not items:
        raise ValueError(f"no See Tickets list events found for {venue_name}")

    today = local_today()
    shows = []
    for item in items:
        title_el = item.select_one("p.event-title a")
        title = title_el.get_text(strip=True) if title_el else None
        date_el = item.select_one("p.event-date")
        date_text = date_el.get_text(strip=True) if date_el else ""
        m = re.search(r"([A-Za-z]{3})[a-z]*\s+(\d{1,2})$", date_text)
        if not (title and m):
            continue
        month = MONTHS.get(m.group(1)[:3])
        if not month:
            continue
        url_ = title_el["href"] if title_el.has_attr("href") else url
        d = roll_year_if_past(month, int(m.group(2)), today)
        shows.append({"venue": venue_name, "title": title, "date": d.isoformat(), "url": url_})
    return shows


def fetch_seetickets_calendar_events(venue_name: str, url: str) -> list[dict]:
    """See Tickets 'calendar grid' layout, used by The Chapel and Rickshaw
    Stop. Events sit in <td> day cells with only a day-of-month number
    (no month/year) - anchor on the cell marked class="today" and walk
    outward, rolling the month/year forward or backward as the day number
    wraps, the same trick digest.py's sibling movies script uses for hour
    rollover."""
    html = get(url).text
    soup = BeautifulSoup(html, "html.parser")
    today = local_today()

    day_cells = [c for c in soup.select("td") if c.select_one(".date-number")]
    if not day_cells:
        raise ValueError(f"no calendar day cells found for {venue_name} - page structure may have changed")

    today_idx = next((i for i, c in enumerate(day_cells) if "today" in c.get("class", [])), None)
    if today_idx is None:
        raise ValueError(f"no 'today' cell found for {venue_name} - page structure may have changed")

    def day_num_of(cell) -> int | None:
        el = cell.select_one(".date-number")
        try:
            return int(el.get_text(strip=True))
        except (TypeError, ValueError):
            return None

    dates_by_idx: dict[int, date] = {today_idx: today}

    cur, prev_day = today, today.day
    for i in range(today_idx + 1, len(day_cells)):
        day_num = day_num_of(day_cells[i])
        if day_num is None:
            continue
        if day_num < prev_day:
            cur = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        try:
            cur = cur.replace(day=day_num)
        except ValueError:
            continue
        dates_by_idx[i] = cur
        prev_day = day_num

    cur, prev_day = today, today.day
    for i in range(today_idx - 1, -1, -1):
        day_num = day_num_of(day_cells[i])
        if day_num is None:
            continue
        if day_num > prev_day:
            cur = cur.replace(day=1) - timedelta(days=1)
        try:
            cur = cur.replace(day=day_num)
        except ValueError:
            continue
        dates_by_idx[i] = cur
        prev_day = day_num

    shows = []
    for i, cell in enumerate(day_cells):
        d = dates_by_idx.get(i)
        if not d:
            continue
        for ev in cell.select(".seetickets-calendar-event-container"):
            title_el = ev.select_one(".seetickets-calendar-event-title a")
            title = title_el.get_text(strip=True) if title_el else None
            if not title:
                continue
            url_ = title_el["href"] if title_el.has_attr("href") else url
            shows.append({"venue": venue_name, "title": title, "date": d.isoformat(), "url": url_})

    if not shows:
        raise ValueError(f"calendar grid parsed but no events found for {venue_name}")
    return shows


def _parse_ticketweb_date(text: str, today: date) -> date | None:
    """TicketWeb renders dates two different ways across venue sites: a
    weekday+month-name form ("Fri, Jul 17") or a bare numeric "7.18"."""
    m = re.search(r"([A-Za-z]{3})[a-z]*\s+(\d{1,2})$", text)
    if m:
        month = MONTHS.get(m.group(1)[:3])
        return roll_year_if_past(month, int(m.group(2)), today) if month else None
    m = re.match(r"^(\d{1,2})\.(\d{1,2})$", text)
    if m:
        return roll_year_if_past(int(m.group(1)), int(m.group(2)), today)
    return None


def fetch_ticketweb_events(venue_name: str, url: str) -> list[dict]:
    """TicketWeb 'tw-' widget, used by Cafe du Nord, Neck of the Woods, and
    Bimbo's 365 Club. Each date marker (one per event, though several events
    can share one date) is immediately followed in document order by that
    event's name div. Bimbo's variant splits the date across a separate
    tw-event-month span ("August") and a bare-number tw-event-date span
    ("6"), rather than a single self-contained date string."""
    html = get(url).text
    soup = BeautifulSoup(html, "html.parser")
    nodes = soup.select("span.tw-event-month, span.tw-event-date, div.tw-name")
    if not nodes:
        raise ValueError(f"no TicketWeb events found for {venue_name} - page structure may have changed")

    today = local_today()
    shows = []
    current_date: date | None = None
    pending_month: int | None = None
    for node in nodes:
        classes = node.get("class", [])
        if "tw-event-month" in classes:
            pending_month = MONTHS.get(node.get_text(strip=True)[:3])
            continue
        if "tw-event-date" in classes:
            text = node.get_text(strip=True)
            current_date = _parse_ticketweb_date(text, today)
            if current_date is None and pending_month and text.isdigit():
                current_date = roll_year_if_past(pending_month, int(text), today)
            continue
        a = node.select_one("a")
        title = a.get_text(strip=True) if a else None
        if not (title and current_date):
            continue
        url_ = a["href"] if a.has_attr("href") else url
        shows.append({"venue": venue_name, "title": title, "date": current_date.isoformat(), "url": url_})
    return shows


def fetch_bottom_of_the_hill_events() -> list[dict]:
    """Plain RSS feed with the date baked into the item title, e.g.
    "2026  07/28  :  Jesse Malin ------cover charge is actually $35/$40"."""
    xml_text = get(BOTTOM_OF_THE_HILL_RSS_URL).text
    root = ET.fromstring(xml_text)
    items = root.findall(".//item")
    if not items:
        raise ValueError("no Bottom of the Hill RSS items found")

    shows = []
    for item in items:
        title_text = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or BOTTOM_OF_THE_HILL_RSS_URL).strip()
        m = re.match(r"(\d{4})\s+(\d{2})/(\d{2})\s*:\s*(.+)", title_text)
        if not m:
            continue
        year, month, day, rest = m.groups()
        name = re.split(r"-{2,}|:::", rest)[0].strip()
        try:
            iso = date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            continue
        shows.append({"venue": "Bottom of the Hill", "title": name, "date": iso, "url": link})
    return shows


def _ticketmaster_events_to_shows(events: list[dict], venue_name: str) -> list[dict]:
    shows = []
    for ev in events:
        name = ev.get("name")
        local_date = ev.get("dates", {}).get("start", {}).get("localDate")
        if not (name and local_date):
            continue
        shows.append({"venue": venue_name, "title": name, "date": local_date, "url": ev.get("url") or "https://www.ticketmaster.com"})
    return shows


def fetch_ticketmaster_by_venue_id(venue_name: str, venue_id: str) -> list[dict]:
    all_events = []
    page = 0
    while True:
        resp = requests.get(
            "https://app.ticketmaster.com/discovery/v2/events.json",
            params={"venueId": venue_id, "apikey": TICKETMASTER_API_KEY, "size": 200, "page": page},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        all_events.extend(data.get("_embedded", {}).get("events", []))
        total_pages = data.get("page", {}).get("totalPages", 1)
        page += 1
        if page >= total_pages:
            break
    return _ticketmaster_events_to_shows(all_events, venue_name)


SFJAZZ_CALENDAR_URL = "https://www.sfjazz.org/calendar/"
SFJAZZ_LOOKAHEAD_MONTHS = 9
SFJAZZ_EXCLUDED_EVENT_TYPES = {"Education", "Classes & Workshops", "Digital Lab"}


def fetch_sfjazz_events() -> list[dict]:
    """SFJAZZ's site sits behind a Cloudflare managed challenge that a plain
    HTTP request can't pass, but a real browser can. Its calendar page's own
    JS calls an internal JSON API (sfjazz.org/ace-api/events/) per calendar
    month - a fresh full page navigation per month (not a reused session
    with an injected fetch call) is what reliably gets past the challenge,
    since the site's own JS constructs whatever per-request token Cloudflare
    is checking for. This makes SFJAZZ far more expensive than every other
    source here (a full browser launch, ~9 page loads instead of one HTTP
    request) - only worth it because it's a real venue with no other clean
    coverage path (AXS-primary, no public API)."""
    from playwright.sync_api import sync_playwright

    today = local_today()
    month_starts = []
    y, m = today.year, today.month
    for _ in range(SFJAZZ_LOOKAHEAD_MONTHS):
        month_starts.append(date(y, m, 15))  # mid-month, safely inside the range regardless of today's day-of-month
        m += 1
        if m > 12:
            m = 1
            y += 1

    events_by_id: dict[str, dict] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for month_date in month_starts:
            page = browser.new_page(user_agent=USER_AGENT)
            captured: list[dict] = []

            def on_response(response, captured=captured):
                if "ace-api/events/?startDate" in response.url and response.status == 200:
                    try:
                        captured.append(response.json())
                    except Exception:
                        pass

            page.on("response", on_response)
            try:
                page.goto(
                    f"{SFJAZZ_CALENDAR_URL}?date={month_date.isoformat()}&layout=A",
                    timeout=30000,
                    wait_until="networkidle",
                )
                page.wait_for_timeout(3000)
            except Exception:
                pass  # one bad month shouldn't sink the whole fetch; falls through with whatever was captured
            finally:
                page.close()

            for batch in captured:
                for ev in batch:
                    ev_id = ev.get("id")
                    if ev_id:
                        events_by_id[ev_id] = ev
        browser.close()

    if not events_by_id:
        raise ValueError("no SFJAZZ events captured across any lookahead month - Cloudflare challenge or API shape may have changed")

    shows = []
    for ev in events_by_id.values():
        if SFJAZZ_EXCLUDED_EVENT_TYPES.intersection(ev.get("eventTypes", [])):
            continue
        name = ev.get("name", "").strip()
        event_date = (ev.get("eventDate") or "")[:10]
        detail_path = ev.get("viewDetailCtaUrl") or ev.get("buyTicketCtaUrl") or ""
        if not (name and event_date):
            continue
        url = f"https://www.sfjazz.org{detail_path}" if detail_path.startswith("/") else (detail_path or SFJAZZ_CALENDAR_URL)
        shows.append({"venue": "SFJAZZ", "title": name, "date": event_date, "url": url})
    return shows


THE_MIDWAY_CALENDAR_URL = "https://themidwaysf.com/calendar/"


def fetch_the_midway_events() -> list[dict]:
    """Also behind a Cloudflare managed challenge, but unlike SFJAZZ a
    single fresh page load reliably passes it (no per-month retry dance
    needed), and the page's own JS calls a plain WordPress REST endpoint
    (wp-json/showfeed/v1/events) that returns every upcoming event, paged -
    the API rejects any limit over 100 with a 400. That feed actually spans
    several venues under the same promoter (Envelop SF, 888 Garage, etc.)
    plus a recurring non-event "Midway Rewards App" filler entry, so
    filtered down to venue == "The Midway" with a real start_time."""
    from playwright.sync_api import sync_playwright

    events: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(THE_MIDWAY_CALENDAR_URL, timeout=30000, wait_until="networkidle")
        page.wait_for_timeout(1500)

        offset = 0
        while True:
            page_events = page.evaluate(f"""
                async () => {{
                    const resp = await fetch("https://themidwaysf.com/wp-json/showfeed/v1/events?limit=100&offset={offset}", {{
                        headers: {{ "Accept": "application/json" }}
                    }});
                    if (!resp.ok) return null;
                    return await resp.json();
                }}
            """)
            if not page_events:
                break
            events.extend(page_events)
            if len(page_events) < 100:
                break
            offset += 100
        browser.close()

    if not events:
        raise ValueError("no events captured from The Midway's showfeed API - Cloudflare challenge or API shape may have changed")

    shows = []
    for ev in events:
        if ev.get("venue") != "The Midway":
            continue
        name = (ev.get("name") or "").strip()
        event_date = (ev.get("start_time") or "")[:10]
        if not (name and event_date):
            continue
        url = ev.get("url") or THE_MIDWAY_CALENDAR_URL
        shows.append({"venue": "The Midway", "title": name, "date": event_date, "url": url})
    return shows


# sfsymphony.org itself sits behind a Queue-it virtual waiting room, but its
# calendar is powered by a public Algolia search index - the app ID and
# search-only API key below are pulled directly from sfsymphony.org's own
# frontend JS (Scripts/algolia.js) and are meant to be public: Algolia
# "search" keys are read-only and safe to embed client-side by design, same
# as SF Symphony itself already does. This is a plain HTTP request, no
# Playwright needed, and covers box-office-only shows (e.g. St. Vincent's
# 2026-07-30 date) that never cross-list on Ticketmaster.
SF_SYMPHONY_ALGOLIA_APP_ID = "3ZVEWSXVK4"
SF_SYMPHONY_ALGOLIA_API_KEY = "e6c0617a0995d310c9dd600df5af93c2"
SF_SYMPHONY_ALGOLIA_INDEX = "prod_sfs_calendar"
SF_SYMPHONY_LOOKAHEAD_DAYS = 550


def _clean_sf_symphony_title(raw: str) -> str:
    import html as html_module

    no_tags = re.sub(r"<[^>]+>", "", raw)
    return html_module.unescape(no_tags).strip()


def fetch_sf_symphony_events() -> list[dict]:
    today_ts = int(datetime.now(ZoneInfo("America/Los_Angeles")).timestamp())
    end_ts = today_ts + SF_SYMPHONY_LOOKAHEAD_DAYS * 86400

    shows = []
    page = 0
    while True:
        params = (
            f"hitsPerPage=100&page={page}"
            f'&facetFilters=%5B%5B%22excludeFromCalendar%3Afalse%22%5D%5D&query='
            f'&numericFilters=%5B%22startDate%3E%3D{today_ts}%22%2C%22startDate%3C%3D{end_ts}%22%5D'
        )
        resp = requests.post(
            f"https://{SF_SYMPHONY_ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/*/queries",
            params={
                "x-algolia-agent": "Algolia for JavaScript",
                "x-algolia-api-key": SF_SYMPHONY_ALGOLIA_API_KEY,
                "x-algolia-application-id": SF_SYMPHONY_ALGOLIA_APP_ID,
            },
            json={"requests": [{"indexName": SF_SYMPHONY_ALGOLIA_INDEX, "params": params}]},
            timeout=20,
        )
        resp.raise_for_status()
        result = resp.json()["results"][0]

        for hit in result.get("hits", []):
            if hit.get("venue") != "Davies Symphony Hall":
                continue
            title = _clean_sf_symphony_title(hit.get("title", ""))
            event_date = (hit.get("performanceDate") or "")[:10]
            kentico_url = hit.get("kenticoUrl") or ""
            if not (title and event_date):
                continue
            url = f"https://www.sfsymphony.org{kentico_url}" if kentico_url.startswith("/") else "https://www.sfsymphony.org/Calendar"
            shows.append({"venue": "SF Symphony (Davies Symphony Hall)", "title": title, "date": event_date, "url": url})

        page += 1
        if page >= result.get("nbPages", 1):
            break

    if not shows:
        raise ValueError("no SF Symphony events found via Algolia - index name or schema may have changed")
    return shows


def build_venue_fetchers() -> list[tuple[str, Callable[[], list[dict]]]]:
    fetchers: list[tuple[str, Callable[[], list[dict]]]] = []

    for venue_name, url in LIVE_NATION_VENUES.items():
        fetchers.append((venue_name, lambda v=venue_name, u=url: fetch_livenation_events(v, u)))

    fetchers.append(("Another Planet Entertainment venues", fetch_ape_events))
    fetchers.append(("The Warfield", fetch_warfield_events))
    fetchers.append(("Great American Music Hall", lambda: fetch_seetickets_list_events("Great American Music Hall", GAMH_URL)))
    fetchers.append(("The Chapel", lambda: fetch_seetickets_calendar_events("The Chapel", CHAPEL_URL)))
    fetchers.append(("Rickshaw Stop", lambda: fetch_seetickets_calendar_events("Rickshaw Stop", RICKSHAW_URL)))
    fetchers.append(("Cafe du Nord", lambda: fetch_ticketweb_events("Cafe du Nord", CAFE_DU_NORD_URL)))
    fetchers.append(("Neck of the Woods", lambda: fetch_ticketweb_events("Neck of the Woods", NECK_OF_THE_WOODS_URL)))
    fetchers.append(("Bimbo's 365 Club", lambda: fetch_ticketweb_events("Bimbo's 365 Club", BIMBOS_365_URL)))
    fetchers.append(("Bottom of the Hill", fetch_bottom_of_the_hill_events))

    for venue_name, venue_id in TICKETMASTER_VENUE_IDS.items():
        fetchers.append((venue_name, lambda v=venue_name, i=venue_id: fetch_ticketmaster_by_venue_id(v, i)))
    fetchers.append(("SFJAZZ", fetch_sfjazz_events))
    fetchers.append(("The Midway", fetch_the_midway_events))
    fetchers.append(("SF Symphony (Davies Symphony Hall)", fetch_sf_symphony_events))

    return fetchers


# ---------------------------------------------------------------------------
# State, notifications, main
# ---------------------------------------------------------------------------


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"schema_version": 1, "last_updated": "", "shows": {}}


def save_state(state: dict) -> None:
    state["last_updated"] = datetime.now(ZoneInfo("America/Los_Angeles")).isoformat()
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def send_ntfy(message: str, title: str) -> None:
    if len(message) > 4000:
        message = message[:3980] + "\n…(truncated)"
    # The Title header must be Latin-1-encodable (HTTP header restriction,
    # unlike the UTF-8 body below) - fall back rather than let a stray
    # non-ASCII character in a title crash the whole run.
    safe_title = title.encode("ascii", errors="replace").decode("ascii")
    requests.post(
        f"https://ntfy.sh/{CONCERTS_NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": safe_title},
        timeout=15,
    )


def format_show_line(show: dict) -> str:
    return f"{show['date']} — {show['venue']}: {show['title']}\n{show['url']}"


def main() -> None:
    today = local_today()
    failures: list[tuple[str, Exception]] = []
    all_shows: list[dict] = []

    for label, fetcher in build_venue_fetchers():
        try:
            all_shows.extend(fetcher())
        except Exception as e:
            failures.append((label, e))

    # Some sources (e.g. Bottom of the Hill's RSS feed) are a rolling archive
    # that includes already-happened shows, not just upcoming ones. Drop
    # those here rather than in the pruning step below - letting a past show
    # into shows_state even briefly would make it look "new" again on every
    # future run once it gets pruned back out.
    today_iso = today.isoformat()
    all_shows = [s for s in all_shows if s["date"] >= today_iso and not is_noise_title(s["title"])]

    if not all_shows:
        msg = "Every venue source failed to fetch this run:\n" + "\n".join(f"- {label}: {e}" for label, e in failures)
        send_ntfy(msg, "Concerts Digest - Total Failure")
        print(msg)
        sys.exit(1)

    state = load_state()
    # Purge any noise entries a prior run already committed before this
    # filter existed - not just future ones caught by the all_shows filter
    # above.
    shows_state = {sid: e for sid, e in state["shows"].items() if not is_noise_title(e["title"])}

    new_shows = []
    for show in all_shows:
        sid = show_id(show)
        if sid not in shows_state:
            new_shows.append(show)
            shows_state[sid] = {
                **show,
                "first_seen": today.isoformat(),
                "new_notified": False,
                "reminder_notified": False,
            }
        else:
            shows_state[sid]["url"] = show["url"]

    reminder_due = []
    for sid, entry in shows_state.items():
        try:
            show_date = date.fromisoformat(entry["date"])
        except ValueError:
            continue
        days_until = (show_date - today).days
        if 0 <= days_until <= REMINDER_WINDOW_DAYS and not entry.get("reminder_notified"):
            reminder_due.append((sid, entry))

    # Everything below is sent as a single combined push rather than one per
    # category: rapid back-to-back pushes to the same device risk getting
    # silently coalesced by the phone's push-delivery layer before they're
    # ever seen, so one run == at most one notification.
    sections = []
    title_parts = []

    if new_shows:
        lines = [format_show_line(s) for s in sorted(new_shows, key=lambda s: s["date"])]
        sections.append(f"NEW SHOWS ({len(new_shows)})\n" + "\n\n".join(lines))
        title_parts.append(f"{len(new_shows)} new")
        for s in new_shows:
            shows_state[show_id(s)]["new_notified"] = True

    if reminder_due:
        lines = [format_show_line(entry) for _, entry in sorted(reminder_due, key=lambda p: p[1]["date"])]
        sections.append(f"REMINDERS ({len(reminder_due)})\n" + "\n\n".join(lines))
        title_parts.append(f"{len(reminder_due)} reminders")
        for sid, entry in reminder_due:
            shows_state[sid]["reminder_notified"] = True

    state["shows"] = {sid: e for sid, e in shows_state.items() if e["date"] >= today.isoformat()}
    save_state(state)

    if failures:
        sections.append(
            f"FAILURES ({len(failures)})\n" + "\n".join(f"- {label}: {e}" for label, e in failures)
        )
        title_parts.append(f"{len(failures)} failed")
        print("Some venues failed to fetch this run (others succeeded, state was still updated):")
        for label, e in failures:
            print(f"- {label}: {e}")

    if sections:
        send_ntfy("\n\n===\n\n".join(sections), "Concerts Digest: " + ", ".join(title_parts))

    print(f"New: {len(new_shows)}, Reminders: {len(reminder_due)}, Failures: {len(failures)}")


if __name__ == "__main__":
    main()
