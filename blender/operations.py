"""Authoritative Blender operation inventory.

This module is the single source of truth for which Blender operations exist
and, for each one, whether it mutates the persisted session.

Why this matters
----------------
Three separate places used to spell out the operation list: the wire protocol
(``blender.protocol.SUPPORTED_OPERATIONS``), the capability adapter
(``BlenderCapabilityAdapter._operations``), and the Blender-side dispatch script
(``blender/scripts/runner.py``'s read-only set). They drifted. S2.9 collapses
the Python-side duplication onto this table and keeps the Blender-side copy
honest with a consistency test.

Session-mutation semantics
--------------------------
The dispatch runner re-saves the session ``.blend`` after every successful
operation *except* the read-only ones. So an operation's ``mutates_session``
flag is not a guess: it is the same distinction the Blender-side script already
acts on. It drives cancellation safety -- see
:mod:`blender.capabilities` for how a forced termination is handled per class.

This module is deliberately dependency-free so the Blender-side script and the
tests can reason about it without importing Blender.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OperationSpec:
    """One entry in the authoritative inventory.

    Attributes
    ----------
    name:
        The operation name as it appears on the wire and in capability calls.
    mutates_session:
        ``True`` when a successful run causes the dispatch runner to re-save the
        session file. Terminating such an operation can leave a torn session,
        so the session must be revalidated before it is reused.
    subprocess:
        ``True`` when the operation runs inside a ``blender.exe`` child process.
        ``False`` for purely local work, which has no child to supervise.
    """

    name: str
    mutates_session: bool
    subprocess: bool = True

    @property
    def interruptible(self) -> bool:
        """Whether terminating the child cannot corrupt the session.

        Local work has no child process to interrupt, so it is never
        interruptible. A subprocess operation is safe to terminate only when it
        does not write the session.
        """
        return self.subprocess and not self.mutates_session

    @property
    def requires_session_revalidation(self) -> bool:
        """Whether a forced termination of this operation demands revalidation."""
        return self.subprocess and self.mutates_session


def _spec(name: str, *, mutates: bool, subprocess: bool = True) -> OperationSpec:
    return OperationSpec(name=name, mutates_session=mutates, subprocess=subprocess)


#: The inventory, in the order the dispatch runner documents them.
_SPECS: tuple[OperationSpec, ...] = (
    # Read-only inspection: the dispatch runner does not re-save the session.
    _spec("inspect_scene", mutates=False),
    _spec("list_objects", mutates=False),
    _spec("inspect_object", mutates=False),
    # Scene mutation: the session is re-saved, so termination needs revalidation.
    _spec("create_object", mutates=True),
    _spec("delete_object", mutates=True),
    _spec("modify_object", mutates=True),
    _spec("transform_object", mutates=True),
    _spec("create_material", mutates=True),
    _spec("assign_material", mutates=True),
    _spec("configure_camera", mutates=True),
    _spec("configure_light", mutates=True),
    _spec("execute_script", mutates=True),
    _spec("save", mutates=True),
    _spec("export", mutates=True),
    _spec("render", mutates=True),
    # Local validation of an already-rendered image. No Blender child.
    _spec("inspect_render", mutates=False, subprocess=False),
)

#: name -> spec, the lookup used everywhere else.
OPERATIONS: dict[str, OperationSpec] = {spec.name: spec for spec in _SPECS}

#: Every operation a capability may invoke, subprocess and local alike.
OPERATION_NAMES: tuple[str, ...] = tuple(spec.name for spec in _SPECS)

#: Operations that run inside a ``blender.exe`` child process.
SUBPROCESS_OPERATIONS: tuple[str, ...] = tuple(
    spec.name for spec in _SPECS if spec.subprocess
)

#: Subprocess operations the dispatch runner treats as read-only.
READ_ONLY_OPERATIONS: frozenset[str] = frozenset(
    spec.name for spec in _SPECS if spec.subprocess and not spec.mutates_session
)

#: Operations handled entirely in-process; no child process exists.
LOCAL_OPERATIONS: tuple[str, ...] = tuple(
    spec.name for spec in _SPECS if not spec.subprocess
)

#: Subprocess operations whose termination can leave a torn session.
SESSION_MUTATING_OPERATIONS: frozenset[str] = frozenset(
    spec.name for spec in _SPECS if spec.requires_session_revalidation
)


def spec_for(operation: str) -> OperationSpec | None:
    """The spec for ``operation``, or ``None`` when it is not in the inventory."""
    return OPERATIONS.get(operation)


def is_known_operation(operation: str) -> bool:
    return operation in OPERATIONS


def is_subprocess_operation(operation: str) -> bool:
    spec = OPERATIONS.get(operation)
    return bool(spec and spec.subprocess)


def is_interruptible(operation: str) -> bool:
    """Whether the operation's child can be terminated without session risk."""
    spec = OPERATIONS.get(operation)
    return bool(spec and spec.interruptible)


def requires_session_revalidation(operation: str) -> bool:
    """Whether a forced termination of ``operation`` demands session revalidation."""
    spec = OPERATIONS.get(operation)
    return bool(spec and spec.requires_session_revalidation)


__all__ = [
    "LOCAL_OPERATIONS",
    "OPERATION_NAMES",
    "OPERATIONS",
    "OperationSpec",
    "READ_ONLY_OPERATIONS",
    "SESSION_MUTATING_OPERATIONS",
    "SUBPROCESS_OPERATIONS",
    "is_interruptible",
    "is_known_operation",
    "is_subprocess_operation",
    "requires_session_revalidation",
    "spec_for",
]
