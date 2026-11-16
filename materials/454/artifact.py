"""Persisted artifacts: atomic save, integrity-checked load, inspection.

An artifact is only trusted after its schema and its self-digest check out.
Opening a damaged artifact never silently rewrites it: the on-disk bytes are
kept, and callers get an explicit "not estimable / incomplete" report.
"""
from __future__ import annotations

import json
import os
import tempfile

from collect import validate_part
from design import validate_plan
from merge import IDENTITY_KEYS
from util import (
    PART_SCHEMA,
    PLAN_SCHEMA,
    SAMPLE_SCHEMA,
    canonical_json,
    digest_object,
    id_sort_key,
    validate_rows,
)


class ArtifactError(ValueError):
    pass


def _atomic_write(path, text):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save(obj, path):
    _atomic_write(path, canonical_json(obj) + "\n")
    return path


def load(path):
    """Load and fully verify an artifact; raise ArtifactError if untrusted."""
    sample, problems = load_with_report(path)
    if problems:
        raise ArtifactError("; ".join(problems))
    return sample


def load_with_report(path):
    """Return (parsed_object_or_None, problems).  Never mutates the file."""
    problems = []
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        return None, [f"cannot read {path!r}: {exc}"]
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"{path!r} is not valid JSON: {exc}"]
    if not isinstance(obj, dict):
        return None, [f"{path!r} does not contain a JSON object"]
    schema = obj.get("schema")
    if schema == SAMPLE_SCHEMA:
        problems.extend(validate_sample(obj))
    elif schema == PART_SCHEMA:
        problems.extend(validate_part(obj))
    elif schema == PLAN_SCHEMA:
        problems.extend(validate_plan(obj))
    else:
        problems.append(f"unknown artifact schema: {schema!r}")
    return obj, problems


def validate_sample(sample):
    """Structural validation of a merged sample artifact (no source access)."""
    errors = []
    if not isinstance(sample, dict):
        return ["sample must be a JSON object"]
    if sample.get("schema") != SAMPLE_SCHEMA:
        errors.append(f"sample.schema must be {SAMPLE_SCHEMA!r}")
    plan = sample.get("plan")
    plan_errors = validate_plan(plan)
    if plan_errors:
        errors.append("sample.plan is invalid: " + "; ".join(plan_errors))
        return errors
    job = sample.get("job")
    if not isinstance(job, dict):
        errors.append("sample.job must be an object")
    else:
        expected = {
            "job_id": plan["job_id"],
            "plan_sha256": plan["plan_sha256"],
            "source_name": plan["source"]["name"],
            "catalog_sha256": plan["source"]["catalog_sha256"],
        }
        for key in IDENTITY_KEYS:
            if job.get(key) != expected[key]:
                errors.append(f"sample.job.{key} does not match sample.plan")
    status = sample.get("status")
    if status not in ("complete", "incomplete"):
        errors.append("sample.status must be 'complete' or 'incomplete'")
    pages = sample.get("pages")
    if not isinstance(pages, list):
        errors.append("sample.pages must be an array")
        return errors
    selected = set(plan["selected"])
    population = {page["id"]: page for page in plan["population"]["pages"]}
    seen = set()
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("id"), str):
            errors.append("sample.pages entries must be objects with an id")
            continue
        pid = page["id"]
        if pid in seen:
            errors.append(f"sample.pages contains duplicate page id {pid!r}")
        seen.add(pid)
        if pid not in selected:
            errors.append(f"sample.pages contains unselected page {pid!r}")
            continue
        meta = population[pid]
        if page.get("page_sha256") != meta["sha256"]:
            errors.append(f"page {pid!r} page_sha256 does not match the frozen catalog")
        if page.get("n_rows") != meta["rows"]:
            errors.append(f"page {pid!r} n_rows does not match the frozen catalog")
        rows = page.get("rows")
        row_errors = validate_rows(rows)
        if row_errors:
            errors.append(f"page {pid!r}: {row_errors[0]}")
        elif page.get("content_sha256") != digest_object(rows):
            errors.append(f"page {pid!r} content_sha256 does not match its rows")
    coverage = sample.get("coverage")
    if not isinstance(coverage, dict):
        errors.append("sample.coverage must be an object")
    else:
        if coverage.get("selected") != plan["selected"]:
            errors.append("sample.coverage.selected does not match plan.selected")
        if not isinstance(coverage.get("missing"), list) or not isinstance(
            coverage.get("covered"), list
        ):
            errors.append("sample.coverage must record covered and missing lists")
        else:
            if sorted(coverage["covered"], key=id_sort_key) != sorted(seen, key=id_sort_key):
                errors.append("sample.coverage.covered does not match sample.pages")
            conflicts = sample.get("conflicts")
            if not isinstance(conflicts, list):
                errors.append("sample.conflicts must be an array")
                conflicts = []
            conflict_ids = {
                item.get("page_id") for item in conflicts if isinstance(item, dict)
            }
            missing = [
                pid for pid in plan["selected"] if pid not in seen and pid not in conflict_ids
            ]
            if sorted(coverage["missing"], key=id_sort_key) != sorted(missing, key=id_sort_key):
                errors.append("sample.coverage.missing is inconsistent with sample.pages")
            if (missing or conflict_ids) and status != "incomplete":
                errors.append("sample with missing/conflicting pages must be incomplete")
            if not missing and not conflict_ids:
                if sorted(coverage["covered"], key=id_sort_key) != sorted(
                    plan["selected"], key=id_sort_key
                ):
                    errors.append("sample claims complete without covering every selected page")
                if status != "complete" and not any(
                    issue.get("fatal") for issue in sample.get("issues", [])
                ):
                    errors.append("sample is marked incomplete without a fatal issue")
    if not isinstance(sample.get("sample_sha256"), str):
        errors.append("sample.sample_sha256 is missing")
    else:
        body = {k: v for k, v in sample.items() if k != "sample_sha256"}
        if digest_object(body) != sample["sample_sha256"]:
            errors.append("sample.sample_sha256 does not match the sample body")
    return errors


