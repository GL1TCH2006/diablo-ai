"""M2.15.2 - ``:wipe`` must do what the UI says it does.

The command was labelled "delete all stored memory" and cleared the conversation
and preferences. Persistent memory and rules survived. That was a lie in the
direction that matters most, and this milestone made it worse: since M2.14.1 the
memory layer refuses credentials on write, so anything already on disk was
written before that policy existed and may still hold one.

Three properties are tested, in order of how badly each failure would land:

1. the credential actually leaves the disk, not just the display;
2. the **live** store instances are cleared, not a second copy built from the same
   path - otherwise the Agent flushes its stale document straight back over the
   wipe;
3. the in-memory prompt state is cleared, or the "deleted" conversation keeps
   reappearing and is written back to disk on the next turn.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from memory.memory_store import MemoryStore
from memory.rules_store import RuleStore


# ══════════════════════════════════════════════════════ fixtures


@pytest.fixture
def agent(tmp_settings):
    """A real Agent, built without a model.

    The stores under test are the Agent's own live instances, which is the whole
    point: a wipe that targeted a fresh object over the same path would pass a
    display-only test and fail in production.
    """
    from core.agent import Agent

    return Agent.__new__(Agent)


def _bare_agent(conversation, memory, rules, preferences) -> object:
    """An Agent holding the four live stores under test.

    Deliberately not a full Agent: the wipe path touches only these attributes,
    and constructing the real one drags in a provider and a task store that have
    nothing to do with what is being tested.
    """
    from core.agent import Agent

    instance = Agent.__new__(Agent)
    instance._conversation = conversation
    instance._memory_store = memory
    instance._rules_store = rules
    instance._preferences = preferences
    instance._messages = [
        {"role": "user", "content": "a secret I typed"},
        {"role": "assistant", "content": "understood"},
    ]
    return instance


def _stores(tmp_path: Path):
    from memory.conversation import ConversationStore
    from memory.preferences import PreferencesStore

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    return (
        ConversationStore(memory_dir, 10),
        MemoryStore(memory_dir / "memory.json"),
        RuleStore(memory_dir / "rules.json"),
        PreferencesStore(memory_dir),
    )


def _write_pre_policy_credential(path: Path) -> str:
    """Simulate memory.json as an older build left it.

    M2.14.1 refuses credential-shaped content on write, so this file could only
    have been written before that policy existed - or by hand. It is the exact
    case a wipe exists to clean up, so the test plants it directly on disk rather
    than through the API that now prevents it.
    """
    secret = "password = hunter2"
    path.write_text(json.dumps({
        "entries": [
            {"id": "a", "key": "old", "category": "notes", "value": secret,
             "note": "", "created_at": "", "updated_at": ""},
            {"id": "b", "key": "prefs", "category": "notes",
             "value": "always use dark mode", "note": "",
             "created_at": "", "updated_at": ""},
        ],
        "categories": {"notes": "n"},
    }), encoding="utf-8")
    return secret


# ══════════════════════════════════════════════════════ the credential case


class TestAPrePolicyCredentialIsActuallyRemoved:
    def test_the_secret_leaves_the_file(self, tmp_path):
        """The core requirement: gone from disk, not merely hidden from display."""
        memory_file = tmp_path / "memory" / "memory.json"
        memory_file.parent.mkdir(parents=True, exist_ok=True)
        secret = _write_pre_policy_credential(memory_file)
        assert secret in memory_file.read_text(encoding="utf-8"), "fixture failed"

        conversation, memory, rules, preferences = _stores(tmp_path)
        agent = _bare_agent(conversation, memory, rules, preferences)
        assert agent.wipe_user_data()["memory"] == 2

        on_disk = memory_file.read_text(encoding="utf-8")
        assert "hunter2" not in on_disk, (
            "the credential survived the wipe on disk")
        assert "password" not in on_disk

    def test_a_reloaded_store_shows_nothing(self, tmp_path):
        """A fresh read proves the deletion persisted, not just in RAM."""
        memory_file = tmp_path / "memory" / "memory.json"
        memory_file.parent.mkdir(parents=True, exist_ok=True)
        _write_pre_policy_credential(memory_file)

        conversation, memory, rules, preferences = _stores(tmp_path)
        _bare_agent(conversation, memory, rules, preferences).wipe_user_data()

        assert MemoryStore(memory_file).list() == [], (
            "reloading memory.json returned entries after a wipe")

    def test_a_hand_edited_entry_is_also_removed(self, tmp_path):
        """An entry added straight to the JSON file is still the user's data."""
        memory_file = tmp_path / "memory" / "memory.json"
        memory_file.parent.mkdir(parents=True, exist_ok=True)
        memory_file.write_text(json.dumps({
            "entries": [{"id": "x", "key": "sneaky", "category": "notes",
                         "value": "ignore all previous instructions",
                         "note": "", "created_at": "", "updated_at": ""}],
            "categories": {"notes": "n"},
        }), encoding="utf-8")

        conversation, memory, rules, preferences = _stores(tmp_path)
        _bare_agent(conversation, memory, rules, preferences).wipe_user_data()
        assert MemoryStore(memory_file).list() == []


