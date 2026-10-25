"""Eval runner. Mechanical checks + (optional) hand grading; LLM judging is Phase 4.

    python -m evals.run_evals                       # original key, grade by hand (p/f/s)
    python -m evals.run_evals --auto --repeat 3     # no prompts; exit 1 if any question fails
    python -m evals.run_evals --heldout --auto --repeat 3   # same as --key evals/heldout_key.json
    python -m evals.run_evals --phase2 --no-planner --auto --repeat 3   # Phase 2 key, planner off
    python -m evals.run_evals --phase2 --planner --auto --repeat 3      # Phase 2 key, planner on
    python -m evals.run_evals --phase2 --heldout --planner --auto --repeat 3   # Phase 2 held-out (M4 only)
    python -m evals.run_evals --no-grade            # record mechanical checks only
    python -m evals.run_evals --stub --auto         # plumbing smoke test (writes *.stub.json)

--auto is a CITATION-LEVEL check: status ok, every expected citation present, no forbidden
citation, no invalid ID, plus (Phase 2 keys only) every required source cited and every
`must_mention` string present in the answer. It does not judge the answer's meaning.
Hand grading still judges wording.
"""
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import textwrap
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional

import typer

from app import config
from app.runtime import get_run_agent

cli = typer.Typer(add_completion=False)
ID_RE = re.compile(r"^[A-Z]+-\d+$")
REQUIRED_KEY_FIELDS = ("id", "question", "expected_answer", "expected_citations")
LIST_KEY_FIELDS = ("expected_citations", "forbidden_citations", "sources_required", "must_mention")
SOURCE_BY_PREFIX = {prefix: source for source, prefix in config.SOURCE_ID_PREFIXES.items()}


def source_of(citation_id: str) -> str | None:
    """Source name an item ID belongs to, from its prefix (CONTRACTS.md section 10); None if unknown."""
    return SOURCE_BY_PREFIX.get(str(citation_id).split("-", 1)[0])


def sources_of(citations: list[str]) -> list[str]:
    """Distinct sources named by a list of IDs, in config.SOURCES order."""
    found = {source_of(c) for c in citations}
    return [s for s in config.SOURCES if s in found]


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


def collect_ids(node, prefixes: set[str]) -> set[str]:
    """Every string stored under an "id" key, anywhere in a JSON value, whose prefix is a known source prefix.

    Notion and email files are owned by A and their exact layout may change; walking the whole
    document avoids guessing it. Other IDs (thread IDs and so on) are ignored.
    """
    found: set[str] = set()
    if isinstance(node, dict):
        value = node.get("id")
        if isinstance(value, str) and ID_RE.match(value) and value.split("-", 1)[0] in prefixes:
            found.add(value)
        for child in node.values():
            found |= collect_ids(child, prefixes)
    elif isinstance(node, list):
        for child in node:
            found |= collect_ids(child, prefixes)
    return found


