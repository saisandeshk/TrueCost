"""Read-only adapter for the query interface used by TrueCost's reports."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from truecost.ledger import Ledger


class ReadOnlyLedger(Ledger):
    """Reuse Ledger queries without its schema creation or WAL initialization."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self._connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA query_only=ON")

    @property
    def conn(self) -> sqlite3.Connection:
        return self._connection

    def close(self):
        self._connection.close()


@contextmanager
def read_ledger(path: str | Path):
    ledger = ReadOnlyLedger(path)
    try:
        yield ledger
    finally:
        ledger.close()
