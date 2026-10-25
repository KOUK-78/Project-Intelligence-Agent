"""SYNTHESIZER node (answer): cited JSON answer from collected evidence only. OWNER: B.

Two nodes share one generate-validate-retry helper: the Phase 1 node (one flat evidence list, used when the planner
is off) and the v2 node (evidence grouped by sub-question, any source's IDs, honest about gaps).
"""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import config
from app.nodes.llm import LLM
from app.nodes.state import AgentState, compact_json, evidence_for
from app.nodes.verify import bracket_bare_ids, evidence_ids, mentioned_ids, strip_trailing_id_list, tidy_brackets, trim_not_found_tangent, verify_citations

SYNTH_SYSTEM = (
    "You write the final answer to a question about project tickets.\n"
    "Rules:\n"
    "1. Answer ONLY from the evidence given. Never use outside knowledge.\n"
    "2. Put the ticket ID in brackets after every factual claim, e.g. [ATL-000]. Use brackets only for ticket IDs.\n"
    "3. If the evidence is not enough, say so plainly. Do not guess.\n"
    "4. Include the specific facts: names, dates, counts, and the reason from the comments.\n"
    "5. If tickets disagree, report the conflict, give each value with its ticket, and do not pick one.\n"
    "6. If the question's premise is contradicted by a ticket, say so.\n"
    "7. Only when the question asks what is blocking something or what depends on something: name the root-cause "
    "ticket first, with its ID, then list EVERY ticket it affects, each with its ID. Otherwise do not add other "
    "tickets' effects.\n"
    "8. If nothing in the evidence answers the question, say that in one sentence with no ticket IDs.\n"
    f"Today's date is {config.TODAY}.\n"
    'Reply as JSON: {"answer": "...", "citations": ["ATL-000"]}. '
    '"citations" lists every ticket ID you cited in "answer".'
)
SYNTH_V2_SYSTEM = (
    "You write the final answer to a question using evidence from several sources.\n"
    "The evidence is grouped by sub-question, and each group names its source.\n"
    "Rules:\n"
    "1. Answer ONLY from the evidence given. Never use outside knowledge.\n"
    "2. Put the record ID in brackets after every factual claim, e.g. [ATL-000], [NTN-000] or [EML-000]. "
    "Use brackets only for record IDs.\n"
    "3. A group marked NOT ENOUGH EVIDENCE: say plainly that there is not enough evidence for that part. "
    "Do not guess. Records shown in such a group were found by keyword search and may be unrelated to the "
    "sub-question: do not report them as its answer and do not cite them, unless a record itself states the "
    "specific fact the sub-question asked for.\n"
    "4. A group marked SOURCE UNAVAILABLE: say 'source unavailable' and name the source for that part. "
    "Do not guess.\n"
    "5. If records or sources disagree about the same fact, state BOTH values, each with its own ID in brackets, "
    "and do not pick one.\n"
    "6. If the question's premise is contradicted by a record, say so.\n"
    "7. Only when the question asks what is blocking something or what depends on something: name the root-cause "
    "record first, with its ID, then list EVERY record it affects, each with its ID. Otherwise do not add other "
    "records' effects.\n"
    "8. Include the specific facts: names, dates, counts, and the reason given in the record text.\n"
    "9. If nothing in the evidence answers the question, say that in one sentence with no IDs.\n"
    f"Today's date is {config.TODAY}.\n"
    'Reply as JSON: {"answer": "...", "citations": ["ATL-000"]}. '
    '"citations" lists every record ID you cited in "answer".'
)
RETRY_NUDGE = 'That was not valid. Reply again with ONLY a JSON object: {"answer": "...", "citations": ["ID"]}'
NO_EVIDENCE_ANSWER = "I could not find any ticket evidence to answer that, so I can't answer it."
NO_EVIDENCE_V2_ANSWER = "There is not enough evidence to answer that."
PARTIAL_PREFIX = "Partial answer (tool step limit reached, evidence may be incomplete): "
INVALID_ANSWER = "I could not produce a valid answer."

# Markers the v2 prompt and the gap notes share, so the model is told one thing and the code checks for the same.
MARK_UNAVAILABLE = "SOURCE UNAVAILABLE"
MARK_NO_EVIDENCE = "NOT ENOUGH EVIDENCE"


