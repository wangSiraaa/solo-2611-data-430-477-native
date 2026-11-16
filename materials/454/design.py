"""Design declaration, frozen selection, and plan validation.

Designs implemented (both public and exactly reproducible):

``wor``
    Simple random sampling without replacement of ``k`` whole pages from the
    ``M`` catalog pages.  Every page has inclusion probability ``k/M``.
``bernoulli``
    Independent Bernoulli inclusion of every whole page with the same
    rational probability ``p``.

Randomisation protocol
----------------------
Ids are ordered by their raw UTF-8 bytes.  For every decision the protocol
hashes the UTF-8 string

    protocol NUL seed NUL page_id [NUL counter]

with SHA-256.  No Python ``random`` state is involved, so the selection can be
replayed from the plan alone.

* ``sha256-rank-v1``: every page gets the hexadecimal digest as a rank; the
  ``k`` smallest ``(digest, id-bytes)`` pairs are selected.  This is a uniform
  simple random sample without replacement (ties are broken deterministically
  by the id bytes).
* ``sha256-bernoulli-v1``: for ``p = a/b`` in lowest terms, draw an integer
  uniformly from ``[0, b)`` by rejection sampling on 256-bit SHA-256 words
  (counter mode), and include the page iff the draw is ``< a``.  The
  acceptance test ``u < floor(2**256/b)*b`` is unbiased, so the inclusion
  probability is exactly ``a/b``.
"""
from __future__ import annotations

import hashlib
from fractions import Fraction

from util import (
    BERNOULLI_PROTOCOL,
    PLAN_SCHEMA,
    WOR_PROTOCOL,
    digest_object,
    fraction_text,
    id_sort_key,
    is_hex_digest,
    normalize_catalog_pages,
    normalize_seed,
    page_population,
    source_digest,
    validate_catalog,
)


class DesignError(ValueError):
    pass


def _sha256(payload: bytes) -> bytes:
    return hashlib.sha256(payload).digest()


def _rank_digest(seed: str, page_id: str) -> str:
    payload = WOR_PROTOCOL.encode("utf-8") + b"\x00" + seed.encode("utf-8") + b"\x00" + page_id.encode("utf-8")
    return _sha256(payload).hex()


def _bernoulli_word(seed: str, page_id: str, counter: int) -> int:
    payload = (
        BERNOULLI_PROTOCOL.encode("utf-8")
        + b"\x00"
        + seed.encode("utf-8")
        + b"\x00"
        + page_id.encode("utf-8")
        + b"\x00"
        + str(counter).encode("ascii")
    )
    return int.from_bytes(_sha256(payload), "big")


