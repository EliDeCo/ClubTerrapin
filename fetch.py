"""Download TerpLink's public events feed and store it in a local SQLite database.

Meant to run once a day. Rerunning is safe: events are matched by their TerpLink
ID, so existing rows are updated instead of duplicated, and events that have
dropped out of the feed (because they already happened) are kept.
"""

import sqlite3
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

FEED_URL = "https://terplink.umd.edu/events.rss"
DB_PATH = Path(__file__).parent / "clubterrapin.db"
EASTERN = ZoneInfo("America/New_York")

# The feed puts start, end, location, status and host in an XML namespace named "events".
NS = "{events}"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id               INTEGER PRIMARY KEY,  -- the number at the end of the TerpLink event URL
    title            TEXT NOT NULL,
    description_html TEXT,
    description_text TEXT,
    start_time       TEXT NOT NULL,        -- ISO 8601 in Eastern time, e.g. 2026-11-02T20:00:00-05:00
    end_time         TEXT,
    location         TEXT,
    status           TEXT,                 -- 'confirmed' or 'cancelled'
    contact_email    TEXT,
    image_url        TEXT,
    link             TEXT,
    first_seen       TEXT NOT NULL,        -- when fetch.py first saw this event
    last_seen        TEXT NOT NULL         -- the last time it was still in the feed
);

CREATE TABLE IF NOT EXISTS organizations (
    id   INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id   INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

-- An event can have several hosts and several categories, so those live in link tables.
CREATE TABLE IF NOT EXISTS event_hosts (
    event_id INTEGER NOT NULL REFERENCES events(id),
    org_id   INTEGER NOT NULL REFERENCES organizations(id),
    PRIMARY KEY (event_id, org_id)
);

CREATE TABLE IF NOT EXISTS event_categories (
    event_id    INTEGER NOT NULL REFERENCES events(id),
    category_id INTEGER NOT NULL REFERENCES categories(id),
    PRIMARY KEY (event_id, category_id)
);

CREATE INDEX IF NOT EXISTS idx_events_start_time ON events(start_time);
"""

# Insert a new event, or if one with the same id already exists, update it in place.
# first_seen is left out of the UPDATE so it keeps the date the event was first fetched.
UPSERT_EVENT = """
INSERT INTO events (id, title, description_html, description_text, start_time, end_time,
                    location, status, contact_email, image_url, link, first_seen, last_seen)
VALUES (:id, :title, :description_html, :description_text, :start_time, :end_time,
        :location, :status, :contact_email, :image_url, :link, :now, :now)
ON CONFLICT(id) DO UPDATE SET
    title            = excluded.title,
    description_html = excluded.description_html,
    description_text = excluded.description_text,
    start_time       = excluded.start_time,
    end_time         = excluded.end_time,
    location         = excluded.location,
    status           = excluded.status,
    contact_email    = excluded.contact_email,
    image_url        = excluded.image_url,
    link             = excluded.link,
    last_seen        = excluded.last_seen
"""


class DescriptionText(HTMLParser):
    """Collects the plain text inside the feed's <div class="p-description"> block."""

    BLOCK_TAGS = {"p", "br", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}

    def __init__(self):
        super().__init__()
        self.depth = 0  # how many divs deep we are inside the description; 0 means outside it
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "div":
            if self.depth:
                self.depth += 1
            elif "p-description" in (dict(attrs).get("class") or "").split():
                self.depth = 1
        if self.depth and tag in self.BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if self.depth and tag in self.BLOCK_TAGS:
            self.parts.append(" ")
        if tag == "div" and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)


def clean(text):
    """Collapse runs of whitespace and trim; returns None for empty text."""
    return " ".join((text or "").split()) or None


def html_to_text(html):
    parser = DescriptionText()
    parser.feed(html)
    parser.close()
    return clean("".join(parser.parts))


def to_eastern(text):
    """Convert an RSS date like 'Tue, 03 Nov 2026 01:00:00 GMT' to Eastern ISO 8601."""
    if not text:
        return None
    return parsedate_to_datetime(text).astimezone(EASTERN).isoformat()


def parse_author(text):
    """Pull the email out of an author string like 'club@gmail.com (Club Name)'."""
    email = (text or "").split(" (")[0].strip()
    return email if "@" in email else None


def download_feed():
    request = urllib.request.Request(FEED_URL, headers={"User-Agent": "ClubTerrapin/0.1 (personal events viewer)"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def parse_events(xml_bytes):
    """Turn the feed XML into a list of dicts, one per event."""
    root = ET.fromstring(xml_bytes)
    events = []
    for item in root.iterfind("channel/item"):
        guid = item.findtext("guid")
        if not guid:
            continue
        description = item.findtext("description") or ""
        enclosure = item.find("enclosure")
        events.append({
            "id": int(guid.rstrip("/").rsplit("/", 1)[-1]),
            "title": clean(item.findtext("title")) or "(untitled)",
            "description_html": description,
            "description_text": html_to_text(description),
            "start_time": to_eastern(item.findtext(NS + "start")),
            "end_time": to_eastern(item.findtext(NS + "end")),
            "location": clean(item.findtext(NS + "location")),
            "status": clean(item.findtext(NS + "status")),
            "contact_email": parse_author(item.findtext("author")),
            "image_url": enclosure.get("url") if enclosure is not None else None,
            "link": clean(item.findtext("link")),
            "hosts": [name for name in map(clean, (h.text for h in item.findall(NS + "host"))) if name],
            "categories": [name for name in map(clean, (c.text for c in item.findall("category"))) if name],
        })
    return events


def init_db(conn):
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)


def get_or_create_id(conn, table, name):
    """Return the id of the row named `name` in organizations/categories, adding it if needed."""
    # `table` is always one of our own table names, never user input, so formatting it in is safe.
    conn.execute(f"INSERT OR IGNORE INTO {table} (name) VALUES (?)", (name,))
    return conn.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()[0]


def save_events(conn, events, now):
    # Everything happens in one transaction: if anything fails, nothing is saved.
    with conn:
        for event in events:
            conn.execute(UPSERT_EVENT, {**event, "now": now})

            # Replace this event's hosts and categories so removed ones don't linger.
            conn.execute("DELETE FROM event_hosts WHERE event_id = ?", (event["id"],))
            for name in event["hosts"]:
                conn.execute(
                    "INSERT OR IGNORE INTO event_hosts (event_id, org_id) VALUES (?, ?)",
                    (event["id"], get_or_create_id(conn, "organizations", name)),
                )

            conn.execute("DELETE FROM event_categories WHERE event_id = ?", (event["id"],))
            for name in event["categories"]:
                conn.execute(
                    "INSERT OR IGNORE INTO event_categories (event_id, category_id) VALUES (?, ?)",
                    (event["id"], get_or_create_id(conn, "categories", name)),
                )


def count_events(conn):
    return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def main():
    try:
        events = parse_events(download_feed())
    except (OSError, ET.ParseError, ValueError) as error:
        print(f"Could not fetch events: {error}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        before = count_events(conn)
        save_events(conn, events, datetime.now(EASTERN).isoformat(timespec="seconds"))
        after = count_events(conn)
    finally:
        conn.close()

    new = after - before
    print(f"Fetched {len(events)} events: {new} new, {len(events) - new} updated. "
          f"Database now holds {after} events.")


if __name__ == "__main__":
    main()