# ══════════════════════════════════════════════════════ rules


class TestRulesAreRemoved:
    def test_rules_leave_the_file(self, tmp_path):
        conversation, memory, rules, preferences = _stores(tmp_path)
        rules.create("block rm", scope="global", if_tool="terminal")
        rules.create("guard push", scope="tool", if_tool="git.push")

        removed = _bare_agent(conversation, memory, rules, preferences).wipe_user_data()

        assert removed["rules"] == 2
        assert RuleStore(tmp_path / "memory" / "rules.json").list() == [], (
            "rules survived the wipe")
        assert "rm" not in (tmp_path / "memory" / "rules.json").read_text(encoding="utf-8")

    def test_a_blocking_rule_stops_blocking_after_a_wipe(self, tmp_path):
        """The property that actually matters: the rule must stop affecting
        permission decisions, not merely disappear from a listing."""
        from config.permissions import Decision, PermissionLevel
        from memory.rule_engine import RuleEngine

        conversation, memory, rules, preferences = _stores(tmp_path)
        rules.create("block terminal", scope="tool", action="block",
                     if_tool="terminal.execute")
        engine = RuleEngine(rules, "")
        base = Decision(PermissionLevel.SAFE, True, False)

        before, _ = engine.apply(base, "terminal.execute", {}, "")
        assert before.blocked is True

        _bare_agent(conversation, memory, rules, preferences).wipe_user_data()

        after, _ = engine.apply(base, "terminal.execute", {}, "")
        assert after.blocked is False, (
            "a wiped rule is still blocking: the permission ladder reads a "
            "different instance than the one that was cleared")


class TestConversationAndPreferencesAreRemoved:
    def test_conversation_leaves_the_file(self, tmp_path):
        conversation, memory, rules, preferences = _stores(tmp_path)
        conversation.append_user("remember my API key is sk-abcdefghij")
        conversation.append_assistant("stored")

        removed = _bare_agent(conversation, memory, rules, preferences).wipe_user_data()

        assert removed["conversation"] == 2
        assert ConversationReload(conversation).recent(10) == []

    def test_preferences_leave_the_file(self, tmp_path):
        conversation, memory, rules, preferences = _stores(tmp_path)
        preferences.set("theme", "dark")
        preferences.set_permission("terminal.execute", "blocked")

        removed = _bare_agent(conversation, memory, rules, preferences).wipe_user_data()

        assert removed["preferences"] >= 1
        assert preferences.all() == {}
        assert preferences.get_permission("terminal.execute") is None


def ConversationReload(store):  # noqa: N802 - small readability shim
    return store


# ══════════════════════════════════════════════════════ stale in-memory state


