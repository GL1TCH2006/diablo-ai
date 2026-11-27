from __future__ import annotations

import re

_CJK = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af\uff00-\uffef]")


def est_tokens(text: str) -> int:
    """Lightweight token estimate: ~4 ASCII chars/token, ~1.3 CJK chars/token."""
    if not text:
        return 0
    ascii_chars = len(_CJK.sub("", text))
    cjk_chars = len(_CJK.findall(text))
    return int(ascii_chars / 4.0 + cjk_chars / 1.5) + 1


def head_tail(text: str, max_chars: int, tail_chars: int = 400) -> str:
    """Trim long text preserving the meaningful head and tail (errors usually live at the end)."""
    if len(text) <= max_chars:
        return text
    head = text[: max(max_chars - tail_chars, 0)]
    tail = text[-min(tail_chars, max_chars):]
    return f"{head}\n[... {len(text) - len(head) - len(tail)} characters omitted]\n{tail}"


def truncate_lines(text: str, max_chars: int, head_lines: int = 200) -> str:
    lines = text.splitlines()
    out_lines: list[str] = []
    used = 0
    for i, line in enumerate(lines, start=1):
        if i > head_lines:
            out_lines.append(f"... ({len(lines) - head_lines} more lines omitted)")
            break
        used += len(line) + 1
        if used > max_chars:
            out_lines.append("... (truncated)")
            break
        out_lines.append(line)
    return "\n".join(out_lines)


class TokenBudget:
    """Allocates a token budget across named prompt sections."""

    def __init__(self, total: int, weights: dict[str, float]):
        self.total = total
        allocated = 0.0
        self.budget: dict[str, int] = {}
        for name, weight in weights.items():
            share = int(total * weight)
            self.budget[name] = share
            allocated += share
        self.budget["reserve"] = max(int(total - allocated), 0)

    def remaining(self, used: dict[str, int]) -> int:
        return max(self.total - sum(used.values()), 0)