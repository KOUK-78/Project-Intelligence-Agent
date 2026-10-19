"""Phase 1 eval runner. Correctness is graded BY HAND; automated judging is Phase 4.

    python -m evals.run_evals            # run all questions, grade interactively
    python -m evals.run_evals --no-grade # record mechanical checks only
    python -m evals.run_evals --stub     # use the stub agent
"""
import json
import re
import textwrap
from pathlib import Path

import typer

from app import config
from app.runtime import get_run_agent

cli = typer.Typer(add_completion=False)
ID_RE = re.compile(r"^[A-Z]+-\d+$")


def load_valid_ids(path: Path = config.DATA_PATH) -> set[str] | None:
    """Ticket IDs that exist in the synthetic data (None if data not available yet)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        tickets = data["tickets"] if isinstance(data, dict) and "tickets" in data else data
        return {t["id"] for t in tickets}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def mechanical_checks(expected: list[str], result: dict, valid_ids: set[str] | None) -> dict:
    cited = result.get("citations") or []
    all_found = set(expected) <= set(cited)
    if valid_ids is None:
        invalid = [c for c in cited if not ID_RE.match(c)]  # format-only fallback
    else:
        invalid = [c for c in cited if c not in valid_ids]
    return {"all_expected_citations_found": all_found,
            "invalid_citations": invalid, "any_invalid_citation": bool(invalid)}


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


def failure_reasons(rec: dict) -> list[str]:
    why = []
    if rec["correct"] is False:
        why.append("marked wrong")
    if rec["status"] != "ok":
        why.append(f"status={rec['status']}")
    if not rec["all_expected_citations_found"]:
        why.append("missing expected citation")
    if rec["any_invalid_citation"]:
        why.append(f"invalid citation {rec['invalid_citations']}")
    return why


def summarize(records: list[dict]) -> str:
    out = [f"{'ID':<5}{'STATUS':<16}{'CITES OK':<10}{'INVALID':<9}{'LATENCY':<10}CORRECT"]
    for r in records:
        c = {True: "yes", False: "NO", None: "-"}[r["correct"]]
        out.append(f"{r['id']:<5}{r['status']:<16}{'yes' if r['all_expected_citations_found'] else 'NO':<10}"
                   f"{'YES' if r['any_invalid_citation'] else 'no':<9}{str(r['latency_ms']) + ' ms':<10}{c}")
    graded = [r for r in records if r["correct"] is not None]
    n_ok = sum(1 for r in graded if r["correct"])
    out.append("")
    out.append(f"SCORE: {n_ok} of {len(records)} correct"
               + (f" ({len(records) - len(graded)} ungraded)" if len(graded) < len(records) else ""))
    fails = [(r["id"], failure_reasons(r)) for r in records if failure_reasons(r)]
    if fails:
        out.append("FAILURES:")
        out += [f"  {i}: {', '.join(w)}" for i, w in fails]
    return "\n".join(out)


@cli.command()
def main(
    key: Path = typer.Option(config.ANSWER_KEY_PATH, help="Answer key JSON"),
    out: Path = typer.Option(config.RESULTS_DIR / "phase1_baseline.json", help="Results file"),
    stub: bool = typer.Option(False, "--stub", help="Use the stub agent"),
    no_grade: bool = typer.Option(False, "--no-grade", help="Skip hand grading"),
) -> None:
    questions = json.loads(Path(key).read_text(encoding="utf-8"))
    run_agent = get_run_agent(use_stub=stub)
    valid_ids = load_valid_ids()
    if valid_ids is None:
        typer.echo("(!) tickets.json not found; invalid-citation check is format-only.")

    records = []
    for q in questions:
        typer.echo(f"\n=== {q['id']}: {q['question']}")
        res = run_agent(q["question"])
        chk = mechanical_checks(q["expected_citations"], res, valid_ids)
        typer.echo(side_by_side("EXPECTED", q["expected_answer"], "ACTUAL", res["answer"]))
        typer.echo(f"  expected cites: {q['expected_citations']}  returned: {res['citations']}  status: {res['status']}")
        correct = None if no_grade else ask_grade()
        records.append({"id": q["id"], "question": q["question"], "status": res["status"],
                        "answer": res["answer"], "citations": res["citations"],
                        "expected_citations": q["expected_citations"], **chk,
                        "latency_ms": res["latency_ms"], "trace_id": res["trace_id"], "correct": correct})

    summary = summarize(records)
    n_ok = sum(1 for r in records if r["correct"])
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps({
        "phase": 1, "today": config.TODAY, "model": config.MODELS["agent"],
        "score": {"correct": n_ok, "total": len(records)},
        "failures": [{"id": r["id"], "reasons": failure_reasons(r)} for r in records if failure_reasons(r)],
        "results": records}, indent=2), encoding="utf-8")
    typer.echo("\n" + summary)
    typer.echo(f"\nSaved: {out}")


if __name__ == "__main__":
    cli()
