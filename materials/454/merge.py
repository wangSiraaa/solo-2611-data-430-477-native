"""Merge worker parts into one auditable job artifact.

Merge never contacts the source.  It

* binds every part to the frozen plan's job identity (job id, plan digest,
  source name and catalog digest), so another job or another source cannot be
  slipped in;
* checks the plan's selection list against the delivered pages;
* deduplicates identical redeliveries of the same page (a redelivery is not a
  new sample);
* flags a page delivered with different content, or a selected page that is
  absent, as an incomplete job;
* salvages individually checkable pages from a damaged part, but never calls
  such a job complete.

The merge result keeps the real rows so that the sample can be re-checked
without reading the source again.
"""
from __future__ import annotations

import time

from collect import validate_part
from design import plan_identity, validate_plan
from util import (
    SAMPLE_SCHEMA,
    digest_object,
    id_sort_key,
    page_content_digest,
    page_total,
    validate_rows,
)


class MergeError(ValueError):
    pass


IDENTITY_KEYS = ("job_id", "plan_sha256", "source_name", "catalog_sha256")


def _issue(code, detail, fatal=True, worker_id=None, page_id=None):
    entry = {"code": code, "detail": detail, "fatal": bool(fatal)}
    if worker_id is not None:
        entry["worker_id"] = worker_id
    if page_id is not None:
        entry["page_id"] = page_id
    return entry


def _timestamp(clock):
    value = clock()
    return int(value) if not isinstance(value, float) else float(value)


def _check_page(page, population, selected_set, worker_id, part_sha, issues):
    """Validate one delivered page.  Return a delivery record or None."""
    if not isinstance(page, dict) or not isinstance(page.get("id"), str):
        issues.append(
            _issue(
                "page_schema_error",
                "a delivered page is not an object with an id",
                worker_id=worker_id,
            )
        )
        return None
    pid = page["id"]
    if pid not in selected_set:
        issues.append(
            _issue(
                "unexpected_page",
                f"part delivered page {pid!r} that the frozen plan did not select",
                worker_id=worker_id,
                page_id=pid,
            )
        )
        return None
    meta = population[pid]
    rows = page.get("rows")
    row_errors = validate_rows(rows)
    if row_errors:
        issues.append(
            _issue(
                "row_schema_error",
                f"page {pid!r} rows are invalid: {row_errors[0]}",
                worker_id=worker_id,
                page_id=pid,
            )
        )
        return None
    content = page_content_digest(rows)
    if page.get("content_sha256") not in (None, content):
        issues.append(
            _issue(
                "content_digest_mismatch",
                f"page {pid!r} rows do not match their recorded content digest",
                worker_id=worker_id,
                page_id=pid,
            )
        )
        return None
    if page.get("n_rows") not in (None, len(rows)):
        issues.append(
            _issue(
                "row_count_mismatch",
                f"page {pid!r} page record n_rows does not match its rows",
                worker_id=worker_id,
                page_id=pid,
            )
        )
        return None
    if len(rows) != meta["rows"]:
        issues.append(
            _issue(
                "row_count_mismatch",
                f"page {pid!r} has {len(rows)} rows, catalog says {meta['rows']}",
                worker_id=worker_id,
                page_id=pid,
            )
        )
        return None
    page_sha = page.get("page_sha256")
    if not isinstance(page_sha, str) or not page_sha:
        issues.append(
            _issue(
                "page_digest_missing",
                f"page {pid!r} has no recorded source bytes digest and cannot be verified",
                worker_id=worker_id,
                page_id=pid,
            )
        )
        return None
    if page_sha != meta["sha256"]:
        issues.append(
            _issue(
                "page_digest_mismatch",
                f"page {pid!r} bytes digest does not match the frozen catalog",
                worker_id=worker_id,
                page_id=pid,
            )
        )
        return None
    return {
        "page_id": pid,
        "worker_id": worker_id,
        "part_sha256": part_sha,
        "page_sha256": page_sha,
        "content_sha256": content,
        "n_rows": len(rows),
        "total": page_total(rows),
        "rows": rows,
    }


