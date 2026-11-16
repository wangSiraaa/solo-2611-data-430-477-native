"""Frozen source access.

A job may only probe the catalog and read *selected whole pages*.  There is no
bulk/scan API here: verification against the full source is deliberately a
separate path in ``verify.py`` so that collection metering stays honest.
"""
from __future__ import annotations

import json
from pathlib import Path

from util import (
    digest_object,
    normalize_catalog_pages,
    parse_page_bytes,
    sha256_bytes,
    validate_catalog,
    validate_rows,
)


class SourceError(Exception):
    pass


class SourceIntegrityError(SourceError):
    pass


class Source:
    def __init__(self, root, allowed_ids=None):
        self.root = Path(root)
        self.reads = []
        self.allowed_ids = None if allowed_ids is None else set(allowed_ids)

    def catalog(self):
        raw = (self.root / "catalog.json").read_bytes()
        try:
            catalog = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceIntegrityError(f"catalog.json is not valid JSON: {exc}") from exc
        validate_catalog(catalog)
        return catalog

    def page(self, id):
        """Read one whole page and verify it against the catalog digest."""
        if self.allowed_ids is not None and id not in self.allowed_ids:
            raise SourceError(
                f"page {id!r} is outside this source view's allowed selection"
            )
        catalog = self.catalog()
        pages = normalize_catalog_pages(catalog)
        if id not in pages:
            raise SourceError(f"page id {id!r} is not listed in the catalog")
        raw = (self.root / (id + ".json")).read_bytes()
        meta = pages[id]
        digest = sha256_bytes(raw)
        self.reads.append({"id": id, "bytes": len(raw), "sha256": digest})
        if digest != meta["sha256"]:
            raise SourceIntegrityError(
                f"page {id!r} sha256 {digest} does not match catalog {meta['sha256']}"
            )
        try:
            rows = parse_page_bytes(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise SourceIntegrityError(f"page {id!r} is not valid row JSON: {exc}") from exc
        errors = validate_rows(rows)
        if errors:
            raise SourceIntegrityError(f"page {id!r} schema error: {errors[0]}")
        if len(rows) != meta["rows"]:
            raise SourceIntegrityError(
                f"page {id!r} has {len(rows)} rows but catalog says {meta['rows']}"
            )
        return rows

    def reads_log(self):
        return [dict(entry) for entry in self.reads]

    def reset_reads(self):
        self.reads = []


def catalog_snapshot(source):
    """Collect the catalog plus its digest without touching any page body."""
    catalog = source.catalog()
    return {
        "source": catalog["source"],
        "catalog": catalog,
        "catalog_sha256": digest_object(catalog),
    }
