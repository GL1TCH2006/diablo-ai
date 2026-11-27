from __future__ import annotations

from config.settings import Settings
from tools.filesystem import write_file


def create_or_update_file(path: str, content: str, settings: Settings) -> tuple[bool, str, str]:
    """Create or update a file. Existing files get a backup via the filesystem tool."""
    result = write_file(path, content, settings)
    if result.success:
        return True, result.output, ""
    return False, "", result.error