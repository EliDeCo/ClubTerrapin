"""Example queries against the events database that fetch.py builds.

Run `python fetch.py` at least once first.
"""

import sqlite3
import sys
from datetime import datetime, timedelta

from fetch import DB_PATH, EASTERN

# Columns every listing below shows. The subquery joins all of an event's hosts into one string.
EVENT_COLUMNS = """
    e.id, e.title, e.start_time, e.location, e.status,
    (SELECT GROUP_CONCAT(o.name, ', ')
       FROM event_hosts eh JOIN organizations o ON o.id = eh.org_id
      WHERE eh.event_id = e.id) AS hosts
"""


def now():
    # Times are stored as Eastern ISO strings, so comparing against one of those works directly.
    return datetime.now(EASTERN).isoformat(timespec="seconds")


def upcoming_events(conn, days=7):
    end = (datetime.now(EASTERN) + timedelta(days=days)).isoformat(timespec="seconds")
    return conn.execute(f"""
        SELECT {EVENT_COLUMNS} FROM events e
        WHERE e.start_time BETWEEN ? AND ? AND e.status != 'cancelled'
        ORDER BY e.start_time
    """, (now(), end)).fetchall()


def events_in_category(conn, category):
    return conn.execute(f"""
        SELECT {EVENT_COLUMNS} FROM events e
        JOIN event_categories ec ON ec.event_id = e.id
        JOIN categories c ON c.id = ec.category_id
        WHERE c.name = ? AND e.start_time >= ? AND e.status != 'cancelled'
        ORDER BY e.start_time
    """, (category, now())).fetchall()


def events_by_club(conn, name_fragment):
    # Includes past events, since fetch.py keeps them after they leave the feed.
    return conn.execute(f"""
        SELECT {EVENT_COLUMNS} FROM events e
        WHERE e.id IN (SELECT eh.event_id FROM event_hosts eh
                       JOIN organizations o ON o.id = eh.org_id
                       WHERE o.name LIKE ?)
        ORDER BY e.start_time
    """, (f"%{name_fragment}%",)).fetchall()


def search(conn, keyword):
    pattern = f"%{keyword}%"
    return conn.execute(f"""
        SELECT {EVENT_COLUMNS} FROM events e
        WHERE (e.title LIKE ? OR e.description_text LIKE ?)
          AND e.start_time >= ? AND e.status != 'cancelled'
        ORDER BY e.start_time
    """, (pattern, pattern, now())).fetchall()


def category_counts(conn, limit=15):
    return conn.execute("""
        SELECT c.name, COUNT(*) AS count FROM categories c
        JOIN event_categories ec ON ec.category_id = c.id
        JOIN events e ON e.id = ec.event_id
        WHERE e.start_time >= ?
        GROUP BY c.id
        ORDER BY count DESC
        LIMIT ?
    """, (now(), limit)).fetchall()


def db_stats(conn):
    return conn.execute("""
        SELECT
            (SELECT COUNT(*) FROM events)                          AS total,
            (SELECT COUNT(*) FROM events WHERE start_time < ?)     AS past,
            (SELECT COUNT(*) FROM events WHERE status = 'cancelled') AS cancelled,
            (SELECT COUNT(*) FROM organizations)                   AS clubs
    """, (now(),)).fetchone()


def print_events(heading, rows, limit=8):
    print(f"\n== {heading} ({len(rows)} found) ==")
    for row in rows[:limit]:
        start = datetime.fromisoformat(row["start_time"])
        cancelled = "  [CANCELLED]" if row["status"] == "cancelled" else ""
        print(f"{start:%a %b %d  %I:%M %p}  {row['title']}{cancelled}")
        print(f"{'':22}{row['hosts'] or 'No host listed'} @ {row['location']}")
    if len(rows) > limit:
        print(f"  ...and {len(rows) - limit} more")


def main():
    if not DB_PATH.exists():
        print("No database yet. Run `python fetch.py` first.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # lets us read columns by name, e.g. row["title"]

    stats = db_stats(conn)
    print(f"{stats['total']} events stored ({stats['past']} past, {stats['cancelled']} cancelled) "
          f"from {stats['clubs']} clubs.")

    print_events("Next 7 days", upcoming_events(conn))
    print_events("Upcoming Arts events", events_in_category(conn, "Arts"))
    print_events("Everything hosted by clubs matching 'Ballet'", events_by_club(conn, "Ballet"))
    print_events("Upcoming events mentioning 'Japanese'", search(conn, "Japanese"))

    print("\n== Most common categories (upcoming events) ==")
    for row in category_counts(conn):
        print(f"{row['count']:5}  {row['name']}")

    conn.close()


if __name__ == "__main__":
    main()
