from __future__ import annotations

"""SQLite persistence for mailzero.

Only domain-analysis data needed by current CLI workflow is stored:
- normalized email metadata
- resolved sender/routing domain fields
- coarse read/flag/junk status snapshots
"""

import sqlite3
from pathlib import Path
from typing import Iterable

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY,
    mail_local_id INTEGER,
    message_id TEXT,
    mailbox TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL DEFAULT '',
    sender_domain TEXT NOT NULL,
    sender_routing_domain TEXT NOT NULL DEFAULT 'unknown',
    sender_domain_source TEXT NOT NULL DEFAULT 'direct',
    subject TEXT,
    body_preview TEXT,
    date_received TEXT,
    is_read INTEGER NOT NULL DEFAULT 0,
    is_flagged INTEGER NOT NULL DEFAULT 0,
    is_junk INTEGER NOT NULL DEFAULT 0,
    source_hash TEXT,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(message_id, mailbox)
);

CREATE INDEX IF NOT EXISTS idx_emails_sender_domain ON emails(sender_domain);
CREATE INDEX IF NOT EXISTS idx_emails_mailbox ON emails(mailbox);
CREATE INDEX IF NOT EXISTS idx_emails_date_received ON emails(date_received);
"""


def default_db_path() -> Path:
    # Project-local DB keeps state self-contained and easy to inspect/backup.
    return Path(".mailzero") / "mailzero.db"


def connect(db_path: Path) -> sqlite3.Connection:
    # row_factory=Row gives dict-like column access while staying lightweight.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    # Idempotent schema bootstrap plus additive migrations.
    conn.executescript(SCHEMA_SQL)
    _ensure_schema_migrations(conn)
    conn.commit()


def _ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    # Simple additive migration strategy:
    # check for columns and add if missing. This avoids full migration tooling
    # for a small local-only SQLite project.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(emails)")}
    if "mail_local_id" not in columns:
        conn.execute("ALTER TABLE emails ADD COLUMN mail_local_id INTEGER")
    if "sender_routing_domain" not in columns:
        conn.execute(
            "ALTER TABLE emails ADD COLUMN sender_routing_domain TEXT NOT NULL DEFAULT 'unknown'"
        )
    if "sender_domain_source" not in columns:
        conn.execute(
            "ALTER TABLE emails ADD COLUMN sender_domain_source TEXT NOT NULL DEFAULT 'direct'"
        )
    if "body_preview" not in columns:
        conn.execute("ALTER TABLE emails ADD COLUMN body_preview TEXT")
    if "recipient" not in columns:
        conn.execute("ALTER TABLE emails ADD COLUMN recipient TEXT NOT NULL DEFAULT ''")
    # Create post-migration so existing DBs missing recipient do not fail during
    # executescript bootstrap.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_recipient ON emails(recipient)")


def reset_for_full_refresh(conn: sqlite3.Connection) -> None:
    # Keep dry-run deterministic: always rebuild from current Mail state.
    conn.execute("DELETE FROM emails")
    conn.commit()


def upsert_emails(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    # Batch materialization is a deliberate performance trade-off:
    # lower sqlite round-trips in exchange for temporary in-memory list.
    payload: list[tuple[object, ...]] = []
    for row in rows:
        payload.append(
            (
                row.get("mail_local_id"),
                row.get("message_id"),
                row["mailbox"],
                row["sender"],
                row.get("recipient", ""),
                row["sender_domain"],
                row.get("sender_routing_domain", "unknown"),
                row.get("sender_domain_source", "direct"),
                row.get("subject"),
                row.get("body_preview"),
                row.get("date_received"),
                int(bool(row.get("is_read", False))),
                int(bool(row.get("is_flagged", False))),
                int(bool(row.get("is_junk", False))),
                row.get("source_hash"),
            )
        )
    if not payload:
        return 0

    conn.executemany(
        """
        INSERT INTO emails (
            mail_local_id,
            message_id,
            mailbox,
            sender,
            recipient,
            sender_domain,
            sender_routing_domain,
            sender_domain_source,
            subject,
            body_preview,
            date_received,
            is_read,
            is_flagged,
            is_junk,
            source_hash,
            last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(message_id, mailbox) DO UPDATE SET
            mail_local_id=excluded.mail_local_id,
            sender=excluded.sender,
            recipient=excluded.recipient,
            sender_domain=excluded.sender_domain,
            sender_routing_domain=excluded.sender_routing_domain,
            sender_domain_source=excluded.sender_domain_source,
            subject=excluded.subject,
            body_preview=excluded.body_preview,
            date_received=excluded.date_received,
            is_read=excluded.is_read,
            is_flagged=excluded.is_flagged,
            is_junk=excluded.is_junk,
            source_hash=excluded.source_hash,
            last_seen_at=CURRENT_TIMESTAMP
        """,
        payload,
    )
    conn.commit()
    return len(payload)


def domain_stats_by_mailbox(
    conn: sqlite3.Connection,
    limit_per_mailbox: int = 90,
    filter_where_sql: str | None = None,
    filter_params: tuple[object, ...] = (),
) -> list[sqlite3.Row]:
    """Top domains per mailbox, with mailbox groups ordered by total volume."""
    filter_clause = f"AND ({filter_where_sql})" if filter_where_sql else ""
    query = f"""
    WITH grouped AS (
        SELECT
            mailbox,
            sender_domain,
            COUNT(*) AS total,
            SUM(CASE WHEN is_read = 0 THEN 1 ELSE 0 END) AS unread,
            SUM(CASE WHEN is_flagged = 1 THEN 1 ELSE 0 END) AS flagged
        FROM emails
        WHERE 1=1
          {filter_clause}
        GROUP BY mailbox, sender_domain
    ),
    ranked AS (
        SELECT
            mailbox,
            sender_domain,
            total,
            unread,
            flagged,
            ROW_NUMBER() OVER (
                PARTITION BY mailbox
                ORDER BY total DESC, sender_domain ASC
            ) AS row_rank
        FROM grouped
    ),
    mailbox_totals AS (
        SELECT mailbox, SUM(total) AS mailbox_total
        FROM grouped
        GROUP BY mailbox
    )
    SELECT r.mailbox, r.sender_domain, r.total, r.unread, r.flagged
    FROM ranked r
    JOIN mailbox_totals m ON m.mailbox = r.mailbox
    WHERE row_rank <= ?
    ORDER BY m.mailbox_total DESC, r.mailbox ASC, r.total DESC, r.sender_domain ASC
    """
    return list(conn.execute(query, (*filter_params, limit_per_mailbox)))


def sender_domain_source_stats(
    conn: sqlite3.Connection,
    filter_where_sql: str | None = None,
    filter_params: tuple[object, ...] = (),
) -> list[sqlite3.Row]:
    # Used in reports to show how many rows were direct vs relay-decoded.
    filter_clause = f"AND ({filter_where_sql})" if filter_where_sql else ""
    query = f"""
    SELECT sender_domain_source, COUNT(*) AS total
    FROM emails
    WHERE 1=1
      {filter_clause}
    GROUP BY sender_domain_source
    ORDER BY total DESC, sender_domain_source ASC
    """
    return list(conn.execute(query, filter_params))


def latest_preview_rows_for_domains(
    conn: sqlite3.Connection,
    domains: list[str],
    per_pair: int = 3,
    filter_where_sql: str | None = None,
    filter_params: tuple[object, ...] = (),
) -> list[sqlite3.Row]:
    """Latest message previews per (mailbox, sender_domain) pair.

    Each row contains subject + stored body preview for one recent message.
    """
    if not domains:
        return []
    placeholders = ",".join("?" for _ in domains)
    query = f"""
    WITH ranked AS (
        SELECT
            mailbox,
            sender_domain,
            COALESCE(subject, '') AS subject,
            COALESCE(body_preview, '') AS body_preview,
            COALESCE(date_received, '') AS date_received,
            ROW_NUMBER() OVER (
                PARTITION BY mailbox, sender_domain
                ORDER BY
                    (date_received IS NULL) ASC,
                    date_received DESC,
                    id DESC
            ) AS row_rank
        FROM emails
        WHERE sender_domain IN ({placeholders})
          {"AND (" + filter_where_sql + ")" if filter_where_sql else ""}
          AND LOWER(mailbox) LIKE '%inbox%'
    )
    SELECT mailbox, sender_domain, subject, body_preview, date_received, row_rank
    FROM ranked
    WHERE row_rank <= ?
    ORDER BY mailbox ASC, sender_domain ASC, row_rank ASC
    """
    return list(conn.execute(query, (*domains, *filter_params, int(per_pair))))