def _bernoulli_draw(seed: str, page_id: str, denominator: int):
    """Uniform integer in [0, denominator), plus the reward for auditing."""
    if denominator <= 0:
        raise DesignError("Bernoulli denominator must be positive")
    if denominator == 1:
        return 0, 0
    modulus = 1 << 256
    limit = (modulus // denominator) * denominator
    counter = 0
    while True:
        word = _bernoulli_word(seed, page_id, counter)
        if word < limit:
            return word % denominator, counter + 1
        counter += 1


def _design_spec(design):
    if not isinstance(design, dict):
        raise DesignError("design must be an object")
    kind = design.get("type")
    if kind == "wor":
        k = design.get("k")
        if not isinstance(k, int) or isinstance(k, bool):
            raise DesignError("wor design requires integer k")
        return {
            "type": "wor",
            "k": k,
            "seed": normalize_seed(design.get("seed")),
            "protocol": WOR_PROTOCOL,
            "order": "utf8-bytes",
        }
    if kind == "bernoulli":
        try:
            p = Fraction(design.get("p"))
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            raise DesignError("bernoulli design requires rational p") from exc
        if p <= 0 or p > 1:
            raise DesignError("bernoulli p must satisfy 0 < p <= 1")
        return {
            "type": "bernoulli",
            "p": fraction_text(p),
            "seed": normalize_seed(design.get("seed")),
            "protocol": BERNOULLI_PROTOCOL,
            "order": "utf8-bytes",
        }
    raise DesignError("design.type must be 'wor' or 'bernoulli'")


def _reproduce(catalog, design):
    """Return (selected ids, inclusion probabilities, randomisation record)."""
    pages = normalize_catalog_pages(catalog)
    ids = sorted(pages, key=id_sort_key)
    M = len(ids)
    kind = design["type"]
    if kind == "wor":
        k = design["k"]
        if k < 2 or k > M:
            raise DesignError(f"wor requires 2 <= k <= M; got k={k}, M={M}")
        keys = {pid: _rank_digest(design["seed"], pid) for pid in ids}
        ranked = sorted(ids, key=lambda pid: (keys[pid], id_sort_key(pid)))
        selected = sorted(ranked[:k], key=id_sort_key)
        probs = {pid: Fraction(k, M) for pid in ids}
        randomization = {
            "protocol": WOR_PROTOCOL,
            "seed": design["seed"],
            "order": "utf8-bytes",
            "keys": keys,
            "ranking": ranked,
        }
        return selected, probs, randomization
    if kind == "bernoulli":
        p = Fraction(design["p"])
        selected = []
        draws = {}
        for pid in ids:
            value, attempts = _bernoulli_draw(design["seed"], pid, p.denominator)
            included = value < p.numerator
            draws[pid] = {
                "value": value,
                "denominator": p.denominator,
                "attempts": attempts,
                "included": included,
            }
            if included:
                selected.append(pid)
        selected = sorted(selected, key=id_sort_key)
        probs = {pid: p for pid in ids}
        randomization = {
            "protocol": BERNOULLI_PROTOCOL,
            "seed": design["seed"],
            "order": "utf8-bytes",
            "draws": draws,
        }
        return selected, probs, randomization
    raise DesignError("unsupported design type")


def build_plan(catalog, job_id, design):
    """Freeze a plan from the catalog only; no page body is read here."""
    validate_catalog(catalog)
    if not isinstance(job_id, str) or not job_id:
        raise DesignError("job_id must be a non-empty string")
    spec = _design_spec(design)
    selected, probs, randomization = _reproduce(catalog, spec)
    population = page_population(catalog)
    plan = {
        "schema": PLAN_SCHEMA,
        "job_id": job_id,
        "design": spec,
        "source": {
            "name": catalog["source"],
            "catalog_sha256": source_digest(catalog),
        },
        "catalog": catalog,
        "population": {
            "M": len(population),
            "N": catalog["rows"],
            "pages": population,
        },
        "selected": selected,
        "inclusion_probabilities": {pid: fraction_text(probs[pid]) for pid in probs},
        "randomization": randomization,
    }
    plan["plan_sha256"] = digest_object(plan)
    return plan


def validate_plan(plan):
    """Validate a plan and prove that its selection follows the declared design."""
    errors = []
    if not isinstance(plan, dict):
        return ["plan must be a JSON object"]
    if plan.get("schema") != PLAN_SCHEMA:
        errors.append(f"plan.schema must be {PLAN_SCHEMA!r}")
    job_id = plan.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        errors.append("plan.job_id must be a non-empty string")
    catalog = plan.get("catalog")
    try:
        validate_catalog(catalog)
    except Exception as exc:  # noqa: BLE001 - report as a schema issue
        errors.append(f"plan.catalog is invalid: {exc}")
        return errors
    try:
        spec = _design_spec(plan.get("design"))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"plan.design is invalid: {exc}")
        return errors
    expected_digest = source_digest(catalog)
    source = plan.get("source")
    if not isinstance(source, dict):
        errors.append("plan.source must be an object")
    else:
        if source.get("name") != catalog["source"]:
            errors.append("plan.source.name does not match plan.catalog.source")
        if source.get("catalog_sha256") != expected_digest:
            errors.append("plan.source.catalog_sha256 does not match plan.catalog")
    population = page_population(catalog)
    expected_pop = {
        "M": len(population),
        "N": catalog["rows"],
        "pages": population,
    }
    if plan.get("population") != expected_pop:
        errors.append("plan.population does not match plan.catalog")
    try:
        selected, probs, randomization = _reproduce(catalog, spec)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"plan selection is invalid: {exc}")
        return errors
    if plan.get("selected") != selected:
        errors.append("plan.selected is not reproducible from the frozen design")
    recorded_probs = plan.get("inclusion_probabilities")
    expected_probs = {pid: fraction_text(probs[pid]) for pid in probs}
    if recorded_probs != expected_probs:
        errors.append("plan.inclusion_probabilities do not match the declared design")
    if plan.get("randomization") != randomization:
        errors.append("plan.randomization does not match the declared randomisation protocol")
    if not is_hex_digest(plan.get("plan_sha256")):
        errors.append("plan.plan_sha256 is missing or malformed")
    else:
        body = {k: v for k, v in plan.items() if k != "plan_sha256"}
        if digest_object(body) != plan["plan_sha256"]:
            errors.append("plan.plan_sha256 does not match the plan body")
    return errors


def design_summary(plan):
    design = plan["design"]
    if design["type"] == "wor":
        return {
            "type": "wor",
            "k": design["k"],
            "M": plan["population"]["M"],
            "inclusion_probability": fraction_text(Fraction(design["k"], plan["population"]["M"])),
            "protocol": design["protocol"],
        }
    return {
        "type": "bernoulli",
        "p": design["p"],
        "M": plan["population"]["M"],
        "inclusion_probability": design["p"],
        "protocol": design["protocol"],
    }


def plan_identity(plan):
    """Stable identity used to prevent cross-job / cross-source merges."""
    return {
        "job_id": plan["job_id"],
        "plan_sha256": plan["plan_sha256"],
        "source_name": plan["source"]["name"],
        "catalog_sha256": plan["source"]["catalog_sha256"],
        "design": design_summary(plan),
    }
