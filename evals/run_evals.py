"""Eval runner. Phase 1 = mechanical checks + (optional) hand grading; LLM judging is Phase 4.

    python -m evals.run_evals                       # original key, grade by hand (p/f/s)
    python -m evals.run_evals --auto --repeat 3     # no prompts; exit 1 if any question fails
    python -m evals.run_evals --heldout --auto --repeat 3   # same as --key evals/heldout_key.json
    python -m evals.run_evals --no-grade            # record mechanical checks only
    python -m evals.run_evals --stub --auto         # plumbing smoke test (writes *.stub.json)

--auto is a CITATION-LEVEL check: status ok, every expected citation present, no forbidden
citation, no invalid ID. It does not read the answer text. Hand grading still judges wording.
"""
import json
import re
import statistics
import textwrap
from pathlib import Path
from typing import Callable

import typer

from app import config
from app.runtime import get_run_agent

cli = typer.Typer(add_completion=False)
ID_RE = re.compile(r"^[A-Z]+-\d+$")
REQUIRED_KEY_FIELDS = ("id", "question", "expected_answer", "expected_citations")


# ---------------------------------------------------------------- loading
def load_valid_ids(path: Path = None) -> set[str] | None:
    """Ticket IDs that exist in the synthetic data (None if the file can't be read)."""
    path = config.DATA_PATH if path is None else path
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        tickets = data["tickets"] if isinstance(data, dict) and "tickets" in data else data
        return {t["id"] for t in tickets}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def load_key(path: Path) -> list[dict]:
    """Load and validate an answer file (answer_key.json or heldout_key.json)."""
    try:
        questions = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise typer.BadParameter(f"cannot read answer file {path}: {e}")
    if not isinstance(questions, list) or not questions:
        raise typer.BadParameter(f"{path} must be a non-empty JSON list")
    seen = set()
    for q in questions:
        missing = [f for f in REQUIRED_KEY_FIELDS if f not in q]
        if missing:
            raise typer.BadParameter(f"{path}: question {q.get('id', '?')} missing {missing}")
        if q["id"] in seen:
            raise typer.BadParameter(f"{path}: duplicate question id {q['id']}")
        seen.add(q["id"])
        for f in ("expected_citations", "forbidden_citations"):
            if f in q and not isinstance(q[f], list):
                raise typer.BadParameter(f"{path}: {q['id']}.{f} must be a list")
    return questions


def default_out_path(key: Path, stub: bool) -> Path:
    """Separate result files per key; stub runs never overwrite real results."""
    names = {"answer_key.json": "phase1_baseline.json", "heldout_key.json": "phase1_heldout.json"}
    name = names.get(Path(key).name, f"{Path(key).stem}_results.json")
    if stub:
        name = name.replace(".json", ".stub.json")
    return config.RESULTS_DIR / name


# ---------------------------------------------------------------- checks
def mechanical_checks(expected: list[str], result: dict, valid_ids: set[str] | None,
                      forbidden: list[str] = ()) -> dict:
    cited = result.get("citations") or []
    if valid_ids is None:
        invalid = [c for c in cited if not ID_RE.match(c)]  # format-only fallback
    else:
        invalid = [c for c in cited if c not in valid_ids]
    hit = [c for c in dict.fromkeys(cited) if c in set(forbidden)]
    return {"all_expected_citations_found": set(expected) <= set(cited),
            "invalid_citations": invalid, "any_invalid_citation": bool(invalid),
            "forbidden_citations_hit": hit, "any_forbidden_citation": bool(hit)}


def auto_pass(rec: dict) -> bool:
    return (rec["status"] == "ok" and rec["all_expected_citations_found"]
            and not rec["any_forbidden_citation"] and not rec["any_invalid_citation"])


def failure_reasons(rec: dict) -> list[str]:
    why = []
    if rec.get("correct") is False:
        why.append("marked wrong")
    if rec["status"] != "ok":
        why.append(f"status={rec['status']}")
    if not rec["all_expected_citations_found"]:
        why.append("missing expected citation")
    if rec.get("any_forbidden_citation"):
        why.append(f"forbidden citation {rec['forbidden_citations_hit']}")
    if rec["any_invalid_citation"]:
        why.append(f"invalid citation {rec['invalid_citations']}")
    return why


