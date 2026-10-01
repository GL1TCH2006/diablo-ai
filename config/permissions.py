from __future__ import annotations

from enum import IntEnum
from pathlib import Path


class ToolBlockedError(Exception):
    pass


class PermissionLevel(IntEnum):
    SAFE = 0
    NORMAL = 1
    SENSITIVE = 2
    BLOCKED = 3


LEVEL_LABELS = {
    PermissionLevel.SAFE: "SAFE",
    PermissionLevel.NORMAL: "NORMAL",
    PermissionLevel.SENSITIVE: "SENSITIVE",
    PermissionLevel.BLOCKED: "BLOCKED",
}


class Decision:
    __slots__ = ("level", "allow", "requires_confirmation", "blocked", "reason")

    def __init__(self, level: PermissionLevel, allow: bool, requires_confirmation: bool, reason: str = ""):
        self.level = level
        self.allow = allow
        self.requires_confirmation = requires_confirmation
        self.blocked = level == PermissionLevel.BLOCKED
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Decision(level={LEVEL_LABELS[self.level]}, allow={self.allow}, confirm={self.requires_confirmation}, reason={self.reason})"


def contains_block_phrase(command: str, phrases: list[str]) -> str | None:
    normalized = " ".join(command.strip().lower().split())
    for phrase in phrases:
        if phrase.lower() in normalized:
            return phrase
    return None


# ---------------------------------------------------------------------------
# Shell-native destructive invocation detection.
#
# The configured blocklist is a flat substring list, so it cannot express
# "this verb, combined with a recursive switch, in any argument order" and it
# knows nothing about PowerShell aliases. This detector adds that reasoning.
# It only ever *adds* hard blocks: configured phrases keep their exact
# substring semantics and every command they matched is still matched.
# ---------------------------------------------------------------------------

_SHELL_SEPARATORS = ("&&", "||", ";", "|", "&", "\n", "\r")

# Removal verbs that are only catastrophic when paired with a recursive switch.
_DESTRUCTIVE_REMOVE_VERBS = frozenset({
    "remove-item", "ri", "rm", "del", "delete", "erase", "rd", "rmdir",
})

# Removal verbs that destroy data with no switch at all.
_DESTRUCTIVE_ALWAYS_VERBS = frozenset({
    "clear-content", "clc",
})

# A destructive-looking token preceded by one of these belongs to another tool
# (for example "git rm -rf --cached build"), not to a shell.
_COMMAND_DRIVERS = frozenset({
    "git", "hg", "svn", "bzr", "npm", "yarn", "pnpm", "bun", "pip", "pip3",
    "conda", "dotnet", "cargo", "docker", "kubectl", "helm", "winget", "choco",
    "scoop", "make", "just", "task", "nuget", "composer", "gem", "brew",
})

# Exact tokens that mean "and everything below".
_RECURSE_EXACT = frozenset({"/s", "--recursive"})

# Roots that must never be handed to a removal verb, even without -Recurse.
_PROTECTED_ROOTS = frozenset({
    "c:\\windows", "c:\\windows\\system32", "c:\\program files",
    "c:\\program files (x86)", "c:\\programdata", "c:\\users",
    "c:\\$recycle.bin", "c:\\system volume information",
    "$recycle.bin", "system volume information",
    "$env:systemroot", "$env:windir", "%systemroot%", "%windir%",
})


def _split_shell_segments(command: str) -> list[str]:
    segments = [command]
    for separator in _SHELL_SEPARATORS:
        expanded: list[str] = []
        for segment in segments:
            expanded.extend(segment.split(separator))
        segments = expanded
    return [segment for segment in segments if segment.strip()]


def _shell_tokens(segment: str) -> list[tuple[str, bool]]:
    """Split one segment into (token, was_quoted) pairs, honouring quotes."""
    tokens: list[tuple[str, bool]] = []
    buf: list[str] = []
    quoted = False
    quote = ""
    for char in segment:
        if quote:
            if char == quote:
                quote = ""
            else:
                buf.append(char)
            continue
        if char in ("'", '"'):
            quote = char
            quoted = True
            continue
        if char.isspace():
            if buf or quoted:
                tokens.append(("".join(buf), quoted))
            buf, quoted = [], False
            continue
        buf.append(char)
    if buf or quoted:
        tokens.append(("".join(buf), quoted))
    return tokens


def _is_switch(token: str) -> bool:
    if not token or token in ("/", "\\", "-", "--"):
        return False
    return token[0] in ("-", "/")


def _is_recurse_switch(token: str) -> bool:
    low = token.lower()
    if low in _RECURSE_EXACT:
        return True
    if not low.startswith("-") or low == "-":
        return False
    flags = low[1:]
    if not flags:
        return False
    if set(flags) <= {"r", "f"}:
        # -r, -rf, -fr, -rff: Remove-Item/rm expose exactly one "r" switch.
        return "r" in flags
    # PowerShell accepts unambiguous parameter prefixes: -recurse, -rec, -re.
    return "recurse".startswith(flags)


def _normalize_path_token(token: str) -> str:
    low = token.lower().replace("/", "\\")
    while len(low) > 1 and low.endswith("\\"):
        low = low[:-1]
    return low


def _is_directory_target(token: str) -> bool:
    return _normalize_path_token(token) in (".", "..", "*", "")


def _is_protected_root(token: str) -> bool:
    normalized = _normalize_path_token(token)
    if normalized in _PROTECTED_ROOTS:
        return True
    return len(normalized) == 2 and normalized[1] == ":" and normalized[0].isalpha()


