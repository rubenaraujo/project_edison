"""Local voice database: people -> one or more voiceprints (e.g. one per microphone)."""
import os
import sqlite3
import time

import numpy as np

from common import HERE

DB = os.environ.get("VOICES_DB") or os.path.join(HERE, "data", "voices.db")
AUTO = 0.55      # cosine similarity to auto-assign a name
SUGGEST = 0.35   # minimum similarity to show as a suggestion
MARGIN = 0.05    # best person must beat the runner-up by this much to auto-assign


def conn():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS person (
            id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE NOT NULL);
        CREATE TABLE IF NOT EXISTS voiceprint (
            id INTEGER PRIMARY KEY,
            person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
            embedding BLOB NOT NULL, device TEXT, source TEXT, created_at TEXT);
        """
    )
    return c


def norm(v):
    v = np.asarray(v, dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def people():
    with conn() as c:
        return [
            {"id": i, "name": n, "prints": k}
            for i, n, k in c.execute(
                "SELECT p.id, p.name, COUNT(v.id) FROM person p "
                "LEFT JOIN voiceprint v ON v.person_id = p.id GROUP BY p.id ORDER BY p.name"
            )
        ]


def person_id(name):
    name = name.strip()
    with conn() as c:
        row = c.execute("SELECT id FROM person WHERE name = ?", (name,)).fetchone()
        if row:
            return row[0]
        return c.execute("INSERT INTO person(name) VALUES (?)", (name,)).lastrowid


def person_name(pid):
    with conn() as c:
        row = c.execute("SELECT name FROM person WHERE id = ?", (pid,)).fetchone()
    return row[0] if row else None


def prints(pid):
    with conn() as c:
        return [
            {"id": i, "device": d or "", "source": s or "", "created_at": t or ""}
            for i, d, s, t in c.execute(
                "SELECT id, device, source, created_at FROM voiceprint WHERE person_id = ? ORDER BY id", (pid,))
        ]


def rename_person(pid, name):
    with conn() as c:
        c.execute("UPDATE person SET name = ? WHERE id = ?", (name.strip(), pid))


def delete_person(pid):
    with conn() as c:
        c.execute("DELETE FROM person WHERE id = ?", (pid,))


def merge_people(src, dst):
    with conn() as c:
        c.execute("UPDATE voiceprint SET person_id = ? WHERE person_id = ?", (dst, src))
        c.execute("DELETE FROM person WHERE id = ?", (src,))


def delete_print(vid):
    with conn() as c:
        c.execute("DELETE FROM voiceprint WHERE id = ?", (vid,))


def update_print(vid, device):
    with conn() as c:
        c.execute("UPDATE voiceprint SET device = ? WHERE id = ?", (device.strip(), vid))


def add_print(pid, emb, device="", source=""):
    with conn() as c:
        c.execute(
            "INSERT INTO voiceprint(person_id, embedding, device, source, created_at) VALUES (?,?,?,?,?)",
            (pid, norm(emb).tobytes(), device.strip(), source, time.strftime("%Y-%m-%d %H:%M:%S")),
        )


def ranking(emb):
    """Per person: best cosine similarity across all of that person's voiceprints."""
    e = norm(emb)
    best = {}
    with conn() as c:
        for pid, name, blob in c.execute(
            "SELECT p.id, p.name, v.embedding FROM voiceprint v JOIN person p ON p.id = v.person_id"
        ):
            s = float(np.dot(e, np.frombuffer(blob, dtype=np.float32)))
            if s > best.get(pid, (None, -1))[1]:
                best[pid] = (name, s)
    return sorted(
        ({"person_id": pid, "name": n, "score": round(s, 3)} for pid, (n, s) in best.items()),
        key=lambda r: -r["score"],
    )


def classify(emb):
    """-> (status, person_id, name, score, suggestions)"""
    r = ranking(emb)
    if not r:
        return "unknown", None, None, 0.0, []
    top = r[0]
    second = r[1]["score"] if len(r) > 1 else -1
    if top["score"] >= AUTO and top["score"] - second >= MARGIN:
        return "matched", top["person_id"], top["name"], top["score"], r[:3]
    sugg = [x for x in r if x["score"] >= SUGGEST][:3]
    return ("suggested" if sugg else "unknown"), None, None, top["score"], sugg
