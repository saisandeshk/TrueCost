"""`truecost init claude-code|codex`: put the in-agent face into a coding agent.

Claude Code: statusline (`truecost status`), a Stop hook (one-line receipt after every turn), an optional
PreToolUse breaker on Bash, and a /truecost command. Codex: the same hooks (its status line only takes
built-in items) and a /prompts:truecost prompt. Dry run by default: prints what would change. With
--write it merges into the settings file (keeping a .bak copy) and never removes existing entries.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path


def _exe() -> str:
    cand = Path(sys.executable).parent / "truecost"
    return shlex.quote(str(cand)) if cand.exists() else "truecost"  # quoted: paths with spaces


def _merge_hooks(cfg: dict, event: str, entry: dict) -> bool:
    hooks = cfg.setdefault("hooks", {}).setdefault(event, [])
    cmd = entry["hooks"][0]["command"]
    if any(h.get("command") == cmd for e in hooks for h in e.get("hooks", [])):
        return False
    hooks.append(entry)
    return True


def plan_claude(settings_path: Path, breaker: bool) -> tuple[dict, dict[str, str]]:
    exe = _exe()
    cfg = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    changes = {}
    if "statusLine" not in cfg:
        cfg["statusLine"] = {"type": "command", "command": f"{exe} status --agent claude", "padding": 0}
        changes["statusLine"] = cfg["statusLine"]["command"]
    if _merge_hooks(cfg, "Stop", {"hooks": [{"type": "command", "command": f"{exe} hook stop --agent claude", "timeout": 10}]}):
        changes["hooks.Stop"] = "one-line receipt after each turn"
    if breaker and _merge_hooks(cfg, "PreToolUse", {"matcher": "Bash", "hooks": [
            {"type": "command", "command": f"{exe} hook pretooluse --agent claude", "timeout": 10}]}):
        changes["hooks.PreToolUse(Bash)"] = "breaker: asks before re-running a test that already failed 3x"
    return cfg, changes


CLAUDE_COMMAND = """---
description: TrueCost receipt for the current coding task (cost per verified outcome)
allowed-tools: Bash({exe} receipt:*)
---
!`{exe} receipt --agent claude`

Show the receipt above to the user exactly as printed, in a code block, and add nothing else.
"""

CODEX_PROMPT = """Run `{exe} receipt --agent codex` in the shell and show its output to the user exactly as printed,
in a code block. Do nothing else.
"""


def init(agent: str, scope: str, write: bool, breaker: bool, project: Path) -> str:
    out = []
    if agent == "claude-code":
        # user scope follows CLAUDE_CONFIG_DIR (e.g. ~/.claude-a) when set, like Claude Code itself
        base = project / ".claude" if scope == "project" else Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
        settings = base / ("settings.local.json" if scope == "project" else "settings.json")
        cfg, changes = plan_claude(settings, breaker)
        cmd_file = base / "commands" / "truecost.md"
        out.append(f"Claude Code ({scope}): {settings}")
        out += [f"  + {k}: {v}" for k, v in changes.items()] or ["  (already installed)"]
        out.append(f"  + /truecost command: {cmd_file}")
        if write:
            settings.parent.mkdir(parents=True, exist_ok=True)
            if settings.exists():
                shutil.copy(settings, settings.with_suffix(settings.suffix + ".bak"))
            settings.write_text(json.dumps(cfg, indent=2) + "\n")
            cmd_file.parent.mkdir(parents=True, exist_ok=True)
            cmd_file.write_text(CLAUDE_COMMAND.format(exe=_exe()))
    elif agent == "codex":
        hooks_file = Path("~/.codex/hooks.json").expanduser()
        cfg = json.loads(hooks_file.read_text()) if hooks_file.exists() else {}
        changes = {}
        # Codex hooks are global: the env prefix keeps TrueCost to this project (silent elsewhere)
        env = f"env TRUECOST_PROJECTS={shlex.quote(str(project.resolve()))} " if scope == "project" else ""
        if _merge_hooks(cfg, "Stop", {"hooks": [{"type": "command", "command": f"{env}{_exe()} hook stop --agent codex", "timeout": 10}]}):
            changes["hooks.Stop"] = (f"one-line receipt after each turn" + (f" (only in {project.resolve()})" if env else "")
                                     + f"\n      command: {env}{_exe()} hook stop --agent codex")
        if breaker and _merge_hooks(cfg, "PreToolUse", {"hooks": [
                {"type": "command", "command": f"{env}{_exe()} hook pretooluse --agent codex", "timeout": 10}]}):
            changes["hooks.PreToolUse"] = "breaker"
        prompt = Path("~/.codex/prompts/truecost.md").expanduser()
        out.append(f"Codex: {hooks_file}")
        out += [f"  + {k}: {v}" for k, v in changes.items()] or ["  (already installed)"]
        out.append(f"  + /prompts:truecost: {prompt}")
        if write:
            if hooks_file.exists():
                shutil.copy(hooks_file, hooks_file.with_suffix(".json.bak"))
            hooks_file.write_text(json.dumps(cfg, indent=2) + "\n")
            prompt.parent.mkdir(parents=True, exist_ok=True)
            prompt.write_text(CODEX_PROMPT.format(exe=_exe()))
    out.append("written." if write else "dry run: nothing changed (add --write to apply).")
    return "\n".join(out)