def destructive_shell_invocation(command: str) -> str | None:
    """Return a reason string when the command is a destructive shell removal.

    Matches PowerShell-native forms, their aliases and cmd/POSIX forms in any
    argument order. Quoted tokens are ignored so that commands which merely
    mention a destructive verb (``python build.py "Remove-Item -Recurse"``) are
    not matched; the configured substring blocklist still covers those.
    """
    for segment in _split_shell_segments(str(command or "")):
        tokens = _shell_tokens(segment)
        for index, (token, quoted) in enumerate(tokens):
            if quoted:
                continue
            verb = token.lower()
            if verb in _DESTRUCTIVE_ALWAYS_VERBS:
                return f"{verb} (destructive without a recursive switch)"
            if verb not in _DESTRUCTIVE_REMOVE_VERBS:
                continue
            if index and tokens[index - 1][0].lower() in _COMMAND_DRIVERS:
                continue
            rest = [value for value, _ in tokens[index + 1:]]
            for candidate in rest:
                if _is_recurse_switch(candidate):
                    return f"{verb} with {candidate.lower()}"
            target = next((value for value in rest if not _is_switch(value)), "")
            if not target:
                continue
            if _is_directory_target(target):
                return f"{verb} targeting {target.lower()}"
            if _is_protected_root(target):
                return f"{verb} targeting {target.lower()}"
    return None


def destructive_command_reason(command: str, blocklist: list[str] | tuple[str, ...]) -> str | None:
    """Hard-block reason for a shell command, or None when it may proceed."""
    phrase = contains_block_phrase(command, list(blocklist or []))
    if phrase:
        return f"blocked phrase: {phrase}"
    reason = destructive_shell_invocation(command)
    if reason:
        return f"destructive invocation: {reason}"
    return None


def is_auto_allowed(command: str, patterns: list[str]) -> bool:
    if any(separator in command for separator in ("\r", "\n", ";", "|", "&")):
        return False
    normalized = " ".join(command.strip().lower().split())
    for pattern in patterns:
        p = pattern.strip().lower()
        if normalized == p:
            return True
        if normalized.startswith(p) and len(normalized) > len(p) and normalized[len(p)] in " \t/":
            return True
    return False


class PermissionManager:
    persistent_deny: dict[str, str] = {}

    def __init__(self, settings, prefs: object | None = None):
        self._settings = settings
        self._prefs = prefs
        self._session_blocked: set[str] = set()
        self.rule_engine = None
        self.last_decision = None

    def _finalize(self, decision, tool_name: str, args: dict, task_id: str = "") -> Decision:
        self.last_decision = (tool_name, decision)
        engine = getattr(self, "rule_engine", None)
        if engine is not None:
            decision, _ = engine.apply(decision, tool_name, args, task_id)
            self.last_decision = (tool_name, decision)
        return decision

    def session_block(self, tool_name: str) -> None:
        self._session_blocked.add(tool_name)

    @property
    def session_blocked(self) -> set[str]:
        return set(self._session_blocked)

    def _pref_rule(self, tool_name: str) -> str | None:
        rule = None
        if self._prefs is not None:
            try:
                rule = self._prefs.get_permission(tool_name)
            except Exception:
                rule = None
        if rule is None:
            rule = self.persistent_deny.get(tool_name)
        return rule

    def evaluate(self, tool_name: str, args: dict, declared: PermissionLevel, level_for=None,
                 task_id: str = "") -> Decision:
        # ``task_id`` is passed by the Executor from the authoritative
        # TaskManager, never from tool arguments, so a task-scoped rule is
        # matched against the real task and cannot be steered by the model.
        f = lambda d: self._finalize(d, tool_name, args, task_id)  # noqa: E731
        if tool_name.startswith("terminal"):
            command = str(args.get("command", ""))
            reason = destructive_command_reason(command, self._settings.terminal_blocklist)
            if reason or tool_name in self._session_blocked:
                detail = f" ({reason})" if reason else ""
                return f(Decision(PermissionLevel.BLOCKED, False, False, f"Blocked by destructive-command policy{detail}."))
            if self._pref_rule(tool_name) == "deny":
                return f(Decision(PermissionLevel.BLOCKED, False, False, "Blocked by user preference."))
            if is_auto_allowed(command, self._settings.terminal_auto_allow):
                return f(Decision(PermissionLevel.NORMAL, True, False))
            return f(Decision(PermissionLevel.SENSITIVE, False, True, "Shell command requires confirmation."))

        level = level_for(args) if level_for else declared
        if level == PermissionLevel.BLOCKED or tool_name in self._session_blocked:
            return f(Decision(PermissionLevel.BLOCKED, False, False, "Blocked by configuration."))
        if tool_name == "capability.execute" and level == PermissionLevel.SENSITIVE:
            if self._pref_rule(tool_name) == "deny":
                return f(Decision(PermissionLevel.BLOCKED, False, False, "Blocked by user preference."))
            return f(Decision(PermissionLevel.SENSITIVE, False, True, "Sensitive capability operation requires explicit confirmation."))
        if self._pref_rule(tool_name) == "deny":
            return f(Decision(PermissionLevel.BLOCKED, False, False, "Blocked by user preference."))
        if self._pref_rule(tool_name) == "allow":
            return f(Decision(level, True, False, "Allowed by user preference."))
        if level == PermissionLevel.SAFE:
            return f(Decision(level, True, False))
        if level == PermissionLevel.NORMAL:
            return f(Decision(level, True, False))
        return f(Decision(level, False, True, "Requires confirmation."))


def path_is_within(path: Path, roots: list[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            root_resolved = root.resolve()
        except OSError:
            continue
        if resolved == root_resolved or resolved.is_relative_to(root_resolved):
            return True
    return False