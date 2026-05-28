from __future__ import annotations

"""Core workflow orchestration for the mailzero CLI.

This module is the "application service layer":
- It coordinates database ingestion from Mail.app metadata.
- It computes domain-level summaries and message previews.
- It performs run/undo side effects and writes backup state.

Design goal: keep each phase deterministic and inspectable via JSON files.
"""

import json
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db
from .filter_expr import CompiledFilter, compile_custom_filter
from .ingest import normalize_rows
from .mail_envelope import connect_envelope_readonly, discover_envelope_index, iter_envelope_rows
from .mail_ops import move_message
from .run_selector import DomainOption, choose_domains
from .ui import accent, app_header, box, color, good, kv, style_domain, table, warn

MAILBOX_TITLE_COLORS = ["34", "36", "32", "35", "33", "37"]
# Customize these knobs to tune the domain table and message previews.
MIN_DOMAIN_TOTAL_FOR_TABLE = 3
MAX_DOMAIN_ROWS_PER_MAILBOX = 90
PREVIEW_ITEMS_PER_DOMAIN = 20

# Runtime hook used by the CLI progress display. This stays optional so core
# logic can run in non-interactive contexts (tests/automation).
_RUN_PROGRESS_CALLBACK: Any = None


def set_run_progress_callback(callback: Any) -> None:
    """Register or clear a callback invoked during RUN-mode action execution."""
    global _RUN_PROGRESS_CALLBACK
    _RUN_PROGRESS_CALLBACK = callback


@dataclass
class MailzeroPaths:
    """Filesystem layout for state artifacts.

    - dry_run_file: canonical dashboard plan
    - last_run_file: backup log consumed by `--undo`
    """
    state_dir: Path
    dry_run_file: Path
    last_run_file: Path


def default_paths() -> MailzeroPaths:
    """Default state lives in project-local `.mailzero/`."""
    state_dir = Path(".mailzero")
    return MailzeroPaths(
        state_dir=state_dir,
        dry_run_file=state_dir / "dry-run.json",
        last_run_file=state_dir / "last-run.json",
    )


def _now_iso() -> str:
    # Store UTC with offset for stable machine parsing and human debugging.
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    # Always create parent directories here so callers can stay focused on
    # workflow logic instead of file system setup boilerplate.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def _read_json(path: Path) -> dict[str, Any]:
    # Fail fast if shape is unexpected; downstream code assumes object access.
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return value


