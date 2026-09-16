"""A nested SQLite operation without taking ownership of caller transactions."""

import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from uuid import uuid4


@contextmanager
def sqlite_savepoint(connection: sqlite3.Connection) -> Iterator[None]:
    name = f"trade_{uuid4().hex}"
    connection.execute(f"SAVEPOINT {name}")
    try:
        yield
        connection.execute(f"RELEASE SAVEPOINT {name}")
    except BaseException:
        connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
        connection.execute(f"RELEASE SAVEPOINT {name}")
        raise
