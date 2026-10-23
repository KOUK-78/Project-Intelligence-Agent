"""EVIDENCE CHECK node (check): is the evidence for this sub-question enough? OWNER: B.

Code decides everything it can: the source is down, every call failed, no record came back. Only when real
records exist does the small model answer one yes/no question. An insufficient answer earns ONE rewrite of the
query (config.MAX_REWRITES_PER_SUBQUESTION) if the step budget allows; after that the sub-question is marked
unresolved. Nothing here loops: every path either re-runs retrieval once or commits a result and moves on.
"""
from typing import Any, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import config
from app.nodes.llm import LLM
from app.nodes.scope import ALL_SOURCES
from app.nodes.state import AgentState, compact_json, evidence_for
from app.nodes.synthesizer import format_evidence
from app.nodes.verify import evidence_ids

CHECK_SYSTEM = (
    "You judge whether the evidence is enough to answer one sub-question.\n"
    "Say sufficient=true only if the evidence itself contains what the sub-question asks for.\n"
    "Say sufficient=false if the evidence is empty, only loosely related, or about something other than what "
    "was asked.\n"
    "Use only the evidence shown. Never use outside knowledge and do not answer the sub-question.\n"
    'Reply as JSON: {"sufficient": true or false, "missing": "what is still needed, in a few words"}'
)
REWRITE_SYSTEM = (
    "A search for a sub-question did not find enough evidence. Rewrite the sub-question once so a keyword "
    "search is more likely to succeed: use different or fewer distinctive words, drop filters, keep the meaning.\n"
    "Do not repeat what was already tried. Do not answer it.\n"
    'Reply as JSON: {"question": "..."}'
)
CHECK_RETRY_NUDGE = 'That was not valid. Reply again with ONLY a JSON object: {"sufficient": true or false, "missing": "..."}'
REWRITE_RETRY_NUDGE = 'That was not valid. Reply again with ONLY a JSON object: {"question": "..."}'

# Error prefixes (from the MCP client) meaning a source could not be reached, as opposed to a bad query.
# "source_unavailable" is the prefix proposed to D for tools of a server that failed to start.
CONNECTIVITY_ERRORS = ("not_connected", "timeout", "call_failed", "source_unavailable")


class CheckOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sufficient: bool
    missing: str = ""


class RewriteOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    question: str = Field(min_length=1)


CHECK_SCHEMA = CheckOutput.model_json_schema()
REWRITE_SCHEMA = RewriteOutput.model_json_schema()


class Verdict(NamedTuple):
    status: str  # sufficient | insufficient | unavailable
    reason: str  # short code kept in the sub-question result
    missing: str = ""  # what the model says is still needed (steers the rewrite)


def code_verdict(evidence: list[dict[str, Any]], inner_status: str) -> Verdict | None:
    """The checks that need no model. None means real records exist, so the model should judge them."""
    if inner_status == "unavailable":
        return Verdict("unavailable", "source_down")
    good = [e for e in evidence if e.get("ok")]
    if not good:
        if inner_status == "error":  # the tool node gave up after repeated failures
            return Verdict("unavailable", "tool_failures")
        errors = [str(e.get("error") or "") for e in evidence]
        if errors and all(err.startswith(CONNECTIVITY_ERRORS) for err in errors):
            return Verdict("unavailable", "unreachable")
        return Verdict("insufficient", "no_successful_evidence")
    if not evidence_ids(good):  # calls worked but returned no record (empty search, not-found payload)
        return Verdict("insufficient", "no_records")
    return None


def describe_tried(evidence: list[dict[str, Any]]) -> str:
    """What was already searched, one line per call, so the rewrite does not repeat it."""
    lines = []
    for e in evidence:
        if not e.get("ok"):
            outcome = f"failed ({str(e.get('error'))[: config.SNIPPET_CHARS]})"
        elif isinstance(e.get("data"), dict) and "count" in e["data"]:
            outcome = f"{e['data']['count']} results"
        else:
            outcome = "ok"
        lines.append(f"- {e.get('tool')}({compact_json(e.get('args', {}), config.SNIPPET_CHARS)}) -> {outcome}")
    return "\n".join(lines) or "- nothing"


async def ask_model(llm: LLM, question: str, evidence: list[dict[str, Any]]) -> tuple[Verdict, list[str]]:
    """Yes/no from the model. One retry on a bad reply; if it is still bad, fail OPEN (sufficient): real records
    exist and the synthesizer is the one that must say 'not enough evidence', so a broken check never throws
    evidence away or burns budget. LLM exceptions propagate to the node."""
    messages = [
        {"role": "system", "content": CHECK_SYSTEM},
        {"role": "user", "content": f"Sub-question: {question}\n\nEvidence:\n"
                                    f"{format_evidence(evidence, config.EVIDENCE_CHECK_CHARS)}"},
    ]
    problems: list[str] = []
    for _attempt in (1, 2):
        reply = await llm.chat("evidence_check", messages, format=CHECK_SCHEMA)
        try:
            out = CheckOutput.model_validate_json(reply.content)
        except ValidationError:
            problems.append(f"evidence_check_invalid: {reply.content[: config.SNIPPET_CHARS]!r}")
            messages = [*messages, {"role": "assistant", "content": reply.content},
                        {"role": "user", "content": CHECK_RETRY_NUDGE}]
            continue
        return Verdict("sufficient" if out.sufficient else "insufficient", "model", out.missing.strip()), problems
    return Verdict("sufficient", "check_invalid"), problems


