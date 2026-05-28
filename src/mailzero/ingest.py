from __future__ import annotations

"""Normalization layer between raw Mail rows and local DB rows."""

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable, Iterator

from .masked_sender import resolve_sender_domain


@dataclass
class EmailRecord:
    # Canonical fields used by current domain analytics pipeline.
    message_id: str | None
    mailbox: str
    sender: str
    recipient: str
    sender_domain: str
    sender_routing_domain: str
    sender_domain_source: str
    subject: str | None
    body_preview: str | None
    date_received: str | None
    is_read: bool
    is_flagged: bool
    is_junk: bool
    source_hash: str

def row_hash(raw: dict) -> str:
    # Stable fingerprint used to detect source-row changes across syncs.
    digest = hashlib.sha256()
    digest.update(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return digest.hexdigest()


def normalize_row(raw: dict) -> EmailRecord:
    # `resolve_sender_domain` encapsulates relay decoding and source labeling.
    sender = str(raw.get("sender") or "")
    resolved = resolve_sender_domain(sender)
    return EmailRecord(
        message_id=raw.get("message_id"),
        mailbox=str(raw.get("mailbox") or "INBOX"),
        sender=sender,
        recipient=str(raw.get("recipient") or ""),
        sender_domain=resolved.sender_domain,
        sender_routing_domain=resolved.sender_routing_domain,
        sender_domain_source=resolved.sender_domain_source,
        subject=raw.get("subject"),
        body_preview=raw.get("body_preview"),
        date_received=raw.get("date_received"),
        is_read=bool(raw.get("is_read", False)),
        is_flagged=bool(raw.get("is_flagged", False)),
        is_junk=bool(raw.get("is_junk", False)),
        source_hash=row_hash(raw),
    )


def normalize_rows(rows: Iterable[dict]) -> Iterator[dict]:
    """Yield DB-ready dictionaries from raw row dictionaries."""
    for raw in rows:
        record = normalize_row(raw)
        yield {
            "mail_local_id": raw.get("mail_local_id"),
            "message_id": record.message_id,
            "mailbox": record.mailbox,
            "sender": record.sender,
            "recipient": record.recipient,
            "sender_domain": record.sender_domain,
            "sender_routing_domain": record.sender_routing_domain,
            "sender_domain_source": record.sender_domain_source,
            "subject": record.subject,
            "body_preview": record.body_preview,
            "date_received": record.date_received,
            "is_read": record.is_read,
            "is_flagged": record.is_flagged,
            "is_junk": record.is_junk,
            "source_hash": record.source_hash,
        }
