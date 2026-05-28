from __future__ import annotations

"""Mail.app side effects via AppleScript (`osascript`).

Kept intentionally small and explicit because this is the highest-risk surface:
moving real user messages.
"""

import subprocess


def _run_osascript(lines: list[str], timeout: int = 30) -> tuple[bool, str]:
    # We pass each script line with `-e` for straightforward quoting/debugging.
    args: list[str] = ["osascript"]
    for line in lines:
        args.extend(["-e", line])
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "osascript timed out"
    except OSError as exc:
        return False, f"osascript unavailable: {exc}"

    if result.returncode != 0:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip()
        return False, detail or f"osascript exit code {result.returncode}"
    return True, (result.stdout or "").strip()


def move_by_local_id(local_id: int, source_mailbox: str, target_mailbox: str) -> tuple[bool, str]:
    """Try moving by Mail internal message id first (most direct lookup)."""
    lines = [
        'tell application "Mail"',
        "set didMove to false",
        "repeat with acc in every account",
        "try",
        f'set srcBox to mailbox "{source_mailbox}" of acc',
        f'set dstBox to mailbox "{target_mailbox}" of acc',
        f"set msgList to (every message of srcBox whose id is {int(local_id)})",
        "if (count of msgList) > 0 then",
        "move msgList to dstBox",
        "set didMove to true",
        "exit repeat",
        "end if",
        "end try",
        "end repeat",
        "if didMove then",
        'return "moved"',
        "else",
        'error "message not found for local id"',
        "end if",
        "end tell",
    ]
    return _run_osascript(lines)


def move_by_message_header(
    header_message_id: str,
    source_mailbox: str,
    target_mailbox: str,
) -> tuple[bool, str]:
    """Fallback move strategy using RFC822 Message-ID header."""
    safe_id = header_message_id.replace("\\", "\\\\").replace('"', '\\"')
    lines = [
        'tell application "Mail"',
        "set didMove to false",
        "repeat with acc in every account",
        "try",
        f'set srcBox to mailbox "{source_mailbox}" of acc',
        f'set dstBox to mailbox "{target_mailbox}" of acc',
        f'set msgList to (every message of srcBox whose message id is "{safe_id}")',
        "if (count of msgList) > 0 then",
        "move msgList to dstBox",
        "set didMove to true",
        "exit repeat",
        "end if",
        "end try",
        "end repeat",
        "if didMove then",
        'return "moved"',
        "else",
        'error "message not found for message-id"',
        "end if",
        "end tell",
    ]
    return _run_osascript(lines)


def move_message(
    source_mailbox: str,
    target_mailbox: str,
    local_id: int | None,
    header_message_id: str | None,
) -> tuple[bool, str]:
    """Best-effort move with fallback order:
    1) local Mail id
    2) message-id header
    """
    if local_id is not None:
        ok, detail = move_by_local_id(local_id, source_mailbox=source_mailbox, target_mailbox=target_mailbox)
        if ok:
            return ok, detail
        if not header_message_id:
            return ok, detail

    if header_message_id:
        return move_by_message_header(
            header_message_id=header_message_id,
            source_mailbox=source_mailbox,
            target_mailbox=target_mailbox,
        )

    return False, "no usable message identifier"
