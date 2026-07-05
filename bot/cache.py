import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from .models import TrackInfo

CACHE_TTL = 24 * 60 * 60
# Неповні картки (без якоїсь із основних платформ) кешуємо ненадовго,
# щоб наступний запит мав шанс дозаповнити їх.
SHORT_TTL = 60 * 60
DB_PATH = Path(__file__).resolve().parent.parent / "cache.db"

_conn: sqlite3.Connection | None = None


def _connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH)
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS tracks ("
            "url TEXT PRIMARY KEY, fetched_at REAL NOT NULL, "
            f"ttl REAL NOT NULL DEFAULT {CACHE_TTL}, data TEXT)"
        )
        try:  # міграція зі старої схеми без колонки ttl
            _conn.execute(
                f"ALTER TABLE tracks ADD COLUMN ttl REAL NOT NULL DEFAULT {CACHE_TTL}"
            )
        except sqlite3.OperationalError:
            pass
        _conn.execute("DELETE FROM tracks WHERE fetched_at + ttl < ?", (time.time(),))
        _conn.commit()
    return _conn


def get(url: str) -> tuple[bool, TrackInfo | None]:
    """Повертає (hit, track): hit=False — у кеші нема або протухло."""
    conn = _connection()
    row = conn.execute(
        "SELECT fetched_at, ttl, data FROM tracks WHERE url = ?", (url,)
    ).fetchone()
    if row is None:
        return False, None
    fetched_at, ttl, data = row
    if time.time() - fetched_at > ttl:
        conn.execute("DELETE FROM tracks WHERE url = ?", (url,))
        conn.commit()
        return False, None
    if data is None:
        return True, None
    return True, TrackInfo(**json.loads(data))


def put(url: str, track: TrackInfo | None, ttl: float = CACHE_TTL) -> None:
    conn = _connection()
    data = json.dumps(asdict(track), ensure_ascii=False) if track is not None else None
    conn.execute(
        "INSERT OR REPLACE INTO tracks (url, fetched_at, ttl, data) VALUES (?, ?, ?, ?)",
        (url, time.time(), ttl, data),
    )
    conn.commit()
