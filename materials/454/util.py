"""Shared helpers for the page-sampling chain.

All arithmetic that affects estimates is kept as :class:`fractions.Fraction`.
JSON helpers are canonical so that digests are stable across runs.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1

# Frozen-source bounds from the task statement.
MAX_PAGES = 20
MAX_ROWS_PER_PAGE = 500

PLAN_SCHEMA = "page-sample-plan/1"
PART_SCHEMA = "page-sample-part/1"
SAMPLE_SCHEMA = "page-sample/1"
BUNDLE_SCHEMA = "page-sample-bundle/1"
MERGE_ERROR_SCHEMA = "page-sample-merge-error/1"

WOR_PROTOCOL = "sha256-rank-v1"
BERNOULLI_PROTOCOL = "sha256-bernoulli-v1"

# All reported decimals are rounded half-even to this many fractional places.
# The exact rational string next to each decimal is authoritative; the maximum
# absolute rounding error of a printed decimal is therefore 5e-19.
DECIMAL_PLACES = 18
NUMERIC_POLICY = (
    "exact values are rationals (numerator/denominator); decimal fields are "
    "rounded half-even to 18 fractional places, so each printed decimal is "
    "within 5e-19 of the exact rational"
)


def canonical_json(obj) -> str:
    """Stable JSON used for every digest and for persisted artifacts."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def digest_object(obj) -> str:
    return sha256_text(canonical_json(obj))


def is_hex_digest(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in "0123456789abcdef" for ch in value)
    )


def id_sort_key(page_id: str):
    """The declared stable order: raw UTF-8 bytes of the page id."""
    return page_id.encode("utf-8")


def normalize_catalog_pages(catalog):
    """Return {page_id: metadata} from either the dict or list catalog form."""
    pages = catalog.get("pages")
    if isinstance(pages, dict):
        return dict(pages)
    if isinstance(pages, list):
        out = {}
        for entry in pages:
            if not isinstance(entry, dict) or "id" not in entry:
                raise ValueError("catalog page entries must be objects with an id")
            pid = entry["id"]
            if pid in out:
                raise ValueError(f"duplicate catalog page id: {pid!r}")
            out[pid] = {k: v for k, v in entry.items() if k != "id"}
        return out
    raise ValueError("catalog.pages must be an object or a list")


def validate_catalog(catalog) -> None:
    if not isinstance(catalog, dict):
        raise ValueError("catalog must be a JSON object")
    if not isinstance(catalog.get("source"), str) or not catalog["source"]:
        raise ValueError("catalog.source must be a non-empty string")
    total = catalog.get("rows")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise ValueError("catalog.rows must be a non-negative integer")
    pages = normalize_catalog_pages(catalog)
    if not pages:
        raise ValueError("catalog must list at least one page")
    if len(pages) > MAX_PAGES:
        raise ValueError(f"catalog lists {len(pages)} pages but the format allows at most {MAX_PAGES}")
    seen = set()
    summed = 0
    for pid, meta in pages.items():
        if not isinstance(pid, str) or not pid:
            raise ValueError("page ids must be non-empty strings")
        if pid in seen:
            raise ValueError(f"duplicate page id: {pid!r}")
        seen.add(pid)
        if not isinstance(meta, dict):
            raise ValueError(f"page metadata for {pid!r} must be an object")
        nrows = meta.get("rows")
        if not isinstance(nrows, int) or isinstance(nrows, bool) or nrows < 0:
            raise ValueError(f"page {pid!r} rows must be a non-negative integer")
        if nrows > MAX_ROWS_PER_PAGE:
            raise ValueError(
                f"page {pid!r} has {nrows} rows but the format allows at most {MAX_ROWS_PER_PAGE}"
            )
        if "id" in meta and meta["id"] != pid:
            raise ValueError(f"page metadata id {meta['id']!r} does not match key {pid!r}")
        if not is_hex_digest(meta.get("sha256")):
            raise ValueError(f"page {pid!r} sha256 must be a 64-char lowercase hex digest")
        summed += nrows
    if summed != total:
        raise ValueError(
            f"catalog.rows={total} does not equal sum of page rows={summed}"
        )