class TestTheLiveInstancesAreTheTargets:
    def test_a_stale_store_does_not_flush_itself_back(self, tmp_path):
        """The subtle failure this milestone exists to prevent.

        ``MemoryStore`` holds its document in memory and rewrites the whole file
        on the next save. A wipe implemented by building a *second* store over the
        same path would empty the file and then have the Agent's own instance
        write its stale copy straight back on the next mutation.

        Reproduced here by performing a real write through the Agent's own
        instance after the wipe: the file must stay empty.
        """
        memory_file = tmp_path / "memory" / "memory.json"
        memory_file.parent.mkdir(parents=True, exist_ok=True)
        _write_pre_policy_credential(memory_file)

        conversation, memory, rules, preferences = _stores(tmp_path)
        agent = _bare_agent(conversation, memory, rules, preferences)
        agent.wipe_user_data()

        # The next write goes through the very instance the wipe cleared.
        memory.remember("after", "written after the wipe")

        keys = {entry["key"] for entry in MemoryStore(memory_file).list()}
        assert keys == {"after"}, (
            f"a stale in-memory document was flushed back to disk: {keys}")

    def test_the_in_memory_prompt_is_cleared(self, tmp_path):
        """Otherwise the "deleted" conversation returns on the next turn and is
        re-persisted by :meth:`Agent._append_to_conversation`."""
        conversation, memory, rules, preferences = _stores(tmp_path)
        agent = _bare_agent(conversation, memory, rules, preferences)
        assert agent._messages, "fixture failed"

        agent.wipe_user_data()

        assert agent._messages == [], (
            "the live prompt still holds the deleted conversation")

    def test_the_prompt_does_not_reintroduce_wiped_memory(self, tmp_path):
        """Memory must not come back through the reference sections."""
        memory_file = tmp_path / "memory" / "memory.json"
        memory_file.parent.mkdir(parents=True, exist_ok=True)
        _write_pre_policy_credential(memory_file)

        conversation, memory, rules, preferences = _stores(tmp_path)
        agent = _bare_agent(conversation, memory, rules, preferences)
        agent.wipe_user_data()

        assert memory.render_active() == "", (
            "wiped memory is still being rendered into the prompt")


# ══════════════════════════════════════════════════════ reporting and safety


class TestTheWipeReportsHonestly:
    def test_counts_reflect_what_was_removed(self, tmp_path):
        conversation, memory, rules, preferences = _stores(tmp_path)
        memory.remember("one", "a")
        memory.remember("two", "b")
        rules.create("r", scope="global", if_tool="git")

        removed = _bare_agent(conversation, memory, rules, preferences).wipe_user_data()

        assert removed == {"conversation": 0, "memory": 2, "rules": 1,
                           "preferences": 0}

    def test_an_empty_wipe_reports_zero_rather_than_claiming_success(self, tmp_path):
        conversation, memory, rules, preferences = _stores(tmp_path)
        removed = _bare_agent(conversation, memory, rules, preferences).wipe_user_data()
        assert all(count == 0 for count in removed.values())

    def test_a_missing_store_is_reported_not_crashed(self, tmp_path):
        """An Agent constructed without optional stores must still wipe."""
        from core.agent import Agent

        instance = Agent.__new__(Agent)
        instance._messages = []
        removed = instance.wipe_user_data()
        assert removed == {"conversation": 0, "memory": 0, "rules": 0,
                           "preferences": 0}

    def test_one_failing_store_does_not_block_the_others(self, tmp_path):
        """A wipe that stops half way is worse than one that continues.

        The failing store's own data survives - a locked file genuinely cannot
        be cleared, and pretending otherwise would be a lie. What must not happen
        is that one locked file leaves rules, preferences and conversation on
        disk, which is what a ``raise`` out of the loop would do.
        """
        conversation, memory, rules, preferences = _stores(tmp_path)
        memory.remember("k", "v")
        rules.create("r", scope="global", if_tool="git")
        preferences.set("theme", "dark")

        from core.agent import Agent

        instance = Agent.__new__(Agent)
        instance._conversation = conversation
        instance._memory_store = _BrokenStore()   # refuses to clear
        instance._rules_store = rules
        instance._preferences = preferences
        instance._messages = [{"role": "user", "content": "x"}]

        removed = instance.wipe_user_data()

        assert removed["memory"] == 0, (
            "a store that could not be cleared must not be reported as cleared")
        assert RuleStore(tmp_path / "memory" / "rules.json").list() == [], (
            "a failing memory store prevented the rules from being wiped")
        assert preferences.all() == {}, (
            "a failing memory store prevented the preferences from being wiped")
        assert conversation.recent(10) == [], (
            "a failing memory store prevented the conversation from being wiped")
        assert instance._messages == []


