"""Observer mode: read coding agents' local session logs into the ledger. No traffic is intercepted.

Privacy: only sessions whose working directory is under an allow-listed project root are read, and
only metadata is stored (tokens, models, tool names, test commands and exit codes, a short task title).
"""

from __future__ import annotations

import re
from pathlib import Path

_RUNNERS = (
    r"pytest|py\.test|python[0-9.]*\s+-m\s+(?:pytest|unittest)|tox|nox|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test|npx\s+(?:jest|vitest)|jest|vitest|"
    r"go\s+test|cargo\s+(?:test|nextest)|make\s+(?:test|check)|ctest|mvn\s+test|(?:\./)?gradlew?\s+test|"
    r"dotnet\s+test|rspec|phpunit|mix\s+test"
)
# A test runner at the start of a command (or after &&, ;, |, or "("), optionally behind env assignments,
# "uv run"/"poetry run" and a path prefix (".venv/bin/python -m pytest").
TEST_RE = re.compile(
    r"(?:^|&&|[;|(])\s*(?:\w+=\S*\s+)*(?:(?:uv|poetry|pipenv|hatch|pdm)\s+run\s+)?(?:\S*/)?(?:" + _RUNNERS + r")\b")


def is_test_command(cmd: str | None) -> bool:
    return bool(cmd) and bool(TEST_RE.search(cmd))


def under(path: str | None, roots: list[str]) -> bool:
    if not path:
        return False
    p = Path(path).expanduser().resolve()
    for r in roots:
        try:
            p.relative_to(Path(r).expanduser().resolve())
            return True
        except ValueError:
            continue
    return False


def title_of(text: str, n: int = 80) -> str:
    line = " ".join(text.strip().split())
    return line if len(line) <= n else line[: n - 1] + "…"