def page_population(catalog):
    validate_catalog(catalog)
    pages = normalize_catalog_pages(catalog)
    return [
        {"id": pid, "rows": pages[pid]["rows"], "sha256": pages[pid]["sha256"]}
        for pid in sorted(pages, key=id_sort_key)
    ]


def source_digest(catalog) -> str:
    """Digest of the frozen catalog, which contains every page body digest."""
    return digest_object(catalog)


def normalize_seed(seed) -> str:
    if isinstance(seed, bool):
        raise ValueError("seed must be a string or number, not bool")
    if isinstance(seed, (str, int)):
        return str(seed)
    raise ValueError("seed must be a string or integer")


def fraction_from(value) -> Fraction:
    if isinstance(value, bool):
        raise ValueError("boolean is not a rational value")
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, str):
        try:
            return Fraction(value)
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError(f"invalid rational {value!r}") from exc
    if isinstance(value, float):
        # Only used by callers that pass a decimal literal; avoid binary float
        # arithmetic by going through its shortest decimal representation.
        return Fraction(str(value))
    raise ValueError(f"unsupported rational value: {type(value).__name__}")


def fraction_text(value) -> str:
    value = fraction_from(value)
    return f"{value.numerator}/{value.denominator}"


def decimal_text(value, places: int = DECIMAL_PLACES) -> str:
    """Rounded decimal rendering.  The rational string is authoritative."""
    value = fraction_from(value)
    with localcontext() as ctx:
        ctx.prec = max(60, places + 40)
        dec = Decimal(value.numerator) / Decimal(value.denominator)
        quantum = Decimal(1).scaleb(-places)
        dec = dec.quantize(quantum, rounding=ROUND_HALF_EVEN)
    return format(dec, "f")


def decimal_sqrt_text(value, places: int = DECIMAL_PLACES) -> str:
    value = fraction_from(value)
    if value < 0:
        raise ValueError("cannot take square root of a negative variance")
    with localcontext() as ctx:
        ctx.prec = max(60, places + 40)
        dec = (Decimal(value.numerator) / Decimal(value.denominator)).sqrt()
        quantum = Decimal(1).scaleb(-places)
        dec = dec.quantize(quantum, rounding=ROUND_HALF_EVEN)
    return format(dec, "f")


def validate_rows(rows):
    """Validate one page's rows and return a list of error strings."""
    errors = []
    if not isinstance(rows, list):
        return ["rows must be a JSON array"]
    seen = set()
    for index, row in enumerate(rows):
        prefix = f"row[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{prefix} must be an object")
            continue
        rid = row.get("row_id")
        if not isinstance(rid, str) or not rid:
            errors.append(f"{prefix}.row_id must be a non-empty string")
        elif rid in seen:
            errors.append(f"{prefix}.row_id {rid!r} is duplicated within the page")
        else:
            seen.add(rid)
        value = row.get("value")
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"{prefix}.value must be an integer (int64)")
        elif value < INT64_MIN or value > INT64_MAX:
            errors.append(f"{prefix}.value is outside int64 range")
    return errors


def page_content_digest(rows) -> str:
    return digest_object(rows)


def page_total(rows) -> int:
    total = 0
    for row in rows:
        total += row["value"]
    return total


def parse_page_bytes(raw: bytes):
    """Parse a page body as a JSON array, a {"rows": [...]} object, or NDJSON."""
    text = raw.decode("utf-8")
    if not text.strip():
        return []
    try:
        return _page_value(json.loads(text))
    except json.JSONDecodeError:
        rows = []
        for line_no, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on page line {line_no}: {exc}") from exc
            if isinstance(value, list):
                rows.extend(value)
            elif isinstance(value, dict) and isinstance(value.get("rows"), list):
                rows.extend(value["rows"])
            elif isinstance(value, dict):
                rows.append(value)
            else:
                raise ValueError(f"page line {line_no} is not a row object")
        return rows


def _page_value(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("rows"), list):
        return value["rows"]
    if isinstance(value, dict):
        return [value]
    raise ValueError("a page body must be a JSON array of row objects")


def read_json_file(path):
    with open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))
