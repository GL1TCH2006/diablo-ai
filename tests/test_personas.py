"""Persona profiles: presentation data only, with a strict loader and a selector.

A persona changes wording. It must never change behaviour. The most important
test in this file is
:func:`test_a_persona_cannot_change_any_permission_decision`, which runs the same
decisions through ``PermissionManager`` under every persona and asserts they come
out identical - so the guarantee is measured, not asserted in a comment.
"""

from __future__ import annotations

import sys
import types

import pytest

from config.permissions import Decision, PermissionLevel, PermissionManager
from config.settings import Settings
from personas import (
    COMPLETED_REQUIRES_VERIFIED,
    DEFAULT_PERSONA,
    OUTCOMES,
    PersonaError,
    alias_index,
    format_status,
    load_all,
    load_persona,
    persona_names,
    select_persona,
)
from personas.loader import Persona

BOTH = ("raphael", "sage")


@pytest.fixture()
def settings(tmp_path) -> Settings:
    root = tmp_path.resolve()
    return Settings(base_dir=root, allowed_roots=[root],
                    memory_dir=root / "memory", raw={})


# ══════════════════════════════════════════════════ loading and validation


def test_both_personas_load():
    for name in BOTH:
        persona = load_persona(name)
        assert persona.name == name
        assert persona.description
        assert persona.orb_palette_key


def test_every_persona_can_describe_every_outcome():
    for name in BOTH:
        persona = load_persona(name)
        for outcome in OUTCOMES:
            rendered = format_status(persona, outcome, "detail")
            assert rendered.strip(), f"{name}/{outcome} rendered nothing"
            assert "{" not in rendered, (
                f"{name}/{outcome} left a template placeholder unsubstituted: "
                f"{rendered!r}")


def test_the_loader_refuses_an_unknown_persona():
    with pytest.raises(PersonaError):
        load_persona("definitely-not-a-persona")


def test_the_loader_refuses_a_non_identifier_name():
    with pytest.raises(PersonaError):
        load_persona("../../etc/passwd")


def test_persona_names_lists_only_profiles():
    names = persona_names()
    for expected in BOTH:
        assert expected in names
    for module in ("loader", "selector", "formats", "__init__"):
        assert module not in names, (
            f"{module} was offered as a persona; only profile modules may be")


# ══════════════════════════════════════════════ bad files fail closed


def _install_fake_persona(monkeypatch, name: str, **fields) -> None:
    """Register a deliberately malformed persona module."""
    module = types.ModuleType(f"personas.{name}")
    for key, value in fields.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, f"personas.{name}", module)


def test_a_missing_reply_outcome_is_refused(monkeypatch):
    """The most important validation: a persona that cannot say 'unverified'."""
    _install_fake_persona(
        monkeypatch, "broken_partial", persona="broken_partial",
        description="has no unverified wording",
        orb_palette_key="gold",
        replies={"completed": "ok", "failed": "no", "cancelled": "stopped"})
    with pytest.raises(PersonaError) as excinfo:
        load_persona("broken_partial")
    assert "unverified" in str(excinfo.value), (
        f"the error does not name the missing outcome: {excinfo.value}")


def test_an_unknown_orb_palette_is_refused(monkeypatch):
    _install_fake_persona(
        monkeypatch, "broken_palette", persona="broken_palette", description="x",
        orb_palette_key="chartreuse", replies={o: "t" for o in OUTCOMES})
    with pytest.raises(PersonaError) as excinfo:
        load_persona("broken_palette")
    assert "palette" in str(excinfo.value).lower()


def test_a_missing_name_is_refused(monkeypatch):
    _install_fake_persona(
        monkeypatch, "broken_noname", description="x", orb_palette_key="gold",
        replies={o: "t" for o in OUTCOMES})
    with pytest.raises(PersonaError):
        load_persona("broken_noname")


def test_a_reserved_alias_is_refused(monkeypatch):
    _install_fake_persona(
        monkeypatch, "broken_alias", persona="broken_alias", description="x",
        orb_palette_key="gold", aliases=["quit"],
        replies={o: "t" for o in OUTCOMES})
    with pytest.raises(PersonaError) as excinfo:
        load_persona("broken_alias")
    assert "reserved" in str(excinfo.value).lower()


def test_a_string_alias_list_is_refused(monkeypatch):
    """``aliases = "sage"`` is a string, not a list; a string would iterate by character."""
    _install_fake_persona(
        monkeypatch, "broken_stralias", persona="broken_stralias", description="x",
        orb_palette_key="gold", aliases="sage",
        replies={o: "t" for o in OUTCOMES})
    with pytest.raises(PersonaError):
        load_persona("broken_stralias")


