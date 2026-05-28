from __future__ import annotations

"""Read adapter for Apple Mail's Envelope Index SQLite database.

Apple's schema varies across macOS versions, so this module dynamically
discovers available columns/tables and composes a compatible query.
"""

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

APPLE_EPOCH_OFFSET = 978307200


@dataclass
class EnvelopeSource:
    # Absolute path to Envelope Index file.
    path: Path
    # Version directory name (e.g., V10) when discoverable.
    version_dir: str


def discover_envelope_index(explicit_path: Path | None = None) -> EnvelopeSource:
    """Locate latest Envelope Index unless an explicit path is provided."""
    if explicit_path is not None:
        version_dir = "custom"
        if (
            explicit_path.parent.name == "MailData"
            and explicit_path.parent.parent.name.startswith("V")
        ):
            version_dir = explicit_path.parent.parent.name
        return EnvelopeSource(path=explicit_path, version_dir=version_dir)

    mail_root = Path.home() / "Library" / "Mail"
    # Reverse sort generally picks highest V* directory first.
    candidates = sorted(mail_root.glob("V*/MailData/Envelope Index"), reverse=True)
    if not candidates:
        raise FileNotFoundError(
            f"No Envelope Index found under {mail_root}. "
            "Expected pattern: ~/Library/Mail/V*/MailData/Envelope Index"
        )
    picked = candidates[0]
    return EnvelopeSource(path=picked, version_dir=picked.parent.parent.name)


def connect_envelope_readonly(path: Path) -> sqlite3.Connection:
    # URI mode=ro avoids accidental writes to Mail's internal database.
    uri = f"file:{quote(str(path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def list_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    # Helper used heavily while generating version-tolerant SQL.
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(r["name"]) for r in rows}


