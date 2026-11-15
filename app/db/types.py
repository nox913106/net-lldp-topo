"""
Column types shared by the models.
"""
from datetime import timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """
    A timestamp that is UTC whatever the process's TZ is.

    Every time this project writes is "now, in UTC", and for a long while it
    was written as a naive ``datetime.utcnow()``. On SQLite that is harmless.
    On PostgreSQL the column is ``timestamptz``, and asyncpg turns a naive
    datetime into one by assuming it is in the *process's local zone* - so in
    the shipped containers, which set ``TZ=Asia/Taipei``, every timestamp
    landed eight hours in the past. Nothing raised. What it broke was
    quieter: the gap between two counter samples came out as eight hours,
    which is past the limit a rate is derived over (app/core/traffic.py), so
    no link ever had a traffic rate, utilisation alerts could not fire, and
    every "last seen" in the UI read eight hours ago. CI ran in UTC, where
    local time *is* UTC, and saw none of it.

    So the zone is pinned here, at the one place every timestamp passes:

      * going in, a naive value is taken to be UTC (which is what every
        caller means) and an aware one is converted to it
      * coming out, the value is always aware and always UTC - PostgreSQL
        already returns it that way, SQLite returns it naive, and code that
        compared the two used to work on one database and raise on the other
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    @staticmethod
    def _utc(value):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_bind_param(self, value, dialect):
        return self._utc(value)

    def process_result_value(self, value, dialect):
        return self._utc(value)