def test_a_broken_persona_is_skipped_not_promoted(monkeypatch):
    """One bad file must not become the default persona."""
    _install_fake_persona(
        monkeypatch, "broken_bad", persona="broken_bad", description="x",
        orb_palette_key="gold", replies={"completed": "only this"})
    monkeypatch.setattr("personas.loader.persona_names",
                        lambda: ("broken_bad",) + BOTH)
    found = load_all()
    assert "broken_bad" not in found
    for good in BOTH:
        assert good in found, f"{good} was lost because another file was broken"


def test_a_failed_load_stays_failed(monkeypatch):
    """No caching of a failure: a second call cannot succeed by accident."""
    _install_fake_persona(
        monkeypatch, "broken_twice", persona="broken_twice", description="x",
        orb_palette_key="gold", replies={"completed": "only this"})
    for _ in range(3):
        with pytest.raises(PersonaError):
            load_persona("broken_twice")


# ═══════════════════════════════════════════════════════ alias selection


def test_an_alias_selects_its_persona():
    assert select_persona("raphael, run the tests").name == "raphael"
    assert select_persona("sol, what is up").name == "sage"


def test_a_spoken_style_request_still_matches():
    """Leading address words are stripped, because that is how names arrive."""
    for text in ("hey raphael what is up", "okay sol do the thing",
                 "Hello Sage, status?", "raphael"):
        assert select_persona(text).name in BOTH, text


def test_a_persona_is_not_triggered_by_a_substring():
    """``sage`` must not fire on 'usage'; ``sol`` must not fire on 'solution'."""
    for text in ("show me the usage", "the solution is obvious",
                 "what is the usage of sage3"):
        assert select_persona(text).name == DEFAULT_PERSONA, (
            f"{text!r} selected {select_persona(text).name}")


def test_an_unmatched_request_uses_the_default():
    assert select_persona("do the thing").name == DEFAULT_PERSONA
    assert select_persona("").name == DEFAULT_PERSONA
    assert select_persona("   ").name == DEFAULT_PERSONA


def test_a_default_can_be_named_explicitly():
    assert select_persona("something else", default="raphael").name == "raphael"


def test_case_and_punctuation_do_not_matter():
    for text in ("RAPHAEL", "Raphael!", "  raphael  ", "raphael."):
        assert select_persona(text).name == "raphael", text


def test_every_alias_resolves_to_a_real_persona():
    index = alias_index()
    assert index, "the alias index is empty"
    for alias, name in index.items():
        assert name in BOTH, f"alias {alias!r} resolves to unknown persona {name!r}"
        assert load_persona(name).name == name


def test_aliases_are_unique_across_personas():
    """A contested alias would make selection order-dependent."""
    seen: dict[str, str] = {}
    for name in BOTH:
        for alias in load_persona(name).aliases:
            assert alias not in seen, (
                f"alias {alias!r} is claimed by both {seen.get(alias)} and {name}")
            seen[alias] = name


# ═════════════════════════════════════ the hard rule: never overclaim success


def test_completed_never_appears_in_unverified_wording():
    for name in BOTH:
        rendered = format_status(load_persona(name), "unverified",
                                "the file was written but nothing checked it").lower()
        assert "completed" not in rendered, (
            f"{name}'s unverified wording says completed: {rendered!r}")


def test_an_unknown_outcome_is_treated_as_unverified_not_success():
    for name in BOTH:
        rendered = format_status(load_persona(name), "totally-unknown", "x").lower()
        assert "completed" not in rendered, (
            f"{name} rendered an unknown outcome as completed: {rendered!r}")
        assert rendered == format_status(load_persona(name), "unverified", "x").lower(), (
            f"{name} did not fall back to the unverified wording")


def test_an_empty_outcome_is_treated_as_unverified():
    for name in BOTH:
        rendered = format_status(load_persona(name), "", "x").lower()
        assert "completed" not in rendered


def test_every_outcome_renders_distinct_wording():
    for name in BOTH:
        persona = load_persona(name)
        rendered = {o: format_status(persona, o, "same details") for o in OUTCOMES}
        assert len(set(rendered.values())) == len(OUTCOMES), (
            f"{name} renders two outcomes identically: {rendered}")


def test_details_are_truncated_so_a_ui_cannot_be_flooded():
    persona = load_persona("sage")
    rendered = format_status(persona, "completed", "x" * 5000)
    assert len(rendered) < 700, f"an unbounded detail was rendered: {len(rendered)}"


