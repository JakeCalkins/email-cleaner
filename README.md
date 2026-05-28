# mailzero

`mailzero` is a lightweight CLI for Inbox Zero on macOS Mail.

It is intentionally scoped to **All Inboxes** only.

## What It Does

- Rebuilds a local index from Mail.app metadata (`Envelope Index`) on each dashboard refresh
- Aggregates by sender domain (including masked relay decoding where possible)
- Shows a grouped domain report (sorted by inbox volume)
- Lets you interactively select domains in RUN mode
- Archives according to row selection (`[all but 1]` or `[all]`) per mailbox/domain
- Stores a backup log so `--undo` can revert the last run

## Usage

```bash
cd /Users/timthecomputer/Documents/email-cleaner
PYTHONPATH=src python3 -m mailzero --help
```

## Install As A macOS Command

You can install `mailzero` as a normal shell command so you do not need `python -m ...`.
The installer prefers `pipx` if available, and otherwise uses user-site `pip`.

```bash
cd /Users/timthecomputer/Documents/email-cleaner
./scripts/install-macos-cli.sh
mailzero --help
```

If your shell cannot find `mailzero`, add your Python user bin directory to `PATH`:

```bash
export PATH="$(python3 -c 'import site; print(site.USER_BASE)')/bin:$PATH"
```

### Modes

- Default dashboard
  - Scans Mail data (`~/Library/Mail/V*/MailData/Envelope Index`)
  - Rebuilds the local working dataset from scratch on every refresh
  - Focuses on sender-domain analysis only
  - Prints one domain table grouped by inbox with visual separators and colors
  - Overwrites `.mailzero/dry-run.json` on each refresh

- RUN mode
  - Reads `.mailzero/dry-run.json`
  - Shows an interactive domain list from the dashboard:
    - Grouped by mailbox (All Inboxes account separation)
    - Up/Down to navigate
    - Tab to cycle archive action for the current row:
      - `[ ]` no action
      - `[all but 1]` archive all but newest
      - `[all]` archive every matching inbox message
    - Enter opens a final review prompt before execution
    - `s` enters sort mode:
      - left/right moves across sortable headers
      - space cycles `asc -> desc -> off`
      - `s` exits sort mode and keeps selected sort state
  - Includes latest-message previews for context
  - Shows a live progress bar and rolling recent activity log during execution
  - Applies actions per mailbox/domain row (same domain in another inbox is independent)
  - Saves backup log to `.mailzero/last-run.json` for undo

- Undo mode
  - Reads `.mailzero/last-run.json`
  - Reverts the last successful RUN-mode operations

## Typical flow

```bash
mailzero
mailzero --undo
```

## Safe testing flow

To test RUN/undo wiring without touching real Mail, use testing mode:

```bash
cd /Users/timthecomputer/Documents/email-cleaner
PYTHONPATH=src python3 -m mailzero --testing
PYTHONPATH=src python3 -m mailzero --undo
```

In testing mode, RUN mode and `--undo` only modify the local `.mailzero` SQLite state.

## Customization

Primary behavior knobs are in [`workflow.py`](/Users/timthecomputer/Documents/email-cleaner/src/mailzero/workflow.py):

- `MIN_DOMAIN_TOTAL_FOR_TABLE`: minimum total messages required to show a domain row
- `MAX_DOMAIN_ROWS_PER_MAILBOX`: per-inbox domain row cap in dashboard output
- `PREVIEW_ITEMS_PER_DOMAIN`: number of latest messages shown in RUN-mode previews

Optional env vars:

- `MAILZERO_USE_DUMMY_DATA=1` to run with fixture data
- `MAILZERO_SELECTED_DOMAINS="domain1.com,domain2.com"` to bypass interactive selection
- `MAILZERO_SELECTED_DOMAINS="*"` to select all domains automatically

## Filter Syntax Quick Reference

Custom filter expressions support:

- Modes: `EXCLUDE`, `ONLY_INCLUDE` (default is include)
- Fields: `FROM`, `TO`, `SUBJECT`, `BEFORE`, `AFTER` (`DATE` token optional after `BEFORE/AFTER`)
- Boolean operators: `AND`, `OR`, `NOT`, `!`
- Grouping: `(...)`
- Comparators: `:` or `=`
- Wildcards: `*` inside values
- Dates: `MM/DD/YY` (interpreted as year `20YY`)

Examples:

- `ONLY_INCLUDE FROM:*@apple.com AND SUBJECT:"receipt"`
- `EXCLUDE (FROM:newsletter@* OR SUBJECT:promo)`
- `ONLY_INCLUDE AFTER:01/01/26 AND BEFORE DATE=05/31/26`

## Notes

- You may need macOS permissions enabled for the host app running this CLI:
  - Full Disk Access (for Mail data files)
  - Automation access to control Mail
- Current write action is mailbox move: `INBOX -> Archive` for planned candidates.

## Performance Notes

- Dashboard refresh does a full rebuild by design so results match current Mail state.
- Local index writes are batched (`executemany`) for faster ingest on large inboxes.
- SQLite is configured with `WAL` + `synchronous=NORMAL` for a practical speed/safety balance.

## Masked sender domain decoding

`mailzero` attempts to classify masked relay senders by extracting the underlying sender domain when it is encoded in the local-part.

Supported patterns:

- SimpleLogin:
  - `ra+sender.at.domain.com+random@simplelogin.co`
  - `sender_at_domain_com_xxxxxx@simplelogin.co`
- Addy / AnonAddy:
  - `alias+sender=domain.com@addymail.com`
  - `alias+sender=domain.com@*.anonaddy.me`
- Apple relay (best-effort):
  - `something_at_domain_tld_xxxxx@privaterelay.appleid.com`
  - `no-reply_at_doordash_com_xxxxx@privaterelay.appleid.com` -> `doordash.com`
  - `something_at_domain_tld_xxxxx@icloud.com`

If no encoded sender domain is present, `mailzero` falls back to the routing domain (for example `simplelogin.co` or `privaterelay.appleid.com`).