async def rewrite_query(llm: LLM, question: str, missing: str, evidence: list[dict[str, Any]]) -> str | None:
    """One rewritten query, or None if the model gave nothing usable or the same query again."""
    messages = [
        {"role": "system", "content": REWRITE_SYSTEM},
        {"role": "user", "content": f"Sub-question: {question}\nStill needed: {missing or 'not stated'}\n"
                                    f"Already tried:\n{describe_tried(evidence)}"},
    ]
    for _attempt in (1, 2):
        reply = await llm.chat("rewrite", messages, format=REWRITE_SCHEMA)
        try:
            out = RewriteOutput.model_validate_json(reply.content)
        except ValidationError:
            messages = [*messages, {"role": "assistant", "content": reply.content},
                        {"role": "user", "content": REWRITE_RETRY_NUDGE}]
            continue
        new = " ".join(out.question.split())[: config.SUBQUESTION_CHARS]
        return new if new and new.lower() != question.lower() else None
    return None


def budget_left(state: AgentState) -> bool:
    return config.MAX_TOTAL_STEPS - state.get("total_steps", 0) > 0


def unavailable_names(sq: dict[str, str], evidence: list[dict[str, Any]], down: set[str], status: str) -> list[str]:
    """Sources to report as unavailable for this sub-question."""
    if sq["source"] != ALL_SOURCES:
        return [sq["source"]] if status == "unavailable" else []
    failed = {str(e.get("source")) for e in evidence if not e.get("ok") and e.get("source") not in (None, ALL_SOURCES)}
    return sorted(down | (failed if status == "unavailable" else set()))


def make_evidence_check_node(llm: LLM, down: set[str] | None = None):
    down_sources = set(down or ())

    def commit(state: AgentState, sq: dict[str, str], evidence: list[dict[str, Any]], status: str, reason: str,
               warnings: list[str]) -> dict[str, Any]:
        """Record the finished sub-question and move to the next one (or to the synthesizer)."""
        attempt = state.get("sq_attempt", 0)
        record = {
            "id": sq["id"],
            "question": sq["question"],
            "source": sq["source"],
            "status": status,  # resolved | unresolved | unavailable
            "reason": reason,
            "steps": state.get("sq_steps", 0),
            "attempts": attempt + 1,
            "rewritten": state.get("sq_question") if attempt > 0 else None,
            "capped": state.get("sq_inner_status") == "loop_cap",
            "unavailable": unavailable_names(sq, evidence, down_sources, status),
        }
        plan = state["plan"]
        nxt = state["sq_index"] + 1
        update: dict[str, Any] = {"sq_results": [record], "sq_index": nxt, "warnings": warnings}
        if nxt < len(plan):
            update.update(route="retrieve", sq_attempt=0, sq_steps=0, sq_question=plan[nxt]["question"],
                          sq_inner_status="")
        else:
            update.update(route="synthesizer")
        return update

    def llm_failure(exc: Exception) -> dict[str, Any]:
        msg = f"llm_unavailable: {type(exc).__name__}: {exc}"[: config.ERROR_TEXT_CHARS]
        return {"route": "end", "status": "error", "answer": f"I could not reach the language model. ({msg})",
                "errors": [msg]}

    async def evidence_check_node(state: AgentState) -> dict[str, Any]:
        sq = state["plan"][state["sq_index"]]
        evidence = evidence_for(state.get("evidence", []), sq["id"])
        inner = state.get("sq_inner_status", "")
        attempt = state.get("sq_attempt", 0)
        warnings: list[str] = []

        if inner == "budget":  # retrieval was skipped: the run's step budget was already spent
            return commit(state, sq, evidence, "unresolved", "budget_exhausted", warnings)

        verdict = code_verdict(evidence, inner)
        if verdict is None:
            try:
                verdict, problems = await ask_model(llm, sq["question"], evidence)
            except Exception as exc:  # noqa: BLE001
                return llm_failure(exc)
            warnings += [f"{p} ({sq['id']})" for p in problems]

        if verdict.status == "sufficient":
            return commit(state, sq, evidence, "resolved", verdict.reason, warnings)
        if verdict.status == "unavailable":
            return commit(state, sq, evidence, "unavailable", verdict.reason, warnings)

        reason = verdict.reason
        if attempt < config.MAX_REWRITES_PER_SUBQUESTION:
            if not budget_left(state):
                reason = "budget_exhausted"
            else:
                try:
                    new_question = await rewrite_query(llm, sq["question"], verdict.missing, evidence)
                except Exception as exc:  # noqa: BLE001
                    return llm_failure(exc)
                if new_question:
                    return {"route": "retrieve", "sq_attempt": attempt + 1, "sq_question": new_question,
                            "sq_inner_status": "", "warnings": warnings}
                reason = "rewrite_invalid"
        return commit(state, sq, evidence, "unresolved", reason, warnings)

    return evidence_check_node
