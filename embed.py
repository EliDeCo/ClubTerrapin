"""Give each event group an embedding (a list of numbers describing its content) for recommendations.

Embeddings come from the BAAI/bge-small-en-v1.5 model. A group's text is the title and
plain-text description of its most recent event, plus every category any of its events
had. fetch.py calls update_embeddings() after every fetch. Run `python embed.py` on its
own to catch up without fetching.

Only new groups, or groups whose text changed, get re-embedded: we store a hash of the
exact text that was embedded and compare it on every run.
"""

import hashlib
import re
import sqlite3
from collections import defaultdict
from datetime import datetime

import numpy as np

from fetch import DB_PATH, EASTERN, init_db
from grouping import update_groups

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMENSIONS = 384

UPSERT_EMBEDDING = """
INSERT INTO group_embeddings (group_id, model, text_hash, embedding, embedded_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(group_id) DO UPDATE SET
    model       = excluded.model,
    text_hash   = excluded.text_hash,
    embedding   = excluded.embedding,
    embedded_at = excluded.embedded_at
"""


def split_camel_case(name):
    """'ThoughtfulLearning' -> 'Thoughtful Learning', so the model sees real words."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)


def embedding_text(title, categories, description):
    """The exact text the model embeds for one group."""
    parts = [title]
    if categories:
        # Sorted so the same categories always produce the same text (and the same hash).
        parts.append("Categories: " + ", ".join(sorted(split_camel_case(c) for c in categories)))
    parts.append(description)
    return "\n".join(part for part in parts if part)


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def update_embeddings(conn):
    """Embed every group that is new or changed since it was last embedded. Returns how many."""
    groups = conn.execute("""
        SELECT g.id, ge.model, ge.text_hash
        FROM event_groups g
        LEFT JOIN group_embeddings ge ON ge.group_id = g.id
    """).fetchall()

    # Events come back oldest first, so each group ends up with its most recent event's text.
    latest = {}
    for group_id, title, description in conn.execute(
        "SELECT group_id, title, description_text FROM events ORDER BY start_time"
    ):
        latest[group_id] = (title, description)

    categories = defaultdict(set)
    for group_id, name in conn.execute("""
        SELECT e.group_id, c.name FROM events e
        JOIN event_categories ec ON ec.event_id = e.id
        JOIN categories c ON c.id = ec.category_id
    """):
        categories[group_id].add(name)

    to_embed = []  # (group_id, text, hash)
    for group_id, stored_model, stored_hash in groups:
        title, description = latest[group_id]
        text = embedding_text(title, categories[group_id], description)
        current_hash = text_hash(text)
        if stored_model != MODEL_NAME or stored_hash != current_hash:
            to_embed.append((group_id, text, current_hash))

    if not to_embed:
        return 0

    # Imported here because loading torch takes a few seconds; skip it when there's nothing to do.
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(MODEL_NAME)
    vectors = model.encode(
        [text for _, text, _ in to_embed],
        batch_size=32,
        normalize_embeddings=True,  # unit length, so cosine similarity is just a dot product
        show_progress_bar=len(to_embed) > 50,
    )

    now = datetime.now(EASTERN).isoformat(timespec="seconds")
    with conn:
        conn.executemany(UPSERT_EMBEDDING, [
            (group_id, MODEL_NAME, hash_, vector.astype(np.float32).tobytes(), now)
            for (group_id, _, hash_), vector in zip(to_embed, vectors)
        ])
    return len(to_embed)


def load_embeddings(conn):
    """Return (group_ids, matrix) where row i of the matrix is the embedding of group_ids[i]."""
    rows = conn.execute(
        "SELECT group_id, embedding FROM group_embeddings WHERE model = ? ORDER BY group_id",
        (MODEL_NAME,),
    ).fetchall()
    if not rows:
        return [], np.empty((0, DIMENSIONS), dtype=np.float32)
    ids = [row[0] for row in rows]
    matrix = np.vstack([np.frombuffer(row[1], dtype=np.float32) for row in rows])
    return ids, matrix


def main():
    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        update_groups(conn)  # make sure every event is in its group before embedding groups
        count = update_embeddings(conn)
    finally:
        conn.close()
    print(f"Embedded {count} groups (new or changed).")


if __name__ == "__main__":
    main()
