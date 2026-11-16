"""Rule compilation for question 459.

A ruleset is a list of substitution rules.  Every rule has a unique id, an
``lhs`` of 1..3 tokens and an ``rhs`` of 1..3 tokens.  Rules are matched
greedily while walking the *original* token array from left to right:

* rules are considered in ``(len(lhs) descending, id ascending)`` order;
* the first matching rule at the current position wins;
* the whole lhs is consumed and scanning continues after it;
* the generated rhs is never re-scanned/expanded.

The ruleset has a content identity (a SHA-256 over its canonical content).
The persistent index stores that identity and refuses to expose an old index
to a different ruleset unless the caller explicitly rebuilds it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

MAX_TOKEN_BYTES = 32
MAX_SIDE_TOKENS = 3


class RuleError(ValueError):
    """Invalid rule / token input."""


class RuleSetMismatch(RuleError):
    """An index was opened with a ruleset that does not match its postings."""


def validate_token(token: Any, what: str = "token") -> str:
    """Validate one already-tokenized lowercase ASCII token.

    No normalization or natural-language tokenization is performed here.  The
    input contract is strict: non-empty printable ASCII, at most 32 bytes, and
    already lowercase (no uppercase ASCII letters).
    """
    if not isinstance(token, str):
        raise RuleError(f"{what} must be a string, got {type(token).__name__}")
    if token == "":
        raise RuleError(f"{what} must be non-empty")
    try:
        raw = token.encode("ascii")
    except UnicodeEncodeError:
        raise RuleError(f"{what} must be ASCII: {token!r}") from None
    if len(raw) > MAX_TOKEN_BYTES:
        raise RuleError(
            f"{what} exceeds {MAX_TOKEN_BYTES} bytes: {token!r} ({len(raw)} bytes)"
        )
    if any(byte < 0x21 or byte > 0x7E for byte in raw):
        raise RuleError(f"{what} must be printable ASCII without whitespace: {token!r}")
    if token != token.lower():
        raise RuleError(f"{what} must already be lowercase: {token!r}")
    return token


def validate_token_array(tokens: Any, what: str = "tokens") -> list[str]:
    if not isinstance(tokens, (list, tuple)):
        raise RuleError(f"{what} must be a list or tuple of strings")
    return [validate_token(tok, f"{what}[{i}]") for i, tok in enumerate(tokens)]


def _rule_id_key(rule_id: Any) -> tuple[int, Any]:
    """Total ordering usable for ids of the supported scalar types."""
    if isinstance(rule_id, bool):
        raise RuleError("rule id must not be a bool")
    if isinstance(rule_id, int):
        return (0, rule_id)
    if isinstance(rule_id, str) and rule_id:
        return (1, rule_id)
    raise RuleError(f"rule id must be a non-empty string or an integer: {rule_id!r}")


def _token_side(value: Any, what: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise RuleError(f"{what} must be a list of tokens")
    if not 1 <= len(value) <= MAX_SIDE_TOKENS:
        raise RuleError(f"{what} must contain 1..{MAX_SIDE_TOKENS} tokens")
    return tuple(validate_token(tok, f"{what}[{i}]") for i, tok in enumerate(value))


@dataclass(frozen=True)
class CompiledRule:
    id: Any
    lhs: tuple[str, ...]
    rhs: tuple[str, ...]
    id_key: tuple[int, Any]

    @property
    def width(self) -> int:
        return max(len(self.lhs), len(self.rhs))

    def to_public(self, rank: int | None = None) -> dict:
        out = {"id": self.id, "lhs": list(self.lhs), "rhs": list(self.rhs)}
        if rank is not None:
            out["rank"] = rank
            out["width"] = self.width
        return out


class RuleSet:
    """Validated, deterministically ordered ruleset."""

    def __init__(self, rules: Iterable[dict], *, source: str | None = None):
        compiled: list[CompiledRule] = []
        seen: set[tuple[int, Any]] = set()
        for pos, raw in enumerate(rules):
            if not isinstance(raw, dict):
                raise RuleError(f"rule #{pos} must be an object")
            if "id" not in raw:
                raise RuleError(f"rule #{pos} is missing its unique id")
            rule_id = raw["id"]
            id_key = _rule_id_key(rule_id)
            if id_key in seen:
                raise RuleError(f"duplicate rule id: {rule_id!r}")
            seen.add(id_key)
            lhs = _token_side(raw.get("lhs"), f"rule {rule_id!r} lhs")
            rhs = _token_side(raw.get("rhs"), f"rule {rule_id!r} rhs")
            compiled.append(CompiledRule(rule_id, lhs, rhs, id_key))

        # Published matching priority: longer lhs first, then id ascending.
        self.rules: tuple[CompiledRule, ...] = tuple(
            sorted(compiled, key=lambda r: (-len(r.lhs), r.id_key))
        )
        self.source = source
        self.fingerprint = self._fingerprint()

    @classmethod
    def load(cls, rules: Any, *, source: str | None = None) -> "RuleSet":
        if isinstance(rules, RuleSet):
            return rules
        return cls(rules, source=source)

    @classmethod
    def from_json_file(cls, path: str) -> "RuleSet":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise RuleError("rules JSON must be an array")
        return cls(data, source=path)

    def _fingerprint(self) -> str:
        # Content identity: ids plus lhs/rhs, independent of input file order.
        canonical_rules = [
            {"id": r.id, "lhs": list(r.lhs), "rhs": list(r.rhs)}
            for r in sorted(self.rules, key=lambda r: r.id_key)
        ]
        payload = json.dumps(
            {"version": 1, "rules": canonical_rules},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def to_public(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "source": self.source,
            "rule_order": [
                r.to_public(rank=i) for i, r in enumerate(self.rules)
            ],
            "order_key": "(-len(lhs), id), ascending rank",
        }

    def to_json(self) -> str:
        return json.dumps(
            [r.to_public() for r in self.rules],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    def match_at(self, tokens: Sequence[str], position: int) -> CompiledRule | None:
        remaining = len(tokens) - position
        for rule in self.rules:
            n = len(rule.lhs)
            if n > remaining:
                continue
            if tuple(tokens[position : position + n]) == rule.lhs:
                return rule
        return None

    def __len__(self) -> int:
        return len(self.rules)

    def __iter__(self):
        return iter(self.rules)