# ---------------------------------------------------------------- running
def run_questions(questions: list[dict], run_agent: Callable[[str], dict], repeat: int,
                  valid_ids: set[str] | None, grader: Callable[[dict, dict], bool | None],
                  echo: Callable[[str], None] = lambda s: None) -> list[dict]:
    """Run every question `repeat` times. grader(question, record) -> True/False/None."""
    records = []
    for q in questions:
        for i in range(1, repeat + 1):
            echo(f"\n=== {q['id']} (run {i}/{repeat}): {q['question']}")
            res = run_agent(q["question"])
            chk = mechanical_checks(q["expected_citations"], res, valid_ids, q.get("forbidden_citations", []))
            rec = {"id": q["id"], "run": i, "question": q["question"], "status": res["status"],
                   "answer": res["answer"], "citations": res["citations"],
                   "expected_citations": q["expected_citations"],
                   "forbidden_citations": q.get("forbidden_citations", []), **chk,
                   "latency_ms": res["latency_ms"], "trace_id": res.get("trace_id")}
            echo(side_by_side("EXPECTED", q["expected_answer"], "ACTUAL", res["answer"]))
            echo(f"  expected: {q['expected_citations']}  forbidden: {rec['forbidden_citations']}  "
                 f"returned: {res['citations']}  status: {res['status']}")
            rec["correct"] = grader(q, rec)
            records.append(rec)
    return records


def aggregate(records: list[dict], min_pass_rate: float = 1.0) -> dict:
    """Per-question and overall stats. A question passes if pass_rate >= min_pass_rate."""
    per_q, order = {}, []
    for r in records:
        if r["id"] not in per_q:
            per_q[r["id"]] = []
            order.append(r["id"])
        per_q[r["id"]].append(r)
    questions = []
    for qid in order:
        runs = per_q[qid]
        passes = sum(1 for r in runs if r["correct"] is True)
        ungraded = sum(1 for r in runs if r["correct"] is None)
        lat = [r["latency_ms"] for r in runs]
        rate = passes / len(runs)
        questions.append({"id": qid, "runs": len(runs), "passes": passes, "ungraded": ungraded,
                          "pass_rate": round(rate, 3), "passed": rate >= min_pass_rate and ungraded == 0,
                          "median_latency_ms": int(statistics.median(lat)), "worst_latency_ms": max(lat),
                          "trace_ids": [r["trace_id"] for r in runs]})
    lat_all = [r["latency_ms"] for r in records]
    total_pass = sum(q["passes"] for q in questions)
    return {"questions": questions,
            "questions_passed": sum(1 for q in questions if q["passed"]),
            "questions_total": len(questions),
            "runs_passed": total_pass, "runs_total": len(records),
            "overall_pass_rate": round(total_pass / len(records), 3) if records else 0.0,
            "median_latency_ms": int(statistics.median(lat_all)) if lat_all else 0,
            "worst_latency_ms": max(lat_all) if lat_all else 0,
            "runs_missing_trace_id": sum(1 for r in records if not r["trace_id"])}


def side_by_side(left_title: str, left: str, right_title: str, right: str, width: int = 38) -> str:
    l = textwrap.wrap(left, width) or [""]
    r = textwrap.wrap(right, width) or [""]
    n = max(len(l), len(r))
    l += [""] * (n - len(l))
    r += [""] * (n - len(r))
    rows = [f"{left_title:<{width}} | {right_title}", "-" * width + "-+-" + "-" * width]
    rows += [f"{a:<{width}} | {b}" for a, b in zip(l, r)]
    return "\n".join(rows)


def ask_grade(default: str = "s") -> bool | None:
    while True:
        v = input("  Correct? [p]ass / [f]ail / [s]kip: ").strip().lower() or default
        if v in ("p", "pass"):
            return True
        if v in ("f", "fail"):
            return False
        if v in ("s", "skip"):
            return None


