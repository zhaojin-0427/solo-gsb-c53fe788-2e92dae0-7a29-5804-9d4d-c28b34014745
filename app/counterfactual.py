"""Counterfactual deletion sets for (possibly non-monotone) query outputs.

For the monotone operators (select / equi-join / project) and for
``count`` / ``min`` / ``max`` (and same-sign ``sum``) the inclusion-minimal
source-row deletion sets that make an output row disappear are exactly the
minimal hitting sets of its minimal witnesses — see
:func:`app.provenance.minimal_hitting_sets`.

``sum`` over mixed positive and negative values is **not** monotone in the
source rows: deleting a row can move the total onto, off, and back onto the
target value, so no witness family alone characterises the killers. This
module therefore:

1. traces the chosen output row backwards through the plan to collect the
   universe of source labels that can influence it, and detects whether any
   ``sum`` aggregate that contributes to the row sees both signs in the
   target group;
2. in that case enumerates candidate deletion sets by increasing size and
   decides membership by **re-running the whole plan on the deleted
   snapshot**, pruning every superset of an already-found minimal killer.

Only inclusion-minimal killers are returned, and the caller receives the
recomputed outcome (full result rows, or the structured recomputation error
— e.g. ``EMPTY_AGGREGATE``) for every surviving candidate.
"""
from __future__ import annotations

from typing import Any

from .errors import ApiError
from .evaluator import Evaluator, Label, Table
from .provenance import label_sort_key
from .values import is_number

# Sentinel used exactly like in Evaluator._aggregate for NULL group keys.
_NULL_KEY = object()


# ---------------------------------------------------------------------------
# Backwards tracing: influence universe + non-monotone-sum detection
# ---------------------------------------------------------------------------

def trace_target(
    ev: Evaluator, node: Any, data: tuple
) -> tuple[set[Label], bool]:
    """Source labels feeding output tuple *data* of *node*, plus a flag.

    The flag is ``True`` when some ``sum`` aggregate on the derivation path
    has both a strictly positive and a strictly negative non-NULL value in
    the group that produces *data* (i.e. witness hitting sets are only a
    monotone approximation of the deletion sets).
    """
    universe: set[Label] = set()
    non_monotone = _trace(ev, node, data, universe, set())
    return universe, non_monotone


def _trace(
    ev: Evaluator,
    node: Any,
    data: tuple,
    universe: set[Label],
    seen: set[tuple[int, tuple]],
) -> bool:
    if not isinstance(node, dict):
        return False
    mark = (id(node), data)
    if mark in seen:
        return False
    seen.add(mark)

    t = node.get("type")
    if t == "scan":
        table = ev.tables[node["table"]]
        for _pk, row_data, label in table.rows:
            if row_data == data:
                universe.add(label)
        return False

    if t == "filter":
        src_node = node["input"]
        return _trace(ev, src_node, data, universe, seen)

    if t == "project":
        src_node = node["input"]
        names = ev.schema(src_node)
        idx = [names.index(frm) for frm, _ in ev._proj_cols(node)]
        flagged = False
        for r in ev.eval(src_node):
            if tuple(r["data"][i] for i in idx) == data:
                flagged |= _trace(ev, src_node, r["data"], universe, seen)
        return flagged

    if t == "join":
        ln = ev.schema(node["left"])
        rn = ev.schema(node["right"])
        on = ev._join_on(node, ln, rn)
        right_key_cols = {rc for _, rc in on}
        left = ev.eval(node["left"])
        right = ev.eval(node["right"])
        flagged = False
        for lr in left:
            for rr in right:
                assembled = list(lr["data"])
                for j, c in enumerate(rn):
                    if c in right_key_cols and c in ln:
                        continue
                    assembled.append(rr["data"][j])
                if tuple(assembled) == data:
                    flagged |= _trace(ev, node["left"], lr["data"], universe, seen)
                    flagged |= _trace(ev, node["right"], rr["data"], universe, seen)
        return flagged

    if t == "aggregate":
        return _trace_aggregate(ev, node, data, universe, seen)

    # Unknown nodes never reach here (evaluation validates the plan first).
    return False


