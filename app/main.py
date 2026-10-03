"""FastAPI application: relation provenance & counterfactual explanation API."""
from __future__ import annotations

import os
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, ConfigDict, ValidationError

from . import canonical
from .counterfactual import (
    enumerate_minimal_deletions,
    evaluate_deletion,
    trace_target,
)
from .errors import ApiError
from .evaluator import Evaluator, Label, Table
from .provenance import (
    build_symbol_table,
    label_to_string,
    minimal_hitting_sets,
    present_row,
)
from .store import IdempotencyStore
from .values import value_sort_key

DB_PATH = os.environ.get("PROVENANCE_DB", "/data/provenance.db")


# ---------------------------------------------------------------------------
# Request models (strict: unknown fields are rejected)
# ---------------------------------------------------------------------------

class ColumnSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    type: Literal["int", "float", "string", "bool", "any"] = "any"


class TableIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table: str
    primary_key: list[str] = Field(min_length=1)
    columns: list[str | ColumnSpec] | None = None
    rows: list[dict[str, Any]] = Field(default_factory=list)


class ExplainIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Select one output row: either its (stable) index in the sorted result,
    # or an exact full-row match.
    index: int | None = None
    row: dict[str, Any] | None = None


class QueryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=200)
    tables: list[TableIn] = Field(min_length=1)
    plan: dict[str, Any]
    explain: ExplainIn | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def malformed_from_pydantic(exc: ValidationError) -> ApiError:
    issues = []
    for err in exc.errors():
        issues.append(
            {
                "path": list(err["loc"]),
                "message": err["msg"],
                "type": err["type"],
            }
        )
    return ApiError(
        "MALFORMED_REQUEST",
        "Request body does not match the required schema",
        400,
        issues=issues,
    )


def binding_fingerprint(payload: QueryIn) -> str:
    """Deterministic digest of the normalized plan + table snapshot + target.

    Key order in submitted JSON objects never affects the binding; the
    fingerprint is computed over canonical JSON.
    """
    tables_norm = [t.model_dump() for t in payload.tables]
    explain = payload.explain.model_dump() if payload.explain else None
    return canonical.fingerprint(
        {
            "plan": payload.plan,
            "tables": tables_norm,
            "explain": explain,
        }
    )


def load_tables(payload: QueryIn) -> dict[str, Table]:
    tables: dict[str, Table] = {}
    for spec in payload.tables:
        raw = spec.model_dump()
        # Pydantic stringifies ColumnSpec already; keep user ordering.
        if spec.columns is not None:
            raw["columns"] = [
                c if isinstance(c, str) else c.model_dump() for c in spec.columns
            ]
        t = Table(raw)
        if t.name in tables:
            raise ApiError(
                "MALFORMED_REQUEST",
                f"Duplicate table name {t.name!r} in submission",
                table=t.name,
            )
        tables[t.name] = t
    return tables


def resolve_target(explain: ExplainIn | None, presented: list[dict], columns: list[str]) -> int:
    assert explain is not None
    if explain.index is not None and explain.row is not None:
        raise ApiError(
            "MALFORMED_REQUEST",
            "explain must use either 'index' or 'row', not both",
        )
    if explain.index is None and explain.row is None:
        raise ApiError(
            "MALFORMED_REQUEST",
            "explain must specify an output 'index' or an exact 'row'",
        )
    if explain.index is not None:
        if explain.index < 0 or explain.index >= len(presented):
            raise ApiError(
                "TARGET_NOT_FOUND",
                f"output index {explain.index} out of range "
                f"(0..{len(presented) - 1})",
                index=explain.index,
                result_size=len(presented),
            )
        return explain.index

    match_row = explain.row
    if set(match_row.keys()) != set(columns):
        raise ApiError(
            "MALFORMED_REQUEST",
            "explain.row must contain exactly the output columns",
            expected=columns,
            received=sorted(match_row.keys()),
        )
    matches = [
        i for i, p in enumerate(presented) if p["row"] == match_row
    ]
    if not matches:
        raise ApiError(
            "TARGET_NOT_FOUND",
            "explain.row does not match any output row",
        )
    return matches[0]


def _sorted_labels(deleted: frozenset[Label]) -> list[Label]:
    return sorted(
        deleted, key=lambda l: (l[0], tuple(value_sort_key(p) for p in l[1]))
    )


def _scenario(deleted: frozenset[Label], outcome: dict) -> dict:
    """Build one counterfactual scenario from a recomputation outcome."""
    labels = [label_to_string(l) for l in _sorted_labels(deleted)]
    if outcome["kind"] == "error":
        # Deleting rows may make an aggregate input empty: keep the existing
        # structured recompute error instead of fabricating an empty result.
        return {
            "delete": labels,
            "target_present": False,
            "recompute_error": {
                "code": outcome["code"],
                "message": outcome["message"],
            },
        }
    recomputed = [
        {c: v for c, v in zip(outcome["columns"], rr["data"])}
        for rr in outcome["rows"]
    ]
    return {
        "delete": labels,
        "target_present": outcome["target_present"],
        "result": {
            "columns": outcome["columns"],
            "row_count": len(recomputed),
            "rows": recomputed,
        },
    }


