import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import cloudscraper


THEATRE_CODE = "1711"
THEATRE_URL = f"https://www.regmovies.com/theatres/regal-hunt-valley-{THEATRE_CODE}"
PROGRAMS_URL = "https://www.regmovies.com/programs"
API_URL = "https://www.regmovies.com/api/getShowtimes"


def slugify_movie_path(title: str, ho_code: str) -> str:
    slug = title.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return f"https://www.regmovies.com/movies/{slug}-{ho_code.lower()}"


def parse_api_date(date_str: str) -> str:
    # API datesWithShows values look like: 2026-06-01T00:00:00
    return date_str.split("T", 1)[0]


def format_event_time(calendar_show_time: str) -> str:
    # calendarShowTime looks like 2026-06-01T19:00:00 (local theatre time).
    dt = datetime.fromisoformat(calendar_show_time)
    hour = dt.hour % 12
    if hour == 0:
        hour = 12
    suffix = "am" if dt.hour < 12 else "pm"
    return f"{hour}:{dt.minute:02d}{suffix}"


def display_title(title: str) -> str:
    m = re.match(r"^[A-Z0-9]{2,8}:\s*(.+)$", title)
    return m.group(1).strip() if m else title


def program_for_title(title: str) -> Optional[str]:
    if re.match(r"^SMX\d{2}:\s", title):
        return "Summer Movie Express"
    if title.startswith("Monday Mystery Movie"):
        return "Monday Mystery Movies"
    if re.match(r"^[A-Z]{3,4}S:\s", title):
        return "Musical Mayhem / Classic Movies"
    if "ghibli" in title.lower() or re.search(r"\bghibli\b", title, re.I):
        return "Ghibli Fest / Anime Films"
    if "anime" in title.lower():
        return "Anime Films"
    if "(sensory)" in title.lower():
        return "My Way Matinee"
    return None


def pick_poster(movie_entry: Dict[str, Any]) -> str:
    media = movie_entry.get("Media") or []
    if not isinstance(media, list):
        return ""
    preferred = [
        "TV_SmallPosterImage",
        "TV_PosterImage",
        "Mobile_Listing",
        "Mobile_MovieFeed",
        "Poster",
    ]
    by_subtype: Dict[str, str] = {}
    for item in media:
        if not isinstance(item, dict):
            continue
        sub = (item.get("SubType") or "").strip()
        url = (item.get("SecureUrl") or item.get("Url") or "").strip()
        if sub and url:
            by_subtype.setdefault(sub, url)
    for sub in preferred:
        if sub in by_subtype:
            return by_subtype[sub]
    # fallback: any image url
    for item in media:
        if isinstance(item, dict) and (item.get("SecureUrl") or item.get("Url")):
            return (item.get("SecureUrl") or item.get("Url") or "").strip()
    return ""