def _trace_aggregate(
    ev: Evaluator,
    node: dict,
    data: tuple,
    universe: set[Label],
    seen: set[tuple[int, tuple]],
) -> bool:
    src_node = node["input"]
    gb = node.get("group_by", [])
    aggs = ev._agg_spec(node)
    names = ev.schema(src_node)
    gi = [names.index(c) for c in gb]

    groups: dict[tuple, list[dict]] = {}
    for r in ev.eval(src_node):
        gkey_vals = tuple(r["data"][i] for i in gi)
        if any(k is None for k in gkey_vals):
            gkey = (_NULL_KEY,) + gkey_vals
        else:
            gkey = gkey_vals
        groups.setdefault(gkey, []).append(r)

    real_key = tuple(data[: len(gb)])
    gkey = (_NULL_KEY,) + real_key if any(k is None for k in real_key) else real_key
    members = groups.get(gkey, [])

    flagged = False
    for fn, col, _as in aggs:
        if fn != "sum":
            continue
        ci = names.index(col)
        vals = [
            m["data"][ci]
            for m in members
            if m["data"][ci] is not None and is_number(m["data"][ci])
        ]
        if any(v < 0 for v in vals) and any(v > 0 for v in vals):
            flagged = True

    for m in members:
        for w in m["witnesses"]:
            universe.update(w)
        flagged |= _trace(ev, src_node, m["data"], universe, seen)
    return flagged


# ---------------------------------------------------------------------------
# Recomputation-based minimal deletion-set enumeration
# ---------------------------------------------------------------------------

def evaluate_deletion(
    plan: dict,
    tables: dict[str, Table],
    deleted: frozenset[Label],
    target_data: tuple,
) -> dict:
    """Recompute the full plan after deleting *deleted*; describe the outcome.

    Outcomes are either ``{"kind": "ok", "columns", "rows", "target_present"}``
    or ``{"kind": "error", "code", "message"}`` (e.g. deleting the last
    members makes an aggregate input empty → EMPTY_AGGREGATE).
    """
    deleted_by_table: dict[str, set[tuple]] = {}
    for lab in deleted:
        deleted_by_table.setdefault(lab[0], set()).add(lab[1])
    filtered = {
        name: t.filtered_copy(deleted_by_table.get(name, set()))
        for name, t in tables.items()
    }
    ev = Evaluator(filtered)
    try:
        rows = ev.eval(plan)
        cols = ev.schema(plan)
    except ApiError as e:
        return {"kind": "error", "code": e.code, "message": e.message}
    return {
        "kind": "ok",
        "columns": cols,
        "rows": rows,
        "target_present": any(r["data"] == target_data for r in rows),
    }


def _kills(outcome: dict) -> bool:
    return outcome["kind"] == "error" or not outcome["target_present"]


def enumerate_minimal_deletions(
    plan: dict,
    tables: dict[str, Table],
    universe: set[Label],
    target_data: tuple,
) -> list[tuple[frozenset[Label], dict]]:
    """All inclusion-minimal deletion sets that make *target_data* disappear.

    Candidates are generated in increasing cardinality; any set containing
    an already-confirmed minimal killer is pruned (it cannot itself be
    minimal). Membership is always decided by a full plan recomputation.
    Results are stably ordered by source labels.
    """
    from itertools import combinations

    labels = sorted(universe, key=label_sort_key)
    n = len(labels)
    killers: list[frozenset[Label]] = []
    outcomes: dict[frozenset[Label], dict] = {}

    for size in range(1, n + 1):
        level = []
        for comb in combinations(labels, size):
            cand = frozenset(comb)
            if not any(k <= cand for k in killers):
                level.append(cand)
        if not level:
            break
        for cand in level:
            outcome = evaluate_deletion(plan, tables, cand, target_data)
            outcomes[cand] = outcome
            if _kills(outcome):
                killers.append(cand)

    def key(deleted: frozenset[Label]):
        return [label_sort_key(l) for l in sorted(deleted, key=label_sort_key)]

    ordered = sorted(killers, key=key)
    return [(d, outcomes[d]) for d in ordered]