def audit_sample(sample):
    """Individually re-check every page in a (possibly damaged) sample artifact.

    This is what keeps checkable work usable even when the artifact as a whole
    cannot be estimated: each page is verified against the frozen catalog and
    its own recorded content digest.  The job still reports ``incomplete``.
    """
    report = {
        "job": None,
        "status": None,
        "artifact_problems": [],
        "checkable_pages": [],
        "rejected_pages": [],
        "complete": False,
        "estimable": False,
    }
    problems = validate_sample(sample)
    report["artifact_problems"] = problems
    if not isinstance(sample, dict):
        return report
    plan = sample.get("plan")
    if not isinstance(plan, dict):
        return report
    report["job"] = sample.get("job")
    report["status"] = sample.get("status")
    try:
        population = {page["id"]: page for page in plan["population"]["pages"]}
        selected = list(plan["selected"])
    except (KeyError, TypeError):
        return report
    pages = sample.get("pages")
    if not isinstance(pages, list):
        return report
    for page in pages:
        if not isinstance(page, dict):
            continue
        pid = page.get("id")
        meta = population.get(pid)
        reason = None
        if meta is None:
            reason = "page is not in the frozen catalog population"
        elif pid not in selected:
            reason = "page is not in the frozen selection"
        else:
            rows = page.get("rows")
            row_errors = validate_rows(rows)
            if row_errors:
                reason = row_errors[0]
            elif len(rows) != meta["rows"]:
                reason = f"{len(rows)} rows but catalog says {meta['rows']}"
            elif page.get("content_sha256") != digest_object(rows):
                reason = "recorded content digest does not match the rows"
            elif page.get("page_sha256") not in (None, meta["sha256"]):
                reason = "page bytes digest does not match the frozen catalog"
        if reason is None:
            report["checkable_pages"].append(
                {
                    "id": pid,
                    "n_rows": len(page["rows"]),
                    "total": sum(row["value"] for row in page["rows"]),
                    "content_sha256": page.get("content_sha256"),
                }
            )
        else:
            report["rejected_pages"].append({"id": pid, "reason": reason})
    checkable = {page["id"] for page in report["checkable_pages"]}
    report["complete"] = (
        not problems
        and sample.get("status") == "complete"
        and checkable == set(selected)
    )
    report["estimable"] = report["complete"]
    return report


def inspect_sample(path):
    """Human/machine readable inspection that works even on damaged files."""
    obj, problems = load_with_report(path)
    report = {
        "path": path,
        "loadable": obj is not None,
        "schema_ok": obj is not None and not problems,
        "problems": problems,
        "estimable_as_complete": False,
    }
    if not isinstance(obj, dict):
        return report
    schema = obj.get("schema")
    report["schema"] = schema
    if schema == SAMPLE_SCHEMA:
        report["status"] = obj.get("status")
        report["job"] = obj.get("job")
        report["coverage"] = obj.get("coverage")
        report["page_status"] = obj.get("page_status")
        report["digest_ok"] = not any("sha256" in p and "not match" in p for p in problems)
        report["estimable_as_complete"] = (
            not problems and obj.get("status") == "complete"
        )
        report["workers"] = [
            {
                "worker_id": worker.get("worker_id"),
                "accepted": worker.get("accepted"),
                "completed": worker.get("completed"),
                "reads": worker.get("reads"),
            }
            for worker in obj.get("workers", [])
        ]
        report["issues"] = obj.get("issues", [])
        report["duplicates"] = obj.get("duplicates", [])
        report["conflicts"] = [
            {"page_id": c.get("page_id"), "deliveries": len(c.get("deliveries", []))}
            for c in obj.get("conflicts", [])
        ]
        report["audit"] = audit_sample(obj)
    elif schema == PART_SCHEMA:
        report["worker_id"] = obj.get("worker_id")
        report["identity"] = obj.get("identity")
        report["completed"] = obj.get("completed")
        report["reads"] = obj.get("reads")
    elif schema == PLAN_SCHEMA:
        report["job_id"] = obj.get("job_id")
        report["design"] = obj.get("design")
        report["source"] = obj.get("source")
        report["selected"] = obj.get("selected")
        report["inclusion_probabilities"] = obj.get("inclusion_probabilities")
    return report


def assert_identity(sample, expected_job):
    """Refuse to mix an artifact from a different frozen job."""
    job = sample.get("job") or {}
    wrong = [key for key in IDENTITY_KEYS if job.get(key) != expected_job.get(key)]
    if wrong:
        raise ArtifactError("artifact belongs to a different job/source: " + ", ".join(wrong))
    return True
