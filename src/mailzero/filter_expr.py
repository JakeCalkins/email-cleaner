from __future__ import annotations

"""Parser + SQL compiler for mailzero custom filtering expressions.

Supported syntax (case-insensitive keywords):
- Prefix mode: EXCLUDE | ONLY_INCLUDE
- Fields: FROM | TO | SUBJECT
- Date predicates: BEFORE | AFTER (with MM/DD/YY values; optional DATE keyword)
- Operators: : or =
- Boolean ops: AND | OR | NOT | !
- Parentheses for precedence control
- Wildcards: * inside match strings (SQL LIKE-style wildcard)
- Values: bare words or quoted strings (single/double quotes)
"""

from dataclasses import dataclass
from datetime import date
from typing import Literal

FilterMode = Literal["exclude", "only_include"]
FieldName = Literal["from", "to", "subject"]
DateFieldName = Literal["before", "after"]


class FilterSyntaxError(ValueError):
    """Raised when custom filter expression parsing fails."""


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    pos: int


@dataclass(frozen=True)
class PredicateNode:
    field: FieldName
    value: str


@dataclass(frozen=True)
class DatePredicateNode:
    op: DateFieldName
    value_iso: str


@dataclass(frozen=True)
class NotNode:
    expr: "ExprNode"


@dataclass(frozen=True)
class AndNode:
    left: "ExprNode"
    right: "ExprNode"


@dataclass(frozen=True)
class OrNode:
    left: "ExprNode"
    right: "ExprNode"


ExprNode = PredicateNode | DatePredicateNode | NotNode | AndNode | OrNode


@dataclass(frozen=True)
class CompiledFilter:
    mode: FilterMode
    source: str
    where_sql: str
    params: tuple[str, ...]


_MODE_WORDS = {"EXCLUDE": "exclude", "ONLY_INCLUDE": "only_include"}
_FIELD_WORDS = {"FROM": "from", "TO": "to", "SUBJECT": "subject"}
_DATE_FIELD_WORDS = {"BEFORE": "before", "AFTER": "after"}
_BINARY_WORDS = {"AND", "OR"}
_UNARY_WORDS = {"NOT"}


def _parse_mmddyy_to_iso(value: str, pos: int) -> str:
    raw = value.strip()
    parts = raw.split("/")
    if len(parts) != 3:
        raise FilterSyntaxError(
            f"Invalid date `{value}` at position {pos}. Expected MM/DD/YY."
        )
    mm, dd, yy = parts
    if not (mm.isdigit() and dd.isdigit() and yy.isdigit()):
        raise FilterSyntaxError(
            f"Invalid date `{value}` at position {pos}. Expected MM/DD/YY."
        )
    if len(mm) != 2 or len(dd) != 2 or len(yy) != 2:
        raise FilterSyntaxError(
            f"Invalid date `{value}` at position {pos}. Use zero-padded MM/DD/YY."
        )
    year = 2000 + int(yy)
    month = int(mm)
    day = int(dd)
    try:
        return date(year, month, day).isoformat()
    except ValueError as exc:
        raise FilterSyntaxError(
            f"Invalid calendar date `{value}` at position {pos}: {exc}"
        ) from exc


