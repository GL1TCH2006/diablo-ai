import pytest

from ai.model_router import ModelRouter, estimate_complexity
from tools import build_registry


def _info(name, param_b, size=None):
    from ai.provider import ModelInfo
    return type("MI", (), {"name": name,
                           "parameter_size": f"{param_b}B",
                           "size_bytes": size if size is not None else int(param_b * 1.3 * 1024 ** 3),
                           "context_length": 32768})


@pytest.fixture
def catalog():
    return [_info("qwen3:8b", 8), _info("qwen3:14b", 14), _info("qwen3:latest", 8)]


def _settings(tmp_settings, mode="auto"):
    tmp_settings.models_mode = mode
    tmp_settings.models_fast = "qwen3:8b"
    tmp_settings.models_strong = "qwen3:14b"
    tmp_settings.models_complexity_threshold = 0.6
    tmp_settings.models_max_escalations = 1
    tmp_settings.resource_check_enabled = False
    return tmp_settings


def test_router_uses_metadata_not_name(tmp_settings, catalog):
    settings = _settings(tmp_settings)
    router = ModelRouter(settings, catalog=catalog)
    assert router.is_fast_identity_verified() is True
    assert router.identities["qwen3:14b"].is_strong_class
    assert router.identities["qwen3:latest"].is_8b_class


def test_router_default_fast(tmp_settings, catalog):
    settings = _settings(tmp_settings)
    router = ModelRouter(settings, catalog=catalog)
    choice = router.choose()
    assert choice.name == "qwen3:8b"
    assert choice.source == "default"


def test_router_auto_escalation_after_verify_fail(tmp_settings, catalog):
    settings = _settings(tmp_settings)
    router = ModelRouter(settings, catalog=catalog)
    choice = router.choose(last_verify_failed=True, escalations_used=0)
    assert choice.name == "qwen3:14b"
    assert choice.source == "escalation"
    assert choice.escalated


def test_router_escalation_capped(tmp_settings, catalog):
    settings = _settings(tmp_settings)
    router = ModelRouter(settings, catalog=catalog)
    choice = router.choose(last_verify_failed=True, escalations_used=1)
    assert choice.name != "qwen3:14b"


def test_router_complexity_triggers_strong(tmp_settings, catalog):
    settings = _settings(tmp_settings)
    router = ModelRouter(settings, catalog=catalog)
    choice = router.choose(complexity=0.9)
    assert choice.name == "qwen3:14b"
    assert choice.source == "auto-complexity"


def test_router_force8b_even_when_verify_failed(tmp_settings, catalog):
    settings = _settings(tmp_settings, mode="force8b")
    router = ModelRouter(settings, catalog=catalog)
    choice = router.choose(last_verify_failed=True)
    assert choice.name == "qwen3:8b"
    assert choice.mode == "force8b"


def test_router_force14b(tmp_settings, catalog):
    settings = _settings(tmp_settings, mode="force14b")
    router = ModelRouter(settings, catalog=catalog)
    choice = router.choose()
    assert choice.name == "qwen3:14b"


def test_router_project_preference(tmp_settings, catalog):
    settings = _settings(tmp_settings, mode="project")
    router = ModelRouter(settings, catalog=catalog)
    choice = router.choose(project_pref="qwen3:14b")
    assert choice.name == "qwen3:14b"
    assert choice.source == "project-preference"


def test_router_project_fallback_invalid_pref(tmp_settings, catalog):
    settings = _settings(tmp_settings, mode="project")
    router = ModelRouter(settings, catalog=catalog)
    choice = router.choose(project_pref="nonexistent:0")
    assert choice.name == "qwen3:8b"
    assert choice.source == "fallback"


def test_router_task_override_wins(tmp_settings, catalog):
    settings = _settings(tmp_settings)
    router = ModelRouter(settings, catalog=catalog)
    choice = router.choose(task_override="qwen3:14b", complexity=1.2, last_verify_failed=True, escalations_used=0)
    assert choice.name == "qwen3:14b"
    assert choice.source == "task"


def test_router_resource_fallback(tmp_settings, catalog, monkeypatch):
    import ai.model_router as mr
    settings = _settings(tmp_settings)
    settings.resource_check_enabled = True
    router = ModelRouter(settings, catalog=catalog)
    identity = router.identities["qwen3:14b"]
    identity.size_bytes = 8 * 1024 ** 3
    # M2.13.0: the guard compares against *total* physical memory, because fit is
    # a question about capacity. Patching _available_ram here would let the
    # 8GiB model pass on a machine with 4GiB free of 16GiB total, which is
    # exactly the false positive this fix removes.
    monkeypatch.setattr(mr, "_total_ram", lambda: 6 * 1024 ** 3)
    choice = router._guard_resource(type("MC", (), {"name": "qwen3:14b", "mode": "force14b", "source": "forced", "reason": "", "escalated": False})())
    assert choice.name == "qwen3:8b"
    assert "resource" in choice.source


def test_router_state_shape(tmp_settings, catalog):
    settings = _settings(tmp_settings)
    router = ModelRouter(settings, catalog=catalog)
    state = router.state()
    assert state["mode"] == "AUTO"
    assert "qwen3:8b" in state["models_installed"]
    assert state["fast_identity_verified"] is True


def test_router_identity_mismatch_flag(tmp_settings):
    settings = _settings(tmp_settings, mode="auto")
    settings.models_fast = "qwen3:14b"  # deliberately wrong fast model
    router = ModelRouter(settings, catalog=[_info("qwen3:14b", 14)])
    assert router.is_fast_identity_verified() is False
    choice = router.choose()
    assert choice.name == "qwen3:14b"  # falls back to configured fast anyway


def test_estimate_complexity_heuristic():
    assert estimate_complexity("") >= 0
    assert estimate_complexity("refactor the concurrent framework with recursion") >= 0.3
    assert estimate_complexity("open notepad") < 0.3


def test_models_tools_registered(tmp_settings):
    registry = build_registry(tmp_settings)
    assert "models.status" in registry.names()
    assert "models.set_mode" in registry.names()
    assert registry.get("models.status").level.value == 0
    assert registry.get("models.set_mode").level.value == 1