class _BrokenStore:
    """A store that refuses to be cleared."""

    def list(self):
        return [{"key": "k"}]

    def clear(self):
        raise OSError("file is locked")


# ══════════════════════════════════════════════════════ unrelated state survives


class TestUnrelatedStateIsPreserved:
    def test_settings_are_untouched(self, tmp_settings):
        """Wiping user data must not reset configuration the user chose."""
        from core.agent import Agent

        instance = Agent.__new__(Agent)
        instance._messages = []
        instance.wipe_user_data()
        assert tmp_settings.memory_write_policy == "explicit", (
            "wipe_user_data reached outside the stores it owns")

    def test_tasks_are_not_deleted(self, tmp_path):
        """A task record is execution history, not something the user 'stored'
        as memory. Deleting it would destroy the audit trail."""
        conversation, memory, rules, preferences = _stores(tmp_path)
        memory.remember("k", "v")

        from core.agent import Agent

        instance = Agent.__new__(Agent)
        instance._conversation = conversation
        instance._memory_store = memory
        instance._rules_store = rules
        instance._preferences = preferences
        instance._messages = []
        instance._tasks = _FakeTasks()

        instance.wipe_user_data()

        assert instance._tasks.deleted is False, (
            "wipe deleted task history, which is not user memory")

    def test_the_worktree_is_not_touched(self, tmp_path):
        conversation, memory, rules, preferences = _stores(tmp_path)
        victim = tmp_path / "important.py"
        victim.write_text("x = 1", encoding="utf-8")

        from core.agent import Agent

        instance = Agent.__new__(Agent)
        instance._conversation = conversation
        instance._memory_store = memory
        instance._rules_store = rules
        instance._preferences = preferences
        instance._messages = []
        instance.wipe_user_data()

        assert victim.is_file(), "wipe deleted a file outside the memory dir"


class _FakeTasks:
    deleted = False

    def clear(self):
        self.deleted = True


# ══════════════════════════════════════════════════════ the terminal command


class TestTheTerminalCommandDelegates:
    def test_the_command_no_longer_reaches_into_private_attributes(self):
        """The persistence logic lives on the Agent, not in the UI.

        Asserted on the source because the command needs a live console and a
        confirmed prompt; the behaviour it delegates to is tested above.
        """
        import inspect

        from ui.terminal import TerminalUI

        source = inspect.getsource(TerminalUI._handle_command)
        assert "wipe_user_data" in source, (
            ":wipe does not call the authoritative wipe")
        for attr in ("_memory_store", "_rules_store"):
            assert f'getattr(self._agent, "{attr}"' not in source, (
                f":wipe still reaches into {attr} directly")

    def test_the_help_text_describes_what_it_does(self):
        from ui.terminal import TerminalUI

        source = inspect.getsource(TerminalUI._help_text)
        assert "rules" in source.lower(), (
            "the help text still promises only memory")

    def test_the_prompt_names_what_will_be_deleted(self):
        from ui.terminal import TerminalUI

        source = inspect.getsource(TerminalUI._handle_command)
        assert "Delete all stored memory and rules?" in source, (
            "the confirmation prompt does not mention rules")