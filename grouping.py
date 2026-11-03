"""Group repeats of the same event (like a weekly club meeting) and label how often they happen.

A group is every event with the same title, start and end time of day, and host
organizations. Every event belongs to a group; a one-off event is a group of one.
fetch.py calls update_groups() after every fetch. Run `python grouping.py` on its
own to regroup without fetching.

Groups are recomputed from all stored events on every run, so new events join their
group automatically and edited events move to the right one. Group ids stay the same
across runs because groups are matched by their key.
"""

import sqlite3
from collections import defaultdict
from datetime import datetime
from statistics import median, median_low

from fetch import DB_PATH, init_db

# RETURNING id gives back the group's id whether it was just inserted or already existed.
UPSERT_GROUP = """
INSERT INTO event_groups (group_key, title, start_time_of_day, end_time_of_day, frequency,
                          event_count, first_start, last_start)
VALUES (:group_key, :title, :start_time_of_day, :end_time_of_day, :frequency,
        :event_count, :first_start, :last_start)
ON CONFLICT(group_key) DO UPDATE SET
    title             = excluded.title,
    start_time_of_day = excluded.start_time_of_day,
    end_time_of_day   = excluded.end_time_of_day,
    frequency         = excluded.frequency,
    event_count       = excluded.event_count,
    first_start       = excluded.first_start,
    last_start        = excluded.last_start
RETURNING id
"""


def time_of_day(iso_time):
    """'2026-11-03T20:00:00-05:00' -> '20:00'. Times are stored in Eastern, so DST doesn't shift this."""
    return datetime.fromisoformat(iso_time).strftime("%H:%M") if iso_time else None


def group_key(title, start_time, end_time, host_ids):
    hosts = ",".join(str(host_id) for host_id in sorted(host_ids))
    return f"{title.casefold()}|{time_of_day(start_time)}|{time_of_day(end_time) or ''}|{hosts}"


def classify_frequency(dates, event_count):
    """Label how often a group repeats, based on the dates its events happen on."""
    dates = sorted(set(dates))
    if len(dates) < 2:
        return "one-time" if event_count == 1 else "other"
    day_gaps = [(later - earlier).days for earlier, later in zip(dates, dates[1:])]

    if len({day.weekday() for day in dates}) >= 5 and median(day_gaps) <= 1:
        return "daily"

    # Compare each weekday only with itself, so a Mon/Wed practice still counts as weekly
    # and a skipped week (like Thanksgiving) doesn't break the pattern.
    by_weekday = defaultdict(list)
    for day in dates:
        by_weekday[day.weekday()].append(day)
    week_gaps = [(later - earlier).days // 7
                 for days in by_weekday.values() for earlier, later in zip(days, days[1:])]
    if week_gaps:
        weeks = median_low(week_gaps)
        if weeks == 1:
            return "weekly"
        if weeks == 2:
            return "bi-weekly"
        if weeks in (4, 5):
            return "monthly"

    days = median(day_gaps)
    if days <= 1:
        return "daily"
    if 27 <= days <= 35:
        return "monthly"  # e.g. the same date each month
    if 90 <= days <= 250:
        return "semesterly"
    if 330 <= days <= 400:
        return "yearly"
    return "other"


def update_groups(conn):
    """Assign every event to its group. Returns (number of groups, number with more than one event)."""
    hosts = defaultdict(list)
    for event_id, org_id in conn.execute("SELECT event_id, org_id FROM event_hosts"):
        hosts[event_id].append(org_id)

    groups = defaultdict(list)  # group key -> [(start datetime, event id, title, start_time, end_time)]
    for event_id, title, start_time, end_time in conn.execute(
        "SELECT id, title, start_time, end_time FROM events"
    ):
        key = group_key(title, start_time, end_time, hosts[event_id])
        groups[key].append((datetime.fromisoformat(start_time), event_id, title, start_time, end_time))

    with conn:
        for key, members in groups.items():
            members.sort()
            _, _, latest_title, latest_start, latest_end = members[-1]
            group_id = conn.execute(UPSERT_GROUP, {
                "group_key": key,
                "title": latest_title,
                "start_time_of_day": time_of_day(latest_start),
                "end_time_of_day": time_of_day(latest_end),
                "frequency": classify_frequency([start.date() for start, *_ in members], len(members)),
                "event_count": len(members),
                "first_start": members[0][3],
                "last_start": latest_start,
            }).fetchall()[0][0]  # fetchall finishes the statement, which SQLite needs before committing
            conn.executemany("UPDATE events SET group_id = ? WHERE id = ?",
                             [(group_id, event_id) for _, event_id, *_ in members])

        # Remove groups no event belongs to anymore (an event's title, time or hosts changed).
        no_members = "SELECT id FROM event_groups WHERE id NOT IN (SELECT group_id FROM events WHERE group_id IS NOT NULL)"
        conn.execute(f"DELETE FROM group_embeddings WHERE group_id IN ({no_members})")
        conn.execute(f"DELETE FROM event_groups WHERE id IN ({no_members})")

    return len(groups), sum(len(members) > 1 for members in groups.values())


def main():
    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        groups, recurring = update_groups(conn)
    finally:
        conn.close()
    print(f"Grouped events into {groups} groups ({recurring} recurring).")


if __name__ == "__main__":
    main()
