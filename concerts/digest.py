"""Weekly SF/Bay Area concert venue tracker.

Fully deterministic, no LLM involved. Tracks a fixed list of venues (not a
fixed artist list — Bandsintown/Songkick both require partnership API access
with no self-serve path) via each venue's own public calendar:
  1. Live Nation venues (The Fillmore, August Hall, The Masonic): JSON-LD
     MusicEvent blocks embedded in plain HTML.
  2. Another Planet Entertainment venues (The Independent, Bill Graham Civic
     Auditorium, Fox Theater, Greek Theatre): one combined calendar fetch,
     filtered by venue name, using schema.org microdata.
  3. Independent venue sites (The Warfield, Great American Music Hall, The
     Chapel, Rickshaw Stop, Cafe du Nord, Neck of the Woods): bespoke
     per-site HTML parsers.
  4. Bottom of the Hill: a plain RSS feed.
  5. Ticketmaster Discovery API (Chase Center, Frost Amphitheatre, SF
     Symphony/Davies Symphony Hall, Regency Ballroom, SFJAZZ best-effort):
     venues that are bot-walled on their own sites or AXS-only.

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

LIVE_NATION_VENUES = {
    "The Fillmore": "https://www.livenation.com/venue/KovZpZAE6eeA/the-fillmore-events",
    "August Hall": "https://www.livenation.com/venue/KovZ917ALXF/august-hall-events",
    "The Masonic": "https://www.livenation.com/venue/KovZpZAJ6nlA/the-masonic-events",
}

APE_CALENDAR_URL = "https://apeconcerts.com/calendar/"
APE_VENUES = {"The Independent", "Bill Graham Civic Auditorium", "Fox Theater", "Greek Theatre"}

WARFIELD_URL = "https://www.thewarfieldtheatre.com/events"
GAMH_URL = "https://gamh.com/calendar/"
CHAPEL_URL = "https://thechapelsf.com/calendar/"
RICKSHAW_URL = "https://rickshawstop.com/calendar/"
CAFE_DU_NORD_URL = "https://cafedunord.com/calendar/"
NECK_OF_THE_WOODS_URL = "https://www.neckofthewoodssf.com/calendar/"
BOTTOM_OF_THE_HILL_RSS_URL = "https://bottomofthehill.com/RSS.xml"

TICKETMASTER_VENUE_IDS = {
    "Chase Center": "230012",
    "Frost Amphitheatre": "230180",
    "SF Symphony (Davies Symphony Hall)": "229526",
}
TICKETMASTER_KEYWORD_VENUES = {
    "Regency Ballroom": ["Regency Ballroom"],
    "SFJAZZ": ["SFJAZZ", "Miner Auditorium"],
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
    """TicketWeb 'tw-' widget, used by Cafe du Nord and Neck of the Woods.
    Each date span (one per event, though several events can share one date)
    is immediately followed in document order by that event's name div."""
    html = get(url).text
    soup = BeautifulSoup(html, "html.parser")
    nodes = soup.select("span.tw-event-date, div.tw-name")
    if not nodes:
        raise ValueError(f"no TicketWeb events found for {venue_name} - page structure may have changed")

    today = local_today()
    shows = []
    current_date: date | None = None
    for node in nodes:
        if "tw-event-date" in node.get("class", []):
            current_date = _parse_ticketweb_date(node.get_text(strip=True), today)
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


def fetch_ticketmaster_by_keyword(venue_name: str, name_filters: list[str]) -> list[dict]:
    """Best-effort: no dedicated Ticketmaster venue ID is known/reliable, so
    search by keyword per filter term and keep only results whose venue name
    actually matches. Will only catch shows Ticketmaster also lists."""
    seen_ids = set()
    events = []
    for kw in name_filters:
        resp = requests.get(
            "https://app.ticketmaster.com/discovery/v2/events.json",
            params={"keyword": kw, "city": "San Francisco", "apikey": TICKETMASTER_API_KEY, "size": 200},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        for ev in data.get("_embedded", {}).get("events", []):
            ev_id = ev.get("id")
            if not ev_id or ev_id in seen_ids:
                continue
            venues = ev.get("_embedded", {}).get("venues", [{}])
            vname = venues[0].get("name", "") if venues else ""
            if any(nf.lower() in vname.lower() for nf in name_filters):
                seen_ids.add(ev_id)
                events.append(ev)
    return _ticketmaster_events_to_shows(events, venue_name)


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
    fetchers.append(("Bottom of the Hill", fetch_bottom_of_the_hill_events))

    for venue_name, venue_id in TICKETMASTER_VENUE_IDS.items():
        fetchers.append((venue_name, lambda v=venue_name, i=venue_id: fetch_ticketmaster_by_venue_id(v, i)))
    for venue_name, keywords in TICKETMASTER_KEYWORD_VENUES.items():
        fetchers.append((venue_name, lambda v=venue_name, k=keywords: fetch_ticketmaster_by_keyword(v, k)))

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
    all_shows = [s for s in all_shows if s["date"] >= today_iso]

    if not all_shows:
        msg = "Every venue source failed to fetch this run:\n" + "\n".join(f"- {label}: {e}" for label, e in failures)
        send_ntfy(msg, "Concerts Digest - Total Failure")
        print(msg)
        sys.exit(1)

    state = load_state()
    shows_state = state["shows"]

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
