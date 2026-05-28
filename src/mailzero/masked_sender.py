from __future__ import annotations

"""Resolve sender domains, including reverse-alias relay patterns.

Goal:
- keep domain analytics useful even when addresses are masked by relay services
- preserve routing domain/source metadata for transparency/debugging
"""

import re
from dataclasses import dataclass
from email.utils import getaddresses

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)

SIMPLELOGIN_BASE_DOMAINS = {
    "simplelogin.co",
    "simplelogin.com",
    "slmail.me",
    "simplelogin.fr",
}
ADDY_BASE_DOMAINS = {
    "anonaddy.com",
    "anonaddy.me",
    "addymail.com",
    "addy.io",
}
APPLE_RELAY_BASE_DOMAINS = {
    "privaterelay.appleid.com",
}


@dataclass
class SenderDomainResolution:
    # Effective domain used by analytics/actions.
    sender_domain: str
    # Actual SMTP routing domain from sender address.
    sender_routing_domain: str
    # Explains how sender_domain was obtained (direct vs decoded alias).
    sender_domain_source: str
    sender_email: str | None


def _looks_domain(value: str) -> bool:
    # Strict but not exhaustive domain validation to avoid false positives from
    # random token sequences in relay local-parts.
    if "." not in value:
        return False
    labels = value.split(".")
    if len(labels) < 2:
        return False
    for label in labels:
        if not label or len(label) > 63:
            return False
        if label[0] == "-" or label[-1] == "-":
            return False
        if not re.fullmatch(r"[a-z0-9-]+", label):
            return False
    tld = labels[-1]
    return len(tld) >= 2 and tld.isalpha()


def _normalize_domain(candidate: str) -> str | None:
    # Remove punctuation wrappers often seen in loosely formatted headers.
    clean = candidate.strip().strip("<>[](){}'\"").lower()
    clean = clean.strip(".")
    if not _looks_domain(clean):
        return None
    return clean


def _extract_sender_email(sender: str) -> str | None:
    # Prefer standards-aware parsing first; regex fallback handles rough input.
    for _, addr in getaddresses([sender or ""]):
        if "@" in addr:
            return addr.lower()
    match = EMAIL_RE.search(sender or "")
    if match:
        return match.group(0).lower()
    return None


def _decode_underscore_at(value: str) -> str | None:
    # Examples:
    # john_at_example_com_abc123
    # john_at_mail_example_co_uk_xyz
    if "_at_" not in value:
        return None
    tail = value.split("_at_", 1)[1]
    tokens = [t for t in tail.split("_") if t]
    if len(tokens) < 2:
        return None

    # Find the longest valid domain prefix. This handles patterns like:
    # doordash_com_prpqjnpt5k_abc212bd  -> doordash.com
    # mail_example_co_uk_xyz123         -> mail.example.co.uk
    best: str | None = None
    for i in range(2, len(tokens) + 1):
        candidate = _normalize_domain(".".join(tokens[:i]))
        if candidate:
            best = candidate
    return best


def _decode_dot_at(value: str) -> str | None:
    # SimpleLogin historical style:
    # ra+sender.at.domain.com+random@simplelogin.co
    # -> sender@domain.com encoded as sender.at.domain.com
    if ".at." not in value:
        return None
    left = value
    if "+" in left:
        # Use the first token that includes ".at." to avoid alias metadata.
        parts = left.split("+")
        for part in parts:
            if ".at." in part:
                left = part
                break
    contact = left.split(".at.", 1)[1]
    return _normalize_domain(contact)


def _decode_addy_sender(local_part: str) -> str | None:
    # Addy examples:
    # alias+hello=example.com@johndoe.anonaddy.com
    # first+hello+whatever=example.com@addymail.com
    if "=" not in local_part:
        return None
    rhs = local_part.rsplit("=", 1)[1]
    # Sometimes address extensions can leave extra separators on the RHS.
    rhs = rhs.strip("+")
    return _normalize_domain(rhs)


def _decode_simplelogin_sender(local_part: str) -> str | None:
    # Support both newer underscore form and older ".at." form.
    return _decode_underscore_at(local_part) or _decode_dot_at(local_part)


def _decode_apple_sender(local_part: str) -> str | None:
    # Apple officially describes Hide My Email aliases as random addresses.
    # In some reply workflows clients may expose a structured local-part
    # with "_at_" separators; decode only when present.
    return _decode_underscore_at(local_part) or _decode_dot_at(local_part)


def resolve_sender_domain(sender: str) -> SenderDomainResolution:
    """Return effective sender domain plus routing/derivation metadata."""
    sender_email = _extract_sender_email(sender)
    if not sender_email:
        return SenderDomainResolution(
            sender_domain="unknown",
            sender_routing_domain="unknown",
            sender_domain_source="unknown",
            sender_email=None,
        )

    local, routing_domain = sender_email.split("@", 1)
    routing_domain = routing_domain.lower()

    # Direct sender domain (default).
    effective_domain = routing_domain
    source = "direct"

    if _matches_base_domain(routing_domain, SIMPLELOGIN_BASE_DOMAINS):
        # Decode original sender domain embedded in alias local-part when present.
        decoded = _decode_simplelogin_sender(local)
        if decoded:
            effective_domain = decoded
            source = "simplelogin_reverse_alias"
    elif _matches_base_domain(routing_domain, ADDY_BASE_DOMAINS):
        decoded = _decode_addy_sender(local) or _decode_underscore_at(local)
        if decoded:
            effective_domain = decoded
            source = "addy_reverse_alias"
    elif _matches_base_domain(routing_domain, APPLE_RELAY_BASE_DOMAINS) or routing_domain == "icloud.com":
        # `icloud.com` inclusion handles cases where relay-like local-part
        # appears behind iCloud routing.
        decoded = _decode_apple_sender(local)
        if decoded:
            effective_domain = decoded
            source = "apple_relay_encoded"

    return SenderDomainResolution(
        sender_domain=effective_domain,
        sender_routing_domain=routing_domain,
        sender_domain_source=source,
        sender_email=sender_email,
    )


def _matches_base_domain(domain: str, base_domains: set[str]) -> bool:
    for base in base_domains:
        if domain == base or domain.endswith("." + base):
            return True
    return False
