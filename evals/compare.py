"""Compare two result files and print per-question deltas. OWNER: C.

    python -m evals.compare evals/results/phase1_baseline.json evals/results/candidate.json
    python -m evals.compare evals/results/phase2_flat.json evals/results/phase2_planner.json --no-floor

OLD is the reference, NEW is the candidate. Both files must come from the same answer key.

Exit codes
    0  NEW holds the regression floor (or the key has none, or --no-floor was given)
    1  REGRESSION: NEW passes fewer runs than the floor for its key.
       Floors live in config.REGRESSION_FLOORS: original key 22/30 runs, held-out key 15/30 runs
       (the bottom of the recorded Phase 1 ranges). With a different run count the floor is scaled to the
       same pass rate, rounded up.
    2  the files cannot be compared (unreadable, different keys, or a stub file)

Comparing a planner-off file with a planner-on file is the normal Phase 2 use; the floor is only a gate for
the two Phase 1 keys. A comparison never hides a worse number: every question that got worse is listed.
"""
import json
import math
from fractions import Fraction
from pathlib import Path

import typer

from app import config

cli = typer.Typer(add_completion=False)


class CompareError(Exception):
    """The two files cannot be compared."""


# ---------------------------------------------------------------- loading
def load_result(path: Path) -> dict:
    """Read a result file written by evals.run_evals and check it has the parts the comparison needs."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CompareError(f"cannot read result file {path}: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("per_question"), list) \
            or not isinstance(data.get("score"), dict) or "key" not in data:
        raise CompareError(f"{path} is not a result file written by evals.run_evals")
    if data.get("agent") == "stub":
        raise CompareError(f"{path} was recorded with the stub agent; a stub run is never a result")
    for q in data["per_question"]:
        if not {"id", "passes", "runs"} <= set(q):
            raise CompareError(f"{path}: per_question entry {q!r} lacks id, passes or runs")
    return data


# ---------------------------------------------------------------- the floor
def floor_required(key_name: str, runs_total: int) -> int | None:
    """Fewest runs NEW must pass for this key, or None if the key has no floor. Scaled when runs_total != reference."""
    ref = config.REGRESSION_FLOORS.get(key_name)
    if ref is None:
        return None
    ref_pass, ref_total = ref
    if runs_total == ref_total:
        return ref_pass
    return math.ceil(Fraction(ref_pass, ref_total) * runs_total) if runs_total else 1


# ---------------------------------------------------------------- comparison
def _rate(passes: int, runs: int) -> Fraction:
    return Fraction(passes, runs) if runs else Fraction(0)


def _verdict(old_rate: Fraction, new_rate: Fraction) -> str:
    if new_rate == old_rate:
        return "same"
    if new_rate > old_rate:
        return "FIXED" if new_rate == 1 else "better"
    return "REGRESSED" if old_rate == 1 else "worse"


def _reasons_by_question(data: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in data.get("failures") or []:
        seen = out.setdefault(f.get("id"), [])
        for reason in f.get("reasons") or []:
            if reason not in seen:
                seen.append(reason)
    return out


def _totals(data: dict, name: str) -> dict:
    return (data.get("totals") or {}).get(name) or {}


def _warnings(old: dict, new: dict) -> list[str]:
    warn = []
    if old.get("repeat") != new.get("repeat"):
        warn.append(f"repeat differs (OLD {old.get('repeat')}, NEW {new.get('repeat')}); pass rates are comparable, run counts are not")
    if old.get("mode") != new.get("mode"):
        warn.append(f"grading mode differs (OLD {old.get('mode')}, NEW {new.get('mode')})")
    if old.get("today") != new.get("today"):
        warn.append(f"frozen TODAY differs (OLD {old.get('today')}, NEW {new.get('today')})")
    if old.get("model") != new.get("model"):
        warn.append(f"model differs (OLD {old.get('model')}, NEW {new.get('model')})")
    if old.get("agent_mode") and new.get("agent_mode") and old["agent_mode"] != new["agent_mode"]:
        warn.append(f"agent mode differs (OLD {old.get('agent_mode')}, NEW {new.get('agent_mode')})")
    if old.get("enabled_sources") and new.get("enabled_sources") and old["enabled_sources"] != new["enabled_sources"] \
            and old.get("agent_mode") == new.get("agent_mode"):
        warn.append(f"enabled sources differ (OLD {old['enabled_sources']}, NEW {new['enabled_sources']})")
    if "phase1_jira_only" in (old.get("agent_mode"), new.get("agent_mode")) and str(new.get("key", "")).startswith("phase2"):
        warn.append("a file was recorded with the Phase 1 graph (Jira only) on a Phase 2 key: it is not a flat baseline, "
                    "so do not present it as the planner's before/after")
    oe, ne = old.get("environment"), new.get("environment")
    if not oe or not ne:
        side = "Neither file has" if not oe and not ne else ("OLD has no" if not oe else "NEW has no")
        warn.append(f"{side} environment record (recorded before Phase 2), so machine and model weights "
                    "cannot be checked; only compare official files recorded on one machine and one model version")
    else:
        if (oe.get("machine") or {}).get("node") != (ne.get("machine") or {}).get("node"):
            warn.append("recorded on different machines (Day 0 decision 8: official files use ONE machine)")
        of, nf = oe.get("model_fingerprint"), ne.get("model_fingerprint")
        if of and nf and of.get("digest") != nf.get("digest"):
            warn.append("the Ollama model digest differs: not the same model weights")
        elif not of or not nf:
            warn.append("model digest not recorded in one file (Ollama was unreachable); weights cannot be confirmed equal")
        for side, env in (("OLD", oe), ("NEW", ne)):
            if env.get("faults"):
                warn.append(f"{side} was recorded with fault injection active {env['faults']}: not a measurement")
    return warn


def compare(old: dict, new: dict, apply_floor: bool = True) -> dict:
    """Pure comparison of two loaded result files. Raises CompareError if they use different keys."""
    if old["key"] != new["key"]:
        raise CompareError(f"different answer keys (OLD {old['key']}, NEW {new['key']}); compare runs of one key")
    old_q = {q["id"]: q for q in old["per_question"]}
    new_q = {q["id"]: q for q in new["per_question"]}
    reasons = _reasons_by_question(new)
    rows = []
    for qid in [i for i in old_q if i in new_q]:
        o, n = old_q[qid], new_q[qid]
        o_rate, n_rate = _rate(o["passes"], o["runs"]), _rate(n["passes"], n["runs"])
        verdict = _verdict(o_rate, n_rate)
        rows.append({"id": qid, "old_passes": o["passes"], "old_runs": o["runs"],
                     "new_passes": n["passes"], "new_runs": n["runs"],
                     "delta_points": round(float(n_rate - o_rate) * 100),
                     "verdict": verdict,
                     "old_median_ms": o.get("median_latency_ms"), "new_median_ms": n.get("median_latency_ms"),
                     "new_reasons": reasons.get(qid, []) if verdict in ("worse", "REGRESSED") else []})
    os_, ns = old["score"], new["score"]
    overall = {"old_runs_passed": os_["runs_passed"], "old_runs_total": os_["runs_total"],
               "new_runs_passed": ns["runs_passed"], "new_runs_total": ns["runs_total"],
               "old_questions_passed": os_.get("questions_passed"), "new_questions_passed": ns.get("questions_passed"),
               "questions_total": ns.get("questions_total"),
               "old_latency": old.get("latency_ms") or {}, "new_latency": new.get("latency_ms") or {},
               "old_tokens": _totals(old, "tokens").get("median_per_run"),
               "new_tokens": _totals(new, "tokens").get("median_per_run"),
               "old_steps": _totals(old, "steps").get("median_per_run"),
               "new_steps": _totals(new, "steps").get("median_per_run"),
               "new_planner": _totals(new, "planner") or None,
               "old_planner_enabled": old.get("planner_enabled"), "new_planner_enabled": new.get("planner_enabled")}
    floor = None
    required = floor_required(new["key"], ns["runs_total"]) if apply_floor else None
    if required is not None:
        floor = {"key": new["key"], "required": required, "got": ns["runs_passed"], "total": ns["runs_total"],
                 "ok": ns["runs_passed"] >= required}
    return {"key": new["key"], "rows": rows,
            "added": sorted(set(new_q) - set(old_q)), "removed": sorted(set(old_q) - set(new_q)),
            "overall": overall, "warnings": _warnings(old, new), "floor": floor,
            "regression": bool(floor and not floor["ok"])}


# ---------------------------------------------------------------- output
def _ms(value) -> str:
    return "-" if value is None else f"{value / 1000:.1f}s"


def render(report: dict, old_name: str = "OLD", new_name: str = "NEW") -> str:
    o = report["overall"]
    on = lambda flag: "" if flag is None else (" (planner on)" if flag else " (planner off)")
    out = [f"Key: {report['key']}", f"OLD: {old_name}{on(o['old_planner_enabled'])}",
           f"NEW: {new_name}{on(o['new_planner_enabled'])}", ""]
    out.append(f"{'ID':<6}{'OLD':<8}{'NEW':<8}{'DELTA':<9}{'VERDICT':<11}{'MEDIAN OLD -> NEW'}")
    for r in report["rows"]:
        out.append(f"{r['id']:<6}{str(r['old_passes']) + '/' + str(r['old_runs']):<8}"
                   f"{str(r['new_passes']) + '/' + str(r['new_runs']):<8}{r['delta_points']:+d} pts".ljust(31)
                   + f"{r['verdict']:<11}{_ms(r['old_median_ms'])} -> {_ms(r['new_median_ms'])}")
        for why in r["new_reasons"]:
            out.append(f"        now failing: {why}")
    if report["added"]:
        out.append(f"Only in NEW: {', '.join(report['added'])}")
    if report["removed"]:
        out.append(f"Only in OLD: {', '.join(report['removed'])}")
    out.append("")
    out.append(f"RUNS:      {o['old_runs_passed']}/{o['old_runs_total']} -> {o['new_runs_passed']}/{o['new_runs_total']}"
               f"  ({o['new_runs_passed'] - o['old_runs_passed']:+d} runs)" if o["old_runs_total"] == o["new_runs_total"]
               else f"RUNS:      {o['old_runs_passed']}/{o['old_runs_total']} -> {o['new_runs_passed']}/{o['new_runs_total']}")
    if o["old_questions_passed"] is not None and o["new_questions_passed"] is not None:
        out.append(f"QUESTIONS: {o['old_questions_passed']}/{o['questions_total']} -> {o['new_questions_passed']}/{o['questions_total']}")
    ol, nl = o["old_latency"], o["new_latency"]
    out.append(f"LATENCY:   median {_ms(ol.get('median'))} -> {_ms(nl.get('median'))}, worst {_ms(ol.get('worst'))} -> {_ms(nl.get('worst'))}")
    if o["old_steps"] is not None or o["new_steps"] is not None:
        out.append(f"STEPS:     median per run {o['old_steps']} -> {o['new_steps']}")
    if o["old_tokens"] is not None or o["new_tokens"] is not None:
        out.append(f"TOKENS:    median per run {o['old_tokens']} -> {o['new_tokens']}")
    p = o["new_planner"]
    if p and p.get("runs"):
        out.append(f"PLANNER:   {p['runs']} planned runs in NEW, {p['invalid']} invalid plan(s), {p['fallback']} fallback(s)")
    for w in report["warnings"]:
        out.append(f"WARNING: {w}")
    f = report["floor"]
    if f:
        verdict = "ok" if f["ok"] else "REGRESSION"
        out.append(f"FLOOR:     {f['key']} needs at least {f['required']}/{f['total']} runs; NEW has {f['got']}/{f['total']} -> {verdict}")
    elif not report["regression"]:
        out.append("FLOOR:     none applies to this key (or --no-floor)")
    return "\n".join(out)


@cli.command()
def main(
    old: Path = typer.Argument(..., help="Reference result file"),
    new: Path = typer.Argument(..., help="Candidate result file"),
    no_floor: bool = typer.Option(False, "--no-floor", help="Print the deltas but do not gate on the regression floor"),
) -> None:
    try:
        old_data, new_data = load_result(old), load_result(new)
        report = compare(old_data, new_data, apply_floor=not no_floor)
    except CompareError as e:
        typer.echo(f"error: {e}")
        raise typer.Exit(2)
    typer.echo(render(report, Path(old).name, Path(new).name))
    if report["regression"]:
        typer.echo("\nREGRESSION: the candidate is below the recorded Phase 1 floor.")
        raise typer.Exit(1)


if __name__ == "__main__":
    cli()
