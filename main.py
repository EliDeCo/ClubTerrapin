"""Example queries against the events database that fetch.py builds.

Run `python fetch.py` at least once first.
"""

import sqlite3
import sys
from datetime import datetime, timedelta

from embed import load_embeddings
from fetch import DB_PATH, EASTERN

# Columns every listing below shows. The subquery joins all of an event's hosts into one string.
EVENT_COLUMNS = """
    e.id, e.group_id, e.title, e.start_time, e.location, e.status,
    (SELECT GROUP_CONCAT(o.name, ', ')
       FROM event_hosts eh JOIN organizations o ON o.id = eh.org_id
      WHERE eh.event_id = e.id) AS hosts
"""

# Columns for listing groups. Every event in a group has the same hosts, so any one member's
# hosts work. next_start is NULL when the group has nothing upcoming. Needs a :now parameter.
GROUP_COLUMNS = """
    g.id, g.title, g.frequency, g.event_count,
    (SELECT MIN(e.start_time) FROM events e
      WHERE e.group_id = g.id AND e.start_time >= :now AND e.status != 'cancelled') AS next_start,
    (SELECT GROUP_CONCAT(o.name, ', ')
       FROM event_hosts eh JOIN organizations o ON o.id = eh.org_id
      WHERE eh.event_id = (SELECT MIN(e.id) FROM events e WHERE e.group_id = g.id)) AS hosts
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


def upcoming_groups(conn):
    """Every group with at least one upcoming, non-cancelled event."""
    rows = conn.execute(f"SELECT {GROUP_COLUMNS} FROM event_groups g", {"now": now()})
    return [row for row in rows if row["next_start"]]


def recurring_groups(conn):
    """Upcoming groups that repeat on a recognizable schedule, largest first."""
    groups = [row for row in upcoming_groups(conn) if row["frequency"] not in ("one-time", "other")]
    return sorted(groups, key=lambda row: row["event_count"], reverse=True)


def similar_groups(conn, group_id, limit=5):
    """Upcoming groups whose embeddings are closest to the given group's."""
    ids, matrix = load_embeddings(conn)
    if group_id not in ids:
        return []
    # Embeddings are unit length, so a dot product gives cosine similarity (1.0 = identical).
    scores = matrix @ matrix[ids.index(group_id)]
    ranked = [ids[i] for i in scores.argsort()[::-1] if ids[i] != group_id]

    upcoming = {row["id"]: row for row in upcoming_groups(conn)}
    return [upcoming[i] for i in ranked if i in upcoming][:limit]


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
            (SELECT COUNT(*) FROM organizations)                   AS clubs,
            (SELECT COUNT(*) FROM event_groups)                    AS groups
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


def print_groups(heading, rows, limit=8):
    print(f"\n== {heading} ({len(rows)} found) ==")
    for row in rows[:limit]:
        next_start = datetime.fromisoformat(row["next_start"])
        count = row["event_count"]
        print(f"{row['frequency']:<10}  {row['title']}  ({count} event{'' if count == 1 else 's'})")
        print(f"{'':12}{row['hosts'] or 'No host listed'}, next {next_start:%a %b %d %I:%M %p}")
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
          f"from {stats['clubs']} clubs, in {stats['groups']} groups.")

    print_events("Next 7 days", upcoming_events(conn))
    print_events("Upcoming Arts events", events_in_category(conn, "Arts"))
    print_events("Everything hosted by clubs matching 'Ballet'", events_by_club(conn, "Ballet"))
    japanese = search(conn, "Japanese")
    print_events("Upcoming events mentioning 'Japanese'", japanese)

    if japanese:
        similar = similar_groups(conn, japanese[0]["group_id"])
        if similar:
            print_groups(f"Groups similar to '{japanese[0]['title']}'", similar)
        else:
            print("\nNo embeddings yet. Run `python embed.py` to see similar groups.")

    print_groups("Biggest recurring groups", recurring_groups(conn))

    print("\n== Most common categories (upcoming events) ==")
    for row in category_counts(conn):
        print(f"{row['count']:5}  {row['name']}")

    conn.close()


if __name__ == "__main__":
    main()