def summarize(records: list[dict], min_pass_rate: float = 1.0) -> str:
    agg = aggregate(records, min_pass_rate)
    out = [f"{'ID':<6}{'RUNS':<6}{'PASS':<8}{'RATE':<8}{'MEDIAN':<11}{'WORST':<11}RESULT"]
    for q in agg["questions"]:
        res = "pass" if q["passed"] else ("UNGRADED" if q["ungraded"] else "FAIL")
        out.append(f"{q['id']:<6}{q['runs']:<6}{str(q['passes']) + '/' + str(q['runs']):<8}"
                   f"{q['pass_rate']:<8.0%}{str(q['median_latency_ms']) + ' ms':<11}"
                   f"{str(q['worst_latency_ms']) + ' ms':<11}{res}")
    out.append("")
    out.append(f"SCORE: {agg['questions_passed']} of {agg['questions_total']} questions pass"
               + (f" (min pass rate {min_pass_rate:.0%})" if agg["runs_total"] > agg["questions_total"] else ""))
    out.append(f"RUNS:  {agg['runs_passed']} of {agg['runs_total']} passed ({agg['overall_pass_rate']:.1%})")
    out.append(f"LATENCY: median {agg['median_latency_ms']} ms, worst {agg['worst_latency_ms']} ms")
    out.append(f"TRACE IDS: {agg['runs_total'] - agg['runs_missing_trace_id']} of {agg['runs_total']} runs have one")
    fails = [(r["id"], r["run"], failure_reasons(r)) for r in records if failure_reasons(r)]
    if fails:
        out.append("FAILURES:")
        out += [f"  {i} run {n}: {', '.join(w)}" for i, n, w in fails]
    return "\n".join(out)


# ---------------------------------------------------------------- CLI
@cli.command()
def main(
    key: Path = typer.Option(config.ANSWER_KEY_PATH, help="Answer file, e.g. evals/heldout_key.json"),
    heldout: bool = typer.Option(False, "--heldout", help="Run the held-out key (measurement only)"),
    out: Path = typer.Option(None, help="Results file (default depends on --key; stub adds .stub)"),
    stub: bool = typer.Option(False, "--stub", help="Use the TEST-ONLY stub agent"),
    auto: bool = typer.Option(False, "--auto", help="No prompts; exit 1 if any question fails"),
    no_grade: bool = typer.Option(False, "--no-grade", help="Record mechanical checks only"),
    repeat: int = typer.Option(1, "--repeat", min=1, help="Runs per question (the 8B model varies)"),
    min_pass_rate: float = typer.Option(1.0, min=0.0, max=1.0, help="Per-question pass rate required"),
    require_trace: bool = typer.Option(False, "--require-trace", help="Exit 1 if any run has no trace_id"),
) -> None:
    if auto and no_grade:
        raise typer.BadParameter("--auto and --no-grade are mutually exclusive")
    if heldout:
        if key != config.ANSWER_KEY_PATH:
            raise typer.BadParameter("--heldout and --key are mutually exclusive")
        key = config.HELDOUT_KEY_PATH
    questions = load_key(key)
    out = out or default_out_path(key, stub)
    run_agent = get_run_agent(use_stub=stub)
    valid_ids = load_valid_ids()
    if valid_ids is None:
        typer.echo(f"(!) {config.DATA_PATH.name} not found; invalid-citation check is format-only.")

    if auto:
        grader = lambda q, rec: auto_pass(rec)
    elif no_grade:
        grader = lambda q, rec: None
    else:
        grader = lambda q, rec: ask_grade()

    records = run_questions(questions, run_agent, repeat, valid_ids, grader, echo=typer.echo)
    agg = aggregate(records, min_pass_rate)
    mode = "auto" if auto else ("mechanical" if no_grade else "manual")

    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps({
        "phase": 1, "today": config.TODAY, "model": config.MODELS["agent"],
        "agent": "stub" if stub else "real", "key": Path(key).name, "mode": mode,
        "repeat": repeat, "min_pass_rate": min_pass_rate,
        "score": {"questions_passed": agg["questions_passed"], "questions_total": agg["questions_total"],
                  "runs_passed": agg["runs_passed"], "runs_total": agg["runs_total"],
                  "overall_pass_rate": agg["overall_pass_rate"]},
        "latency_ms": {"median": agg["median_latency_ms"], "worst": agg["worst_latency_ms"]},
        "runs_missing_trace_id": agg["runs_missing_trace_id"],
        "per_question": agg["questions"],
        "failures": [{"id": r["id"], "run": r["run"], "reasons": failure_reasons(r)}
                     for r in records if failure_reasons(r)],
        "results": records}, indent=2), encoding="utf-8")
    typer.echo("\n" + summarize(records, min_pass_rate))
    typer.echo(f"\nMode: {mode}" + ("  (citation-level check; answer text not judged)" if auto else ""))
    typer.echo(f"Saved: {out}")

    if require_trace and agg["runs_missing_trace_id"]:
        typer.echo(f"FAIL: {agg['runs_missing_trace_id']} run(s) have no trace_id (is Phoenix running?)")
        raise typer.Exit(1)
    if auto and agg["questions_passed"] < agg["questions_total"]:
        raise typer.Exit(1)


if __name__ == "__main__":
    cli()
