"""Source registry tests (D1). No model, no subprocess."""
import pytest

from app import sources
from app.sources import Source


def test_only_jira_registered_by_default():
    assert [s.name for s in sources.all_sources()] == ["jira"]


def test_jira_entry_matches_phase1_behaviour():
    j = sources.get_source("jira")
    assert j.tool_names == ("search_tickets", "get_ticket", "list_overdue")
    assert j.id_prefix == "ATL" and j.data_env == "JIRA_DATA_PATH"
    assert j.id_re.findall("see ATL-112 and ATL-7, not XATL-3") == ["ATL-112", "ATL-7"]


def test_lookup_by_tool_and_id():
    assert sources.source_for_tool("get_ticket").name == "jira"
    assert sources.source_for_tool("nope") is None
    assert sources.source_for_id("ATL-9").name == "jira"
    assert sources.source_for_id("ZZZ-9") is None


def test_data_path_env_override(tmp_path):
    j = sources.get_source("jira")
    assert j.resolved_data_path({}) == j.data_path
    assert str(j.resolved_data_path({"JIRA_DATA_PATH": str(tmp_path / "x.json")})) == str(tmp_path / "x.json")


@pytest.fixture
def clean_registry(monkeypatch):
    monkeypatch.setattr(sources, "_REGISTRY", dict(sources._REGISTRY))


def _src(**kw):
    base = dict(name="other", server_module="m", id_prefix="OTH", data_env="OTH_DATA",
                data_path=sources.config.ROOT, tool_names=("other_tool",))
    return Source(**{**base, **kw})


def test_register_rejects_duplicates(clean_registry):
    sources.register(_src())
    with pytest.raises(ValueError, match="already registered"):
        sources.register(_src())
    with pytest.raises(ValueError, match="prefix"):
        sources.register(_src(name="b", tool_names=("t2",)))
    with pytest.raises(ValueError, match="tool names"):
        sources.register(_src(name="c", id_prefix="CCC", tool_names=("get_ticket",)))


def test_normalizer_runs_only_for_owning_source(clean_registry):
    sources.register(_src(normalizer=lambda a, q: {**a, "touched": True}))
    assert sources.normalize_for_tool("other_tool", {}, "q") == {"touched": True}
    assert "touched" not in sources.normalize_for_tool("search_tickets", {"keyword": "x"}, "q")
    assert sources.normalize_for_tool("unknown", {"a": 1}, "q") == {"a": 1}


def test_jira_guards_stay_jira_only(clean_registry):
    sources.register(_src(tool_names=("other_tool",)))
    args = {"status": "Blocked", "label": "two words"}
    assert sources.normalize_for_tool("other_tool", args, "unrelated") == args
    assert "label" not in sources.normalize_for_tool("search_tickets", args, "unrelated")


def test_scope_sources_adapter_matches_the_planner_contract():
    from app.nodes.scope import Source as ScopeSource

    rows = sources.scope_sources()
    assert [r.name for r in rows] == ["jira"] and isinstance(rows[0], ScopeSource)
    assert rows[0].tools == ("search_tickets", "get_ticket", "list_overdue") and rows[0].description


def test_phase2_runtime_is_a_no_op_with_the_planner_off(monkeypatch):
    monkeypatch.setattr(sources.config, "PLANNER_ENABLED", False)
    assert sources.phase2_runtime() == (None, None)
    assert [s.name for s in sources.all_sources()] == ["jira"]


def test_phase2_runtime_with_the_planner_on(clean_registry, monkeypatch):
    from app.nodes.mcp_client import McpRouter

    monkeypatch.setattr(sources.config, "PLANNER_ENABLED", True)
    client, planner_sources = sources.phase2_runtime()
    assert isinstance(client, McpRouter)
    assert [s.name for s in planner_sources] == ["jira", "notion", "email"]
    assert all(s.description for s in planner_sources)
