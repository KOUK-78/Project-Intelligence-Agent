"""PLANNER node (plan): split the question into source-tagged sub-questions. OWNER: B.

One model call with structured output, validated in code. Every failure ends the same way: one retry with the
problem named, then a single sub-question over all sources (the Phase 1 behaviour), so the planner can never
stop a run. Source names and one-line descriptions come from the registry at run time; the prompt text below
contains no dataset content.
"""
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, ValidationError

from app import config
from app.nodes.llm import LLM
from app.nodes.scope import ALL_SOURCES, Source
from app.nodes.state import AgentState

PLANNER_SYSTEM = (
    "You plan how to answer a question from several sources of records.\n"
    "Split the question into the fewest sub-questions needed. Each sub-question is answered from ONE source.\n"
    "Rules:\n"
    "1. Use only the sources listed, and copy each source name exactly.\n"
    "2. If one source can answer the whole question, write ONE sub-question. Do not split for the sake of it.\n"
    "3. Write every sub-question so it makes sense on its own: say what to look for and keep the distinctive "
    "words from the question.\n"
    "4. Each sub-question is answered separately and cannot see the other sub-questions' results, so never write "
    "one that depends on another's answer. Use each source at most once: put everything to look up in a source "
    "into one sub-question.\n"
    "5. Do not answer anything and do not invent facts, names or IDs.\n"
    '6. "reason" is one short sentence on why that source.\n'
    'Reply as JSON: {"sub_questions": [{"id": "sq1", "question": "...", "source": "<source name>", "reason": "..."}]}'
)
PLAN_RETRY_NUDGE = "That plan was not valid. Reply again with ONLY the JSON object, and fix this:"


class SubQuestionOut(BaseModel):
    model_config = ConfigDict(extra="ignore")  # a stray key from the model is not worth a retry
    id: str = ""
    question: str = ""
    source: str = ""
    reason: str = ""


class PlanOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sub_questions: list[SubQuestionOut]


class PlanError(Exception):
    """The model's plan is unusable. `kind` is a short code for the warning; `detail` is what the retry says."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


def plan_schema(source_names: Sequence[str]) -> dict[str, Any]:
    """Flat JSON schema for Ollama structured output. Source is an enum of this run's sources (built per run);
    the size cap is enforced here and again in code."""
    return {
        "type": "object",
        "properties": {
            "sub_questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": config.MAX_SUBQUESTIONS,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "question": {"type": "string"},
                        "source": {"type": "string", "enum": list(source_names)},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "question", "source", "reason"],
                },
            }
        },
        "required": ["sub_questions"],
    }


def _tidy(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


def validate_plan(content: str, source_names: Sequence[str]) -> tuple[list[dict[str, str]], int | None]:
    """Model reply -> (plan, original_length_if_cut). Raises PlanError.

    Ids are reassigned sq1..sqN in order: the model's own ids add nothing and could collide. Source names match
    case-insensitively. Repeated (source, question) pairs are dropped. A plan longer than MAX_SUBQUESTIONS is cut,
    not rejected: the cap is a budget, and the first sub-questions are the ones the model ordered first."""
    try:
        out = PlanOutput.model_validate_json(content)
    except ValidationError:
        raise PlanError("invalid_json", 'the reply was not the JSON object {"sub_questions": [...]}') from None
    canonical = {name.lower(): name for name in source_names}
    plan: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in out.sub_questions:
        question = _tidy(item.question, config.SUBQUESTION_CHARS)
        if not question:
            raise PlanError("empty_question", "a sub-question has no text")
        source = canonical.get(item.source.strip().lower())
        if source is None:
            raise PlanError("unknown_source", f"{item.source.strip()!r} is not a source; use one of: {', '.join(source_names)}")
        key = (source, question.lower())
        if key in seen:
            continue
        seen.add(key)
        plan.append({
            "id": f"sq{len(plan) + 1}",
            "question": question,
            "source": source,
            "reason": _tidy(item.reason, config.REASON_CHARS),
        })
    if not plan:
        raise PlanError("empty_plan", "the plan has no sub-questions")
    total = len(plan)
    if total > config.MAX_SUBQUESTIONS:
        return plan[: config.MAX_SUBQUESTIONS], total
    return plan, None


def fallback_plan(question: str) -> list[dict[str, str]]:
    """Phase 1 behaviour: the whole question, one retrieval loop, every available tool."""
    return [{"id": "sq1", "question": question, "source": ALL_SOURCES,
             "reason": "planner fallback: one question over all sources"}]


def plan_update(plan: list[dict[str, str]], warnings: list[str]) -> dict[str, Any]:
    """State update that starts the sub-question loop at the first sub-question."""
    return {
        "plan": plan,
        "sq_index": 0,
        "sq_attempt": 0,
        "sq_steps": 0,
        "total_steps": 0,
        "sq_question": plan[0]["question"],
        "route": "retrieve",
        "warnings": warnings,
    }


def planner_user_message(question: str, sources: Sequence[Source]) -> str:
    listing = "\n".join(f"- {s.name}: {s.description}" for s in sources)
    return f"Sources:\n{listing}\n\nAt most {config.MAX_SUBQUESTIONS} sub-questions.\n\nQuestion: {question}"


def make_planner_node(llm: LLM, sources: Sequence[Source]):
    names = [s.name for s in sources]
    schema = plan_schema(names) if names else {}

    async def planner_node(state: AgentState) -> dict[str, Any]:
        question = state["question"]
        if not names:  # no registry: nothing to route between
            return plan_update(fallback_plan(question), ["planner_fallback:no_sources"])

        messages = [
            {"role": "system", "content": PLANNER_SYSTEM},
            {"role": "user", "content": planner_user_message(question, sources)},
        ]
        problem = ""
        for _attempt in (1, 2):  # one retry, no more
            try:
                reply = await llm.chat("planner", messages, format=schema)
            except Exception as exc:  # noqa: BLE001 - the agent node reports an unreachable model properly
                return plan_update(fallback_plan(question), [f"planner_fallback:llm_error:{type(exc).__name__}"])
            try:
                plan, cut_from = validate_plan(reply.content, names)
            except PlanError as err:
                problem = err.kind
                messages = [
                    *messages,
                    {"role": "assistant", "content": reply.content},
                    {"role": "user", "content": f"{PLAN_RETRY_NUDGE} {err.detail}."},
                ]
                continue
            warnings = [f"planner_truncated:{cut_from}->{len(plan)}"] if cut_from else []
            return plan_update(plan, warnings)
        return plan_update(fallback_plan(question), [f"planner_fallback:{problem}"])

    return planner_node
