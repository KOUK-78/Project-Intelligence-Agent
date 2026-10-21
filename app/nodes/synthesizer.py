"""SYNTHESIZER node (answer): cited JSON answer from collected evidence only. OWNER: B."""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import config
from app.nodes.llm import LLM
from app.nodes.state import AgentState, compact_json
from app.nodes.verify import bracket_bare_ids, evidence_ids, mentioned_ids, strip_trailing_id_list, tidy_brackets, trim_not_found_tangent, verify_citations

SYNTH_SYSTEM = (
    "You write the final answer to a question about project tickets.\n"
    "Rules:\n"
    "1. Answer ONLY from the evidence given. Never use outside knowledge.\n"
    "2. Put the ticket ID in brackets after every factual claim, e.g. [ATL-000]. Use brackets only for ticket IDs.\n"
    "3. If the evidence is not enough, say so plainly. Do not guess.\n"
    "4. Include the specific facts: names, dates, counts, days overdue, and the reason from the comments.\n"
    "5. If tickets disagree, say they conflict, give each value with its ticket, and do not pick one.\n"
    "6. If the question's premise is contradicted by a ticket, say so.\n"
    "7. If tickets wait on or depend on another ticket, name that root cause first, then list EVERY ticket "
    "it holds up, each with its ID.\n"
    "8. If nothing in the evidence answers the question, say that in one sentence and cite nothing.\n"
    f"Today's date is {config.TODAY}.\n"
    'Reply as JSON: {"answer": "...", "citations": ["ATL-000"]}. '
    '"citations" lists every ticket ID you cited in "answer".'
)
RETRY_NUDGE = 'That was not valid. Reply again with ONLY a JSON object: {"answer": "...", "citations": ["ID"]}'
NO_EVIDENCE_ANSWER = "I could not find any ticket evidence to answer that, so I can't answer it."
INVALID_ANSWER = "I could not produce a valid answer."



class SynthOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str = Field(min_length=1)
    citations: list[str]


SYNTH_SCHEMA = SynthOutput.model_json_schema()


def format_evidence(evidence: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    used = 0
    for i, e in enumerate(evidence):
        args = compact_json(e["args"], 200)
        body = compact_json(e["data"], config.EVIDENCE_ITEM_CHARS) if e["ok"] else f"FAILED: {e['error']}"
        line = f"- {e['tool']}({args}) -> {body}"
        if used + len(line) > config.EVIDENCE_TOTAL_CHARS:
            lines.append(f"- ({len(evidence) - i} further tool results omitted for length)")
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


def make_synthesizer_node(llm: LLM, known_ids: set[str] | None):
    async def synthesizer_node(state: AgentState) -> dict[str, Any]:
        evidence = state.get("evidence", [])
        if not any(e["ok"] for e in evidence):
            # Nothing usable came back: answer deterministically rather than let a model guess.
            return {"route": "end", "status": "ok", "answer": NO_EVIDENCE_ANSWER, "citations": []}

        messages = [
            {"role": "system", "content": SYNTH_SYSTEM},
            {"role": "user", "content": f"Question: {state['question']}\n\nEvidence:\n{format_evidence(evidence)}"},
        ]
        output: SynthOutput | None = None
        errors: list[str] = []
        for attempt in (1, 2):  # one retry, no more
            try:
                reply = await llm.chat("synthesizer", messages, format=SYNTH_SCHEMA)
            except Exception as exc:  # noqa: BLE001
                msg = f"llm_unavailable: {type(exc).__name__}: {exc}"[: config.ERROR_TEXT_CHARS]
                return {
                    "route": "end",
                    "status": "error",
                    "answer": f"I could not reach the language model. ({msg})",
                    "errors": [msg],
                }
            try:
                output = SynthOutput.model_validate_json(reply.content)
                break
            except ValidationError:
                errors.append(f"invalid_output (attempt {attempt}): {reply.content[: config.SNIPPET_CHARS]!r}")
                messages = [
                    *messages,
                    {"role": "assistant", "content": reply.content},
                    {"role": "user", "content": RETRY_NUDGE},
                ]

        if output is None:
            return {"route": "end", "status": "invalid_output", "answer": INVALID_ANSWER, "citations": [], "errors": errors}

        answer, citations, warnings = verify_citations(
            trim_not_found_tangent(strip_trailing_id_list(bracket_bare_ids(tidy_brackets(output.answer)))),
            output.citations,
            known_ids=known_ids,
            ev_ids=evidence_ids(evidence),
            mentioned=set(mentioned_ids(evidence)),
        )
        return {
            "route": "end",
            "status": "ok",
            "answer": answer,
            "citations": citations,
            "errors": errors,
            "warnings": warnings,
        }

    return synthesizer_node