def merge(plan, parts, clock=time.time):
    """Merge parts for exactly one frozen job plan."""
    plan_errors = validate_plan(plan)
    if plan_errors:
        raise MergeError("plan is invalid: " + "; ".join(plan_errors))
    if not isinstance(parts, list):
        raise MergeError("parts must be a list")

    population = {page["id"]: page for page in plan["population"]["pages"]}
    selected = list(plan["selected"])
    selected_set = set(selected)
    expected_identity = plan_identity(plan)

    issues = []
    workers = []
    deliveries = {pid: [] for pid in selected}
    duplicate_records = []

    for index, part in enumerate(parts):
        label = None
        if isinstance(part, dict):
            label = part.get("worker_id") or f"part[{index}]"
        part_errors = validate_part(part)
        if part_errors:
            detail = "part is not a valid collection artifact: " + "; ".join(part_errors)
            salvageable = (
                isinstance(part, dict)
                and isinstance(part.get("identity"), dict)
                and all(part["identity"].get(k) == expected_identity[k] for k in IDENTITY_KEYS)
                and isinstance(part.get("pages"), list)
            )
            issues.append(
                _issue(
                    "part_schema_error",
                    detail + (" (individually checkable pages were salvaged)" if salvageable else ""),
                    worker_id=label,
                )
            )
            salvaged = []
            if salvageable:
                for page in part["pages"]:
                    delivery = _check_page(
                        page,
                        population,
                        selected_set,
                        label,
                        part.get("part_sha256"),
                        issues,
                    )
                    if delivery is not None:
                        deliveries[delivery["page_id"]].append(delivery)
                        salvaged.append(delivery["page_id"])
            workers.append(
                {
                    "worker_id": label,
                    "part_sha256": part.get("part_sha256") if isinstance(part, dict) else None,
                    "accepted": False,
                    "detail": part_errors,
                    "salvaged_pages": salvaged,
                }
            )
            continue

        identity = part["identity"]
        mismatched = [key for key in IDENTITY_KEYS if identity.get(key) != expected_identity[key]]
        if mismatched:
            issues.append(
                _issue(
                    "foreign_part",
                    "part identity differs from the frozen job in: " + ", ".join(mismatched),
                    worker_id=part["worker_id"],
                )
            )
            workers.append(
                {
                    "worker_id": part["worker_id"],
                    "part_sha256": part["part_sha256"],
                    "accepted": False,
                    "detail": {"foreign_identity_fields": mismatched},
                    "requested": part.get("requested", []),
                    "completed": part.get("completed", []),
                    "reads": part.get("reads", []),
                }
            )
            continue

        workers.append(
            {
                "worker_id": part["worker_id"],
                "part_sha256": part["part_sha256"],
                "accepted": True,
                "requested": part["requested"],
                "completed": part["completed"],
                "failed": part.get("failed", []),
                "reads": part["reads"],
            }
        )
        for failure in part.get("failed", []):
            # A failed read is only fatal if no other worker covers the page;
            # that is decided below by the missing-page check.
            issues.append(
                _issue(
                    "page_read_failure",
                    f"worker could not read page {failure.get('id')!r}: {failure.get('error')}",
                    fatal=False,
                    worker_id=part["worker_id"],
                    page_id=failure.get("id"),
                )
            )
        for page in part["pages"]:
            delivery = _check_page(
                page,
                population,
                selected_set,
                part["worker_id"],
                part["part_sha256"],
                issues,
            )
            if delivery is not None:
                deliveries[delivery["page_id"]].append(delivery)

    accepted_pages = []
    conflicts = []
    conflict_pages = set()
    for pid in selected:
        entries = sorted(
            deliveries[pid], key=lambda entry: (entry["worker_id"] or "", entry["part_sha256"] or "")
        )
        if not entries:
            continue
        by_content = {}
        for entry in entries:
            by_content.setdefault(entry["content_sha256"], []).append(entry)
        if len(by_content) > 1:
            conflict_pages.add(pid)
            issues.append(
                _issue(
                    "page_content_conflict",
                    f"page {pid!r} was delivered with {len(by_content)} different contents",
                    page_id=pid,
                )
            )
            conflicts.append(
                {
                    "page_id": pid,
                    "deliveries": [
                        {
                            "worker_id": entry["worker_id"],
                            "part_sha256": entry["part_sha256"],
                            "page_sha256": entry["page_sha256"],
                            "content_sha256": entry["content_sha256"],
                            "n_rows": entry["n_rows"],
                            "total": entry["total"],
                            "rows": entry["rows"],
                        }
                        for entry in entries
                    ],
                }
            )
            continue
        kept = entries[0]
        accepted_pages.append(
            {
                "id": pid,
                "n_rows": kept["n_rows"],
                "page_sha256": kept["page_sha256"],
                "content_sha256": kept["content_sha256"],
                "total": kept["total"],
                "rows": kept["rows"],
            }
        )
        for duplicate in entries[1:]:
            duplicate_records.append(
                {
                    "page_id": pid,
                    "worker_id": duplicate["worker_id"],
                    "part_sha256": duplicate["part_sha256"],
                    "content_sha256": duplicate["content_sha256"],
                    "kept_worker_id": kept["worker_id"],
                    "counted_as_new_sample": False,
                }
            )

    covered = [page["id"] for page in accepted_pages]
    missing = [pid for pid in selected if pid not in set(covered) and pid not in conflict_pages]
    for pid in missing:
        issues.append(
            _issue(
                "missing_page",
                f"selected page {pid!r} has no accepted delivery",
                page_id=pid,
            )
        )
    if not selected:
        issues.append(
            _issue(
                "no_selected_pages",
                "the design selected no pages; the estimate is 0 and the design variance is 0",
                fatal=False,
            )
        )

    fatal = any(issue["fatal"] for issue in issues)
    status = "incomplete" if fatal else "complete"
    page_status = {}
    for pid in selected:
        if pid in conflict_pages:
            page_status[pid] = "conflict"
        elif pid in set(covered):
            page_status[pid] = "accepted"
        else:
            page_status[pid] = "missing"
    # Canonical ordering so the artifact digest does not depend on the order in
    # which worker parts were handed to merge.
    workers_sorted = sorted(
        workers,
        key=lambda worker: (
            worker.get("worker_id") or "",
            worker.get("part_sha256") or "",
        ),
    )
    issues_sorted = sorted(
        issues,
        key=lambda issue: (
            issue.get("code") or "",
            issue.get("page_id") or "",
            issue.get("worker_id") or "",
            issue.get("detail") or "",
        ),
    )
    sample = {
        "schema": SAMPLE_SCHEMA,
        "job": expected_identity,
        "plan": plan,
        "status": status,
        "pages": sorted(accepted_pages, key=lambda page: id_sort_key(page["id"])),
        "page_status": page_status,
        "coverage": {"selected": selected, "covered": covered, "missing": missing},
        "workers": workers_sorted,
        "deliveries": {
            pid: [
                {k: v for k, v in entry.items() if k != "rows"}
                for entry in sorted(
                    deliveries[pid],
                    key=lambda e: (e["worker_id"] or "", e["part_sha256"] or ""),
                )
            ]
            for pid in selected
        },
        "duplicates": sorted(
            duplicate_records,
            key=lambda item: (
                item.get("page_id") or "",
                item.get("worker_id") or "",
                item.get("part_sha256") or "",
            ),
        ),
        "conflicts": sorted(conflicts, key=lambda item: id_sort_key(item["page_id"])),
        "issues": issues_sorted,
        "created_at": _timestamp(clock),
        "notes": [
            "HT estimates use whole-page totals and the frozen inclusion probabilities.",
            "An identical duplicate delivery is counted once, never as a new sample.",
            "A conflicting or absent selected page makes the job incomplete and non-estimable.",
            "Parts from another job or source are rejected and never pooled.",
            "Checkable pages salvaged from a damaged part do not make the job complete.",
        ],
    }
    sample["sample_sha256"] = digest_object(sample)
    return sample


def independent_samples(samples):
    """Keep per-job artifacts as separate estimates; never pool their pages."""
    if not isinstance(samples, list):
        raise MergeError("samples must be a list")
    entries = []
    seen = {}
    for sample in samples:
        if not isinstance(sample, dict) or sample.get("schema") != SAMPLE_SCHEMA:
            raise MergeError("every entry must be a sample artifact")
        job = sample.get("job") or {}
        key = (job.get("job_id"), job.get("plan_sha256"))
        if key in seen:
            previous = seen[key]
            if previous.get("sample_sha256") != sample.get("sample_sha256"):
                raise MergeError(
                    "two different artifacts claim the same job/plan; keep them apart and resolve first"
                )
            continue
        seen[key] = sample
        entries.append(sample)
    return {
        "schema": "page-sample-independent-list/1",
        "note": "Each entry is an independent estimate for its own frozen job; pages are never pooled across jobs.",
        "entries": entries,
    }
