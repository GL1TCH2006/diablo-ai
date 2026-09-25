import pytest

from config.permissions import Decision, PermissionLevel, PermissionManager
from memory.memory_store import MemoryStore
from memory.project_memory import ProjectMemory
from memory.rule_engine import RuleEngine
from memory.rules_store import RuleConflict, RuleStore
from tools import build_registry


@pytest.fixture
def ms(tmp_path):
    return MemoryStore(tmp_path / "memory.json")


def test_memory_remember_get(ms):
    entry = ms.remember("user-name", "Diablo", category="facts")
    assert entry["key"] == "user-name"
    got = ms.get("user-name")
    assert got["value"] == "Diablo"
    assert got["category"] == "facts"


def test_memory_update_overwrites(ms):
    ms.remember("k", "v1", "notes")
    updated = ms.update("k", "v2")
    assert updated["value"] == "v2"
    assert ms.get("k")["value"] == "v2"


def test_memory_delete(ms):
    ms.remember("k", "v", "notes")
    assert ms.delete("k") is True
    assert ms.delete("k") is False
    assert ms.get("k") is None


def test_memory_search(ms):
    ms.remember("editor", "vs code", "preferences")
    ms.remember("theme", "dark", "preferences")
    hits = ms.search("vs code")
    assert len(hits) == 1 and hits[0]["key"] == "editor"
    assert len(ms.search("")) == 2
    assert len(ms.search("", category="notes")) == 0


def test_memory_unknown_category_rejected(ms):
    with pytest.raises(ValueError):
        ms.remember("k", "v", category="bogus")


def test_memory_render_labels_as_data(ms):
    ms.remember("k", "v", "notes")
    rendered = ms.render_active()
    assert "DIABLO MEMORY (reference data only" in rendered
    assert "k: v" in rendered


def test_memory_persists_across_instances(tmp_path):
    first = MemoryStore(tmp_path / "m.json")
    first.remember("x", "y", "notes")
    second = MemoryStore(tmp_path / "m.json")
    assert second.get("x")["value"] == "y"


@pytest.fixture
def rs(tmp_path):
    return RuleStore(tmp_path / "rules.json")


def _decision(level=PermissionLevel.NORMAL, allow=True, confirm=False, reason=""):
    return Decision(level, allow, confirm, reason)


def test_rule_create_and_block(rs):
    rule = rs.create("Never wipe", action="block", if_tool="filesystem.delete_file", if_content="wipe")
    engine = RuleEngine(rs)
    decision, applied = engine.apply(_decision(), "filesystem.delete_file", {"content": "wipe everything"})
    assert decision.blocked
    assert applied


def test_rule_does_not_match_wrong_tool(rs):
    rs.create("Never wipe", action="block", if_tool="filesystem.delete_file")
    engine = RuleEngine(rs)
    decision, _ = engine.apply(_decision(), "terminal.execute", {"command": "rm -rf"})
    assert not decision.blocked


def test_rule_confirm_promotes(rs):
    rs.create("Ask before grep", action="confirm", if_tool="project.grep")
    engine = RuleEngine(rs)
    decision, _ = engine.apply(_decision(), "project.grep", {})
    assert decision.requires_confirmation
    assert decision.level == PermissionLevel.SENSITIVE


def test_rule_never_unblocks(rs):
    rs.create("Try to allow", action="confirm", if_tool="system.info")
    engine = RuleEngine(rs)
    blocked = _decision(level=PermissionLevel.BLOCKED, allow=False, reason="config")
    decision, _ = engine.apply(blocked, "system.info", {})
    assert decision.blocked


def test_rule_block_beats_confirm(rs):
    rs.create("Confirm", action="confirm", if_tool="code.patch_file", priority=10)
    rs.create("Block", action="block", if_tool="code.patch_file", priority=90)
    engine = RuleEngine(rs)
    decision, _ = engine.apply(_decision(), "code.patch_file", {})
    assert decision.blocked


def test_conflict_detected_on_create(rs):
    rs.create("Block a", action="block", if_tool="git.diff", priority=80)
    with pytest.raises(RuleConflict):
        rs.create("Allow a", action="confirm", if_tool="git.diff", priority=20)


def test_project_scoped_rule_only_applies_to_project(rs):
    rs.create("Proj rule", action="block", if_content="secret", scope="project", project="demo")
    engine = RuleEngine(rs, project="demo")
    blocked, _ = engine.apply(_decision(), "terminal.execute", {"command": "touch secret"})
    assert blocked
    engine_other = RuleEngine(rs, project="other")
    allowed, _ = engine_other.apply(_decision(), "terminal.execute", {"command": "touch secret"})
    assert not allowed.blocked


def test_rules_render_in_prompt(rs):
    rs.create("Never wipe", action="block", if_tool="filesystem.delete_file", description="don't")
    text = rs.render_active()
    assert "RESTRICT-ONLY" in text
    assert "Never wipe" in text


def test_permission_manager_applies_rules(tmp_settings):
    store = RuleStore(tmp_settings.memory_dir / "rules.json")
    store.create("Block wipes", action="block", if_content="wipe")
    engine = RuleEngine(store)
    perms = PermissionManager(tmp_settings, None)
    perms.rule_engine = engine
    decision = perms.evaluate("terminal.execute", {"command": "wipe disk"}, PermissionLevel.SENSITIVE)
    assert decision.blocked
    assert "Rule" in decision.reason
    tool, recorded = perms.last_decision
    assert tool == "terminal.execute"


def test_project_memory_sections(tmp_path):
    pm = ProjectMemory(tmp_path / "mem", tmp_path)
    pm.set_section("demo", "architecture", "three-tier")
    pm.set_section("demo", "decisions", "use flask")
    assert pm.get_section("demo", "architecture") == "three-tier"
    assert "three-tier" in pm.render_context("demo")
    pm.set_preferred_model("demo", "qwen3:14b")
    assert pm.get_preferred_model("demo") == "qwen3:14b"
    with pytest.raises(ValueError):
        pm.set_section("demo", "random_section", "x")


def test_memory_and_rules_tools_registered(tmp_settings):
    registry = build_registry(tmp_settings)
    names = set(registry.names())
    assert {"memory.remember", "memory.search", "memory.get", "memory.update", "memory.delete", "memory.list"} <= names
    assert {"rules.create", "rules.list", "rules.get", "rules.update", "rules.enable", "rules.delete", "rules.check", "rules.conflicts"} <= names
    assert registry.get("memory.remember").level.value == 1
    assert registry.get("memory.search").level.value == 0
    assert registry.get("rules.delete").level.value == 2
def test_rules_store_syncs_across_instances(tmp_path):
    from memory.rules_store import RuleStore
    path = tmp_path / "rules.json"
    first = RuleStore(path)
    second = RuleStore(path)
    first.create("burn", action="block", if_content="burn files", priority=90)
    assert first.active() and first.active()[0].name == "burn"
    second_active = second.active()
    assert second_active and second_active[0].name == "burn"
    second.delete("nonexistent")