def _cleanup(answer: str) -> str:
    out = strip_trailing_id_list(bracket_bare_ids(tidy_brackets(answer)))
    # HEURISTIC not_found_trim [specific-risk, default OFF]: cut tangents after a 'not found' sentence.
    return trim_not_found_tangent(out) if config.HEURISTICS["not_found_trim"] else out


class SynthOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str = Field(min_length=1)
    citations: list[str]


SYNTH_SCHEMA = SynthOutput.model_json_schema()


def format_evidence(evidence: list[dict[str, Any]], total_chars: int | None = None) -> str:
    """One line per tool result. `total_chars` defaults to config.EVIDENCE_TOTAL_CHARS (Phase 1 output unchanged)."""
    limit = config.EVIDENCE_TOTAL_CHARS if total_chars is None else total_chars
    lines: list[str] = []
    used = 0
    for i, e in enumerate(evidence):
        args = compact_json(e["args"], 200)
        body = compact_json(e["data"], config.EVIDENCE_ITEM_CHARS) if e["ok"] else f"FAILED: {e['error']}"
        line = f"- {e['tool']}({args}) -> {body}"
        if used + len(line) > limit:
            lines.append(f"- ({len(evidence) - i} further tool results omitted for length)")
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


async def generate(llm: LLM, messages: list[dict]) -> tuple[SynthOutput | None, list[str], dict[str, Any] | None]:
    """Ask the synthesizer model for the JSON answer, with one retry on an invalid reply.

    Returns (output, errors, failure). When `failure` is set the node returns it as its state update
    (model unreachable -> status error; still invalid after the retry -> status invalid_output)."""
    errors: list[str] = []
    for attempt in (1, 2):  # one retry, no more
        try:
            reply = await llm.chat("synthesizer", messages, format=SYNTH_SCHEMA)
        except Exception as exc:  # noqa: BLE001
            msg = f"llm_unavailable: {type(exc).__name__}: {exc}"[: config.ERROR_TEXT_CHARS]
            return None, errors, {
                "route": "end",
                "status": "error",
                "answer": f"I could not reach the language model. ({msg})",
                "errors": [msg],
            }
        try:
            return SynthOutput.model_validate_json(reply.content), errors, None
        except ValidationError:
            errors.append(f"invalid_output (attempt {attempt}): {reply.content[: config.SNIPPET_CHARS]!r}")
            messages = [
                *messages,
                {"role": "assistant", "content": reply.content},
                {"role": "user", "content": RETRY_NUDGE},
            ]
    return None, errors, {"route": "end", "status": "invalid_output", "answer": INVALID_ANSWER, "citations": [],
                          "errors": errors}


def make_synthesizer_node(llm: LLM, known_ids: set[str] | None, unchecked_prefixes: frozenset[str] | set[str] = frozenset()):
    """`unchecked_prefixes`: ID prefixes of sources whose data file could not be read (see the v2 node). The default
    is empty, so a Jira-only run, which is Phase 1, behaves exactly as before. Flat mode (planner off, several
    sources behind one router) uses this node too, so it needs the same protection."""

    async def synthesizer_node(state: AgentState) -> dict[str, Any]:
        evidence = state.get("evidence", [])
        if not any(e["ok"] for e in evidence):
            # Nothing usable came back: answer deterministically rather than let a model guess.
            return {"route": "end", "status": "ok", "answer": NO_EVIDENCE_ANSWER, "citations": []}

        messages = [
            {"role": "system", "content": SYNTH_SYSTEM},
            {"role": "user", "content": f"Question: {state['question']}\n\nEvidence:\n{format_evidence(evidence)}"},
        ]
        output, errors, failure = await generate(llm, messages)
        if failure is not None:
            return failure
        assert output is not None

        answer, citations, warnings = verify_citations(
            _cleanup(output.answer),
            output.citations,
            known_ids=known_ids,
            ev_ids=evidence_ids(evidence),
            mentioned=set(mentioned_ids(evidence)),
            unchecked_prefixes=unchecked_prefixes,
        )
        capped = bool(state.get("capped"))
        return {
            "route": "end",
            "status": "loop_cap" if capped else "ok",
            "answer": PARTIAL_PREFIX + answer if capped else answer,
            "citations": citations,
            "errors": errors,
            "warnings": warnings,
        }

    return synthesizer_node


# ---------------------------------------------------------------- v2: grouped by sub-question

