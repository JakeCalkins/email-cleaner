from __future__ import annotations

"""CLI entrypoint for mailzero.

High-level lifecycle:
1) Launch dashboard: rebuild local index from Mail metadata and show a plan.
2) From dashboard: enter RUN mode to choose domains and apply archive actions.
3) `--undo`: read last run log and attempt to reverse successful actions.
"""

import argparse
import curses
import re
import signal
import sys
from pathlib import Path

from . import db
from .filter_expr import FilterSyntaxError, compile_custom_filter
from .ui import ANSI_RE, RunProgressDisplay, accent, box, color, dim, good, warn
from .workflow import (
    MIN_DOMAIN_TOTAL_FOR_TABLE,
    default_paths,
    render_dry_run_report,
    render_run_report,
    render_undo_report,
    run_apply_from_dry_run,
    run_dry_run,
    run_undo,
    set_run_progress_callback,
)

_DASHBOARD_EXIT_SIGNALS: tuple[int, ...] = (
    signal.SIGINT,
    *(() if not hasattr(signal, "SIGTSTP") else (signal.SIGTSTP,)),
)
_FILTER_PUNCT = set("():=!*,\"'")
_FILTER_KEYWORDS = {"EXCLUDE", "ONLY_INCLUDE", "AND", "OR", "NOT"}
_FILTER_FIELDS = {"FROM", "TO", "SUBJECT", "BEFORE", "AFTER", "DATE"}
_FILTER_LEGEND: tuple[tuple[str, str], ...] = (
    ("MODE", "EXCLUDE or ONLY_INCLUDE (default: ONLY_INCLUDE)"),
    ("FIELDS", "FROM, TO, SUBJECT, BEFORE, AFTER (optional DATE token)"),
    ("BOOLEAN", "AND, OR, NOT, !   |   Parentheses supported: ( ... )"),
    ("MATCH", "Use : or =   |   Wildcard * in values"),
    ("DATE", "MM/DD/YY only; year is interpreted as 20YY"),
    ("EXAMPLE", 'ONLY_INCLUDE (FROM:*@apple.com OR SUBJECT:"receipt") AND AFTER:01/01/25'),
)


