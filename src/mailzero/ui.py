from __future__ import annotations

"""Terminal rendering helpers.

All helpers here are pure formatting primitives except `RunProgressDisplay`,
which owns live terminal repaint behavior during RUN execution.
"""

import os
import re
import sys
from collections import deque
from typing import Any

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _use_color() -> bool:
    # Respect NO_COLOR convention and only emit ANSI on TTYs.
    if os.getenv("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def color(text: str, code: str) -> str:
    if not _use_color():
        return text
    return f"\033[{code}m{text}\033[0m"


def dim(text: str) -> str:
    return color(text, "2")


def bold(text: str) -> str:
    return color(text, "1")


def good(text: str) -> str:
    return color(text, "32")


def warn(text: str) -> str:
    return color(text, "33")


def bad(text: str) -> str:
    return color(text, "31")


def accent(text: str) -> str:
    return color(text, "36")


def rule(width: int = 72, char: str = "─") -> str:
    return dim(char * width)


def app_header(title: str, subtitle: str | None = None) -> str:
    lines = [bold(accent(f"◆ {title}")), rule()]
    if subtitle:
        lines.append(dim(subtitle))
        lines.append("")
    return "\n".join(lines)


def box(title: str, lines: list[str], width: int = 72) -> str:
    # Width calculations strip ANSI sequences so colored content still aligns.
    expanded: list[str] = []
    for line in lines:
        for sub in line.splitlines():
            expanded.append(sub)
    visible_lengths = [len(ANSI_RE.sub("", line)) for line in expanded] or [0]
    inner_width = max(max(visible_lengths), width - 4)
    inner_width = max(inner_width, len(title) + 1)
    top = f"┌─ {title} " + "─" * max(0, inner_width - len(title) - 1) + "┐"
    body: list[str] = []
    for line in expanded:
        visible_len = len(ANSI_RE.sub("", line))
        padding = max(0, inner_width - visible_len)
        body.append(f"│ {line}{' ' * padding} │")
    bottom = "└" + "─" * (inner_width + 2) + "┘"
    return "\n".join([dim(top), *body, dim(bottom)])


def table(headers: list[str], rows: list[list[Any]], aligns: list[str] | None = None) -> str:
    if not rows:
        return dim("(none)")
    if aligns is None:
        aligns = ["left"] * len(headers)
    else:
        aligns = [a.lower() for a in aligns]
        if len(aligns) < len(headers):
            aligns = aligns + (["left"] * (len(headers) - len(aligns)))
        elif len(aligns) > len(headers):
            aligns = aligns[: len(headers)]
    # Compute visible widths (sans ANSI) so columns align under color styling.
    widths = [len(ANSI_RE.sub("", h)) for h in headers]
    for row in rows:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], len(ANSI_RE.sub("", str(value))))
    sep = "  "
    header_parts: list[str] = []
    for i, h in enumerate(headers):
        text = str(h)
        justified = text.rjust(widths[i]) if aligns[i] == "right" else text.ljust(widths[i])
        header_parts.append(accent(bold(justified)))
    header = sep.join(header_parts)
    divider = dim(sep.join("─" * widths[i] for i in range(len(headers))))
    body: list[str] = []
    for row in rows:
        parts: list[str] = []
        for i, value in enumerate(row):
            text = str(value)
            vis = len(ANSI_RE.sub("", text))
            if aligns[i] == "right":
                parts.append((" " * max(0, widths[i] - vis)) + text)
            else:
                parts.append(text + (" " * max(0, widths[i] - vis)))
        body.append(sep.join(parts))
    return "\n".join([header, divider, *body])


def kv(key: str, value: str, status: str | None = None) -> str:
    if status == "good":
        label = good("●")
    elif status == "warn":
        label = warn("●")
    elif status == "bad":
        label = bad("●")
    else:
        label = dim("•")
    return f"{label} {dim(key + ':')} {value}"


def style_domain(domain: str) -> str:
    clean = (domain or "").strip()
    if "." not in clean:
        return bold(clean)
    base, tld = clean.rsplit(".", 1)
    return f"{bold(base)}.{dim(tld)}"


class RunProgressDisplay:
    def __init__(self, title: str = "mailzero :: run") -> None:
        self.title = title
        # Keep only recent lines to avoid unbounded memory and noisy screens.
        self.logs: deque[str] = deque(maxlen=10)
        self.enabled = sys.stdout.isatty()
        self._live_mode = False

    def _enter_live_mode(self) -> None:
        """Switch to terminal alternate screen for in-place transient rendering."""
        if not self.enabled or self._live_mode:
            return
        # 1049h: alternate screen buffer, 25l: hide cursor.
        sys.stdout.write("\033[?1049h\033[?25l")
        sys.stdout.flush()
        self._live_mode = True

    def finish(self) -> None:
        """Restore terminal state and remove transient progress UI from view."""
        if not self.enabled or not self._live_mode:
            return
        # 25h: show cursor, 1049l: leave alternate screen buffer.
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()
        self._live_mode = False

    def update(self, current: int, total: int, status: str, detail: str) -> None:
        if not self.enabled:
            return
        self._enter_live_mode()
        # Newest first so operator sees latest action near the progress block.
        self.logs.appendleft(f"{status} {detail}")
        percent = 100 if total == 0 else int((current / total) * 100)
        bar_width = 38
        fill = 0 if total == 0 else int((current / total) * bar_width)
        bar = good("█" * fill) + dim("░" * max(0, bar_width - fill))
        lines = [
            "\033[H\033[2J",
            app_header(f"{self.title}", "Applying selected mailbox actions"),
            box(
                "Progress",
                [
                    kv("Completed", f"{current}/{total}"),
                    kv("Percent", f"{percent}%"),
                    f"{bar} {bold(str(percent) + '%')}",
                ],
            ),
            "",
            box("Recent Activity (latest 10)", list(self.logs) or [dim("(starting...)")]),
        ]
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
