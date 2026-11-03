"""Export the events database to CSV files you can open in Excel to look through.

This is just for inspecting the data, not part of the app. Run `python fetch.py`
first, then `python export.py`. Files are written to the export/ folder and
overwritten on every run.
"""

import csv
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from fetch import DB_PATH, EASTERN

EXPORT_DIR = Path(__file__).parent / "export"

# One row per event, with its hosts and categories joined into single cells.
EVENTS_QUERY = """
    SELECT e.id, e.title, e.start_time, e.end_time, e.location, e.status,
           (SELECT GROUP_CONCAT(o.name, '; ')
              FROM event_hosts eh JOIN organizations o ON o.id = eh.org_id
             WHERE eh.event_id = e.id) AS hosts,
           (SELECT GROUP_CONCAT(c.name, '; ')
              FROM event_categories ec JOIN categories c ON c.id = ec.category_id
             WHERE ec.event_id = e.id) AS categories,
           e.contact_email, e.link, e.image_url, e.first_seen, e.last_seen, e.description_text
    FROM events e
    ORDER BY e.start_time
"""

# One row per club, with how active it is and which categories its events use.
CLUBS_QUERY = """
    SELECT o.name AS club,
           COUNT(e.id) AS total_events,
           SUM(e.start_time >= :now) AS upcoming_events,
           MIN(CASE WHEN e.start_time >= :now THEN e.start_time END) AS next_event,
           MAX(e.start_time) AS latest_event,
           (SELECT GROUP_CONCAT(name, '; ') FROM (
               SELECT DISTINCT c.name FROM event_hosts eh2
               JOIN event_categories ec ON ec.event_id = eh2.event_id
               JOIN categories c ON c.id = ec.category_id
               WHERE eh2.org_id = o.id ORDER BY c.name)) AS categories
    FROM organizations o
    JOIN event_hosts eh ON eh.org_id = o.id
    JOIN events e ON e.id = eh.event_id
    GROUP BY o.id
    ORDER BY total_events DESC, club
"""

CATEGORIES_QUERY = """
    SELECT c.name AS category,
           COUNT(e.id) AS total_events,
           SUM(e.start_time >= :now) AS upcoming_events
    FROM categories c
    JOIN event_categories ec ON ec.category_id = c.id
    JOIN events e ON e.id = ec.event_id
    GROUP BY c.id
    ORDER BY total_events DESC
"""

SUMMARY_QUERY = """
    SELECT 'Events stored'      AS metric, COUNT(*) AS value FROM events
    UNION ALL SELECT 'Upcoming events',  COUNT(*) FROM events WHERE start_time >= :now
    UNION ALL SELECT 'Past events',      COUNT(*) FROM events WHERE start_time < :now
    UNION ALL SELECT 'Cancelled events', COUNT(*) FROM events WHERE status = 'cancelled'
    UNION ALL SELECT 'Events with no host', COUNT(*) FROM events
              WHERE id NOT IN (SELECT event_id FROM event_hosts)
    UNION ALL SELECT 'Clubs',            COUNT(*) FROM organizations
    UNION ALL SELECT 'Categories',       COUNT(*) FROM categories
    UNION ALL SELECT 'Earliest event',   MIN(start_time) FROM events
    UNION ALL SELECT 'Latest event',     MAX(start_time) FROM events
    UNION ALL SELECT 'Last fetched',     MAX(last_seen) FROM events
"""

# Columns whose timestamps get reformatted for Excel. "value" is the summary's column,
# which mixes counts and dates; excel_time leaves the counts alone.
TIME_COLUMNS = {"start_time", "end_time", "first_seen", "last_seen", "next_event", "latest_event", "value"}


def excel_time(value):
    """'2026-11-03T20:00:00-05:00' -> '2026-11-03 20:00', which Excel recognizes as a date."""
    if not isinstance(value, str):
        return value
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def write_csv(conn, filename, query, params):
    cursor = conn.execute(query, params)
    columns = [description[0] for description in cursor.description]
    rows = cursor.fetchall()

    # utf-8-sig adds a marker that tells Excel the file is UTF-8, so accents and emoji show correctly.
    with open(EXPORT_DIR / filename, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(columns)
        for row in rows:
            writer.writerow(
                excel_time(value) if column in TIME_COLUMNS else value
                for column, value in zip(columns, row)
            )
    print(f"  {filename:<16} {len(rows):>5} rows")


def main():
    if not DB_PATH.exists():
        print("No database yet. Run `python fetch.py` first.")
        sys.exit(1)

    EXPORT_DIR.mkdir(exist_ok=True)
    params = {"now": datetime.now(EASTERN).isoformat(timespec="seconds")}
    conn = sqlite3.connect(DB_PATH)

    print(f"Writing CSV files to {EXPORT_DIR}")
    try:
        write_csv(conn, "summary.csv", SUMMARY_QUERY, params)
        write_csv(conn, "events.csv", EVENTS_QUERY, params)
        write_csv(conn, "clubs.csv", CLUBS_QUERY, params)
        write_csv(conn, "categories.csv", CATEGORIES_QUERY, params)
    except PermissionError as error:
        print(f"Could not write {error.filename}. Is it open in Excel? Close it and run again.")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