def _tokenize(source: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "(":
            tokens.append(Token("LPAREN", ch, i))
            i += 1
            continue
        if ch == ")":
            tokens.append(Token("RPAREN", ch, i))
            i += 1
            continue
        if ch == ":":
            tokens.append(Token("COLON", ch, i))
            i += 1
            continue
        if ch == "=":
            tokens.append(Token("EQUAL", ch, i))
            i += 1
            continue
        if ch == "!":
            tokens.append(Token("BANG", ch, i))
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            start = i
            i += 1
            buf: list[str] = []
            while i < n:
                cur = source[i]
                if cur == "\\" and i + 1 < n:
                    buf.append(source[i + 1])
                    i += 2
                    continue
                if cur == quote:
                    i += 1
                    break
                buf.append(cur)
                i += 1
            else:
                raise FilterSyntaxError(f"Unterminated quoted string at position {start}.")
            tokens.append(Token("STRING", "".join(buf), start))
            continue

        # Bare token: run until whitespace or control punctuation.
        start = i
        while i < n and (not source[i].isspace()) and source[i] not in "():=!":
            i += 1
        value = source[start:i]
        if value:
            tokens.append(Token("WORD", value, start))
            continue
        raise FilterSyntaxError(f"Unexpected character `{ch}` at position {i}.")
    return tokens


class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.idx = 0

    def _peek(self) -> Token | None:
        if self.idx >= len(self.tokens):
            return None
        return self.tokens[self.idx]

    def _consume(self) -> Token:
        tok = self._peek()
        if tok is None:
            raise FilterSyntaxError("Unexpected end of expression.")
        self.idx += 1
        return tok

    def _match_kind(self, *kinds: str) -> Token | None:
        tok = self._peek()
        if tok is None:
            return None
        if tok.kind in kinds:
            self.idx += 1
            return tok
        return None

    def _match_word(self, *words: str) -> Token | None:
        tok = self._peek()
        if tok is None or tok.kind != "WORD":
            return None
        up = tok.value.upper()
        if up in words:
            self.idx += 1
            return tok
        return None

    def parse_mode(self) -> FilterMode:
        tok = self._peek()
        if tok is None:
            raise FilterSyntaxError("Empty filter expression.")
        if tok.kind == "WORD":
            up = tok.value.upper()
            if up in _MODE_WORDS:
                self.idx += 1
                return _MODE_WORDS[up]  # type: ignore[return-value]
        return "only_include"

    def parse_expression(self) -> ExprNode:
        expr = self._parse_or()
        if self._peek() is not None:
            tok = self._peek()
            assert tok is not None
            raise FilterSyntaxError(
                f"Unexpected token `{tok.value}` at position {tok.pos}."
            )
        return expr

    def _parse_or(self) -> ExprNode:
        node = self._parse_and()
        while self._match_word("OR"):
            right = self._parse_and()
            node = OrNode(left=node, right=right)
        return node

    def _parse_and(self) -> ExprNode:
        node = self._parse_unary()
        while self._match_word("AND"):
            right = self._parse_unary()
            node = AndNode(left=node, right=right)
        return node

    def _parse_unary(self) -> ExprNode:
        if self._match_word("NOT") or self._match_kind("BANG"):
            return NotNode(expr=self._parse_unary())
        return self._parse_primary()

    def _parse_primary(self) -> ExprNode:
        if self._match_kind("LPAREN"):
            expr = self._parse_or()
            if not self._match_kind("RPAREN"):
                raise FilterSyntaxError("Expected `)` to close parenthesized expression.")
            return expr
        return self._parse_predicate()

    def _parse_predicate(self) -> ExprNode:
        field_tok = self._consume()
        if field_tok.kind != "WORD":
            raise FilterSyntaxError(
                f"Expected field name (FROM/TO/SUBJECT/BEFORE/AFTER) at position {field_tok.pos}."
            )
        field_up = field_tok.value.upper()
        if field_up in _DATE_FIELD_WORDS:
            # Allow optional DATE token: BEFORE DATE:05/28/26
            _ = self._match_word("DATE")
            if not (self._match_kind("COLON") or self._match_kind("EQUAL")):
                raise FilterSyntaxError(
                    f"Expected `:` or `=` after `{field_tok.value}` at position {field_tok.pos}."
                )
            value_tok = self._consume()
            if value_tok.kind not in {"WORD", "STRING"}:
                raise FilterSyntaxError(
                    f"Expected date value for `{field_tok.value}` at position {value_tok.pos}."
                )
            value_iso = _parse_mmddyy_to_iso(value_tok.value, value_tok.pos)
            return DatePredicateNode(op=_DATE_FIELD_WORDS[field_up], value_iso=value_iso)  # type: ignore[arg-type]
        if field_up not in _FIELD_WORDS:
            raise FilterSyntaxError(
                f"Unknown field `{field_tok.value}` at position {field_tok.pos}. "
                "Expected FROM, TO, SUBJECT, BEFORE, or AFTER."
            )
        if not (self._match_kind("COLON") or self._match_kind("EQUAL")):
            raise FilterSyntaxError(
                f"Expected `:` or `=` after `{field_tok.value}` at position {field_tok.pos}."
            )
        value_tok = self._consume()
        if value_tok.kind not in {"WORD", "STRING"}:
            raise FilterSyntaxError(
                f"Expected value for `{field_tok.value}` at position {value_tok.pos}."
            )
        value = value_tok.value.strip()
        if not value:
            raise FilterSyntaxError(
                f"Empty value for `{field_tok.value}` at position {value_tok.pos}."
            )
        return PredicateNode(field=_FIELD_WORDS[field_up], value=value)  # type: ignore[arg-type]


def _escape_like(value: str) -> str:
    # Escape LIKE meta chars to preserve literal "contains" semantics.
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _compile_expr(node: ExprNode) -> tuple[str, list[str]]:
    if isinstance(node, PredicateNode):
        column_by_field = {
            "from": "sender",
            "to": "recipient",
            "subject": "subject",
        }
        column = column_by_field[node.field]
        sql = f"(LOWER(COALESCE({column}, '')) LIKE ? ESCAPE '\\')"
        escaped = _escape_like(node.value.lower())
        # Wildcard behavior:
        # - value with `*` uses explicit wildcard placement
        # - value without `*` keeps historical contains semantics
        if "*" in node.value:
            param = escaped.replace("*", "%")
        else:
            param = f"%{escaped}%"
        return sql, [param]
    if isinstance(node, DatePredicateNode):
        op_sql = "<" if node.op == "before" else ">"
        sql = f"(date(substr(COALESCE(date_received, ''), 1, 10)) {op_sql} date(?))"
        return sql, [node.value_iso]
    if isinstance(node, NotNode):
        sql, params = _compile_expr(node.expr)
        return f"(NOT {sql})", params
    if isinstance(node, AndNode):
        left_sql, left_params = _compile_expr(node.left)
        right_sql, right_params = _compile_expr(node.right)
        return f"({left_sql} AND {right_sql})", [*left_params, *right_params]
    if isinstance(node, OrNode):
        left_sql, left_params = _compile_expr(node.left)
        right_sql, right_params = _compile_expr(node.right)
        return f"({left_sql} OR {right_sql})", [*left_params, *right_params]
    raise FilterSyntaxError("Unsupported filter expression node.")


def compile_custom_filter(source: str | None) -> CompiledFilter | None:
    """Parse and compile user filter to a SQL WHERE fragment.

    Returns None when source is empty.
    """
    raw = (source or "").strip()
    if not raw:
        return None
    tokens = _tokenize(raw)
    parser = _Parser(tokens)
    mode = parser.parse_mode()
    expr = parser.parse_expression()
    expr_sql, params = _compile_expr(expr)
    if mode == "exclude":
        where_sql = f"(NOT {expr_sql})"
    else:
        where_sql = expr_sql
    return CompiledFilter(
        mode=mode,
        source=raw,
        where_sql=where_sql,
        params=tuple(params),
    )