def format_grouped_evidence(plan: list[dict], results: list[dict], evidence: list[dict[str, Any]]) -> str:
    """Evidence grouped by sub-question. A group the code knows is empty or unavailable says so in capitals, so the
    model is told, not left to infer it from silence."""
    by_id = {r["id"]: r for r in results}
    share = config.EVIDENCE_TOTAL_CHARS // max(len(plan), 1)
    blocks: list[str] = []
    for sq in plan:
        res = by_id.get(sq["id"], {})
        status = res.get("status", "unresolved")
        down = ", ".join(res.get("unavailable") or [])
        items = evidence_for(evidence, sq["id"])
        if status == "unavailable":
            body = f"{MARK_UNAVAILABLE}: {down or sq['source']}. Do not guess."
        elif not evidence_ids([e for e in items if e.get("ok")]):
            body = f"{MARK_NO_EVIDENCE}: nothing relevant was found for this sub-question."
        else:
            body = format_evidence(items, share)
            if status == "unresolved":
                body += (f"\n{MARK_NO_EVIDENCE}: the records above did not answer this sub-question. They may be unrelated; "
                         "do not present them as the answer.")
            if down:
                body += f"\n{MARK_UNAVAILABLE}: {down}."
        blocks.append(f"Sub-question {sq['id']} (source: {sq['source']}): {sq['question']}\n{body}")
    return "\n\n".join(blocks)


def gaps_of(results: list[dict]) -> tuple[list[str], list[str], bool]:
    """(sources unavailable, unresolved sub-question texts, whether the step budget cut anything short)."""
    unavailable: list[str] = []
    for r in results:
        for name in r.get("unavailable") or []:
            if name not in unavailable:
                unavailable.append(name)
    unresolved = [r["question"] for r in results if r.get("status") == "unresolved"]
    return unavailable, unresolved, any(r.get("reason") == "budget_exhausted" for r in results)


def gap_answer(unavailable: list[str]) -> str:
    """The deterministic answer when no record came back at all."""
    if unavailable:
        return f"{NO_EVIDENCE_V2_ANSWER} Source unavailable: {', '.join(unavailable)}."
    return NO_EVIDENCE_V2_ANSWER


def ensure_gap_notes(answer: str, unavailable: list[str], unresolved: list[str]) -> str:
    """The answer must admit what is missing even if the model skipped it. Only adds a note the answer lacks."""
    low = answer.lower()
    if unavailable and MARK_UNAVAILABLE.lower() not in low:
        answer = f"{answer} Source unavailable: {', '.join(unavailable)}."
    if unresolved and MARK_NO_EVIDENCE.lower() not in low:
        answer = f"{answer} Not enough evidence for: {'; '.join(unresolved)}."
    return answer


def make_synthesizer_v2_node(llm: LLM, known_ids: set[str] | None, unchecked_prefixes: frozenset[str] | set[str] = frozenset()):
    """`unchecked_prefixes`: ID prefixes of sources whose data file could not be read. Their IDs are checked against
    this run's evidence only, since there is no list of real IDs to check them against."""

    async def synthesizer_v2_node(state: AgentState) -> dict[str, Any]:
        plan = state.get("plan", [])
        results = state.get("sq_results", [])
        evidence = state.get("evidence", [])
        unavailable, unresolved, budget_cut = gaps_of(results)
        status = "loop_cap" if budget_cut else "ok"

        if not evidence_ids(evidence):
            # No record from any source: answer deterministically, never let a model guess.
            return {"route": "end", "status": status, "answer": gap_answer(unavailable), "citations": []}

        messages = [
            {"role": "system", "content": SYNTH_V2_SYSTEM},
            {"role": "user", "content": f"Question: {state['question']}\n\nEvidence:\n"
                                        f"{format_grouped_evidence(plan, results, evidence)}"},
        ]
        output, errors, failure = await generate(llm, messages)
        if failure is not None:
            return failure
        assert output is not None

        answer, citations, warnings = verify_citations(
            _cleanup(output.answer),
            output.citations,
            known_ids=known_ids,
            ev_ids=evidence_ids(evidence),
            mentioned=set(mentioned_ids(evidence)),
            unchecked_prefixes=unchecked_prefixes,
        )
        return {
            "route": "end",
            "status": status,
            "answer": ensure_gap_notes(answer, unavailable, unresolved),
            "citations": citations,
            "errors": errors,
            "warnings": warnings,
        }

    return synthesizer_v2_node
