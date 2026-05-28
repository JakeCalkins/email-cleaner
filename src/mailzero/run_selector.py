from __future__ import annotations

"""Interactive curses selector used by RUN mode.

Selection semantics:
- user navigates per mailbox-domain row
- toggle is per-row (mailbox + domain), so the same domain in other inboxes
  remains untouched unless explicitly selected there too
"""

import curses
import re
import sys
from dataclasses import dataclass


@dataclass
class DomainOption:
    # Raw mailbox label from Envelope Index (or normalized fallback).
    mailbox: str
    domain: str
    total: int
    unread: int
    flagged: int
    # Latest message previews as tuples of (subject, first body line, date_received).
    preview_items: list[tuple[str, str, str]]


@dataclass
class SelectionResult:
    selected_domains: list[str]
    full_remove_domains: list[str]
    selected_rows: list[tuple[str, str]]
    full_remove_rows: list[tuple[str, str]]
    preserve_flagged: bool
    edit_filter_requested: bool = False


DOMAIN_RE = re.compile(r"^(?P<base>.+?)\.(?P<tld>[a-z0-9-]+)$", re.IGNORECASE)


def _split_domain(domain: str) -> tuple[str, str]:
    m = DOMAIN_RE.match(domain.strip())
    if not m:
        return domain, ""
    return m.group("base"), m.group("tld")


def _truncate(text: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[: max_len - 3] + "..."


def _clip(text: str, max_len: int) -> str:
    """Hard cut text to fit width without ellipsis."""
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len]


def _mm_yy(date_text: str) -> str:
    raw = (date_text or "").strip()
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        # ISO-like YYYY-MM-DD...
        mm = raw[5:7]
        yy = raw[2:4]
        if mm.isdigit() and yy.isdigit():
            return f"{mm}/{yy}"
    # Fallback for unknown date format.
    return "--/--"


