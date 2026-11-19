"""
The back office: who may log in, what they did, and what the system sends out.

Until these existed the application had no idea who was using it - the only
gate was a single shared password on the reverse proxy - so nothing could be
attributed to anybody and nothing could be restricted to anybody. These
tables are what the login (app/core/auth.py), the audit trail
(app/core/audit.py) and the notifications (app/core/notify.py) stand on.
"""
from sqlalchemy import (
    Boolean, Column, ForeignKey, Index, Integer, JSON, String, Text
)
from sqlalchemy.sql import func

from app.db.database import Base
from app.db.types import UTCDateTime


class User(Base):
    """
    An account that can log in.

    Two kinds, told apart by ``source``:

      * ``local`` - the password hash lives here. The account the installer
        creates is one of these, and so is the emergency account an operator
        keeps for the day the directory is unreachable.
      * ``ldap`` - authenticated against the directory on every login; no
        password is ever stored. The row is created on the first successful
        login, with role ``guest`` until an administrator grants one, so
        being in the directory is not by itself access to the network map.
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    # Lower case, compared lower case: AD does not care about case, so
    # neither may this, or "Alice" and "alice" become two accounts.
    username = Column(String(128), unique=True, nullable=False, index=True)
    display_name = Column(String(200))
    email = Column(String(320))
    source = Column(String(16), nullable=False, default="local")
    # admin / operator / viewer / guest - app/core/auth.py ROLES
    role = Column(String(16), nullable=False, default="viewer")
    password_hash = Column(String(512))
    is_active = Column(Boolean, nullable=False, default=True)
    # Forces a password change on the next login: set on accounts created
    # with a password somebody else chose (the installer, an administrator).
    must_change_password = Column(Boolean, nullable=False, default=False)
    created_at = Column(UTCDateTime, server_default=func.now())
    last_login_at = Column(UTCDateTime)
    password_changed_at = Column(UTCDateTime)
    # Lockout: failures since the last success, and until when logins are
    # refused. Per account rather than per address because the thing being
    # protected is the account, and behind a NAT every guess comes from one
    # address anyway.
    failed_logins = Column(Integer, nullable=False, default=0)
    locked_until = Column(UTCDateTime)


class UserSession(Base):
    """
    A logged-in browser.

    Only a hash of the token is stored: the cookie is the credential, and a
    database backup (which an administrator can download) must not be a list
    of live sessions anybody can paste into a browser.
    """
    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    created_at = Column(UTCDateTime, server_default=func.now())
    last_seen_at = Column(UTCDateTime)
    expires_at = Column(UTCDateTime, nullable=False)
    ip = Column(String(64))
    user_agent = Column(String(300))


class AuditLog(Base):
    """
    One thing somebody did.

    Written for every request that changes anything (app/core/audit.py), plus
    logins, failed logins and logouts. Never updated and never deleted except
    by the retention purge, which writes a row of its own saying it ran.
    """
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
    at = Column(UTCDateTime, server_default=func.now(), nullable=False, index=True)
    # Kept as text as well as by id: the account may be deleted later, and
    # "user #7" is no answer to "who turned the alerts off".
    user_id = Column(Integer)
    username = Column(String(128), index=True)
    # A stable key such as "device.update" (app/core/audit.py ACTIONS) - the
    # UI translates it, the CSV keeps it, so a filter means the same in every
    # language.
    action = Column(String(64), nullable=False, index=True)
    target = Column(String(300))
    method = Column(String(8))
    path = Column(String(300))
    status = Column(Integer)
    ip = Column(String(64))
    # The request body with every secret masked, or what the action needs to
    # be understood (a failed login's reason).
    detail = Column(JSON)

    __table_args__ = (Index("ix_audit_log_action_at", "action", "at"),)


class SystemSetting(Base):
    """
    A section of back-office settings, as one JSON document.

    Not settings.json (app/core/app_settings.py): those are read by the
    collector on every cycle and must survive a slow database. These are
    read by the application on demand - the directory to log in against, the
    notification channels, the backup schedule - and an administrator
    changes them from the web, so the database is where they belong and a
    backup carries them.
    """
    __tablename__ = "system_settings"

    key = Column(String(64), primary_key=True)
    value = Column(JSON, nullable=False)
    updated_at = Column(UTCDateTime, server_default=func.now(), onupdate=func.now())
    updated_by = Column(String(128))


class NotificationEvent(Base):
    """
    Something worth telling a human about, waiting to be sent.

    The alert engine runs in the collector; the senders run in the
    application. This table is the hand-off between the two processes: the
    collector inserts a row in the same transaction that creates or resolves
    the alert, so an alert can never exist without its notification having
    been queued, and the application sends it when it gets to it - a Teams
    endpoint that takes ten seconds to answer must not stall a poll cycle.
    """
    __tablename__ = "notification_events"

    id = Column(Integer, primary_key=True)
    created_at = Column(UTCDateTime, server_default=func.now(), nullable=False, index=True)
    # alert_new / alert_resolved / health_failed / job_finished / test
    kind = Column(String(32), nullable=False)
    severity = Column(String(16))
    alert_id = Column(Integer)
    payload = Column(JSON)
    # pending -> processed (fanned out to deliveries) or skipped
    status = Column(String(16), nullable=False, default="pending", index=True)
    processed_at = Column(UTCDateTime)


class NotificationDelivery(Base):
    """One attempt series of one message to one channel."""
    __tablename__ = "notification_deliveries"

    id = Column(Integer, primary_key=True)
    created_at = Column(UTCDateTime, server_default=func.now(), nullable=False, index=True)
    channel_id = Column(String(32), nullable=False)
    channel_name = Column(String(100))
    channel_type = Column(String(16))
    # The events this message carries: one, or a digest of many
    event_ids = Column(JSON)
    title = Column(Text)
    body = Column(Text)
    # pending / sent / failed (gave up after the last retry)
    status = Column(String(16), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(UTCDateTime)
    last_error = Column(Text)
    sent_at = Column(UTCDateTime)