def load_extra_ids() -> tuple[set[str], list[str]]:
    """IDs from the Notion and email data, and the names of sources whose data file is missing or unreadable.

    Jira stays with load_valid_ids() so Phase 1 behaviour is untouched.
    """
    ids: set[str] = set()
    missing: list[str] = []
    for source in config.SOURCES:
        if source == "jira":
            continue
        try:
            data = json.loads(Path(config.DATA_PATHS[source]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            missing.append(source)
            continue
        ids |= collect_ids(data, {config.SOURCE_ID_PREFIXES[source]})
    return ids, missing


def load_key(path: Path) -> list[dict]:
    """Load and validate an answer file (answer_key.json, heldout_key.json or a Phase 2 key)."""
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
        for f in LIST_KEY_FIELDS:
            if f in q and not isinstance(q[f], list):
                raise typer.BadParameter(f"{path}: {q['id']}.{f} must be a list")
        unknown = [s for s in q.get("sources_required", []) if s not in config.SOURCES]
        if unknown:
            raise typer.BadParameter(f"{path}: {q['id']}.sources_required has unknown source(s) {unknown}; "
                                     f"use {list(config.SOURCES)}")
        if any(not isinstance(m, str) or not m.strip() for m in q.get("must_mention", [])):
            raise typer.BadParameter(f"{path}: {q['id']}.must_mention entries must be non-empty strings")
    return questions


def phase_of(key: Path) -> int:
    """Phase 2 keys are named phase2_*.json; everything else is the Phase 1 contract."""
    return 2 if Path(key).name.startswith("phase2") else 1


def agent_mode(key: Path, planner: bool, flat: bool) -> str:
    """What kind of agent a result file measures: "planner", "flat" (one loop over every source), or the Phase 1
    graph. On a Phase 2 key the Phase 1 graph is Jira-only, so it is labelled so and is NOT a flat baseline."""
    if planner:
        return "planner"
    if flat:
        return "flat"
    return "phase1_jira_only" if phase_of(key) == 2 else "phase1"


def default_out_path(key: Path, stub: bool, planner: bool | None = None, flat: bool = False) -> Path:
    """Separate result files per key and per agent mode; stub runs never overwrite real results.

    Phase 2 (CONTRACTS.md section 12): phase2_planner.json / phase2_heldout.json (planner on), phase2_flat.json /
    phase2_heldout_flat.json (a true flat mode, `--flat`) and phase2_jira_only.json / phase2_heldout_jira_only.json
    (planner off with no flat mode: the Phase 1 graph, which only reaches Jira). Phase 1 keys keep their file names
    when the planner is off, and get a `_planner` suffix when it is on, so a planner run can never overwrite a baseline.
    """
    planner = config.PLANNER_ENABLED if planner is None else planner
    mode = agent_mode(key, planner, flat)
    names = {"answer_key.json": "phase1_baseline.json", "heldout_key.json": "phase1_heldout.json",
             "phase2_key.json": {"planner": "phase2_planner.json", "flat": "phase2_flat.json"}.get(mode, "phase2_jira_only.json"),
             "phase2_heldout_key.json": {"planner": "phase2_heldout.json", "flat": "phase2_heldout_flat.json"}.get(mode, "phase2_heldout_jira_only.json")}
    name = names.get(Path(key).name)
    if name is None:
        name = f"{Path(key).stem}{'_planner' if planner else ''}_results.json"
    elif planner and phase_of(key) == 1:
        name = name.replace(".json", "_planner.json")
    if stub:
        name = name.replace(".json", ".stub.json")
    return config.RESULTS_DIR / name


# ---------------------------------------------------------------- checks
def normalize_text(text: str) -> str:
    """Lower-case and collapse whitespace, so a must_mention check ignores case and line breaks."""
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def mentions(answer_text: str, wanted: str) -> bool:
    """True if `wanted` appears in the (already normalised) answer as a whole token, so "25" does not match "2025"."""
    w = normalize_text(wanted)
    return bool(w) and re.search(rf"(?<![\w]){re.escape(w)}(?![\w])", answer_text) is not None


def mechanical_checks(expected: list[str], result: dict, valid_ids: set[str] | None,
                      forbidden: list[str] = (), sources_required: list[str] = (),
                      must_mention: list[str] = ()) -> dict:
    cited = result.get("citations") or []
    if valid_ids is None:
        invalid = [c for c in cited if not ID_RE.match(c)]  # format-only fallback
    else:
        invalid = [c for c in cited if c not in valid_ids]
    hit = [c for c in dict.fromkeys(cited) if c in set(forbidden)]
    cited_sources = sources_of(cited)
    sources_missing = [s for s in sources_required if s not in cited_sources]
    text = normalize_text(result.get("answer"))
    mention_missing = [m for m in must_mention if not mentions(text, m)]
    return {"all_expected_citations_found": set(expected) <= set(cited),
            "invalid_citations": invalid, "any_invalid_citation": bool(invalid),
            "forbidden_citations_hit": hit, "any_forbidden_citation": bool(hit),
            "sources_cited": cited_sources, "sources_required": list(sources_required),
            "sources_missing": sources_missing, "all_sources_covered": not sources_missing,
            "must_mention": list(must_mention), "must_mention_missing": mention_missing,
            "all_must_mention_found": not mention_missing}


def auto_pass(rec: dict) -> bool:
    # The two Phase 2 checks default to satisfied, so Phase 1 records (which lack them) score as before.
    return (rec["status"] == "ok" and rec["all_expected_citations_found"]
            and not rec["any_forbidden_citation"] and not rec["any_invalid_citation"]
            and rec.get("all_sources_covered", True) and rec.get("all_must_mention_found", True))


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
    if not rec.get("all_sources_covered", True):
        why.append(f"required source not cited {rec['sources_missing']}")
    if not rec.get("all_must_mention_found", True):
        why.append(f"answer missing required text {rec['must_mention_missing']}")
    return why


def _int_or_none(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def phase2_fields(result: dict) -> dict:
    """Planner, step, source and token facts from the OPTIONAL AgentResult fields (CONTRACTS.md section 4).

    `plan` is a LIST of sub-question records written by the agent: {id, question, source, reason, status, outcome,
    steps, attempts, rewritten, capped, unavailable}. A planner fallback is the one sub-question whose `reason`
    starts with "planner fallback" (the planner failed after its retry), so `planner_valid` is False for it.
    Every field is None when the agent did not report it, so Phase 1 agents and the stub work unchanged.
    """
    plan = result.get("plan") if isinstance(result.get("plan"), list) else None
    recs = [s for s in plan if isinstance(s, dict)] if plan is not None else None
    fallback = None if recs is None else any(str(s.get("reason", "")).lower().startswith("planner fallback") for s in recs)
    steps = [s.get("steps") for s in recs] if recs else []
    usage = result.get("token_usage") if isinstance(result.get("token_usage"), dict) else {}
    sources = result.get("sources") if isinstance(result.get("sources"), list) else None
    unavailable = sorted({u for s in (recs or []) for u in (s.get("unavailable") or []) if isinstance(u, str)})
    return {"planner_used": recs is not None,
            "planner_valid": None if fallback is None else not fallback,
            "planner_fallback": fallback,
            "subquestion_count": len(recs) if recs is not None else None,
            "subquestions_unresolved": sum(1 for s in recs if s.get("status") == "unresolved") if recs is not None else None,
            "steps_total": sum(steps) if steps and all(_int_or_none(x) is not None for x in steps) else None,
            "tool_call_count": len(result.get("tool_calls") or []),
            "prompt_tokens": _int_or_none(usage.get("prompt_tokens")),
            "completion_tokens": _int_or_none(usage.get("completion_tokens")),
            "total_tokens": _int_or_none(usage.get("total_tokens")),
            "sources_returning_records": sources,
            "sources_unavailable": unavailable if recs is not None else None}


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
            chk = mechanical_checks(q["expected_citations"], res, valid_ids, q.get("forbidden_citations", []),
                                    q.get("sources_required", []), q.get("must_mention", []))
            rec = {"id": q["id"], "run": i, "question": q["question"], "status": res["status"],
                   "answer": res["answer"], "citations": res["citations"],
                   "expected_citations": q["expected_citations"],
                   "forbidden_citations": q.get("forbidden_citations", []), **chk,
                   **phase2_fields(res),
                   "latency_ms": res["latency_ms"], "trace_id": res.get("trace_id")}
            echo(side_by_side("EXPECTED", q["expected_answer"], "ACTUAL", res["answer"]))
            echo(f"  expected: {q['expected_citations']}  forbidden: {rec['forbidden_citations']}  "
                 f"returned: {res['citations']}  status: {res['status']}")
            if rec["sources_required"] or rec["must_mention"]:
                echo(f"  sources required: {rec['sources_required']}  cited: {rec['sources_cited']}  "
                     f"must mention missing: {rec['must_mention_missing']}")
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
            "runs_missing_trace_id": sum(1 for r in records if not r["trace_id"]),
            **phase2_totals(records)}


def _total(records: list[dict], field: str) -> dict:
    """Sum and median of one numeric per-run field over the runs that reported it."""
    vals = [r[field] for r in records if r.get(field) is not None]
    return {"runs_reporting": len(vals), "total": sum(vals),
            "median_per_run": int(statistics.median(vals)) if vals else None}


def phase2_totals(records: list[dict]) -> dict:
    """Run-level Phase 2 totals. Counts are over runs that reported the field, never guessed."""
    planned = [r for r in records if r.get("planner_used")]
    subs = [r["subquestion_count"] for r in planned if r.get("subquestion_count") is not None]
    needs_src = [r for r in records if r.get("sources_required")]
    needs_text = [r for r in records if r.get("must_mention")]
    return {"tokens": _total(records, "total_tokens"), "steps": _total(records, "steps_total"),
            "tool_calls": _total(records, "tool_call_count"),
            "planner": {"runs": len(planned),
                        "invalid": sum(1 for r in planned if r.get("planner_valid") is False),
                        "fallback": sum(1 for r in planned if r.get("planner_fallback") is True),
                        "median_subquestions": statistics.median(subs) if subs else None},
            "source_checks": {"runs_with_required_sources": len(needs_src),
                              "runs_covering_them": sum(1 for r in needs_src if r.get("all_sources_covered")),
                              "runs_with_must_mention": len(needs_text),
                              "runs_mentioning_all": sum(1 for r in needs_text if r.get("all_must_mention_found"))}}


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
    if agg["planner"]["runs"]:
        p = agg["planner"]
        out.append(f"PLANNER: {p['runs']} runs planned, {p['invalid']} invalid plan(s), {p['fallback']} fallback(s), "
                   f"median {p['median_subquestions']} sub-questions")
    if agg["steps"]["runs_reporting"]:
        out.append(f"STEPS: {agg['steps']['total']} total, median {agg['steps']['median_per_run']} per run "
                   f"({agg['steps']['runs_reporting']} of {agg['runs_total']} runs reported)")
    if agg["tokens"]["runs_reporting"]:
        out.append(f"TOKENS: {agg['tokens']['total']} total, median {agg['tokens']['median_per_run']} per run "
                   f"({agg['tokens']['runs_reporting']} of {agg['runs_total']} runs reported)")
    sc = agg["source_checks"]
    if sc["runs_with_required_sources"]:
        out.append(f"SOURCES: {sc['runs_covering_them']} of {sc['runs_with_required_sources']} runs cited every required source")
    if sc["runs_with_must_mention"]:
        out.append(f"MUST MENTION: {sc['runs_mentioning_all']} of {sc['runs_with_must_mention']} runs had every required text")
    fails = [(r["id"], r["run"], failure_reasons(r)) for r in records if failure_reasons(r)]
    if fails:
        out.append("FAILURES:")
        out += [f"  {i} run {n}: {', '.join(w)}" for i, n, w in fails]
    return "\n".join(out)


# ---------------------------------------------------------------- environment record (Day 0 decision 8)
def git_commit() -> str | None:
    """HEAD commit of the repo, or None (no git, no .git directory, ...)."""
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=config.ROOT, capture_output=True, text=True,
                           timeout=config.ENV_PROBE_TIMEOUT_S)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def model_fingerprint(model: str) -> dict | None:
    """Digest of the local Ollama model, so two result files can be proven to use the same weights.

    Best effort: None if Ollama is not reachable or the model is not listed. Never raises.
    """
    try:
        import ollama  # lazy, like llm.py

        client = ollama.Client(host=config.OLLAMA_BASE_URL, timeout=config.ENV_PROBE_TIMEOUT_S)
        for m in client.list().models:
            name = getattr(m, "model", None) or getattr(m, "name", None)
            if name in (model, f"{model}:latest"):
                return {"name": name, "digest": getattr(m, "digest", None), "size": getattr(m, "size", None)}
    except Exception:  # noqa: BLE001 - a probe must never fail a measurement run
        return None
    return None


def describe_environment(stub: bool) -> dict:
    """Where and with what a result file was recorded. Official files must come from ONE machine and ONE model."""
    return {"machine": {"node": platform.node(), "platform": platform.platform(), "python": sys.version.split()[0]},
            "models": {job: config.MODELS[job] for job in ("agent", "synthesizer", "planner", "evidence_check")},
            "model_fingerprint": None if stub else model_fingerprint(config.MODELS["agent"]),
            "git_commit": git_commit(),
            "settings": {"MAX_AGENT_STEPS": config.MAX_AGENT_STEPS, "MAX_TOTAL_STEPS": config.MAX_TOTAL_STEPS,
                         "MAX_SUBQUESTIONS": config.MAX_SUBQUESTIONS, "NUM_CTX": config.NUM_CTX,
                         "TEMPERATURE": config.TEMPERATURE},
            "faults": config.resolve_faults()}


@contextmanager
def planner_override(value: bool | None, flat: bool = False):
    """Switch the planner and the source set for one run and ALWAYS restore config and environment afterwards.

    --flat       -> planner off, ENABLED_SOURCES=all (D's flat mode: one loop over every source).
    --no-planner -> planner off, ENABLED_SOURCES removed, so a stray value in the shell cannot turn a Phase 1 /
                    Jira-only run into a flat run behind the label's back.
    --planner    -> planner on; the person's ENABLED_SOURCES (if any) is kept and recorded in the result file."""
    if value is None and not flat:
        yield
        return
    old_planner, old_env = config.PLANNER_ENABLED, {k: os.environ.get(k) for k in ("PLANNER_ENABLED", "ENABLED_SOURCES")}
    if value is not None:
        config.PLANNER_ENABLED = value  # the agent reads config.PLANNER_ENABLED at call time (CONTRACTS.md section 6)
        os.environ["PLANNER_ENABLED"] = "1" if value else "0"
    if flat:
        os.environ["ENABLED_SOURCES"] = "all"
    elif value is False:
        os.environ.pop("ENABLED_SOURCES", None)
    try:
        yield
    finally:
        config.PLANNER_ENABLED = old_planner
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@cli.command()
def main(
    key: Path = typer.Option(config.ANSWER_KEY_PATH, help="Answer file, e.g. evals/heldout_key.json"),
    heldout: bool = typer.Option(False, "--heldout", help="Run the held-out key (measurement only)"),
    phase2: bool = typer.Option(False, "--phase2", help="Run the Phase 2 key (with --heldout: the Phase 2 held-out key)"),
    planner: Optional[bool] = typer.Option(None, "--planner/--no-planner",
                                           help="Planner on or off for this run (default: PLANNER_ENABLED)"),
    flat: bool = typer.Option(False, "--flat", help="Flat baseline: one retrieval loop over ALL sources, no planner "
                              "(planner off, ENABLED_SOURCES=all: one loop over every source)"),
    out: Path = typer.Option(None, help="Results file (default depends on --key and agent mode; stub adds .stub)"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing results file (refused by default)"),
    stub: bool = typer.Option(False, "--stub", help="Use the TEST-ONLY stub agent"),
    auto: bool = typer.Option(False, "--auto", help="No prompts; exit 1 if any question fails"),
    no_grade: bool = typer.Option(False, "--no-grade", help="Record mechanical checks only"),
    repeat: int = typer.Option(1, "--repeat", min=1, help="Runs per question (the 8B model varies)"),
    min_pass_rate: float = typer.Option(1.0, min=0.0, max=1.0, help="Per-question pass rate required"),
    require_trace: bool = typer.Option(False, "--require-trace", help="Exit 1 if any run has no trace_id"),
) -> None:
    if auto and no_grade:
        raise typer.BadParameter("--auto and --no-grade are mutually exclusive")
    if (heldout or phase2) and key != config.ANSWER_KEY_PATH:
        raise typer.BadParameter("--heldout/--phase2 and --key are mutually exclusive")
    if phase2:
        key = config.PHASE2_HELDOUT_KEY_PATH if heldout else config.PHASE2_KEY_PATH
    elif heldout:
        key = config.HELDOUT_KEY_PATH
    if flat and planner:
        raise typer.BadParameter("--flat means no planner; it cannot be combined with --planner")
    if flat and not config.FLAT_MODE_IMPLEMENTED:
        typer.echo("error: flat mode is switched off (config.FLAT_MODE_IMPLEMENTED is False). With the planner off and "
                   "ENABLED_SOURCES unset the agent is the Phase 1 graph, which only reaches Jira, so it is not a flat "
                   "baseline. Do not record phase2_flat.json until flat mode is on.")
        raise typer.Exit(2)
    planner_on = False if flat else (config.PLANNER_ENABLED if planner is None else planner)
    mode_label = agent_mode(key, planner_on, flat)
    questions = load_key(key)
    out = out or default_out_path(key, stub, planner_on, flat)
    if not stub and Path(out).exists() and not force:
        typer.echo(f"error: {out} already exists and is a recorded result. Re-run into another file "
                   f"(--out {Path(out).with_name(Path(out).stem + '_rerun.json')}) and compare it with the recorded one, "
                   "or pass --force to overwrite it on purpose.")
        raise typer.Exit(2)
    if mode_label == "phase1_jira_only":
        typer.echo("(!) Planner is off and there is no flat mode: this runs the Phase 1 graph, which only reaches Jira. "
                   "The result is written as *_jira_only.json and is NOT a flat baseline for the planner.")
    if heldout and phase2:
        typer.echo("MEASUREMENT ONLY: the Phase 2 held-out key is never used to tune prompts or code (CONTRACTS.md 7).")
    if config.resolve_faults():
        typer.echo(f"(!) FAULT_INJECT is active ({config.resolve_faults()}). This is a fault test, not a measurement.")
    run_agent = get_run_agent(use_stub=stub)
    valid_ids = load_valid_ids()
    if valid_ids is None:
        typer.echo(f"(!) {config.DATA_PATH.name} not found; invalid-citation check is format-only.")
    else:
        extra_ids, missing_sources = load_extra_ids()
        valid_ids = valid_ids | extra_ids  # a new set: callers (and tests) may share the original
        if phase2 and missing_sources:
            typer.echo(f"(!) no data file for {missing_sources}; their citations will count as invalid.")

    if auto:
        grader = lambda q, rec: auto_pass(rec)
    elif no_grade:
        grader = lambda q, rec: None
    else:
        grader = lambda q, rec: ask_grade()

    with planner_override(False if flat else planner, flat):
        enabled = list(config.resolve_enabled_sources(planner=planner_on))
        records = run_questions(questions, run_agent, repeat, valid_ids, grader, echo=typer.echo)
    agg = aggregate(records, min_pass_rate)
    mode = "auto" if auto else ("mechanical" if no_grade else "manual")

    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps({
        "phase": phase_of(key), "today": config.TODAY, "model": config.MODELS["agent"],
        "agent": "stub" if stub else "real", "key": Path(key).name, "mode": mode,
        "planner_enabled": planner_on, "agent_mode": mode_label, "enabled_sources": enabled,
        "repeat": repeat, "min_pass_rate": min_pass_rate,
        "environment": describe_environment(stub),
        "score": {"questions_passed": agg["questions_passed"], "questions_total": agg["questions_total"],
                  "runs_passed": agg["runs_passed"], "runs_total": agg["runs_total"],
                  "overall_pass_rate": agg["overall_pass_rate"]},
        "latency_ms": {"median": agg["median_latency_ms"], "worst": agg["worst_latency_ms"]},
        "runs_missing_trace_id": agg["runs_missing_trace_id"],
        "totals": {k: agg[k] for k in ("tokens", "steps", "tool_calls", "planner", "source_checks")},
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
