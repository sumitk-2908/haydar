import builtins
import contextlib
import logging
import sqlite3
from typing import Any

from haydar.config import DB_DIR

logger = logging.getLogger(__name__)

class FileCache:
    """
    SQLite-backed cache for mtime, size, and file_hash.
    Prevents reading and re-hashing files that haven't changed on disk.
    """
    def __init__(self):
        self.db_path = DB_DIR / "haydar_cache.db"
        self._conn = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            # Check if DB directory exists
            DB_DIR.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_db(self):
        try:
            conn = self._get_conn()
            conn.execute('''
                CREATE TABLE IF NOT EXISTS file_cache (
                    filepath TEXT PRIMARY KEY,
                    mtime REAL,
                    size INTEGER,
                    file_hash TEXT,
                    chunk_count INTEGER
                )
            ''')
            conn.commit()
        except sqlite3.DatabaseError as e:
            logger.warning(f"Cache database is corrupted or inaccessible: {e}. Rebuilding...")
            if self._conn:
                self._conn.close()
                self._conn = None
            if self.db_path.exists():
                with contextlib.suppress(OSError):
                    self.db_path.unlink()
            # Try again
            conn = self._get_conn()
            conn.execute('''
                CREATE TABLE IF NOT EXISTS file_cache (
                    filepath TEXT PRIMARY KEY,
                    mtime REAL,
                    size INTEGER,
                    file_hash TEXT,
                    chunk_count INTEGER
                )
            ''')
            conn.commit()

    def get(self, filepath: str) -> dict[str, Any] | None:
        """Get cached info for a file."""
        try:
            cur = self._get_conn().cursor()
            cur.execute("SELECT mtime, size, file_hash, chunk_count FROM file_cache WHERE filepath = ?", (filepath,))
            row = cur.fetchone()
            if row:
                return dict(row)
        except sqlite3.Error as e:
            logger.debug(f"Cache read error for {filepath}: {e}")
        return None

    def set(self, filepath: str, mtime: float, size: int, file_hash: str, chunk_count: int):
        """Update cache for a file."""
        try:
            conn = self._get_conn()
            conn.execute('''
                INSERT OR REPLACE INTO file_cache (filepath, mtime, size, file_hash, chunk_count)
                VALUES (?, ?, ?, ?, ?)
            ''', (filepath, mtime, size, file_hash, chunk_count))
            conn.commit()
        except sqlite3.Error as e:
            logger.debug(f"Cache write error for {filepath}: {e}")

    def remove(self, filepath: str):
        """Remove a file from the cache."""
        try:
            conn = self._get_conn()
            conn.execute("DELETE FROM file_cache WHERE filepath = ?", (filepath,))
            conn.commit()
        except sqlite3.Error as e:
            logger.debug(f"Cache delete error for {filepath}: {e}")

    def remove_many(self, filepaths: list[str]):
        """Remove multiple files from the cache."""
        if not filepaths:
            return
        try:
            conn = self._get_conn()
            conn.executemany("DELETE FROM file_cache WHERE filepath = ?", [(f,) for f in filepaths])
            conn.commit()
        except sqlite3.Error as e:
            logger.debug(f"Cache bulk delete error: {e}")

    def get_all_filepaths(self) -> builtins.set[str]:
        """Return all filepaths in the cache."""
        try:
            cur = self._get_conn().cursor()
            cur.execute("SELECT filepath FROM file_cache")
            return {row[0] for row in cur.fetchall()}
        except sqlite3.Error as e:
            logger.debug(f"Cache list error: {e}")
            return set()

    def clear(self):
        """Clear the entire cache."""
        try:
            conn = self._get_conn()
            conn.execute("DELETE FROM file_cache")
            conn.commit()
        except sqlite3.Error:
            pass

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