def to_iso_timestamp(value: object) -> str | None:
    """Normalize mixed timestamp types to ISO-8601 strings."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        # Mail stores many timestamps as Cocoa seconds since 2001-01-01.
        # If the number looks too small for unix seconds, treat it as Cocoa.
        if ts < 1_000_000_000:
            ts += APPLE_EPOCH_OFFSET
        dt_value = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
        return dt_value.isoformat()
    if isinstance(value, str):
        return value
    return str(value)


def _first_nonempty_line(value: object) -> str | None:
    """Return first non-empty line from text-like content."""
    if value is None:
        return None
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    for line in text.split("\n"):
        clean = line.strip()
        if clean:
            return clean
    return None


def build_metadata_query(conn: sqlite3.Connection, inbox_only: bool, limit: int | None) -> str:
    """Build a best-effort portable SELECT over Envelope Index schema variants."""
    m_cols = list_columns(conn, "messages")
    subj_cols = list_columns(conn, "subjects")
    addr_cols = list_columns(conn, "addresses")
    mb_cols = list_columns(conn, "mailboxes")
    sum_cols = list_columns(conn, "summaries")

    if not m_cols:
        raise RuntimeError("messages table not available in Envelope Index")

    message_id_expr = "NULL AS message_id"
    # Prefer semantically stable IDs when present.
    for name in ("message_id", "global_message_id", "remote_id", "document_id"):
        if name in m_cols:
            message_id_expr = f"CAST(m.{name} AS TEXT) AS message_id"
            break

    sender_expr = "'' AS sender"
    recipient_expr = "'' AS recipient"
    joins: list[str] = []
    if "sender" in m_cols and addr_cols:
        sender_parts: list[str] = []
        if "address" in addr_cols:
            sender_parts.append("a.address")
        if "comment" in addr_cols:
            sender_parts.append("a.comment")
        sender_parts.append("CAST(m.sender AS TEXT)")
        sender_expr = f"COALESCE({', '.join(sender_parts)}) AS sender"
        joins.append("LEFT JOIN addresses a ON a.ROWID = m.sender")
    elif "sender" in m_cols:
        sender_expr = "CAST(m.sender AS TEXT) AS sender"

    recipient_col: str | None = None
    for candidate in ("to", "recipients", "recipient", "to_address"):
        if candidate in m_cols:
            recipient_col = candidate
            break
    if recipient_col is not None and addr_cols and recipient_col in {"to", "recipient"}:
        recipient_parts: list[str] = []
        if "address" in addr_cols:
            recipient_parts.append("ar.address")
        if "comment" in addr_cols:
            recipient_parts.append("ar.comment")
        recipient_parts.append(f'CAST(m."{recipient_col}" AS TEXT)')
        recipient_expr = f"COALESCE({', '.join(recipient_parts)}) AS recipient"
        joins.append(f'LEFT JOIN addresses ar ON ar.ROWID = m."{recipient_col}"')
    elif recipient_col is not None:
        recipient_expr = f'CAST(m."{recipient_col}" AS TEXT) AS recipient'

    subject_expr = "NULL AS subject"
    if "subject" in m_cols and "subject" in subj_cols:
        joins.append("LEFT JOIN subjects s ON s.ROWID = m.subject")
        subject_expr = "COALESCE(s.subject, CAST(m.subject AS TEXT)) AS subject"
    elif "subject" in m_cols:
        subject_expr = "CAST(m.subject AS TEXT) AS subject"

    body_expr = "NULL AS body_preview"
    if "summary" in m_cols and "summary" in sum_cols:
        joins.append("LEFT JOIN summaries sm ON sm.ROWID = m.summary")
        body_expr = "COALESCE(sm.summary, CAST(m.summary AS TEXT)) AS body_preview"
    elif "summary" in m_cols:
        body_expr = "CAST(m.summary AS TEXT) AS body_preview"
    elif "snippet" in m_cols:
        body_expr = "CAST(m.snippet AS TEXT) AS body_preview"
    elif "preview" in m_cols:
        body_expr = "CAST(m.preview AS TEXT) AS body_preview"
    elif "synopsis" in m_cols:
        body_expr = "CAST(m.synopsis AS TEXT) AS body_preview"

    mailbox_expr = "'UNKNOWN' AS mailbox"
    inbox_filter_expr = None
    if "mailbox" in m_cols and mb_cols:
        joins.append("LEFT JOIN mailboxes mb ON mb.ROWID = m.mailbox")
        for candidate in ("url", "name", "external_id"):
            if candidate in mb_cols:
                mailbox_expr = f"COALESCE(mb.{candidate}, 'UNKNOWN') AS mailbox"
                break
        name_expr = "LOWER(COALESCE(mb.name, ''))" if "name" in mb_cols else "''"
        url_expr = "UPPER(COALESCE(mb.url, ''))" if "url" in mb_cols else "''"
        external_expr = (
            "LOWER(COALESCE(mb.external_id, ''))" if "external_id" in mb_cols else "''"
        )
        inbox_filter_expr = (
            "("
            f"{name_expr} = 'inbox' "
            f"OR {url_expr} LIKE '%/INBOX' "
            f"OR {url_expr} LIKE '%/INBOX/%' "
            f"OR {external_expr} = 'inbox'"
            ")"
        )
    elif "mailbox" in m_cols:
        mailbox_expr = "CAST(m.mailbox AS TEXT) AS mailbox"
        inbox_filter_expr = "LOWER(CAST(m.mailbox AS TEXT)) = 'inbox'"

    date_expr = "NULL AS date_received"
    for candidate in ("date_received", "display_date", "date_sent"):
        if candidate in m_cols:
            date_expr = f"m.{candidate} AS date_received"
            break

    is_read_expr = "0 AS is_read"
    if "read" in m_cols:
        is_read_expr = "m.read AS is_read"

    is_flagged_expr = "0 AS is_flagged"
    # Prefer explicit flagged columns when available. Fallback to a minimal
    # bit-test on flags, because `flags <> 0` overcounts (many non-flag states
    # set bits unrelated to user flag markers).
    if "flagged" in m_cols:
        is_flagged_expr = "m.flagged AS is_flagged"
    elif "is_flagged" in m_cols:
        is_flagged_expr = "m.is_flagged AS is_flagged"
    elif "flags" in m_cols:
        is_flagged_expr = "CASE WHEN (m.flags & 1) <> 0 THEN 1 ELSE 0 END AS is_flagged"

    is_junk_expr = "0 AS is_junk"
    if inbox_filter_expr is not None and "mailbox" in m_cols and mb_cols and "name" in mb_cols:
        is_junk_expr = (
            "CASE WHEN LOWER(COALESCE(mb.name, '')) LIKE '%junk%' THEN 1 ELSE 0 END AS is_junk"
        )

    where_parts = ["1=1"]
    if inbox_only:
        # Safety: refuse to broaden scope if we cannot confidently infer inbox.
        if inbox_filter_expr is None:
            raise RuntimeError(
                "Unable to derive a reliable INBOX filter from Envelope Index schema. "
                "Refusing to ingest beyond All Inboxes scope."
            )
        where_parts.append(inbox_filter_expr)

    limit_clause = ""
    if limit is not None and limit > 0:
        limit_clause = f"\nLIMIT {int(limit)}"

    sql = f"""
    SELECT
      m.ROWID AS mail_local_id,
      {message_id_expr},
      {mailbox_expr},
      {sender_expr},
      {recipient_expr},
      {subject_expr},
      {body_expr},
      {date_expr},
      {is_read_expr},
      {is_flagged_expr},
      {is_junk_expr}
    FROM messages m
    {' '.join(joins)}
    WHERE {' AND '.join(where_parts)}
    ORDER BY m.ROWID DESC
    {limit_clause}
    """
    return sql


def iter_envelope_rows(
    conn: sqlite3.Connection,
    inbox_only: bool = True,
    limit: int | None = 50_000,
) -> Iterator[dict]:
    """Yield normalized row dicts for downstream ingest normalization."""
    sql = build_metadata_query(conn, inbox_only=inbox_only, limit=limit)
    for row in conn.execute(sql):
        raw_sender = row["sender"] if "sender" in row.keys() else ""
        sender = raw_sender or "unknown@unknown"
        yield {
            "mail_local_id": row["mail_local_id"],
            "message_id": row["message_id"],
            "mailbox": row["mailbox"] or "UNKNOWN",
            "sender": str(sender),
            "recipient": str(row["recipient"] or ""),
            "subject": row["subject"],
            "body_preview": _first_nonempty_line(row["body_preview"]),
            "date_received": to_iso_timestamp(row["date_received"]),
            "is_read": bool(row["is_read"]),
            "is_flagged": bool(row["is_flagged"]),
            "is_junk": bool(row["is_junk"]),
        }