def build_parser() -> argparse.ArgumentParser:
    # Keep parser intentionally small: default dashboard mode + undo, with a
    # hidden db override used mainly for testing/development.
    parser = argparse.ArgumentParser(
        prog="mailzero",
        description=(
            "Inbox-zero workflow for macOS Mail (All Inboxes only). "
            "Launches a dashboard plan by default; enter RUN mode from there."
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=db.default_db_path(),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--undo",
        action="store_true",
        help="Undo the last RUN-mode operation using the saved backup log.",
    )
    parser.add_argument(
        "--testing",
        action="store_true",
        help="Use testing mode (safe local fixture data, no live Mail writes).",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help=(
            "Optional custom source path. In real mode: explicit Envelope Index path. "
            "In testing mode: shown as source label."
        ),
    )
    return parser


def _clear_screen() -> None:
    # ANSI clear is used instead of shelling out to keep startup latency low.
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")


def _wait_for_enter(message: str = "Press Enter to continue") -> bool:
    """Pause until Enter; allow quick quit with `q`.

    Returns:
        True when the user asked to quit, else False.
    """
    if not sys.stdin.isatty():
        return False
    raw = input(f"{message} (or q to quit): ").strip().lower()
    return raw in {"q", "quit", "exit"}


def _highlight_filter(expr: str) -> str:
    """Syntax-highlight custom filter expression with token background colors."""
    if not expr.strip():
        return color(" (none) ", "30;100")
    parts = re.split(r"(\s+|\(|\)|:|=|!|\*)", expr)
    out: list[str] = []
    for part in parts:
        if part == "":
            continue
        up = part.upper()
        if up in {"EXCLUDE", "ONLY_INCLUDE", "AND", "OR", "NOT"}:
            out.append(color(part, "30;103"))
        elif up in {"FROM", "TO", "SUBJECT", "BEFORE", "AFTER", "DATE"}:
            out.append(color(part, "30;106"))
        elif part in {"(", ")", ":", "=", "!"}:
            out.append(color(part, "30;107"))
        elif part == "*":
            out.append(color(part, "30;102"))
        else:
            out.append(color(part, "30;47"))
    return " " + "".join(out) + " "


def _render_dashboard_controls(
    custom_filter_query: str,
    *,
    testing_mode: bool,
    source_path: Path | None,
    show_help_panel: bool,
    extra_info_lines: list[str] | None = None,
) -> str:
    """Render a styled control footer for the launch dashboard."""

    def _visible_len(text: str) -> int:
        return len(ANSI_RE.sub("", text))

    def _ljust_visible(text: str, width: int) -> str:
        pad = max(0, width - _visible_len(text))
        return text + (" " * pad)

    def _cell(title: str, hotkey: str) -> str:
        return f"{dim('[' + hotkey + ']')} {title}"

    def _row(left: str, right: str, col_w: int = 40) -> str:
        return f"{_ljust_visible(left, col_w)}{right}"

    controls_lines = [
        _row(
            _cell("Set Minimum Emails", "m"),
            _cell("Edit Custom Filter", "f"),
        ),
        _row(
            _cell("Undo Last Run", "u"),
            _cell("More Info", "?"),
        ),
    ]

    settings_lines = [
        (f"Mode: {warn('testing')}" if testing_mode else ""),
        (f"Custom Source: {str(source_path)}" if source_path is not None else ""),
    ]
    settings_compact = [line for line in settings_lines if line]

    filter_lines = [
        f"Current Filter: {_highlight_filter(custom_filter_query)}",
    ]

    help_lines = [
        "Hotkeys: [m] minimum emails, [f] edit filter, [r] run, [u] undo, [q] quit",
    ]
    if extra_info_lines:
        help_lines.extend(["", *extra_info_lines])

    blocks = [
        box("Dashboard Controls", [*settings_compact, *([""] if settings_compact else []), *controls_lines]),
        "",
        box("Custom Filter", filter_lines),
    ]
    if show_help_panel:
        blocks.extend(["", box("Help", help_lines)])
    blocks.extend(
        [
            "",
            good("Enter RUN mode by entering [r]...") + " " + color("([q] to quit)", "3;31"),
        ]
    )
    return "\n".join(blocks)


def _word_left(text: str, cursor: int) -> int:
    i = max(0, min(cursor, len(text)))
    while i > 0 and text[i - 1].isspace():
        i -= 1
    while i > 0 and (not text[i - 1].isspace()):
        i -= 1
    return i


def _word_right(text: str, cursor: int) -> int:
    i = max(0, min(cursor, len(text)))
    n = len(text)
    while i < n and text[i].isspace():
        i += 1
    while i < n and (not text[i].isspace()):
        i += 1
    return i


def _editor_segments(expr: str) -> list[tuple[str, str]]:
    parts = re.split(r"(\s+|\(|\)|:|=|!|\*)", expr)
    segments: list[tuple[str, str]] = []
    # Ignore the currently typed trailing word until punctuation/space commits it.
    trailing_pending = bool(expr) and (not expr[-1].isspace()) and (expr[-1] not in _FILTER_PUNCT)
    last_word_index = -1
    if trailing_pending:
        for idx in range(len(parts) - 1, -1, -1):
            token = parts[idx]
            if token and (not token.isspace()) and token not in {"(", ")", ":", "=", "!", "*"}:
                last_word_index = idx
                break
    for idx, part in enumerate(parts):
        if part == "":
            continue
        if part.isspace():
            segments.append((part, "space"))
            continue
        if part in {"(", ")", ":", "=", "!", "*"}:
            segments.append((part, "punct"))
            continue
        if idx == last_word_index:
            segments.append((part, "pending"))
            continue
        up = part.upper()
        if up in _FILTER_KEYWORDS:
            segments.append((part, "keyword"))
        elif up in _FILTER_FIELDS:
            segments.append((part, "field"))
        else:
            segments.append((part, "value"))
    return segments


def _validate_filter_syntax(expr: str) -> str:
    """Return a user-facing syntax message without raising."""
    raw = expr.strip()
    if not raw:
        return ""
    try:
        compile_custom_filter(raw)
    except FilterSyntaxError as exc:
        return f"Syntax error: {exc}"
    except Exception as exc:
        return f"Syntax error: {exc}"
    return ""


def _safe_addnstr(stdscr: curses.window, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
    """Best-effort draw helper that avoids tiny-screen curses crashes."""
    if width <= 0:
        return
    try:
        stdscr.addnstr(y, x, text, width, attr)
    except curses.error:
        # Tiny terminal geometry can reject writes near edges; ignore safely.
        return


def _clip_text(text: str, max_len: int) -> str:
    """Hard-clip helper for curses lines where ellipsis is unnecessary."""
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len]


def _read_meta_sequence(stdscr: curses.window) -> list[int]:
    """Read trailing bytes after ESC to detect alt/meta and modified arrows."""
    seq: list[int] = []
    stdscr.nodelay(True)
    try:
        for _ in range(8):
            nxt = stdscr.getch()
            if nxt == -1:
                break
            seq.append(nxt)
    finally:
        stdscr.nodelay(False)
    return seq


def _delete_word_left(text: str, cursor: int) -> tuple[str, int]:
    left = _word_left(text, cursor)
    return text[:left] + text[cursor:], left


def _delete_word_right(text: str, cursor: int) -> tuple[str, int]:
    right = _word_right(text, cursor)
    return text[:cursor] + text[right:], cursor


def _edit_filter_interactive(current_filter: str) -> str | None:
    """Curses line editor with live syntax color and non-crashing validation."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return current_filter

    state = {"text": current_filter, "cursor": len(current_filter), "error": _validate_filter_syntax(current_filter)}

    def _editor(stdscr: curses.window) -> None:
        curses.curs_set(1)
        stdscr.keypad(True)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_YELLOW, -1)   # punctuation
            curses.init_pair(2, curses.COLOR_GREEN, -1)    # keywords
            curses.init_pair(3, curses.COLOR_CYAN, -1)     # fields
            curses.init_pair(4, curses.COLOR_WHITE, -1)    # values
            curses.init_pair(5, curses.COLOR_RED, -1)      # errors
            curses.init_pair(6, curses.COLOR_MAGENTA, -1)  # title
        while True:
            h, w = stdscr.getmaxyx()
            stdscr.erase()
            title_attr = curses.A_BOLD | (curses.color_pair(6) if curses.has_colors() else 0)
            _safe_addnstr(stdscr, 0, 0, "Edit Filter", max(0, w - 1), title_attr)
            if state["error"]:
                _safe_addnstr(
                    stdscr,
                    2, 0, state["error"], max(0, w - 1), curses.A_BOLD | (curses.color_pair(5) if curses.has_colors() else 0)
                )

            controls_y = 3
            controls_x = 0
            key_hint_attr = curses.A_DIM | (curses.color_pair(4) if curses.has_colors() else 0)
            text_hint_attr = curses.A_DIM
            save_attr = curses.A_BOLD | (curses.color_pair(2) if curses.has_colors() else 0)
            exit_attr = curses.A_BOLD | (curses.color_pair(5) if curses.has_colors() else 0)
            controls_segments = [
                ("← →", key_hint_attr),
                (" to navigate, ", text_hint_attr),
                ("[enter]", key_hint_attr),
                (" to ", text_hint_attr),
                ("save", save_attr),
                (", ", text_hint_attr),
                ("[esc]", key_hint_attr),
                (" to ", text_hint_attr),
                ("exit", exit_attr),
            ]
            for seg_text, seg_attr in controls_segments:
                if controls_x >= w - 1:
                    break
                _safe_addnstr(stdscr, controls_y, controls_x, seg_text, max(0, w - 1 - controls_x), seg_attr)
                controls_x += len(seg_text)

            prompt = "filter> "
            y = 4
            _safe_addnstr(stdscr, y, 0, prompt, max(0, w - 1), curses.A_BOLD)
            x = len(prompt)
            for text, kind in _editor_segments(state["text"]):
                if x >= w - 1:
                    break
                if kind == "keyword":
                    attr = curses.A_BOLD | (curses.color_pair(2) if curses.has_colors() else 0)
                elif kind == "field":
                    attr = curses.A_BOLD | (curses.color_pair(3) if curses.has_colors() else 0)
                elif kind == "punct":
                    attr = curses.A_BOLD | (curses.color_pair(1) if curses.has_colors() else 0)
                elif kind == "pending":
                    attr = curses.A_ITALIC if hasattr(curses, "A_ITALIC") else curses.A_DIM
                else:
                    attr = curses.color_pair(4) if curses.has_colors() else 0
                _safe_addnstr(stdscr, y, x, text, max(0, w - 1 - x), attr)
                x += len(text)

            # Cursor placement on single-line editor.
            visible_width = max(1, w - 1 - len(prompt))
            scroll_x = 0
            if state["cursor"] >= visible_width:
                scroll_x = state["cursor"] - visible_width + 1
            if scroll_x > 0:
                # Redraw with horizontal scroll for long expressions.
                _safe_addnstr(stdscr, y, len(prompt), " " * visible_width, visible_width, 0)
                x = len(prompt)
                logical_x = 0
                for text, kind in _editor_segments(state["text"]):
                    seg_start = logical_x
                    seg_end = logical_x + len(text)
                    logical_x = seg_end
                    if seg_end <= scroll_x:
                        continue
                    if seg_start >= scroll_x + visible_width:
                        break
                    cut_l = max(0, scroll_x - seg_start)
                    cut_r = max(0, seg_end - (scroll_x + visible_width))
                    visible_text = text[cut_l : len(text) - cut_r if cut_r else len(text)]
                    if not visible_text:
                        continue
                    if kind == "keyword":
                        attr = curses.A_BOLD | (curses.color_pair(2) if curses.has_colors() else 0)
                    elif kind == "field":
                        attr = curses.A_BOLD | (curses.color_pair(3) if curses.has_colors() else 0)
                    elif kind == "punct":
                        attr = curses.A_BOLD | (curses.color_pair(1) if curses.has_colors() else 0)
                    elif kind == "pending":
                        attr = curses.A_ITALIC if hasattr(curses, "A_ITALIC") else curses.A_DIM
                    else:
                        attr = curses.color_pair(4) if curses.has_colors() else 0
                    _safe_addnstr(stdscr, y, x, visible_text, max(0, w - 1 - x), attr)
                    x += len(visible_text)

            cursor_x = min(w - 1, len(prompt) + max(0, state["cursor"] - scroll_x))

            ly = y + 2
            for title, body in _FILTER_LEGEND:
                if ly >= h:
                    break
                title_text = f"{title}: "
                _safe_addnstr(
                    stdscr,
                    ly,
                    0,
                    title_text,
                    max(0, w - 1),
                    curses.A_BOLD | (curses.color_pair(3) if curses.has_colors() else 0),
                )
                _safe_addnstr(
                    stdscr,
                    ly,
                    len(title_text),
                    _clip_text(body, max(0, w - 1 - len(title_text))),
                    max(0, w - 1 - len(title_text)),
                    curses.A_DIM,
                )
                ly += 1

            # Keep the visible terminal cursor anchored to the filter input
            # after all helper text draws, since addnstr advances cursor state.
            try:
                stdscr.move(y, cursor_x)
            except curses.error:
                pass

            stdscr.refresh()
            key = stdscr.getch()
            text = state["text"]
            cur = state["cursor"]

            if key in (10, 13, curses.KEY_ENTER):
                err = _validate_filter_syntax(text)
                state["error"] = err
                if err:
                    continue
                return
            if key in (27,):
                seq = _read_meta_sequence(stdscr)
                if not seq:
                    state["text"] = current_filter
                    state["cursor"] = len(current_filter)
                    state["error"] = "__CANCEL__"
                    return
                # Alt/Meta key handling (terminal dependent).
                if seq in ([98],):  # Alt+B
                    state["cursor"] = _word_left(text, cur)
                    continue
                if seq in ([102],):  # Alt+F
                    state["cursor"] = _word_right(text, cur)
                    continue
                if seq in ([100],):  # Alt+D
                    state["text"], state["cursor"] = _delete_word_right(text, cur)
                    state["error"] = _validate_filter_syntax(state["text"])
                    continue
                if seq in ([127],):  # Alt+Backspace
                    state["text"], state["cursor"] = _delete_word_left(text, cur)
                    state["error"] = _validate_filter_syntax(state["text"])
                    continue
                if seq == [91, 49, 59, 53, 68]:  # Ctrl+Left
                    state["cursor"] = _word_left(text, cur)
                    continue
                if seq == [91, 49, 59, 53, 67]:  # Ctrl+Right
                    state["cursor"] = _word_right(text, cur)
                    continue
                if seq == [91, 49, 59, 50, 68]:  # Shift+Left
                    state["cursor"] = max(0, cur - 1)
                    continue
                if seq == [91, 49, 59, 50, 67]:  # Shift+Right
                    state["cursor"] = min(len(text), cur + 1)
                    continue
                # Any unknown ESC sequence should behave like cancel so plain
                # Escape is always reliable across terminal variants.
                state["text"] = current_filter
                state["cursor"] = len(current_filter)
                state["error"] = "__CANCEL__"
                return
            if key in (curses.KEY_LEFT,):
                state["cursor"] = max(0, cur - 1)
                continue
            if key in (curses.KEY_RIGHT,):
                state["cursor"] = min(len(text), cur + 1)
                continue
            if key in (getattr(curses, "KEY_SLEFT", -1),):
                state["cursor"] = max(0, cur - 1)
                continue
            if key in (getattr(curses, "KEY_SRIGHT", -1),):
                state["cursor"] = min(len(text), cur + 1)
                continue
            if key in (getattr(curses, "KEY_CLEFT", -1),):
                state["cursor"] = _word_left(text, cur)
                continue
            if key in (getattr(curses, "KEY_CRIGHT", -1),):
                state["cursor"] = _word_right(text, cur)
                continue
            if key in (curses.KEY_HOME, 1):  # Home / Ctrl+A
                state["cursor"] = 0
                continue
            if key in (curses.KEY_END, 5):  # End / Ctrl+E
                state["cursor"] = len(text)
                continue
            if key == 23:  # Ctrl+W delete previous word
                state["text"], state["cursor"] = _delete_word_left(text, cur)
                state["error"] = _validate_filter_syntax(state["text"])
                continue
            if key == 21:  # Ctrl+U kill to start
                state["text"] = text[cur:]
                state["cursor"] = 0
                state["error"] = _validate_filter_syntax(state["text"])
                continue
            if key == 11:  # Ctrl+K kill to end
                state["text"] = text[:cur]
                state["error"] = _validate_filter_syntax(state["text"])
                continue
            if key in (curses.KEY_BACKSPACE, 127, 8):
                if cur > 0:
                    state["text"] = text[: cur - 1] + text[cur:]
                    state["cursor"] = cur - 1
                    state["error"] = _validate_filter_syntax(state["text"])
                continue
            if key == curses.KEY_DC:
                if cur < len(text):
                    state["text"] = text[:cur] + text[cur + 1 :]
                    state["error"] = _validate_filter_syntax(state["text"])
                continue
            if key == 2:  # Ctrl+B word-left
                state["cursor"] = _word_left(text, cur)
                continue
            if key == 6:  # Ctrl+F word-right
                state["cursor"] = _word_right(text, cur)
                continue
            if 32 <= key <= 126:
                ch = chr(key)
                state["text"] = text[:cur] + ch + text[cur:]
                state["cursor"] = cur + 1
                state["error"] = _validate_filter_syntax(state["text"])
                continue

    curses.wrapper(_editor)
    if state["error"] == "__CANCEL__":
        return None
    return state["text"]


def _run_with_progress(
    paths,
    min_domain_total: int | None,
    custom_filter_query: str | None,
) -> dict:
    progress = RunProgressDisplay()

    def _callback(event: dict) -> None:
        # Workflow emits coarse-grained progress events as dictionaries.
        # We normalize types defensively before rendering.
        progress.update(
            current=int(event.get("current", 0)),
            total=int(event.get("total", 0)),
            status=str(event.get("status", "")),
            detail=str(event.get("detail", "")),
        )

    set_run_progress_callback(_callback)
    try:
        return run_apply_from_dry_run(
            paths=paths,
            min_domain_total=min_domain_total,
            custom_filter_query=custom_filter_query,
        )
    finally:
        set_run_progress_callback(None)
        # Always restore terminal state before printing final report.
        progress.finish()


def _dashboard_session(
    db_path: Path,
    paths,
    *,
    testing_mode: bool,
    source_path: Path | None,
) -> int:
    """Interactive launch mode that merges former dry-run and run entrypoints."""
    threshold = MIN_DOMAIN_TOTAL_FOR_TABLE
    custom_filter_query = ""
    show_help_panel = False
    payload: dict | None = None
    needs_refresh = True
    previous_handlers: dict[int, signal.Handlers] = {}

    def _exit_handler(_signum, _frame) -> None:
        raise KeyboardInterrupt()

    try:
        # Treat Ctrl/Cmd+C and Ctrl/Cmd+Z like a soft "quit dashboard".
        for sig in _DASHBOARD_EXIT_SIGNALS:
            previous_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, _exit_handler)

        while True:
            if needs_refresh:
                payload = run_dry_run(
                    db_path=db_path,
                    paths=paths,
                    min_domain_total=threshold,
                    custom_filter_query=custom_filter_query,
                    data_mode=("testing" if testing_mode else "real"),
                    source_path=source_path,
                )
                needs_refresh = False

            _clear_screen()
            assert payload is not None
            print(render_dry_run_report(payload))
            print()
            print(
                _render_dashboard_controls(
                    custom_filter_query,
                    testing_mode=testing_mode,
                    source_path=source_path,
                    show_help_panel=show_help_panel,
                    extra_info_lines=[
                        f"Plan file: {paths.dry_run_file}",
                        "All Inboxes scanned: yes",
                        "Refresh mode: full_rebuild",
                    ],
                )
            )
            try:
                choice = (
                    input(accent("Enter a key to choose an action: ")).strip().lower()
                    if sys.stdin.isatty()
                    else "q"
                )
            except (KeyboardInterrupt, EOFError):
                return 0

            if choice in {"q", "quit", "exit"}:
                return 0
            if choice in {"?"}:
                show_help_panel = not show_help_panel
                continue
            if choice in {""}:
                needs_refresh = True
                continue
            if choice in {"m", "min", "threshold"}:
                raw = input("Set minimum emails per sender domain (>=1): ").strip()
                try:
                    new_threshold = int(raw)
                    if new_threshold < 1:
                        raise ValueError
                except ValueError:
                    print("Invalid value. Enter an integer >= 1.")
                    if _wait_for_enter():
                        return 0
                    continue
                threshold = new_threshold
                needs_refresh = True
                continue
            if choice in {"f", "filter"}:
                edited = _edit_filter_interactive(custom_filter_query)
                if edited is None:
                    continue
                raw = edited.strip()
                if not raw:
                    custom_filter_query = ""
                    needs_refresh = True
                    continue
                try:
                    compile_custom_filter(raw)
                except FilterSyntaxError as exc:
                    print(f"Filter syntax error: {exc}")
                    if _wait_for_enter():
                        return 0
                    continue
                custom_filter_query = raw
                needs_refresh = True
                continue
            if choice in {"u", "undo"}:
                undo_record = run_undo(paths=paths)
                _clear_screen()
                print(render_undo_report(undo_record))
                if _wait_for_enter():
                    return 0
                needs_refresh = True
                continue
            if choice in {"r", "run"}:
                run_record = _run_with_progress(
                    paths=paths,
                    min_domain_total=threshold,
                    custom_filter_query=custom_filter_query,
                )
                if bool(run_record.get("filter_edit_requested")):
                    edited = _edit_filter_interactive(custom_filter_query)
                    if edited is None:
                        needs_refresh = True
                        continue
                    raw = edited.strip()
                    if not raw:
                        custom_filter_query = ""
                    else:
                        try:
                            compile_custom_filter(raw)
                        except FilterSyntaxError as exc:
                            print(f"Filter syntax error: {exc}")
                            if _wait_for_enter():
                                return 0
                            continue
                        custom_filter_query = raw
                    needs_refresh = True
                    continue
                _clear_screen()
                print(render_run_report(run_record, backup_file=paths.last_run_file))
                if _wait_for_enter():
                    return 0
                needs_refresh = True
                continue

            print(f"Unknown action: {choice}")
            if _wait_for_enter():
                return 0
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def main() -> int:
    # Main is intentionally imperative so control flow is obvious in CLI logs
    # and exception handling is centralized in one place.
    parser = build_parser()
    args = parser.parse_args()
    paths = default_paths()
    try:
        if args.undo:
            undo_record = run_undo(paths=paths)
            print(render_undo_report(undo_record))
            return 0
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            # Non-interactive fallback still emits useful plan output.
            payload = run_dry_run(
                db_path=args.db,
                paths=paths,
                min_domain_total=MIN_DOMAIN_TOTAL_FOR_TABLE,
                data_mode=("testing" if args.testing else "real"),
                source_path=args.source,
            )
            print(render_dry_run_report(payload))
            return 0
        return _dashboard_session(
            db_path=args.db,
            paths=paths,
            testing_mode=bool(args.testing),
            source_path=args.source,
        )
    except Exception as exc:
        # Single-line stderr output keeps failures readable when users paste logs.
        print(f"Error: {exc}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
