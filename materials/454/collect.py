"""Worker-side whole-page collection.

A worker can only ask the source for pages that the frozen plan selected.  It
records the bytes and digest actually read, so the sample identity is auditable
later without re-reading the source.
"""
from __future__ import annotations

import time

from design import validate_plan
from util import (
    PART_SCHEMA,
    digest_object,
    id_sort_key,
    page_content_digest,
    page_total,
)


class CollectError(ValueError):
    pass


def collect(plan, source, worker_id, pages=None, clock=time.time):
    """Read a subset of the plan's selected pages and return a part artifact.

    ``pages=None`` means every selected page.  Requesting a page that is not in
    ``plan['selected']`` is refused: unselected bodies must never be read.
    """
    plan_errors = validate_plan(plan)
    if plan_errors:
        raise CollectError("plan is invalid: " + "; ".join(plan_errors))
    if not isinstance(worker_id, str) or not worker_id:
        raise CollectError("worker_id must be a non-empty string")
    selected = list(plan["selected"])
    if pages is None:
        wanted = selected
    else:
        if isinstance(pages, str):
            pages = [pages]
        wanted = list(pages)
        if len(set(wanted)) != len(wanted):
            raise CollectError("pages requested by a worker must be distinct")
        unselected = [pid for pid in wanted if pid not in selected]
        if unselected:
            raise CollectError(
                "refusing to read pages outside the frozen selection: "
                + ", ".join(map(repr, sorted(unselected, key=id_sort_key)))
            )
    wanted = sorted(wanted, key=id_sort_key)

    collected = []
    reads = []
    failed = []
    for pid in wanted:
        before = len(source.reads)
        try:
            rows = source.page(pid)  # verifies catalog sha256, schema, row count
        except Exception as exc:  # noqa: BLE001 - a bad page must not lose good ones
            # Keep the bytes actually read for the audit trail, then record the
            # page as failed.  The job will merge as incomplete, not estimable.
            for entry in source.reads[before:]:
                reads.append(dict(entry))
            failed.append({"id": pid, "error": f"{type(exc).__name__}: {exc}"})
            continue
        read_entries = [dict(entry) for entry in source.reads[before:]]
        if len(read_entries) != 1 or read_entries[0]["id"] != pid:
            failed.append({"id": pid, "error": "unexpected read log shape"})
            continue
        read = read_entries[0]
        reads.append(read)
        collected.append(
            {
                "id": pid,
                "n_rows": len(rows),
                "page_sha256": read["sha256"],
                "content_sha256": page_content_digest(rows),
                "total": page_total(rows),
                "rows": rows,
            }
        )

    identity = {
        "job_id": plan["job_id"],
        "plan_sha256": plan["plan_sha256"],
        "source_name": plan["source"]["name"],
        "catalog_sha256": plan["source"]["catalog_sha256"],
    }
    part = {
        "schema": PART_SCHEMA,
        "identity": identity,
        "worker_id": worker_id,
        "requested": wanted,
        "completed": [page["id"] for page in collected],
        "failed": failed,
        "pages": collected,
        "reads": reads,
        "created_at": _timestamp(clock),
    }
    part["part_sha256"] = digest_object(part)
    return part


def _timestamp(clock):
    value = clock()
    return int(value) if not isinstance(value, float) else float(value)


def validate_part(part):
    errors = []
    if not isinstance(part, dict):
        return ["part must be a JSON object"]
    if part.get("schema") != PART_SCHEMA:
        errors.append(f"part.schema must be {PART_SCHEMA!r}")
    identity = part.get("identity")
    if not isinstance(identity, dict):
        errors.append("part.identity must be an object")
    else:
        for key in ("job_id", "plan_sha256", "source_name", "catalog_sha256"):
            if not isinstance(identity.get(key), str) or not identity[key]:
                errors.append(f"part.identity.{key} must be a non-empty string")
    completed = part.get("completed")
    requested = part.get("requested")
    failed = part.get("failed", [])
    if not isinstance(completed, list) or not isinstance(requested, list):
        errors.append("part.requested/completed must be arrays")
        return errors
    if not isinstance(failed, list):
        errors.append("part.failed must be an array")
        failed = []
    if len(set(requested)) != len(requested):
        errors.append("part.requested must not repeat a page id")
    if len(set(completed)) != len(completed):
        errors.append("part.completed must not repeat a page id")
    extra = [pid for pid in completed if pid not in requested]
    if extra:
        errors.append("part.completed contains pages that were not requested")
    pages = part.get("pages")
    if not isinstance(pages, list):
        errors.append("part.pages must be an array")
        return errors
    seen = set()
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("id"), str):
            errors.append("part.pages entries must be objects with an id")
            continue
        if page["id"] in seen:
            errors.append(f"part contains page {page['id']!r} more than once")
        seen.add(page["id"])
        rows = page.get("rows")
        if not isinstance(rows, list):
            errors.append(f"part page {page['id']!r} rows must be an array")
        elif page.get("content_sha256") != page_content_digest(rows):
            errors.append(f"part page {page['id']!r} content_sha256 mismatch")
        if page.get("n_rows") is not None and isinstance(rows, list):
            if page["n_rows"] != len(rows):
                errors.append(f"part page {page['id']!r} n_rows mismatch")
    if sorted(seen, key=id_sort_key) != sorted(completed, key=id_sort_key):
        errors.append("part.completed must list exactly the pages present in part.pages")
    for failure in failed:
        if not isinstance(failure, dict) or not isinstance(failure.get("id"), str):
            errors.append("part.failed entries must be objects with an id")
            continue
        if failure["id"] not in requested:
            errors.append(f"part.failed page {failure['id']!r} was not requested")
        if failure["id"] in completed:
            errors.append(f"part.failed page {failure['id']!r} is also completed")
        if not isinstance(failure.get("error"), str) or not failure["error"]:
            errors.append("part.failed entries must describe the error")
    if not isinstance(part.get("worker_id"), str) or not part["worker_id"]:
        errors.append("part.worker_id must be a non-empty string")
    if not isinstance(part.get("part_sha256"), str):
        errors.append("part.part_sha256 must be present")
    else:
        body = {k: v for k, v in part.items() if k != "part_sha256"}
        if digest_object(body) != part["part_sha256"]:
            errors.append("part.part_sha256 does not match the part body")
    return errors
