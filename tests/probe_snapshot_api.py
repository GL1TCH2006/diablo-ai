import inspect
import dataclasses
from pathlib import Path

lines = []
lines.append("### LIVE API PROBE (authoritative) ###")
lines.append("")

# --- Snapshot dataclass ---
from coding.snapshot_manager import Snapshot, SnapshotManager  # noqa: E402

lines.append("== Snapshot dataclass ==")
for f in dataclasses.fields(Snapshot):
    has_default = f.default is not dataclasses.MISSING
    has_factory = f.default_factory is not dataclasses.MISSING
    d = "REQUIRED" if (not has_default and not has_factory) else ("default" if has_default else "factory")
    lines.append(f"  {f.name}: {f.type}   [{d}]")
lines.append("  methods: " + ", ".join(sorted(m for m in dir(Snapshot) if callable(getattr(Snapshot, m)) and not m.startswith("_"))))
lines.append("")

lines.append("== SnapshotManager ==")
for name in ("__init__", "create_snapshot", "list_snapshots", "get_snapshot",
             "validate_snapshot", "restore_snapshot", "get_latest_snapshot"):
    obj = getattr(SnapshotManager, name, None)
    if obj is None:
        lines.append(f"  MISSING: {name}")
        continue
    try:
        lines.append(f"  {name}{inspect.signature(obj)}")
    except (ValueError, TypeError):
        lines.append(f"  {name}(no signature)")
lines.append("")

# --- tools.git snapshot wrappers ---
from tools import git  # noqa: E402
lines.append("== tools.git snapshot wrappers ==")
for name in ("git_snapshot", "git_snapshot_list", "git_snapshot_restore"):
    obj = getattr(git, name, None)
    if obj is None:
        lines.append(f"  MISSING: {name}")
        continue
    try:
        lines.append(f"  {name}{inspect.signature(obj)}")
    except (ValueError, TypeError):
        lines.append(f"  {name}(no signature)")
lines.append("")

# --- registry entries ---
from tools import build_registry  # noqa: E402
from config.defaults import load_default_settings  # noqa: E402
settings = load_default_settings()
reg = build_registry(settings)
lines.append("== registry keys containing 'snapshot' ==")
for name in sorted(reg.tools.keys() if hasattr(reg, "tools") else []):
    if "snapshot" in name:
        spec = reg.tools[name]
        lines.append(f"  {name}: level={getattr(spec, 'level', '?')} safe={getattr(spec, 'safe', '?')}")
        params = getattr(spec, "params", None)
        if params:
            lines.append(f"    params={list(params.keys())}")
            req = [k for k, v in params.items() if getattr(v, "required", False)]
            lines.append(f"    required={req}")
lines.append("")

print("\n".join(lines))
