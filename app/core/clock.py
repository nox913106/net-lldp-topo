"""
The one clock the application reads.
"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    """
    Now, in UTC, and saying so.

    ``datetime.utcnow()`` returns the same instant with no zone attached,
    which every library downstream is then free to interpret - and asyncpg
    interprets it as local time (app/db/types.py has what that cost). An
    aware value cannot be misread, so this is what the application uses.
    """
    return datetime.now(timezone.utc)