def test_details_are_copied_rather_than_interpolated():
    """A detail containing braces must survive verbatim, not be re-substituted.

    The detail is passed as a *substitution value*, never spliced into the format
    string, so a detail like ``{confidence}`` is shown as written rather than
    being resolved to "high".
    """
    persona = load_persona("sage")
    rendered = format_status(persona, "completed", "{confidence} and {risk}")
    # The detail's own braces are still there, unexpanded...
    assert "{confidence}" in rendered, rendered
    assert "{risk}" in rendered, rendered
    # ...while the template's placeholders were substituted normally.
    assert "Confidence: high" in rendered, rendered
    assert "Risk: none observed." in rendered, rendered


def test_completed_requires_verified_is_exported_and_documented():
    assert COMPLETED_REQUIRES_VERIFIED == "completed"
    assert "NEEDS HUMAN DECISION" in (
        __import__("personas").__doc__), (
        "the names are undecided; the package must say so")


# ══════════════════════════════════════ the property: personas change nothing


PERMISSION_CASES = [
    ("filesystem.read_file", {"path": "C:/PROJECTS/a.txt"}, PermissionLevel.SAFE),
    ("filesystem.write_file", {"path": "C:/nowhere/a.txt"}, PermissionLevel.SENSITIVE),
    ("terminal.execute", {"command": "shutdown /s"}, PermissionLevel.SENSITIVE),
    ("terminal.execute", {"command": "git status"}, PermissionLevel.SENSITIVE),
    ("git.push", {"root": "C:/repo"}, PermissionLevel.SENSITIVE),
    ("git.push", {"root": "C:/PROJECTS/inside"}, PermissionLevel.NORMAL),
    ("applications.open", {"app": "notepad"}, PermissionLevel.NORMAL),
    ("filesystem.delete_file", {"path": "C:/PROJECTS/x"}, PermissionLevel.SENSITIVE),
]


def test_a_persona_cannot_change_any_permission_decision(settings):
    """Every permission decision, under every persona, must be identical.

    The property test this package exists to make checkable: a persona is
    presentation data, so if it could influence an allow/deny it would be a
    privilege-escalation surface. Every persona is loaded first - a persona that
    failed to load is itself a finding, so it is asserted rather than skipped.
    """
    personas = {name: load_persona(name) for name in BOTH}
    assert personas, "no persona loaded, so the property proved nothing"

    baseline = None
    for name in BOTH:
        manager = PermissionManager(settings, None)
        outcomes: list[tuple] = []
        for tool, args, declared in PERMISSION_CASES:
            decision: Decision = manager.evaluate(tool, args, declared)
            outcomes.append((tool, tuple(sorted(args.items())), declared.value,
                             decision.level.value, decision.allow,
                             decision.requires_confirmation))
        if baseline is None:
            baseline = outcomes
            assert outcomes, "the permission case list is empty"
            continue
        assert outcomes == baseline, (
            f"persona {name!r} changed a permission decision; a persona is "
            f"presentation data and must not be able to do that")


def test_persona_loading_does_not_import_into_the_permission_layer():
    """Structural half of the same guarantee: nothing in config/ imports personas."""
    import config.permissions as permissions_module

    source = permissions_module.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "personas" not in text, (
        "config/permissions.py references the personas package; that boundary "
        "must stay closed")


def test_formatting_a_status_touches_no_permission_state(settings):
    """format_status must be free of side effects on the permission manager."""
    manager = PermissionManager(settings, None)
    before = manager.last_decision
    for name in BOTH:
        for outcome in OUTCOMES:
            format_status(load_persona(name), outcome, "detail")
    assert manager.last_decision == before, (
        "formatting a persona reply changed permission state")
    assert manager.session_blocked == set(), (
        "formatting a persona reply blocked a tool")


# ══════════════════════════════════════════════════════ the persona object


def test_a_persona_is_immutable():
    persona = load_persona("sage")
    with pytest.raises(Exception):
        persona.name = "something-else"


def test_reply_for_refuses_an_unknown_outcome():
    persona = load_persona("sage")
    with pytest.raises(PersonaError):
        persona.reply_for("not-an-outcome")


def test_personas_are_distinguishable():
    raphael = load_persona("raphael")
    sage = load_persona("sage")
    assert raphael.replies["completed"] != sage.replies["completed"], (
        "the two personas render completion identically, so one of them is doing "
        "nothing")
    assert raphael.orb_palette_key != sage.orb_palette_key, (
        "the two personas use the same orb palette")