def execute_query(payload: QueryIn) -> dict[str, Any]:
    tables = load_tables(payload)
    ev = Evaluator(tables)
    rows = ev.eval(payload.plan)
    columns = ev.schema(payload.plan)

    # Global symbol table over every source row referenced by any witness.
    all_labels: set[Label] = set()
    for r in rows:
        for w in r["witnesses"]:
            all_labels.update(w)
    symbols = build_symbol_table(all_labels)

    presented = [present_row(r, columns, symbols) for r in rows]

    body: dict[str, Any] = {
        "result": {
            "columns": columns,
            "row_count": len(presented),
            "rows": presented,
        },
        "symbol_table": [
            {"symbol": sym, "source": label_to_string(lab)}
            for lab, sym in sorted(symbols.items(), key=lambda kv: kv[1])
        ],
    }

    if payload.explain is not None:
        target_idx = resolve_target(payload.explain, presented, columns)
        target_raw = rows[target_idx]
        target_presented = presented[target_idx]

        # Trace the target backwards through the plan. When a sum aggregate
        # mixes positive and negative values in the target group, witness
        # hitting sets are only a monotone approximation; decide deletion
        # sets by full recomputation instead, so every inclusion-minimal
        # killer is found (including singletons the witnesses miss).
        universe, sum_non_monotone = trace_target(
            ev, payload.plan, target_raw["data"]
        )
        if sum_non_monotone:
            found = enumerate_minimal_deletions(
                payload.plan, tables, universe, target_raw["data"]
            )
            deletions = [h for h, _ in found]
            scenarios = [_scenario(h, outcome) for h, outcome in found]
        else:
            deletions = minimal_hitting_sets(target_raw["witnesses"])
            scenarios = []
            for h in deletions:
                deleted = frozenset(h)
                outcome = evaluate_deletion(
                    payload.plan, tables, deleted, target_raw["data"]
                )
                scenarios.append(_scenario(deleted, outcome))

        body["counterfactual"] = {
            "target": target_presented["row"],
            "target_index": target_idx,
            "target_provenance": target_presented["provenance"],
            "minimal_deletion_count": len(deletions),
            "minimal_deletion_sets": [
                [label_to_string(l) for l in _sorted_labels(h)]
                for h in deletions
            ],
            "scenarios": scenarios,
        }

    return body


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Relation Provenance & Counterfactual Explanation API",
    version="1.0.0",
    docs_url="/docs",
    redoc_url=None,
)

db_dir = os.path.dirname(DB_PATH)
if db_dir:
    os.makedirs(db_dir, exist_ok=True)
store = IdempotencyStore(DB_PATH)


@app.get("/")
def root():
    return Response(
        content=canonical.dumps(
            {
                "service": "relation-provenance-api",
                "version": "1.0.0",
                "endpoints": {
                    "POST /query": "execute plan, return provenance and optional counterfactuals",
                    "GET /health": "liveness probe",
                },
            }
        ),
        media_type="application/json",
    )


@app.get("/health")
def health():
    return Response(content=canonical.dumps({"status": "ok"}), media_type="application/json")


@app.post("/query")
async def query(request: Request):
    raw = await request.body()
    try:
        import json

        payload = QueryIn.model_validate(json.loads(raw))
    except ValidationError as e:
        err = malformed_from_pydantic(e)
        return Response(
            content=canonical.dumps(err.to_body()),
            status_code=err.status_code,
            media_type="application/json",
        )
    except Exception as e:  # JSON decode errors etc.
        err = ApiError("MALFORMED_REQUEST", f"Invalid JSON body: {e}", 400)
        return Response(
            content=canonical.dumps(err.to_body()),
            status_code=400,
            media_type="application/json",
        )

    fp = binding_fingerprint(payload)
    cached = store.get(payload.idempotency_key)
    if cached is not None:
        stored_fp, status, body = cached
        if stored_fp != fp:
            err = ApiError(
                "IDEMPOTENCY_KEY_CONFLICT",
                "Idempotency key was already bound to a different normalized "
                "request (plan, table snapshot or explain target differs)",
                409,
            )
            return Response(
                content=canonical.dumps(err.to_body()),
                status_code=409,
                media_type="application/json",
            )
        return Response(
            content=bytes(body),
            status_code=status,
            media_type="application/json",
            headers={"X-Idempotent-Replay": "true"},
        )

    try:
        result = execute_query(payload)
        body = canonical.dumps({"status": "ok", **result})
        store.put(payload.idempotency_key, fp, 200, body)
        return Response(
            content=body,
            status_code=200,
            media_type="application/json",
            headers={"X-Idempotent-Replay": "false"},
        )
    except ApiError as e:
        body = canonical.dumps(e.to_body())
        # Errors are also pinned to the idempotency key, so a retried invalid
        # request remains byte-identical.
        store.put(payload.idempotency_key, fp, e.status_code, body)
        return Response(
            content=body, status_code=e.status_code, media_type="application/json"
        )