def choose_domains(
    options: list[DomainOption],
    *,
    current_filter_mode: str = "",
) -> SelectionResult:
    """Open selector UI and return selected mailbox/domain row choices.

    Args:
        options: mailbox/domain rows from the current dashboard plan.
    """
    if not options:
        return SelectionResult(
            selected_domains=[],
            full_remove_domains=[],
            selected_rows=[],
            full_remove_rows=[],
            preserve_flagged=True,
        )
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise RuntimeError("Interactive selection requires a TTY terminal.")

    # Per-row selection mode (mailbox + domain):
    # - "normal": archive all but most recent
    # - "full": archive all emails in domain
    selection_mode_by_row: dict[tuple[str, str], str] = {}
    # Totals are row-scoped to match row-scoped selection behavior.
    row_totals: dict[tuple[str, str], int] = {}
    row_flagged_totals: dict[tuple[str, str], int] = {}
    for opt in options:
        row_key = (opt.mailbox or "INBOX", opt.domain)
        row_totals[row_key] = row_totals.get(row_key, 0) + int(opt.total)
        row_flagged_totals[row_key] = row_flagged_totals.get(row_key, 0) + int(opt.flagged)

    grouped: dict[str, list[DomainOption]] = {}
    for opt in options:
        mailbox = opt.mailbox or "INBOX"
        grouped.setdefault(mailbox, []).append(opt)
    ordered_mailboxes = sorted(
        grouped.keys(),
        key=lambda mailbox: (-sum(int(row.total) for row in grouped[mailbox]), mailbox),
    )

    active_mailbox_idx = 0
    cursor_by_mailbox: dict[str, int] = {mailbox: 0 for mailbox in ordered_mailboxes}
    offset_by_mailbox: dict[str, int] = {mailbox: 0 for mailbox in ordered_mailboxes}
    cursor_singlebox = 0
    offset_singlebox = 0
    singlebox_mode = False
    confirmed = False
    edit_filter_requested = False
    preserve_flagged = True
    hide_flagged_from_view = False
    # In-run sorting controls.
    # - `sort_key=None` means "off" (preserve current source order).
    # - otherwise, sort by one of: total, unread, flagged, domain.
    sort_key: str | None = "total"
    sort_desc = True
    sort_mode_active = False
    sortable_keys = ["total", "unread", "flagged", "domain"]
    sort_cursor_idx = 0
    # Enter opens a lightweight review prompt before exiting selector.
    review_before_exit = False

    def _render(stdscr: curses.window) -> None:
        nonlocal active_mailbox_idx, confirmed, preserve_flagged, hide_flagged_from_view
        nonlocal cursor_singlebox, offset_singlebox, singlebox_mode
        nonlocal edit_filter_requested
        nonlocal sort_key, sort_desc
        nonlocal sort_mode_active, sort_cursor_idx
        nonlocal review_before_exit

        def safe_ch(y: int, x: int, ch: int | str, attr: int = 0) -> None:
            h, w = stdscr.getmaxyx()
            if y < 0 or y >= h or x < 0 or x >= w:
                return
            try:
                stdscr.addch(y, x, ch, attr)
            except curses.error:
                return

        curses.curs_set(0)
        stdscr.keypad(True)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)    # title
            curses.init_pair(2, curses.COLOR_GREEN, -1)   # selected
            curses.init_pair(3, curses.COLOR_YELLOW, -1)  # hint
            curses.init_pair(5, curses.COLOR_RED, -1)     # cancel/help
            curses.init_pair(6, curses.COLOR_BLUE, -1)    # mailbox A
            curses.init_pair(7, curses.COLOR_CYAN, -1)    # mailbox B
            curses.init_pair(8, curses.COLOR_GREEN, -1)   # mailbox C
            curses.init_pair(9, curses.COLOR_MAGENTA, -1) # mailbox D
            curses.init_pair(10, curses.COLOR_YELLOW, -1) # mailbox E
            # Active-tab background fills for clearer mailbox focus.
            curses.init_pair(12, curses.COLOR_BLACK, curses.COLOR_BLUE)
            curses.init_pair(13, curses.COLOR_BLACK, curses.COLOR_CYAN)
            curses.init_pair(14, curses.COLOR_BLACK, curses.COLOR_GREEN)
            curses.init_pair(15, curses.COLOR_BLACK, curses.COLOR_MAGENTA)
            curses.init_pair(16, curses.COLOR_BLACK, curses.COLOR_YELLOW)
            curses.init_pair(17, curses.COLOR_BLACK, curses.COLOR_WHITE)  # active row cursor
            curses.init_pair(18, curses.COLOR_BLACK, curses.COLOR_YELLOW)  # selected row
            curses.init_pair(19, curses.COLOR_BLACK, curses.COLOR_RED)     # full-remove row
        title_attr = curses.A_BOLD | (curses.color_pair(1) if curses.has_colors() else 0)
        hint_attr = curses.color_pair(3) if curses.has_colors() else curses.A_DIM
        selected_attr = curses.A_BOLD | (curses.color_pair(2) if curses.has_colors() else 0)
        desc_attr = curses.A_DIM | (curses.color_pair(3) if curses.has_colors() else curses.A_DIM)
        footer_hotkey_attr = curses.A_DIM | (curses.color_pair(1) if curses.has_colors() else curses.A_DIM)
        pipe_attr = curses.A_DIM
        italic_attr = getattr(curses, "A_ITALIC", 0)
        on_attr = curses.A_BOLD | (curses.color_pair(2) if curses.has_colors() else 0)
        off_attr = curses.A_BOLD | (curses.color_pair(5) if curses.has_colors() else 0)
        view_tabs_attr = curses.A_BOLD | (curses.color_pair(6) if curses.has_colors() else 0)
        filter_title_attr = curses.A_BOLD | (curses.color_pair(9) if curses.has_colors() else 0)
        preview_title_attr = curses.A_BOLD | (curses.color_pair(9) if curses.has_colors() else 0)
        sender_domain_value_attr = curses.A_BOLD | (curses.color_pair(17) if curses.has_colors() else 0)
        preview_date_attr = curses.A_DIM | (curses.color_pair(3) if curses.has_colors() else curses.A_DIM)
        metric_emails_attr = curses.A_BOLD | (curses.color_pair(9) if curses.has_colors() else 0)
        metric_archive_attr = curses.A_BOLD | (curses.color_pair(9) if curses.has_colors() else 0)
        metric_count_attr = curses.A_BOLD | (curses.color_pair(2) if curses.has_colors() else 0)
        col_title_attr = curses.A_BOLD | (curses.color_pair(9) if curses.has_colors() else 0)
        cursor_attr = (
            curses.A_BOLD | (curses.color_pair(17) if curses.has_colors() else curses.A_REVERSE)
        )
        # Keep TLD rendering to plain dim so it remains readable across terminals
        # and avoids block artifacts from mixed color-pair/background handling.
        domain_tld_attr = curses.A_DIM
        mailbox_pairs = [6, 7, 8, 9, 10]
        mailbox_tab_bg_pairs = [12, 13, 14, 15, 16]
        mailbox_to_pair: dict[str, int] = {}
        mailbox_to_tab_bg_pair: dict[str, int] = {}
        for idx, mailbox in enumerate(ordered_mailboxes):
            mailbox_to_pair[mailbox] = mailbox_pairs[idx % len(mailbox_pairs)]
            mailbox_to_tab_bg_pair[mailbox] = mailbox_tab_bg_pairs[idx % len(mailbox_tab_bg_pairs)]

        def _sort_rows(rows: list[DomainOption]) -> list[DomainOption]:
            if sort_key is None:
                return list(rows)
            if sort_key == "unread":
                return sorted(rows, key=lambda row: (row.unread, row.total, row.domain.lower()), reverse=sort_desc)
            if sort_key == "flagged":
                return sorted(rows, key=lambda row: (row.flagged, row.total, row.domain.lower()), reverse=sort_desc)
            if sort_key == "domain":
                return sorted(rows, key=lambda row: row.domain.lower(), reverse=sort_desc)
            return sorted(rows, key=lambda row: (row.total, row.unread, row.domain.lower()), reverse=sort_desc)

        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            active_mailbox = ordered_mailboxes[active_mailbox_idx]

            # Build current render rows. In singlebox mode, this collapses tabs
            # into one combined list with separator rows between inbox groups.
            # Row tuple shape: (mailbox_name, option_or_none_for_separator).
            render_rows: list[tuple[str, DomainOption | None]] = []
            if singlebox_mode:
                for mailbox in ordered_mailboxes:
                    source_rows = grouped.get(mailbox, [])
                    rows = [row for row in source_rows if row.flagged == 0] if hide_flagged_from_view else source_rows
                    rows = _sort_rows(rows)
                    if not rows:
                        continue
                    if render_rows:
                        render_rows.append((mailbox, None))
                    for row in rows:
                        render_rows.append((mailbox, row))
                cursor = cursor_singlebox
                offset = offset_singlebox
            else:
                source_rows = grouped.get(active_mailbox, [])
                rows = [row for row in source_rows if row.flagged == 0] if hide_flagged_from_view else source_rows
                rows = _sort_rows(rows)
                render_rows = [(active_mailbox, row) for row in rows]
                cursor = cursor_by_mailbox[active_mailbox]
                offset = offset_by_mailbox[active_mailbox]

            selectable_indices = [idx for idx, (_, row) in enumerate(render_rows) if row is not None]
            if selectable_indices:
                cursor = max(0, min(cursor, len(selectable_indices) - 1))
            else:
                cursor = 0
            offset = max(0, min(offset, max(0, len(render_rows) - 1)))
            active_entry_index = selectable_indices[cursor] if selectable_indices else -1
            selected_rows = set(selection_mode_by_row.keys())
            selected_domain_count = len(selected_rows)
            selected_emails = sum(row_totals.get(row_key, 0) for row_key in selected_rows)

            # Tiny terminal fallback: avoid out-of-bounds curses writes.
            if height < 14 or width < 50:
                if height > 0 and width > 2:
                    stdscr.addnstr(0, 0, "mailzero :: run", width - 1, title_attr)
                if height > 1 and width > 2:
                    line = f"Selected: {selected_domain_count} domains / {selected_emails} emails"
                    stdscr.addnstr(1, 0, _truncate(line, width - 1), width - 1, hint_attr)
                if height > 2 and width > 2:
                    stdscr.addnstr(2, 0, "Window too small. Resize for full UI.", width - 1, hint_attr)
                if height > 3 and width > 2:
                    stdscr.addnstr(3, 0, "ENTER run  |  q cancel", width - 1, desc_attr)
                stdscr.refresh()
                key = stdscr.getch()
                if key in (10, 13, curses.KEY_ENTER):
                    review_before_exit = True
                    continue
                if key in (27, ord("q")):
                    break
                if key in (ord("t"), ord("T")):
                    singlebox_mode = not singlebox_mode
                continue

            logo_lines = [
                "┌───────┐",
                "│  RUN  │",
                "└───────┘",
            ]
            logo_top = 1
            for y, line in enumerate(logo_lines, start=logo_top):
                if y >= height:
                    break
                stdscr.addnstr(y, 0, line, width - 1, title_attr)
            header_x = len(logo_lines[0]) + 2
            compact_header = width < 100
            compact_footer = width < 120
            ultra_compact_footer = width < 92
            super_tiny_table = width < 86
            # Summary is selection-global (across all inbox tabs), not per-tab.
            if preserve_flagged:
                selected_archive = sum(
                    (
                        max(0, row_totals.get(row_key, 0))
                        if selection_mode_by_row.get(row_key) == "full"
                        else max(0, (row_totals.get(row_key, 0) - row_flagged_totals.get(row_key, 0)) - 1)
                    )
                    for row_key in selected_rows
                )
            else:
                selected_archive = sum(
                    (
                        max(0, row_totals.get(row_key, 0))
                        if selection_mode_by_row.get(row_key) == "full"
                        else max(0, row_totals.get(row_key, 0) - 1)
                    )
                    for row_key in selected_rows
                )

            if review_before_exit:
                stdscr.erase()
                stdscr.addnstr(2, 0, "Review Run", width - 1, title_attr)
                review_line = f"Will archive {selected_archive} emails across {selected_domain_count} domains"
                stdscr.addnstr(4, 0, _truncate(review_line, width - 1), width - 1, hint_attr)
                stdscr.addnstr(6, 0, "Back to editing [b]   Quit as-is [q]", width - 1, desc_attr)
                stdscr.addnstr(7, 0, "Press ENTER to continue with current selection", width - 1, selected_attr)
                stdscr.refresh()
                review_key = stdscr.getch()
                if review_key in (ord("b"), ord("B"), 27):
                    review_before_exit = False
                    continue
                if review_key in (ord("q"), ord("Q")):
                    # Explicit quit from review should cancel run entirely.
                    confirmed = False
                    break
                if review_key in (10, 13, curses.KEY_ENTER):
                    confirmed = True
                    break
                continue

            # Header layout:
            # row1 summary, row3 mode/filter, row5 tabs, row6/7 helper+separator, then table.
            tab_row = 5
            if not singlebox_mode:
                inline_left_hint = "← l-arrow  " if not compact_header else "←  "
                x_tab = 0
                stdscr.addnstr(tab_row, x_tab, inline_left_hint, max(0, width - 1 - x_tab), pipe_attr)
                x_tab += len(inline_left_hint) + 1
                inline_right_hint = "  r-arrow →  " if not compact_header else "  →  "
                right_reserve = len(inline_right_hint) + 1
                overflowed = False
                for idx, mailbox in enumerate(ordered_mailboxes):
                    tab_label = f"[{idx + 1}]"
                    tab_attr = curses.A_BOLD if idx == active_mailbox_idx else curses.A_DIM
                    if curses.has_colors():
                        if idx == active_mailbox_idx:
                            tab_attr |= curses.color_pair(mailbox_to_tab_bg_pair.get(mailbox, 12))
                        else:
                            tab_attr |= curses.color_pair(mailbox_to_pair.get(mailbox, 6))
                    if x_tab + len(tab_label) + right_reserve >= width - 1:
                        overflowed = True
                        break
                    if x_tab < width - 1:
                        stdscr.addnstr(tab_row, x_tab, tab_label, max(0, width - 1 - x_tab), tab_attr)
                    x_tab += len(tab_label) + 1
                if overflowed and x_tab < width - 1:
                    stdscr.addnstr(tab_row, x_tab, "… ", max(0, width - 1 - x_tab), desc_attr)
                    x_tab += 2
                if x_tab < width - 1:
                    stdscr.addnstr(tab_row, x_tab, inline_right_hint, max(0, width - 1 - x_tab), pipe_attr)
                    x_tab += len(inline_right_hint)
            else:
                # In unified mode, tabs are collapsed and no explicit badge is shown.
                pass

            # Selected metrics row.
            primary_segments = [
                (f"{selected_emails}", metric_count_attr | italic_attr),
                (" emails selected ", metric_emails_attr),
                ("|", pipe_attr),
                (" ", hint_attr),
                (f"{selected_archive}", metric_count_attr | italic_attr),
                (" to be archived", metric_archive_attr),
            ]
            x = header_x
            for text, attr in primary_segments:
                if x >= width - 1:
                    break
                seg_attr = attr
                if "emails selected" in text or "to be archived" in text:
                    seg_attr |= curses.A_UNDERLINE
                stdscr.addnstr(1, x, text, max(0, width - 1 - x), seg_attr)
                x += len(text)

            # View mode / filter row.
            view_prefix = "View Mode "
            stdscr.addnstr(3, header_x, view_prefix, max(0, width - 1 - header_x), filter_title_attr)
            hk_x = header_x + len(view_prefix)
            stdscr.addnstr(3, hk_x, "[t]", max(0, width - 1 - hk_x), footer_hotkey_attr)
            sep = ": "
            sep_x = hk_x + 3
            stdscr.addnstr(3, sep_x, sep, max(0, width - 1 - sep_x), filter_title_attr)
            right = "Unified" if singlebox_mode else "Tabs"
            right_attr = (selected_attr | italic_attr) if singlebox_mode else (view_tabs_attr | italic_attr)
            right_x = sep_x + len(sep)
            if right_x < width - 1:
                stdscr.addnstr(3, right_x, right, max(0, width - 1 - right_x), right_attr)
            info_x = right_x + len(right) + 1
            filter_mode_text = current_filter_mode.strip().upper() or "none"
            if info_x < width - 1:
                stdscr.addnstr(3, info_x, "| ", max(0, width - 1 - info_x), pipe_attr)
                info_x += 2
                stdscr.addnstr(3, info_x, "Filter ", max(0, width - 1 - info_x), filter_title_attr)
                info_x += len("Filter ")
                stdscr.addnstr(3, info_x, "[f]", max(0, width - 1 - info_x), footer_hotkey_attr)
                info_x += 3
                stdscr.addnstr(3, info_x, ": ", max(0, width - 1 - info_x), filter_title_attr)
                info_x += 2
                stdscr.addnstr(
                    3,
                    info_x,
                    _truncate(filter_mode_text, max(0, width - 1 - info_x)),
                    max(0, width - 1 - info_x),
                    (selected_attr | italic_attr) if filter_mode_text != "none" else desc_attr,
                )

            # Sort mode helper (only shown while sort mode is active), directly above grid.
            if sort_mode_active:
                sort_help = "Sort Mode [s]: \u2190/\u2192 choose column  |  SPACE cycles asc \u2192 desc \u2192 off  |  [s] exit"
                stdscr.addnstr(6, 0, _truncate(sort_help, width - 1), width - 1, desc_attr | italic_attr)
                separator_row = 7
                header_row = 8
                divider_row = 9
                first_data_row = 10
            else:
                separator_row = 6
                header_row = 7
                divider_row = 8
                first_data_row = 9

            # explicit separator between selected summary and table.
            stdscr.addnstr(separator_row, 0, "─" * max(1, width - 1), width - 1, curses.A_DIM)

            # Expand preview panel when vertical space allows.
            preview_panel_lines = min(12, max(5, height // 3))
            table_bottom_limit = max(10, height - (preview_panel_lines + 6))
            visible = max(3, table_bottom_limit - first_data_row)
            # Scroll-window bookkeeping.
            if active_entry_index >= 0 and active_entry_index < offset:
                offset = active_entry_index
            if active_entry_index >= 0 and active_entry_index >= offset + visible:
                offset = active_entry_index - visible + 1

            available = max(24, width - 1)
            flagged_header = "FLAGGED h:show" if hide_flagged_from_view else "FLAGGED h:hide"
            flagged_header_attr = desc_attr if hide_flagged_from_view else col_title_attr
            if super_tiny_table:
                action_w = 14
                total_w = 5
                unread_w = 5
                flagged_w = 16
                padding = 1
                sep_header = "║"
                sep_body = "│"
                fixed = (
                    action_w
                    + padding
                    + len(sep_body)
                    + padding
                    + total_w
                    + padding
                    + len(sep_body)
                    + padding
                    + unread_w
                    + padding
                    + len(sep_body)
                    + padding
                    + flagged_w
                    + padding
                    + len(sep_body)
                    + padding
                )
                domain_w = max(8, available - fixed)
            else:
                action_w = 18
                total_w = 7
                unread_w = 8
                flagged_w = 16
                padding = 2
                sep_header = "║"
                sep_body = "│"
                fixed = (
                    action_w
                    + padding
                    + len(sep_body)
                    + padding
                    + total_w
                    + padding
                    + len(sep_body)
                    + padding
                    + unread_w
                    + padding
                    + len(sep_body)
                    + padding
                    + flagged_w
                    + padding
                    + len(sep_body)
                    + padding
                )
                domain_w = max(14, available - fixed)

            # Render header with colored column titles and vertical separators.
            header_cols = [
                ("ARCHIVE ACTION", action_w, "<"),
                ("TOTAL", total_w, ">"),
                ("UNREAD", unread_w, ">"),
                (flagged_header, flagged_w, ">"),
                ("DOMAIN", domain_w, "<"),
            ]
            sortable_col_by_idx = {1: "total", 2: "unread", 3: "flagged", 4: "domain"}
            sort_cursor_col_idx = 1 + (sort_cursor_idx % max(1, len(sortable_keys)))
            cursor_x = 0
            for col_idx, (label_text, col_w, align) in enumerate(header_cols):
                col_sort_key = sortable_col_by_idx.get(col_idx)
                if col_sort_key is not None and sort_key == col_sort_key:
                    direction = "↓" if sort_desc else "↑"
                    label_text = f"{label_text} {direction}"
                fmt = f"{{:{align}{col_w}}}"
                rendered = _clip(fmt.format(label_text), col_w)
                label_attr = flagged_header_attr if col_idx == 3 else col_title_attr
                if sort_mode_active and col_idx == sort_cursor_col_idx:
                    label_attr = cursor_attr
                hide_pos = rendered.find("h:hide") if col_idx == 3 else -1
                for idx_in_cell, char in enumerate(rendered):
                    if cursor_x >= width - 1:
                        break
                    char_attr = label_attr
                    # Keep h:hide secondary compared to the rest of the flagged header.
                    if hide_pos >= 0 and hide_pos <= idx_in_cell < hide_pos + len("h:hide"):
                        char_attr = desc_attr
                    stdscr.addnstr(header_row, cursor_x, char, max(0, width - 1 - cursor_x), char_attr)
                    cursor_x += 1
                if cursor_x >= width - 1:
                    break
                if col_idx < len(header_cols) - 1:
                    pad_text = (" " * padding) + sep_header + (" " * padding)
                    stdscr.addnstr(header_row, cursor_x, pad_text, max(0, width - 1 - cursor_x), pipe_attr)
                    cursor_x += len(pad_text)

            stdscr.addnstr(divider_row, 0, "━" * max(1, width - 1), width - 1, curses.A_BOLD | curses.A_DIM)

            end = min(len(render_rows), offset + visible)
            screen_row = first_data_row
            for row_index in range(offset, end):
                if screen_row >= table_bottom_limit:
                    break
                mailbox_name, opt = render_rows[row_index]
                if opt is None:
                    stdscr.addnstr(screen_row, 0, "─" * max(1, width - 1), width - 1, curses.A_DIM)
                    screen_row += 1
                    continue

                row_key = (opt.mailbox or mailbox_name, opt.domain)
                mode = selection_mode_by_row.get(row_key)
                is_selected = mode is not None
                is_full_remove = mode == "full"
                is_cursor = row_index == active_entry_index
                if is_cursor and not is_selected:
                    action_text = "Tab to toggle"
                elif is_full_remove:
                    action_text = "[all]"
                elif is_selected:
                    action_text = "[all but 1]"
                else:
                    action_text = "[ ]"
                unread_display = "" if opt.unread == 0 else str(opt.unread)
                flagged_display = "" if opt.flagged == 0 else str(opt.flagged)
                if is_cursor:
                    row_attr = cursor_attr
                elif is_full_remove:
                    if curses.has_colors():
                        row_attr = curses.A_BOLD | curses.A_UNDERLINE | curses.color_pair(19)
                    else:
                        row_attr = curses.A_BOLD | curses.A_STANDOUT | curses.A_UNDERLINE
                elif is_selected:
                    if curses.has_colors():
                        row_attr = curses.A_BOLD | curses.color_pair(18)
                    else:
                        row_attr = curses.A_BOLD | curses.A_STANDOUT
                else:
                    if curses.has_colors():
                        row_attr = curses.color_pair(mailbox_to_pair.get(mailbox_name, 6))
                    else:
                        row_attr = 0
                if is_cursor and not is_selected:
                    row_attr |= italic_attr
                prefix = (
                    f"{_clip(action_text, action_w):<{action_w}}"
                    + (" " * padding)
                    + sep_body
                    + (" " * padding)
                    + f"{opt.total:>{total_w}}"
                    + (" " * padding)
                    + sep_body
                    + (" " * padding)
                    + f"{unread_display:>{unread_w}}"
                    + (" " * padding)
                    + sep_body
                    + (" " * padding)
                    + f"{flagged_display:>{flagged_w}}"
                    + (" " * padding)
                    + sep_body
                    + (" " * padding)
                )
                stdscr.addnstr(screen_row, 0, prefix, width - 1, row_attr | curses.A_BOLD)
                # Keep structural column lines visually stable across row states.
                sep_positions = [
                    action_w + padding,
                    action_w + padding + 1 + padding + total_w + padding,
                    action_w + padding + 1 + padding + total_w + padding + 1 + padding + unread_w + padding,
                    action_w + padding + 1 + padding + total_w + padding + 1 + padding + unread_w + padding + 1 + padding + flagged_w + padding,
                ]
                for sx in sep_positions:
                    safe_ch(screen_row, sx, sep_body, pipe_attr)

                x = min(width - 1, len(prefix))
                base, tld = _split_domain(opt.domain)
                domain_text = base + (("." + tld) if tld else "")
                domain_text = _truncate(domain_text, domain_w)
                base_draw, tld_draw = _split_domain(domain_text)
                stdscr.addnstr(screen_row, x, base_draw, max(0, width - 1 - x), row_attr | curses.A_BOLD)
                x += len(base_draw)
                if tld_draw:
                    stdscr.addnstr(screen_row, x, ".", max(0, width - 1 - x), row_attr | domain_tld_attr)
                    x += 1
                    stdscr.addnstr(screen_row, x, tld_draw, max(0, width - 1 - x), row_attr | domain_tld_attr)
                    x += len(tld_draw)
                domain_pad = max(0, domain_w - len(domain_text))
                if domain_pad:
                    stdscr.addnstr(screen_row, x, " " * domain_pad, max(0, width - 1 - x), row_attr)
                    x += domain_pad
                screen_row += 1

            # Hover-style preview panel (cursor-focused row).
            preview_start = min(height - 8, table_bottom_limit + 1)
            stdscr.addnstr(preview_start, 0, "─" * max(1, width - 1), width - 1, curses.A_DIM)
            preview_pad = 2
            preview_title = "Message Preview"
            stdscr.addnstr(
                preview_start + 1,
                preview_pad,
                _truncate(preview_title, max(0, width - 1 - preview_pad)),
                max(0, width - 1 - preview_pad),
                preview_title_attr,
            )
            active_option: DomainOption | None = None
            if active_entry_index >= 0 and active_entry_index < len(render_rows):
                active_option = render_rows[active_entry_index][1]
            preview_rows = active_option.preview_items if active_option is not None else []
            panel_capacity = max(0, min(preview_panel_lines - 3, (height - 5) - (preview_start + 4)))
            if not preview_rows:
                stdscr.addnstr(preview_start + 3, 0, "-", width - 1, hint_attr)
            else:
                domain_label = active_option.domain if active_option is not None else "-"
                title_x = preview_pad + len(preview_title) + 2
                if title_x < width - 1:
                    stdscr.addnstr(
                        preview_start + 1,
                        title_x,
                        _truncate(f"[ {domain_label} ]", max(0, width - 1 - title_x)),
                        max(0, width - 1 - title_x),
                        sender_domain_value_attr,
                    )
                for idx, item in enumerate(preview_rows[:panel_capacity]):
                    subject = item[0] if len(item) > 0 else ""
                    first_line = item[1] if len(item) > 1 else ""
                    date_recv = item[2] if len(item) > 2 else ""
                    y = preview_start + 3 + idx
                    if y >= height - 3:
                        break
                    date_prefix = f"[{_mm_yy(date_recv)}]"
                    subject_max = max(18, width // 3)
                    left = _clip(f"{subject.strip() or '(no subject)'}", subject_max)
                    right = _clip(first_line.strip() or "-", max(8, width - len(left) - len(date_prefix) - 4))
                    stdscr.addnstr(y, preview_pad, date_prefix, max(0, width - 1 - preview_pad), preview_date_attr)
                    subj_x = min(width - 1, preview_pad + len(date_prefix) + 1)
                    stdscr.addnstr(y, subj_x, left, max(0, width - 1 - subj_x), selected_attr)
                    sep_x = min(width - 1, subj_x + len(left) + 1)
                    stdscr.addnstr(y, sep_x, f" {right}", max(0, width - 1 - sep_x), hint_attr)
            # Visual buffer before footer separator.
            stdscr.addnstr(height - 3, 0, " " * max(1, width - 1), width - 1, pipe_attr)
            stdscr.addnstr(height - 2, 0, "─" * max(1, width - 1), width - 1, curses.A_DIM)
            # Footer: all controls and settings are consolidated here.
            x = 0
            if sort_mode_active:
                if ultra_compact_footer:
                    sort_footer_segments = [
                        ("Sort ", hint_attr),
                        ("[s]", footer_hotkey_attr),
                        (" ON ", on_attr),
                        ("| ", pipe_attr),
                        ("\u2190/\u2192 ", desc_attr),
                        ("| ", pipe_attr),
                        ("SPACE", footer_hotkey_attr),
                    ]
                elif compact_footer:
                    sort_footer_segments = [
                        ("Sort ", hint_attr),
                        ("[s]", footer_hotkey_attr),
                        (" ON ", on_attr),
                        ("| ", pipe_attr),
                        ("\u2190/\u2192 col ", desc_attr),
                        ("| ", pipe_attr),
                        ("SPACE", footer_hotkey_attr),
                        (" cycle", desc_attr),
                    ]
                else:
                    sort_footer_segments = [
                        ("Sort Mode ", hint_attr),
                        ("[s]", footer_hotkey_attr),
                        (" ON ", on_attr),
                        ("| ", pipe_attr),
                        ("\u2190/\u2192 choose column ", desc_attr),
                        ("| ", pipe_attr),
                        ("SPACE", footer_hotkey_attr),
                        (" toggle asc/desc/off", desc_attr),
                    ]
            else:
                if ultra_compact_footer:
                    sort_footer_segments = [
                        ("Sort ", hint_attr),
                        ("[s]", footer_hotkey_attr),
                    ]
                elif compact_footer:
                    sort_footer_segments = [
                        ("Sort ", hint_attr),
                        ("[s]", footer_hotkey_attr),
                        (" mode", desc_attr),
                    ]
                else:
                    sort_footer_segments = [
                        ("Sort Mode ", hint_attr),
                        ("[s]", footer_hotkey_attr),
                    ]

            if ultra_compact_footer:
                footer_segments = [
                    ("Pres ", hint_attr),
                    ("[p]", footer_hotkey_attr),
                    (": ", hint_attr),
                    (("ON" if preserve_flagged else "OFF"), (on_attr if preserve_flagged else off_attr)),
                    (" ║ ", pipe_attr),
                    ("Run ", selected_attr),
                    ("[ENTER]", footer_hotkey_attr),
                    (" | ", pipe_attr),
                    ("Cancel ", off_attr),
                    ("[q]", footer_hotkey_attr),
                ]
            elif compact_footer:
                footer_segments = [
                    ("Preserve ", hint_attr),
                    ("[p]", footer_hotkey_attr),
                    (": ", hint_attr),
                    (("ON" if preserve_flagged else "OFF"), (on_attr if preserve_flagged else off_attr)),
                    (" ║ ", pipe_attr),
                    ("All ", hint_attr),
                    ("[a]", footer_hotkey_attr),
                    (" | ", pipe_attr),
                    ("Clear ", hint_attr),
                    ("[c]", footer_hotkey_attr),
                    (" ║ ", pipe_attr),
                    ("Run ", selected_attr),
                    ("[ENTER]", footer_hotkey_attr),
                    (" | ", pipe_attr),
                    ("Cancel ", off_attr),
                    ("[q]", footer_hotkey_attr),
                ]
            else:
                footer_segments = [
                    ("Preserve Flagged ", hint_attr),
                    ("[p]", footer_hotkey_attr),
                    (": ", hint_attr),
                    (("ON" if preserve_flagged else "OFF"), (on_attr if preserve_flagged else off_attr)),
                    (" ║ ", pipe_attr),
                    ("All ", hint_attr),
                    ("[a]", footer_hotkey_attr),
                    (" | ", pipe_attr),
                    ("Clear ", hint_attr),
                    ("[c]", footer_hotkey_attr),
                    (" ║ ", pipe_attr),
                    ("Run ", selected_attr),
                    ("[ENTER]", footer_hotkey_attr),
                    (" | ", pipe_attr),
                    ("Cancel ", off_attr),
                    ("[q]", footer_hotkey_attr),
                ]
            footer_segments = sort_footer_segments + [(" ║ ", pipe_attr)] + footer_segments
            for text, attr in footer_segments:
                if x >= width - 1:
                    break
                stdscr.addnstr(height - 1, x, text, max(0, width - 1 - x), attr)
                x += len(text)
            stdscr.refresh()

            key = stdscr.getch()
            if sort_mode_active:
                # Dedicated sort editor keymap while sort mode is active.
                if key in (ord("s"), ord("S")):
                    sort_mode_active = False
                elif key in (curses.KEY_LEFT, curses.KEY_UP):
                    sort_cursor_idx = (sort_cursor_idx - 1) % len(sortable_keys)
                elif key in (curses.KEY_RIGHT, curses.KEY_DOWN):
                    sort_cursor_idx = (sort_cursor_idx + 1) % len(sortable_keys)
                elif key == ord(" "):
                    active_sort_key = sortable_keys[sort_cursor_idx]
                    # Tri-state cycle: ascending -> descending -> off.
                    if sort_key != active_sort_key:
                        sort_key = active_sort_key
                        sort_desc = False
                    elif not sort_desc:
                        sort_desc = True
                    else:
                        sort_key = None
                elif key in (curses.KEY_HOME,):
                    sort_cursor_idx = 0
                elif key in (curses.KEY_END,):
                    sort_cursor_idx = len(sortable_keys) - 1
                elif key in (27, ord("q")):
                    break
                # Ignore unrelated keys while editing sort cells.
                if singlebox_mode:
                    cursor_singlebox = cursor
                    offset_singlebox = offset
                else:
                    cursor_by_mailbox[active_mailbox] = cursor
                    offset_by_mailbox[active_mailbox] = offset
                continue

            if key in (curses.KEY_UP, ord("k")):
                cursor = max(0, cursor - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                cursor = min(max(0, len(selectable_indices) - 1), cursor + 1)
            elif key == curses.KEY_LEFT:
                if not singlebox_mode:
                    active_mailbox_idx = (active_mailbox_idx - 1) % len(ordered_mailboxes)
            elif key == curses.KEY_RIGHT:
                if not singlebox_mode:
                    active_mailbox_idx = (active_mailbox_idx + 1) % len(ordered_mailboxes)
            elif key in (ord("t"), ord("T")):
                # Toggle between per-tab and collapsed singlebox layouts.
                singlebox_mode = not singlebox_mode
                if not singlebox_mode and active_entry_index >= 0:
                    active_opt = render_rows[active_entry_index][1]
                    if active_opt is not None and active_opt.mailbox in ordered_mailboxes:
                        active_mailbox_idx = ordered_mailboxes.index(active_opt.mailbox)
            elif key in (9, curses.KEY_BTAB):
                if active_entry_index < 0:
                    continue
                active_opt = render_rows[active_entry_index][1]
                if active_opt is None:
                    continue
                row_key = (active_opt.mailbox or active_mailbox, active_opt.domain)
                mode = selection_mode_by_row.get(row_key)
                if mode == "normal":
                    selection_mode_by_row[row_key] = "full"
                elif mode == "full":
                    selection_mode_by_row.pop(row_key, None)
                else:
                    selection_mode_by_row[row_key] = "normal"
            elif key in (ord("a"),):
                # Select all visible mailbox/domain rows.
                selection_mode_by_row.clear()
                for opt in options:
                    selection_mode_by_row[(opt.mailbox or "INBOX", opt.domain)] = "normal"
            elif key in (ord("c"), ord("C")):
                selection_mode_by_row.clear()
            elif key in (ord("p"), ord("P")):
                preserve_flagged = not preserve_flagged
            elif key in (ord("h"), ord("H")):
                hide_flagged_from_view = not hide_flagged_from_view
            elif key in (ord("f"), ord("F")):
                edit_filter_requested = True
                break
            elif key in (ord("s"), ord("S")):
                # Enter sort mode and align cursor with current active sort.
                sort_mode_active = True
                if sort_key in sortable_keys:
                    sort_cursor_idx = sortable_keys.index(sort_key)
            elif key in (10, 13, curses.KEY_ENTER):
                review_before_exit = True
            elif key in (27, ord("q")):
                break

            if singlebox_mode:
                cursor_singlebox = cursor
                offset_singlebox = offset
            else:
                cursor_by_mailbox[active_mailbox] = cursor
                offset_by_mailbox[active_mailbox] = offset

    curses.wrapper(_render)
    if edit_filter_requested:
        return SelectionResult(
            selected_domains=[],
            full_remove_domains=[],
            selected_rows=[],
            full_remove_rows=[],
            preserve_flagged=preserve_flagged,
            edit_filter_requested=True,
        )
    if not confirmed:
        return SelectionResult(
            selected_domains=[],
            full_remove_domains=[],
            selected_rows=[],
            full_remove_rows=[],
            preserve_flagged=preserve_flagged,
        )
    ordered_rows: list[tuple[str, str]] = []
    ordered_full_remove_rows: list[tuple[str, str]] = []
    seen_rows: set[tuple[str, str]] = set()
    ordered_domains: list[str] = []
    ordered_full_remove_domains: list[str] = []
    seen_domains: set[str] = set()
    for opt in options:
        row_key = (opt.mailbox or "INBOX", opt.domain)
        mode = selection_mode_by_row.get(row_key)
        if mode and row_key not in seen_rows:
            seen_rows.add(row_key)
            ordered_rows.append(row_key)
            if opt.domain not in seen_domains:
                seen_domains.add(opt.domain)
                ordered_domains.append(opt.domain)
            if mode == "full":
                ordered_full_remove_rows.append(row_key)
                if opt.domain not in ordered_full_remove_domains:
                    ordered_full_remove_domains.append(opt.domain)
    return SelectionResult(
        selected_domains=ordered_domains,
        full_remove_domains=ordered_full_remove_domains,
        selected_rows=ordered_rows,
        full_remove_rows=ordered_full_remove_rows,
        preserve_flagged=preserve_flagged,
    )