def movie_to_record(movie_entry: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    ho_code = movie_entry.get("MasterMovieCode") or ""
    title = (movie_entry.get("Title") or "").strip()
    rating = (movie_entry.get("Rating") or "").strip()
    duration = movie_entry.get("Duration")
    genre_primary = (movie_entry.get("GenrePrimary") or "").strip()
    genre_secondary = (movie_entry.get("GenreSecondary") or "").strip()
    genre = " / ".join([g for g in [genre_primary, genre_secondary] if g])
    description = (movie_entry.get("Description") or "").strip()
    trailer = (movie_entry.get("TrailerUrl") or "").strip()
    poster = pick_poster(movie_entry)
    record: Dict[str, Any] = {
        "title": title,
        "displayTitle": display_title(title),
        "rating": rating,
        "durationMinutes": duration if isinstance(duration, int) else None,
        "runtime": f"{duration // 60}HR {duration % 60}MINS" if isinstance(duration, int) else "",
        "genre": genre,
        "description": description,
        "poster": poster,
        "trailer": trailer,
    }
    # Keep durationMinutes/runtime consistent with existing file (no nulls)
    if record["durationMinutes"] is None:
        record["durationMinutes"] = 0
    if not record["runtime"] and record["durationMinutes"]:
        d = int(record["durationMinutes"])
        record["runtime"] = f"{d // 60}HR {d % 60}MINS"
    return ho_code, record


def api_url_for(date_yyyy_mm_dd: str) -> str:
    dt = datetime.fromisoformat(date_yyyy_mm_dd)
    return f"{API_URL}?theatres={THEATRE_CODE}&date={dt:%m-%d-%Y}"


@dataclass(frozen=True)
class EventKey:
    performance_id: Optional[int]
    ho_code: str
    date: str
    time: str


def key_for_event(event: Dict[str, Any]) -> EventKey:
    pid = event.get("performanceId")
    return EventKey(
        performance_id=pid if isinstance(pid, int) else None,
        ho_code=str(event.get("hoCode") or ""),
        date=str(event.get("date") or ""),
        time=str(event.get("time") or ""),
    )


def merge_movie_record(
    old: Optional[Dict[str, Any]], new: Dict[str, Any]
) -> Dict[str, Any]:
    if not old:
        return new
    merged = dict(old)
    for key, value in new.items():
        if key in ["title", "displayTitle"]:
            if value and value != old.get(key):
                merged[key] = value
            continue
        if not merged.get(key) and value:
            merged[key] = value
    for required in [
        "title",
        "displayTitle",
        "rating",
        "durationMinutes",
        "runtime",
        "genre",
        "description",
        "poster",
        "trailer",
    ]:
        merged.setdefault(required, "" if required not in ["durationMinutes"] else 0)
    return merged


def build_schedule(
    reference_date: str, max_days: int, old_data: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )

    def fetch_json(url: str) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for _ in range(3):
            try:
                resp = scraper.get(url, timeout=45)
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                ctype = (resp.headers.get("content-type") or "").lower()
                if "json" not in ctype:
                    raise RuntimeError(f"unexpected content-type: {ctype or 'unknown'}")
                return resp.json()
            except Exception as exc:  # noqa: BLE001 - best-effort scraping
                last_error = exc
        raise RuntimeError(f"Failed to fetch JSON from {url}: {last_error}")

    # Best-effort "crawl" of the public pages requested by the automation.
    for url in [THEATRE_URL, PROGRAMS_URL]:
        try:
            scraper.get(url, timeout=30)
        except Exception:
            pass

    first = fetch_json(api_url_for(reference_date))
    dates_with_shows = [parse_api_date(d) for d in (first.get("datesWithShows") or [])]

    start = datetime.fromisoformat(reference_date).date()
    end = start + timedelta(days=max_days)
    candidate_dates = [
        d for d in dates_with_shows if start <= datetime.fromisoformat(d).date() <= end
    ]

    old_movies: Dict[str, Dict[str, Any]] = {}
    if isinstance(old_data, dict) and isinstance(old_data.get("movies"), dict):
        old_movies = old_data["movies"]

    movies_raw: Dict[str, Dict[str, Any]] = {}
    events: List[Dict[str, Any]] = []

    def merge_movies(api_json: Dict[str, Any]) -> None:
        for entry in api_json.get("movies") or []:
            if not isinstance(entry, dict):
                continue
            ho_code, record = movie_to_record(entry)
            if not ho_code:
                continue
            movies_raw[ho_code] = merge_movie_record(old_movies.get(ho_code), record)

    merge_movies(first)

    def add_event(ev: Dict[str, Any]) -> None:
        events.append(ev)

    def extract_events_for_day(api_json: Dict[str, Any], date_str: str) -> None:
        shows = api_json.get("shows") or []
        for show in shows:
            if str(show.get("TheatreCode") or "") != THEATRE_CODE:
                continue
            for film in show.get("Film") or []:
                title = (film.get("Title") or "").strip()
                ho_code = (film.get("MasterMovieCode") or "").strip()
                program = program_for_title(title)
                if not program or not ho_code:
                    continue
                for perf in film.get("Performances") or []:
                    calendar = perf.get("CalendarShowTime")
                    utc = perf.get("UtcShowTime") or ""
                    if not calendar:
                        continue
                    attributes = perf.get("PerformanceAttributes") or []
                    if not isinstance(attributes, list):
                        attributes = []
                    event = {
                        "program": program,
                        "title": title,
                        "displayTitle": display_title(title),
                        "hoCode": ho_code,
                        "date": date_str,
                        "time": format_event_time(calendar),
                        "calendarShowTime": calendar,
                        "utcShowTime": utc,
                        "performanceId": perf.get("PerformanceId"),
                        "auditorium": perf.get("Auditorium") or "",
                        "format": "IMAX"
                        if any("imax" in str(a).lower() for a in attributes)
                        else "Standard",
                        "attributes": attributes,
                        "soldOut": bool(perf.get("StopSales") is True),
                        "url": slugify_movie_path(title, ho_code),
                        "source": api_url_for(date_str),
                    }
                    add_event(event)

    for date_str in candidate_dates:
        try:
            api_json = fetch_json(api_url_for(date_str))
        except Exception:
            continue
        merge_movies(api_json)
        extract_events_for_day(api_json, date_str)

    # Add future shows (no exact time available yet).
    existing_keys = {key_for_event(e) for e in events}
    future_shows = first.get("futureShows") or []
    for item in future_shows:
        if not isinstance(item, dict):
            continue
        ho_code = (item.get("hoCode") or "").strip()
        if not ho_code:
            continue
        title = movies_raw.get(ho_code, {}).get("title", "")
        program = program_for_title(title) if title else None
        if not program:
            continue
        for d in item.get("dates") or []:
            if not isinstance(d, dict):
                continue
            raw_date = (d.get("date") or "").strip()
            if not raw_date:
                continue
            # futureShows uses M-D-YYYY (no leading zeros).
            month, day, year = raw_date.split("-")
            date_str = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
            if datetime.fromisoformat(date_str).date() > end:
                continue
            ev = {
                "program": program,
                "title": title,
                "displayTitle": display_title(title),
                "hoCode": ho_code,
                "date": date_str,
                "time": "TBD",
                "calendarShowTime": f"{date_str}T23:59:00",
                "utcShowTime": "",
                "performanceId": None,
                "auditorium": "",
                "format": "TBD",
                "attributes": [],
                "soldOut": False,
                "url": slugify_movie_path(title, ho_code),
                "source": THEATRE_URL,
                "note": "Regal lists this event date, but exact showtime is not available yet.",
            }
            k = key_for_event(ev)
            # Avoid adding duplicates where a timed showing already exists for the same hoCode/date.
            if any(
                e.ho_code == k.ho_code and e.date == k.date and e.time != "TBD"
                for e in existing_keys
            ):
                continue
            if k in existing_keys:
                continue
            existing_keys.add(k)
            add_event(ev)

    events.sort(key=lambda e: (e["date"], e["time"] == "TBD", e["time"], e["title"]))

    referenced = {str(e.get("hoCode") or "") for e in events if e.get("hoCode")}
    movies: Dict[str, Dict[str, Any]] = {}
    for ho_code in sorted(referenced):
        if ho_code in movies_raw:
            movies[ho_code] = movies_raw[ho_code]

    generated_at = datetime.utcnow().isoformat(timespec="milliseconds") + "Z"

    return {
        "generatedAt": generated_at,
        "theatre": {
            "name": "Regal Hunt Valley",
            "code": THEATRE_CODE,
            "address": "11511 MCCORMICK RD, HUNT VALLEY MD 21030",
            "zipSearched": "21152",
            "url": THEATRE_URL,
        },
        "source": {
            "programsUrl": PROGRAMS_URL,
            "theatreUrl": THEATRE_URL,
            "api": f"{API_URL}?theatres={THEATRE_CODE}&date=MM-DD-YYYY",
        },
        "notes": [
            "Exact times are theatre-specific and were pulled for Regal Hunt Valley.",
            "Only events that Regal exposes in Hunt Valley showtimes are included.",
        ],
        "movies": movies,
        "events": events,
    }


def diff_events(
    old_events: List[Dict[str, Any]], new_events: List[Dict[str, Any]]
) -> Dict[str, Any]:
    def key(event: Dict[str, Any]) -> Tuple[str, str, str]:
        pid = event.get("performanceId")
        if isinstance(pid, int):
            return ("pid", str(pid), "")
        return ("slot", str(event.get("hoCode") or ""), f"{event.get('date')}|{event.get('time')}")

    old_map = {key(e): e for e in old_events}
    new_map = {key(e): e for e in new_events}

    added = [new_map[k] for k in new_map.keys() - old_map.keys()]
    removed = [old_map[k] for k in old_map.keys() - new_map.keys()]

    changed: List[Tuple[Dict[str, Any], Dict[str, Any], List[str]]] = []
    for k in new_map.keys() & old_map.keys():
        before = old_map[k]
        after = new_map[k]
        fields = [
            "program",
            "title",
            "date",
            "time",
            "auditorium",
            "format",
            "attributes",
            "soldOut",
            "note",
        ]
        diffs = [f for f in fields if before.get(f) != after.get(f)]
        if diffs:
            changed.append((before, after, diffs))

    return {"added": added, "removed": removed, "changed": changed}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data/regal-hunt-valley-programs.json")
    parser.add_argument(
        "--reference-date",
        default=datetime.now().date().isoformat(),
        help="YYYY-MM-DD used to seed datesWithShows/futureShows",
    )
    parser.add_argument(
        "--max-days",
        type=int,
        default=180,
        help="Max days ahead to include from API datesWithShows/futureShows",
    )
    args = parser.parse_args()

    out_path = Path(args.out)
    old = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else None
    new = build_schedule(args.reference_date, args.max_days, old_data=old)

    if old:
        delta = diff_events(old.get("events") or [], new.get("events") or [])
        if not delta["added"] and not delta["removed"] and not delta["changed"]:
            print("No schedule changes detected; leaving file untouched.")
            return 0
        # Preserve prior generatedAt only if needed? We intentionally update it for real changes.

    out_path.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