def _use_dummy_data() -> bool:
    """Gate for safe local testing without touching live Mail.app state."""
    value = os.getenv("MAILZERO_USE_DUMMY_DATA", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _selected_domains_from_env(available_domains: list[str]) -> list[str] | None:
    # Optional non-interactive override for automation/tests.
    # Examples:
    # MAILZERO_SELECTED_DOMAINS="apple.com,doordash.com"
    # MAILZERO_SELECTED_DOMAINS="*"
    raw = os.getenv("MAILZERO_SELECTED_DOMAINS", "").strip()
    if not raw:
        return None
    if raw == "*":
        return list(available_domains)
    desired = {item.strip().lower() for item in raw.split(",") if item.strip()}
    out = [domain for domain in available_domains if domain.lower() in desired]
    return out


def _preserve_flagged_from_env(default: bool = True) -> bool:
    """Optional non-interactive override for preserving flagged emails.

    MAILZERO_PRESERVE_FLAGGED values:
    - 1/true/yes/on  -> preserve flagged emails in inbox
    - 0/false/no/off -> allow flagged emails to be archived
    """
    raw = os.getenv("MAILZERO_PRESERVE_FLAGGED", "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _compile_filter(source: str | None) -> CompiledFilter | None:
    """Compile custom filter expression into SQL where fragment."""
    return compile_custom_filter(source)


def _dummy_rows() -> list[dict[str, Any]]:
    # Purposefully includes multiple items per domain so RUN mode can archive all
    # but the most recent message in each selected domain.
    return [
        {
            "mail_local_id": None,
            "message_id": "dummy-1",
            "mailbox": "Work INBOX",
            "sender": "Deals <offers@shop.example>",
            "recipient": "me@example.com",
            "subject": "Sale this week",
            "body_preview": "Tap to unlock this week's member discount.",
            "date_received": "2026-05-26T10:00:00+00:00",
            "is_read": True,
            "is_flagged": False,
            "is_junk": False,
        },
        {
            "mail_local_id": None,
            "message_id": "dummy-2",
            "mailbox": "Work INBOX",
            "sender": "Deals <offers@shop.example>",
            "recipient": "me@example.com",
            "subject": "Receipt #4412",
            "body_preview": "Thanks for your purchase. Your receipt is attached.",
            "date_received": "2026-05-25T10:00:00+00:00",
            "is_read": True,
            "is_flagged": False,
            "is_junk": False,
        },
        {
            "mail_local_id": None,
            "message_id": "dummy-3",
            "mailbox": "Work INBOX",
            "sender": "Deals <offers@shop.example>",
            "recipient": "me@example.com",
            "subject": "Order update",
            "body_preview": "Your order has shipped and is on the way.",
            "date_received": "2026-05-24T10:00:00+00:00",
            "is_read": True,
            "is_flagged": True,
            "is_junk": False,
        },
        {
            "mail_local_id": None,
            "message_id": "dummy-4",
            "mailbox": "Personal INBOX",
            "sender": "News Daily <digest@news.example>",
            "recipient": "me@example.com",
            "subject": "Morning digest",
            "body_preview": "Top headlines for your morning read.",
            "date_received": "2026-05-26T08:00:00+00:00",
            "is_read": False,
            "is_flagged": False,
            "is_junk": False,
        },
        {
            "mail_local_id": None,
            "message_id": "dummy-5",
            "mailbox": "Personal INBOX",
            "sender": "News Daily <digest@news.example>",
            "recipient": "me@example.com",
            "subject": "Evening digest",
            "body_preview": "Here is your evening roundup.",
            "date_received": "2026-05-25T20:00:00+00:00",
            "is_read": False,
            "is_flagged": False,
            "is_junk": False,
        },
        {
            "mail_local_id": None,
            "message_id": "dummy-6",
            "mailbox": "Work INBOX",
            "sender": "Alerts <alerts@build.example>",
            "recipient": "me@example.com",
            "subject": "Build failed",
            "body_preview": "Pipeline failed on test step in main branch.",
            "date_received": "2026-05-26T07:00:00+00:00",
            "is_read": False,
            "is_flagged": True,
            "is_junk": False,
        },
        {
            "mail_local_id": None,
            "message_id": "dummy-7",
            "mailbox": "Work INBOX",
            "sender": "Alerts <alerts@build.example>",
            "recipient": "me@example.com",
            "subject": "Build restored",
            "body_preview": "Pipeline is healthy again after rerun.",
            "date_received": "2026-05-25T07:00:00+00:00",
            "is_read": True,
            "is_flagged": False,
            "is_junk": False,
        },
        {
            "mail_local_id": None,
            "message_id": "dummy-8",
            "mailbox": "Personal INBOX",
            "sender": "Friend <alex@friend.test>",
            "recipient": "me@example.com",
            "subject": "Dinner this week?",
            "body_preview": "Are you free Thursday night around 7?",
            "date_received": "2026-05-26T06:00:00+00:00",
            "is_read": False,
            "is_flagged": True,
            "is_junk": False,
        },
    ]


def _archive_candidates_for_domains(
    conn: Any,
    selected_domains: list[str],
    selected_rows: list[tuple[str, str]] | None = None,
    full_remove_domains: list[str] | None = None,
    full_remove_rows: list[tuple[str, str]] | None = None,
    preserve_flagged: bool = True,
    filter_where_sql: str | None = None,
    filter_params: tuple[object, ...] = (),
) -> list[dict[str, Any]]:
    """Return run actions: archive every message except most recent per selection.

    Important behavior detail:
    - Scope is all inbox-like rows only (`mailbox LIKE '%inbox%'`).
    - "Most recent" is computed by sort order and first-seen keep semantics.
    - When `selected_rows` is provided, selection is mailbox+domain scoped.
      This allows selecting `gmail.com` in one inbox without affecting others.
    """
    if not selected_domains and not selected_rows:
        return []
    use_row_scope = bool(selected_rows)
    placeholders = ",".join("?" for _ in selected_domains) if selected_domains else ""
    row_filter_sql = ""
    row_filter_params: list[object] = []
    if use_row_scope:
        pairs = list(selected_rows or [])
        row_filter_sql = " OR ".join("(e.mailbox = ? AND e.sender_domain = ?)" for _ in pairs)
        for mailbox, domain in pairs:
            row_filter_params.extend([mailbox, domain])
    filter_clause = f"AND ({filter_where_sql})" if filter_where_sql else ""
    full_remove_set = set(full_remove_domains or [])
    full_remove_row_set = set(full_remove_rows or [])
    selection_clause = (
        f"AND ({row_filter_sql})"
        if use_row_scope
        else f"AND e.sender_domain IN ({placeholders})"
    )
    query = f"""
    SELECT
      e.id AS email_id,
      e.mail_local_id AS mail_local_id,
      e.message_id AS message_id,
      e.mailbox AS mailbox,
      e.sender_domain AS sender_domain,
      e.sender AS sender,
      e.subject AS subject,
      e.date_received AS date_received,
      e.is_flagged AS is_flagged
    FROM emails e
    WHERE LOWER(e.mailbox) LIKE '%inbox%'
      {selection_clause}
      {filter_clause}
    ORDER BY e.mailbox ASC,
             e.sender_domain ASC,
             (e.date_received IS NULL) ASC,
             e.date_received DESC,
             e.id DESC
    """
    query_params: tuple[object, ...]
    if use_row_scope:
        query_params = (*row_filter_params, *filter_params)
    else:
        query_params = (*selected_domains, *filter_params)
    rows = conn.execute(query, query_params).fetchall()
    keep_seen: set[tuple[str, str]] = set() if use_row_scope else set()
    actions: list[dict[str, Any]] = []
    for row in rows:
        domain = row["sender_domain"]
        mailbox = row["mailbox"]
        row_key = (str(mailbox), str(domain))
        is_flagged = bool(row["is_flagged"])

        # Preserve flagged mode excludes flagged rows from candidate set unless
        # domain is explicitly marked as "full remove" ([!]) by the operator.
        is_full_remove = (
            (row_key in full_remove_row_set) if use_row_scope else (domain in full_remove_set)
        )
        if preserve_flagged and is_flagged and not is_full_remove:
            continue

        if is_full_remove:
            actions.append(
                {
                    "email_id": row["email_id"],
                    "mail_local_id": row["mail_local_id"],
                    "message_id": row["message_id"],
                    "sender_domain": row["sender_domain"],
                    "sender": row["sender"],
                    "subject": row["subject"],
                    "date_received": row["date_received"],
                    "from_mailbox": row["mailbox"],
                    "to_mailbox": "Archive",
                    "strategy": ("all_by_mailbox_domain" if use_row_scope else "all_by_domain"),
                }
            )
            continue

        keep_key: tuple[str, str] | str = row_key if use_row_scope else str(domain)
        if keep_key not in keep_seen:
            keep_seen.add(keep_key)
            continue
        actions.append(
            {
                "email_id": row["email_id"],
                "mail_local_id": row["mail_local_id"],
                "message_id": row["message_id"],
                "sender_domain": row["sender_domain"],
                "sender": row["sender"],
                "subject": row["subject"],
                "date_received": row["date_received"],
                "from_mailbox": row["mailbox"],
                "to_mailbox": "Archive",
                "strategy": (
                    "all_but_most_recent_by_mailbox_domain"
                    if use_row_scope
                    else "all_but_most_recent_by_domain"
                ),
            }
        )
    return actions


def _extract_domain_previews(
    conn: Any,
    mailbox_domain_pairs: list[tuple[str, str]],
    max_items: int = PREVIEW_ITEMS_PER_DOMAIN,
    filter_where_sql: str | None = None,
    filter_params: tuple[object, ...] = (),
) -> dict[tuple[str, str], list[dict[str, str]]]:
    """Build per-(mailbox, domain) preview lists for run-selector hover panel.

    Each preview item contains:
    - subject: latest known subject
    - body_first_line: first non-empty line from indexed preview/snippet content
    """
    if not mailbox_domain_pairs:
        return {}
    unique_domains = sorted({domain for _, domain in mailbox_domain_pairs})
    rows = db.latest_preview_rows_for_domains(
        conn,
        unique_domains,
        per_pair=max_items,
        filter_where_sql=filter_where_sql,
        filter_params=filter_params,
    )
    out: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        key = (str(row["mailbox"]), str(row["sender_domain"]))
        out[key].append(
            {
                "subject": str(row["subject"] or ""),
                "body_first_line": str(row["body_preview"] or ""),
                "date_received": str(row["date_received"] or ""),
            }
        )
    return dict(out)


def run_dry_run(
    db_path: Path,
    paths: MailzeroPaths | None = None,
    min_domain_total: int | None = None,
    custom_filter_query: str | None = None,
    data_mode: str = "real",
    source_path: Path | None = None,
) -> dict[str, Any]:
    """Full refresh pass.

    This always rebuilds the local index from source metadata so report output
    reflects current Mail.app state after syncs/moves.
    """
    paths = paths or default_paths()
    # Default behavior keeps the top-domain list focused on meaningful bulk senders.
    min_total = MIN_DOMAIN_TOTAL_FOR_TABLE if min_domain_total is None else max(1, int(min_domain_total))
    compiled_filter = _compile_filter(custom_filter_query)
    filter_where_sql = compiled_filter.where_sql if compiled_filter else None
    filter_params = compiled_filter.params if compiled_filter else ()
    conn = db.connect(db_path)
    envelope_conn = None
    try:
        db.init_db(conn)
        db.reset_for_full_refresh(conn)

        effective_mode = str(data_mode or "real").strip().lower()
        # Backward-compatible env override for existing local workflows.
        if effective_mode not in {"real", "testing"}:
            raise ValueError("data_mode must be 'real' or 'testing'")
        if effective_mode == "real" and _use_dummy_data():
            effective_mode = "testing"

        if effective_mode == "testing":
            # Testing mode keeps run/undo development safe and repeatable.
            source_descriptor = str(source_path) if source_path else "dummy_data_fixture"
            ingested_count = db.upsert_emails(conn, normalize_rows(_dummy_rows()))
        else:
            # Real mode reads the latest Envelope Index snapshot read-only.
            source = discover_envelope_index(explicit_path=source_path)
            envelope_conn = connect_envelope_readonly(source.path)
            source_descriptor = str(source.path)
            ingest_rows = normalize_rows(iter_envelope_rows(envelope_conn, inbox_only=True, limit=None))
            ingested_count = db.upsert_emails(conn, ingest_rows)

        top_domains_rows = db.domain_stats_by_mailbox(
            conn,
            limit_per_mailbox=MAX_DOMAIN_ROWS_PER_MAILBOX,
            filter_where_sql=filter_where_sql,
            filter_params=filter_params,
        )
        top_domains = [
            {
                "mailbox": row["mailbox"],
                "sender_domain": row["sender_domain"],
                "total": row["total"],
                "unread": row["unread"],
                "flagged": row["flagged"],
            }
            for row in top_domains_rows
            if int(row["total"]) >= min_total
        ]
        pair_list = [(row["mailbox"], row["sender_domain"]) for row in top_domains]
        previews = _extract_domain_previews(
            conn,
            pair_list,
            max_items=PREVIEW_ITEMS_PER_DOMAIN,
            filter_where_sql=filter_where_sql,
            filter_params=filter_params,
        )
        for row in top_domains:
            row["preview_items"] = previews.get((row["mailbox"], row["sender_domain"]), [])

        domain_source_rows = db.sender_domain_source_stats(
            conn,
            filter_where_sql=filter_where_sql,
            filter_params=filter_params,
        )
        domain_sources = [
            {"source": row["sender_domain_source"], "total": row["total"]}
            for row in domain_source_rows
        ]

        payload: dict[str, Any] = {
            "created_at": _now_iso(),
            "db_path": str(db_path),
            "mail_scope": "all_inboxes_only",
            "refresh_mode": "full_rebuild",
            "domain_min_total": min_total,
            "custom_filter_query": compiled_filter.source if compiled_filter else "",
            "custom_filter_mode": compiled_filter.mode if compiled_filter else "",
            "data_mode": effective_mode,
            "source": source_descriptor,
            "ingested_count": ingested_count,
            "top_domains": top_domains,
            "sender_domain_sources": domain_sources,
        }
        _write_json(paths.dry_run_file, payload)
        return payload
    finally:
        # Explicit close calls make resource lifetime obvious for readers coming
        # from languages where context managers are less common.
        if envelope_conn is not None:
            envelope_conn.close()
        conn.close()


def render_dry_run_report(payload: dict[str, Any]) -> str:
    """Render colorful CLI report for the dashboard plan."""
    def summary_item(key: str, value: str) -> str:
        # Avoid terminal-weight dependence; emphasize with color + casing only.
        return f"{accent(key.upper() + ':')} {color(value, '3')}"

    lines: list[str] = [
        app_header("mailzero :: dry-run", "Plan built from All Inboxes"),
        box(
            "Summary",
            [
                summary_item("Minimum emails per sender", str(payload.get("domain_min_total", MIN_DOMAIN_TOTAL_FOR_TABLE))),
                *(
                    [summary_item("Data mode", "testing")]
                    if str(payload.get("data_mode", "real")) in {"testing", "dummy"}
                    else []
                ),
                summary_item("Source", str(payload.get("source", "unknown"))),
                summary_item("Emails ingested", str(payload["ingested_count"])),
                summary_item("Selectable mailbox/domain rows", str(len(payload.get("top_domains", [])))),
            ],
        ),
        "",
    ]

    top_domains = payload.get("top_domains", [])
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in top_domains:
        mailbox_label = str(row.get("mailbox") or "INBOX")
        grouped[mailbox_label].append(row)

    mailbox_rank = sorted(
        grouped.keys(),
        # Sort groups by total inbox volume so biggest cleanup opportunities
        # appear first.
        key=lambda mailbox: sum(int(item.get("total", 0)) for item in grouped[mailbox]),
        reverse=True,
    )

    domain_rows: list[list[Any]] = []
    for idx, mailbox_label in enumerate(mailbox_rank):
        rows = grouped[mailbox_label]
        color_code = MAILBOX_TITLE_COLORS[idx % len(MAILBOX_TITLE_COLORS)]
        row_color = lambda text: color(text, color_code)
        if domain_rows:
            # Empty visual separator row between inbox groups.
            domain_rows.append(["", "", "", ""])
        for item in rows:
            domain_rows.append(
                [
                    row_color(str(item["total"])),
                    ("" if int(item["unread"]) == 0 else row_color(str(item["unread"]))),
                    ("" if int(item.get("flagged", 0)) == 0 else row_color(str(item.get("flagged", 0)))),
                    row_color(style_domain(item["sender_domain"])),
                ]
            )

    lines.append(
        box(
            "Top Sender Domains (color coded by inbox)",
            [table(["total", "unread", "flagged", "domain"], domain_rows)],
        )
    )
    lines.append("")

    source_name_map = {
        "direct": "Unaliased",
        "apple_relay_encoded": "Apple Private Relay",
        "addy_reverse_alias": "AnonAddy",
        "simplelogin_reverse_alias": "SimpleLogin",
    }
    source_rows = [
        [row["total"], source_name_map.get(row["source"], row["source"])]
        for row in payload.get("sender_domain_sources", [])
    ]
    lines.append(box("Email Aliases", [table(["count", "source"], source_rows, aligns=["right", "left"])]))
    lines.append("")
    return "\n".join(lines)


def run_apply_from_dry_run(
    paths: MailzeroPaths | None = None,
    *,
    min_domain_total: int | None = None,
    custom_filter_query: str | None = None,
) -> dict[str, Any]:
    """Execute selected row/domain archive plan and persist reversible run log."""
    paths = paths or default_paths()
    if not paths.dry_run_file.exists():
        raise FileNotFoundError(f"Dry-run file not found: {paths.dry_run_file}")
    payload = _read_json(paths.dry_run_file)

    db_path = Path(str(payload.get("db_path", "")))
    if not db_path:
        raise ValueError("Invalid dry-run file: missing db_path")
    conn = db.connect(db_path)
    db.init_db(conn)

    effective_filter_query = (
        custom_filter_query
        if custom_filter_query is not None
        else str(payload.get("custom_filter_query", "") or "")
    )
    compiled_filter = _compile_filter(effective_filter_query)
    filter_where_sql = compiled_filter.where_sql if compiled_filter else None
    filter_params = compiled_filter.params if compiled_filter else ()

    top_domains_raw = payload.get("top_domains", [])
    if not isinstance(top_domains_raw, list) or not top_domains_raw:
        raise ValueError("Dry-run file does not contain top domain data.")

    min_total = None if min_domain_total is None else max(1, int(min_domain_total))
    filtered_rows = (
        [row for row in top_domains_raw if int(row.get("total", 0)) >= min_total]
        if min_total is not None
        else top_domains_raw
    )

    options: list[DomainOption] = []
    for row in filtered_rows:
        options.append(
            DomainOption(
                mailbox=str(row.get("mailbox", "INBOX")),
                domain=str(row.get("sender_domain")),
                total=int(row.get("total", 0)),
                unread=int(row.get("unread", 0)),
                flagged=int(row.get("flagged", 0)),
                preview_items=[
                    (
                        str(item.get("subject", "")),
                        str(item.get("body_first_line", "")),
                        str(item.get("date_received", "")),
                    )
                    for item in row.get("preview_items", [])
                    if isinstance(item, dict)
                ],
            )
        )

    available_domains = [opt.domain for opt in options]
    selected_domains = _selected_domains_from_env(available_domains)
    selected_rows: list[tuple[str, str]] = []
    full_remove_domains: list[str] = []
    full_remove_rows: list[tuple[str, str]] = []
    preserve_flagged = _preserve_flagged_from_env(default=True)
    if selected_domains is None:
        # Interactive path used by humans in the terminal.
        selection = choose_domains(
            options,
            current_filter_mode=(compiled_filter.mode if compiled_filter else ""),
        )
        if selection.edit_filter_requested:
            run_record = {
                "run_at": _now_iso(),
                "dry_run_created_at": payload.get("created_at"),
                "data_mode": payload.get("data_mode", "real"),
                "custom_filter_query": effective_filter_query,
                "selected_domains": [],
                "full_remove_domains": [],
                "selected_rows": [],
                "full_remove_rows": [],
                "preserve_flagged": preserve_flagged,
                "applied": [],
                "filter_edit_requested": True,
                "message": "Filter edit requested from run UI.",
            }
            _write_json(paths.last_run_file, run_record)
            return run_record
        selected_domains = selection.selected_domains
        selected_rows = selection.selected_rows
        full_remove_domains = selection.full_remove_domains
        full_remove_rows = selection.full_remove_rows
        preserve_flagged = selection.preserve_flagged
    else:
        # Non-interactive env selection keeps prior domain semantics: all inbox rows
        # for matching domains are selected.
        desired = {domain.lower() for domain in selected_domains}
        selected_rows = [
            (opt.mailbox, opt.domain)
            for opt in options
            if opt.domain.lower() in desired
        ]
    if not selected_domains:
        run_record = {
            "run_at": _now_iso(),
            "dry_run_created_at": payload.get("created_at"),
            "data_mode": payload.get("data_mode", "real"),
            "custom_filter_query": effective_filter_query,
            "selected_domains": [],
            "full_remove_domains": [],
            "selected_rows": [],
            "full_remove_rows": [],
            "preserve_flagged": preserve_flagged,
            "applied": [],
            "message": "No domains selected; no changes applied.",
        }
        _write_json(paths.last_run_file, run_record)
        return run_record

    try:
        actions = _archive_candidates_for_domains(
            conn,
            selected_domains,
            selected_rows=selected_rows,
            full_remove_domains=full_remove_domains,
            full_remove_rows=full_remove_rows,
            preserve_flagged=preserve_flagged,
            filter_where_sql=filter_where_sql,
            filter_params=filter_params,
        )
        data_mode = str(payload.get("data_mode", "real"))
        callback = _RUN_PROGRESS_CALLBACK
        if callback is not None:
            callback(
                {
                    "current": 0,
                    "total": len(actions),
                    "status": "INIT",
                    "detail": "Preparing archive operations...",
                }
            )

        applied: list[dict[str, Any]] = []
        for idx, action in enumerate(actions, start=1):
            if data_mode in {"dummy", "testing"}:
                # Testing mode mutates only local sqlite rows, not Mail.app.
                conn.execute("UPDATE emails SET mailbox = ? WHERE id = ?", ("Archive", action["email_id"]))
                conn.commit()
                ok, detail = True, "testing_mode: archived in local db only"
            else:
                # Real mode delegates mailbox move automation to AppleScript.
                ok, detail = move_message(
                    source_mailbox=str(action.get("from_mailbox", "INBOX")),
                    target_mailbox=str(action.get("to_mailbox", "Archive")),
                    local_id=(
                        int(action["mail_local_id"])
                        if action.get("mail_local_id") is not None
                        else None
                    ),
                    header_message_id=(
                        str(action["message_id"])
                        if action.get("message_id") not in (None, "")
                        else None
                    ),
                )
            applied.append(
                {
                    "timestamp": _now_iso(),
                    "ok": ok,
                    "detail": detail,
                    "action": action,
                }
            )
            if callback is not None:
                status = "OK" if ok else "ERR"
                detail_line = (
                    f"{action.get('mailbox', 'INBOX')} | {action.get('sender_domain', '?')} | "
                    f"{action.get('subject', '')[:44]}"
                )
                callback(
                    {
                        "current": idx,
                        "total": len(actions),
                        "status": status,
                        "detail": detail_line,
                    }
                )
        if callback is not None:
            callback(
                {
                    "current": len(actions),
                    "total": len(actions),
                    "status": "DONE",
                    "detail": "Run phase completed.",
                }
            )

        run_record = {
            "run_at": _now_iso(),
            "dry_run_created_at": payload.get("created_at"),
            "data_mode": data_mode,
            "custom_filter_query": effective_filter_query,
            "selected_domains": selected_domains,
            "full_remove_domains": full_remove_domains,
            "selected_rows": selected_rows,
            "full_remove_rows": full_remove_rows,
            "preserve_flagged": preserve_flagged,
            "applied": applied,
        }
        _write_json(paths.last_run_file, run_record)
        return run_record
    finally:
        conn.close()


def render_run_report(run_record: dict[str, Any], backup_file: Path) -> str:
    """Render post-run summary with operator-focused execution facts.

    Formatting rules:
    - keep the summary compact for the common success path
    - only surface failure sections when failures exist
    - include enough detail (domain + subject) for manual follow-up triage
    """
    applied = run_record.get("applied", [])
    success = sum(1 for row in applied if row.get("ok"))
    failed = len(applied) - success
    attempted = len(applied)
    selected_domains = run_record.get("selected_domains", [])
    preserve_flagged = bool(run_record.get("preserve_flagged", True))
    data_mode = str(run_record.get("data_mode", "real"))
    custom_filter_query = str(run_record.get("custom_filter_query", "") or "")

    summary_lines = [
        kv("Backup file", str(backup_file), "good"),
        kv("Selected domains", str(len(selected_domains))),
        kv("Preserve flagged", "yes" if preserve_flagged else "no", "warn" if not preserve_flagged else None),
        kv("Messages archived", f"{success} (of {attempted} selected)", "good" if success else None),
    ]
    if custom_filter_query:
        summary_lines.insert(2, kv("Custom filter", custom_filter_query))
    if data_mode in {"testing", "dummy"}:
        summary_lines.insert(1, kv("Data mode", "testing"))

    if failed:
        summary_lines.append(kv("Failed moves", str(failed), "bad"))
        summary_lines.append(warn("Failed messages (domain | subject)"))
        for row in applied:
            if row.get("ok"):
                continue
            action = row.get("action", {})
            domain = str(action.get("sender_domain", "?"))
            subject = str(action.get("subject", "")).strip() or "(no subject)"
            summary_lines.append(f"- {domain} | {subject[:90]}")

    lines = [
        app_header("mailzero :: run", "Archive all but most recent per selected domain"),
        box("Execution Summary", summary_lines),
    ]
    if run_record.get("message"):
        lines.append("")
        lines.append(warn(str(run_record["message"])))
    lines.append("")
    lines.append(good("Use `mailzero --undo` to revert this run."))
    return "\n".join(lines)


def run_undo(paths: MailzeroPaths | None = None) -> dict[str, Any]:
    """Undo the last run by replaying successful actions in reverse order."""
    paths = paths or default_paths()
    if not paths.last_run_file.exists():
        raise FileNotFoundError(f"Run backup file not found: {paths.last_run_file}")
    run_record = _read_json(paths.last_run_file)
    applied = run_record.get("applied", [])
    if not isinstance(applied, list):
        raise ValueError("Invalid run backup: applied must be a list")

    data_mode = str(run_record.get("data_mode", "real"))
    undo_applied: list[dict[str, Any]] = []

    conn = None
    if data_mode in {"dummy", "testing"}:
        db_path = Path(str(_read_json(paths.dry_run_file).get("db_path", "")))
        conn = db.connect(db_path)
        db.init_db(conn)

    try:
        # Reverse order reduces dependency risk when multiple actions touched
        # related state during run phase.
        for entry in reversed(applied):
            if not entry.get("ok"):
                continue
            action = entry.get("action", {})
            if data_mode in {"dummy", "testing"}:
                assert conn is not None
                conn.execute("UPDATE emails SET mailbox = ? WHERE id = ?", ("INBOX", action["email_id"]))
                conn.commit()
                ok, detail = True, "testing_mode: restored mailbox to INBOX in local db"
            else:
                ok, detail = move_message(
                    source_mailbox=str(action.get("to_mailbox", "Archive")),
                    target_mailbox=str(action.get("from_mailbox", "INBOX")),
                    local_id=(
                        int(action["mail_local_id"])
                        if action.get("mail_local_id") is not None
                        else None
                    ),
                    header_message_id=(
                        str(action["message_id"])
                        if action.get("message_id") not in (None, "")
                        else None
                    ),
                )
            undo_applied.append(
                {
                    "timestamp": _now_iso(),
                    "ok": ok,
                    "detail": detail,
                    "action": action,
                }
            )
    finally:
        if conn is not None:
            conn.close()

    result = {
        "undo_at": _now_iso(),
        "source_run_at": run_record.get("run_at"),
        "data_mode": data_mode,
        "applied": undo_applied,
    }
    return result


def render_undo_report(undo_record: dict[str, Any]) -> str:
    """Render undo summary."""
    applied = undo_record.get("applied", [])
    success = sum(1 for row in applied if row.get("ok"))
    failed = len(applied) - success
    data_mode = str(undo_record.get("data_mode", "real"))
    lines = [
        app_header("mailzero :: undo", "Reverting last run"),
        box(
            "Undo Summary",
            [
                *([kv("Data mode", "testing")] if data_mode in {"testing", "dummy"} else []),
                kv("Undo attempts", str(len(applied))),
                kv("Successful restores", str(success), "good" if success else None),
                kv("Failed restores", str(failed), "bad" if failed else "good"),
            ],
        ),
    ]
    return "\n".join(lines)
