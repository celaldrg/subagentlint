"""Lint Claude Code subagent Markdown files."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .frontmatter import Frontmatter, FrontmatterError, parse_frontmatter
from .yaml_lite import YVal, unwrap


KNOWN_FIELDS = {
    "name",
    "description",
    "tools",
    "disallowedTools",
    "model",
    "permissionMode",
    "maxTurns",
    "autoCompactWindow",
    "skills",
    "mcpServers",
    "hooks",
    "memory",
    "background",
    "omitClaudeMd",
    "effort",
    "isolation",
    "color",
    "initialPrompt",
    "experimental",
}

FIELD_ALIASES = {
    "permission_mode": "permissionMode",
    "permissionmode": "permissionMode",
    "max_turns": "maxTurns",
    "maxturns": "maxTurns",
    "auto_compact_window": "autoCompactWindow",
    "autocompactwindow": "autoCompactWindow",
    "disallowed_tools": "disallowedTools",
    "disallowedtools": "disallowedTools",
    "disallowtools": "disallowedTools",
    "disallowTools": "disallowedTools",
    "disallowedTool": "disallowedTools",
    "tool": "tools",
    "models": "model",
    "mcp_servers": "mcpServers",
    "mcpservers": "mcpServers",
    "initial_prompt": "initialPrompt",
    "omit_claude_md": "omitClaudeMd",
    "omitclaudemd": "omitClaudeMd",
    "permission-mode": "permissionMode",
}

PERMISSION_MODES = {
    "default",
    "acceptEdits",
    "auto",
    "dontAsk",
    "bypassPermissions",
    "plan",
    "manual",
}

MEMORY_VALUES = {"user", "project", "local"}
ISOLATION_VALUES = {"worktree"}
COLOR_VALUES = {"red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan"}
EFFORT_VALUES = {"low", "medium", "high", "xhigh", "max"}
CACHE_TTL_VALUES = {"5m", "1h"}
MODEL_ALIASES = {"sonnet", "opus", "haiku", "fable", "inherit"}

KNOWN_TOOLS = {
    "Read",
    "Glob",
    "Grep",
    "Bash",
    "PowerShell",
    "Edit",
    "Write",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "TodoWrite",
    "Skill",
    "ToolSearch",
    "EnterWorktree",
    "ExitWorktree",
    "Monitor",
    "TaskStop",
    "SendMessage",
    "Artifact",
    "SubagentHandback",
    "Agent",
    "Task",
    "LSP",
    "AskUserQuestion",
    "EndConversation",
    "EnterPlanMode",
    "ExitPlanMode",
    "ScheduleWakeup",
    "WaitForMcpServers",
    "Workflow",
    "ListAgents",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskUpdate",
    "CronCreate",
    "CronDelete",
    "CronList",
}

HOOK_EVENTS = {
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionRequest",
    "Stop",
    "StopFailure",
    "SubagentStart",
    "SubagentStop",
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "PreCompact",
    "Notification",
    "Setup",
    "TeammateIdle",
    "TaskCreated",
    "TaskCompleted",
}

HOOK_TYPES = {"command", "prompt", "agent"}

BUILTIN_NAMES = {
    "Explore",
    "Plan",
    "general-purpose",
    "claude",
    "statusline-setup",
    "claude-code-guide",
}

SKIP_DIRS = {
    ".git",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "__pycache__",
    ".venv",
    "venv",
    "target",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "site-packages",
    ".eggs",
    "eggs",
}

DESCRIPTION_BUDGET_TOKENS = 15_000
NAME_MAX = 256
SHORT_DESCRIPTION = 40
LONG_BODY_TOKENS = 20_000

# Matches Agent(worker, researcher) or mcp__github__* style entries.
AGENT_SPAWN = re.compile(r"^Agent\((.*)\)\s*$")
MCP_TOOL = re.compile(r"^mcp__[A-Za-z0-9_*-]+$")
TASK_SPAWN = re.compile(r"^Task\((.*)\)\s*$")
BASH_SPEC = re.compile(r"^(Bash|PowerShell)\(.*\)\s*$")


@dataclass
class Finding:
    path: Path
    line: int
    level: str  # error, warning, info
    rule: str
    message: str
    agent: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "line": self.line,
            "level": self.level,
            "rule": self.rule,
            "message": self.message,
            "agent": self.agent,
        }


@dataclass
class AgentReport:
    path: Path
    name: Optional[str]
    description: Optional[str]
    tokens: int
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> int:
        return sum(1 for f in self.findings if f.level == "error")

    @property
    def warnings(self) -> int:
        return sum(1 for f in self.findings if f.level == "warning")

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "name": self.name,
            "description": self.description,
            "tokens": self.tokens,
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass
class LintResult:
    agents: list[AgentReport]
    extra: list[Finding] = field(default_factory=list)

    @property
    def findings(self) -> list[Finding]:
        out: list[Finding] = []
        for agent in self.agents:
            out.extend(agent.findings)
        out.extend(self.extra)
        return out

    @property
    def errors(self) -> int:
        return sum(1 for f in self.findings if f.level == "error")

    @property
    def warnings(self) -> int:
        return sum(1 for f in self.findings if f.level == "warning")

    def to_dict(self) -> dict:
        return {
            "agents": [a.to_dict() for a in self.agents],
            "findings": [f.to_dict() for f in self.findings],
            "summary": {
                "agents": len(self.agents),
                "errors": self.errors,
                "warnings": self.warnings,
            },
        }


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def discover_agent_files(paths: Iterable[Path], include_user: bool = False) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved in seen or not path.is_file():
            return
        if path.suffix.lower() != ".md":
            return
        seen.add(resolved)
        found.append(path)

    for raw in paths:
        path = Path(raw)
        if path.is_file():
            add(path)
            continue
        if path.is_dir():
            _discover_in_dir(path, add)
    if include_user:
        user_dir = Path.home() / ".claude" / "agents"
        if user_dir.is_dir():
            _collect_markdown(user_dir, add)
    found.sort(key=lambda p: str(p).lower())
    return found


def _discover_in_dir(root: Path, add) -> None:
    # If this folder is (or contains only) an agents directory, scan it.
    name = root.name
    if name == "agents" or (root / "SKILL.md").exists() is False and _looks_like_agents_dir(root):
        _collect_markdown(root, add)
    agents = root / ".claude" / "agents"
    if agents.is_dir():
        _collect_markdown(agents, add)
    # Walk for nested .claude/agents, skipping heavy/hidden dirs.
    for dirpath, dirnames, _filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and (d == ".claude" or not d.startswith("."))]
        if base.name == "agents" and base.parent.name == ".claude":
            _collect_markdown(base, add)


def _looks_like_agents_dir(root: Path) -> bool:
    try:
        entries = list(root.iterdir())
    except OSError:
        return False
    md = [p for p in entries if p.is_file() and p.suffix.lower() == ".md"]
    return bool(md) and not (root / ".claude").is_dir()


def _collect_markdown(folder: Path, add) -> None:
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.endswith(".md"):
                add(Path(dirpath) / name)


def lint_paths(paths: Iterable[os.PathLike[str] | str], include_user: bool = False, ignore: Optional[Iterable[str]] = None) -> LintResult:
    files = discover_agent_files([Path(p) for p in paths], include_user=include_user)
    skip = set(ignore or [])
    reports = [lint_path(path) for path in files]
    result = LintResult(agents=reports)
    _check_duplicate_names(result)
    _check_description_budget(result)
    if skip:
        for report in result.agents:
            report.findings = [f for f in report.findings if f.rule not in skip]
        result.extra = [f for f in result.extra if f.rule not in skip]
    return result


def lint_path(path: Path) -> AgentReport:
    path = Path(path)
    report = AgentReport(path=path, name=None, description=None, tokens=0)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        report.findings.append(
            Finding(path, 1, "error", "encoding", "file is not valid UTF-8")
        )
        return report
    except OSError as exc:
        report.findings.append(
            Finding(path, 1, "error", "io", f"cannot read file: {exc}")
        )
        return report

    try:
        fm = parse_frontmatter(text)
    except FrontmatterError as exc:
        # A file with no frontmatter is documentation, not a broken agent,
        # unless the user pointed at the file directly inside an agents dir.
        report.tokens = estimate_tokens(text)
        report.findings.append(
            Finding(path, exc.line, "error" if exc.rule != "frontmatter-missing" else "warning", exc.rule, exc.message)
        )
        return report

    mapping = fm.mapping
    name = _string_field(mapping, "name")
    description = _string_field(mapping, "description")
    report.name = name
    report.description = description
    report.tokens = estimate_tokens(description or "") + estimate_tokens(fm.body)

    _check_name(report, mapping, name)
    _check_description(report, mapping, name, description)
    _check_unknown_fields(report, mapping)
    _check_enum_fields(report, mapping)
    _check_numeric_fields(report, mapping)
    _check_bool_fields(report, mapping)
    _check_tools(report, mapping, "tools")
    _check_tools(report, mapping, "disallowedTools")
    _check_skills(report, mapping)
    _check_hooks(report, mapping)
    _check_mcp_servers(report, mapping)
    _check_experimental(report, mapping)
    _check_body(report, fm)
    _check_filename(report, name)
    for finding in report.findings:
        if finding.agent is None:
            finding.agent = report.name
    return report


def _string_field(mapping: dict[str, YVal], key: str) -> Optional[str]:
    node = mapping.get(key)
    if node is None:
        return None
    value = unwrap(node)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    return None


def _add(report: AgentReport, line: int, level: str, rule: str, message: str) -> None:
    report.findings.append(
        Finding(report.path, line, level, rule, message, agent=report.name)
    )


def _check_name(report: AgentReport, mapping: dict[str, YVal], name: Optional[str]) -> None:
    node = mapping.get("name")
    line = node.line if node else 1
    if node is None:
        if any(k != "name" for k in mapping):
            _add(
                report,
                1,
                "error",
                "name-missing",
                "no 'name' field; Claude Code treats a file without a name as documentation and will not load it as a subagent",
            )
        else:
            _add(
                report,
                1,
                "warning",
                "name-missing",
                "no 'name' field; Claude Code treats this file as documentation and will not load it as a subagent",
            )
        return
    raw = unwrap(node)
    if not isinstance(raw, str) or raw.strip() == "":
        _add(report, line, "error", "name-format", "name must be a non-empty string")
        return
    if name is None:
        return
    if name.startswith("-"):
        _add(
            report,
            line,
            "error",
            "name-format",
            f"name '{name}' starts with '-', which Claude Code rejects; the file will not load",
        )
    if ":" in name:
        _add(
            report,
            line,
            "error",
            "name-format",
            f"name '{name}' contains ':', which is reserved for plugin-scoped identifiers; the file will not load",
        )
    if len(name) > NAME_MAX:
        _add(
            report,
            line,
            "error",
            "name-length",
            f"name is {len(name)} characters; the limit is {NAME_MAX}",
        )
    if name in BUILTIN_NAMES or any(name.lower() == b.lower() for b in BUILTIN_NAMES):
        _add(
            report,
            line,
            "warning",
            "name-builtin",
            f"name '{name}' overrides a built-in Claude Code subagent; this replaces the built-in for this project",
        )


def _check_description(
    report: AgentReport, mapping: dict[str, YVal], name: Optional[str], description: Optional[str]
) -> None:
    node = mapping.get("description")
    if name and node is None:
        _add(
            report,
            mapping["name"].line if "name" in mapping else 1,
            "error",
            "description-missing",
            "name is set but description is missing; Claude Code skips the file and only records the reason in the debug log",
        )
        return
    if node is None:
        return
    line = node.line
    raw = unwrap(node)
    if not isinstance(raw, str) or raw.strip() == "":
        _add(
            report,
            line,
            "error",
            "description-missing",
            "description must be a non-empty string; Claude Code skips a named file without one",
        )
        return
    if len(raw.strip()) < SHORT_DESCRIPTION:
        _add(
            report,
            line,
            "warning",
            "description-short",
            "description is very short; Claude uses it to decide when to delegate, so say what the subagent does and when to use it",
        )


def _check_unknown_fields(report: AgentReport, mapping: dict[str, YVal]) -> None:
    for key, node in mapping.items():
        if key in KNOWN_FIELDS:
            continue
        suggestion = _suggest_field(key)
        if suggestion:
            message = (
                f"unknown frontmatter field '{key}'; did you mean '{suggestion}'? "
                "Claude Code ignores unrecognized fields without reporting an error in the session"
            )
        else:
            message = (
                f"unknown frontmatter field '{key}'; "
                "Claude Code ignores unrecognized fields without reporting an error in the session"
            )
        _add(report, node.line, "warning", "unknown-field", message)


def _suggest_field(key: str) -> Optional[str]:
    if key in FIELD_ALIASES:
        return FIELD_ALIASES[key]
    compact = key.replace("_", "").replace("-", "")
    if compact in FIELD_ALIASES:
        return FIELD_ALIASES[compact]
    # camelCase typo: permissionMod, maxTurn, etc.
    best = None
    best_dist = 3
    for field in KNOWN_FIELDS:
        dist = _levenshtein(key, field)
        if dist < best_dist:
            best_dist = dist
            best = field
    return best


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            ins = cur[j - 1] + 1
            delete = prev[j] + 1
            sub = prev[j - 1] + (ca != cb)
            cur.append(min(ins, delete, sub))
        prev = cur
    return prev[-1]


def _check_enum_fields(report: AgentReport, mapping: dict[str, YVal]) -> None:
    _enum(report, mapping, "permissionMode", PERMISSION_MODES, "permission-mode")
    _enum(report, mapping, "memory", MEMORY_VALUES, "memory-value")
    _enum(report, mapping, "isolation", ISOLATION_VALUES, "isolation-value")
    _enum(report, mapping, "color", COLOR_VALUES, "color-value")
    _enum(report, mapping, "effort", EFFORT_VALUES, "effort-value")
    node = mapping.get("model")
    if node is None:
        return
    value = unwrap(node)
    if not isinstance(value, str) or value.strip() == "":
        _add(report, node.line, "error", "model-value", "model must be a string (alias, inherit, or a full model id)")
        return
    if value in MODEL_ALIASES or value.startswith("claude-"):
        return
    _add(
        report,
        node.line,
        "warning",
        "model-unknown",
        f"model '{value}' is not a known alias (sonnet, opus, haiku, fable, inherit) or a claude-… id",
    )


def _enum(report: AgentReport, mapping: dict[str, YVal], key: str, allowed: set[str], rule: str) -> None:
    node = mapping.get(key)
    if node is None:
        return
    value = unwrap(node)
    if not isinstance(value, str) or value not in allowed:
        options = ", ".join(sorted(allowed))
        _add(report, node.line, "error", rule, f"{key} must be one of: {options}")


def _check_numeric_fields(report: AgentReport, mapping: dict[str, YVal]) -> None:
    node = mapping.get("maxTurns")
    if node is not None:
        value = unwrap(node)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            _add(report, node.line, "error", "max-turns", "maxTurns must be a positive integer")
    node = mapping.get("autoCompactWindow")
    if node is not None:
        value = unwrap(node)
        if isinstance(value, str) and re.fullmatch(r"\d+[kKmM]", value):
            _add(
                report,
                node.line,
                "error",
                "auto-compact-window",
                f"autoCompactWindow value '{value}' is not a plain integer; write 200000, not 200k, or Claude Code ignores the field",
            )
        elif not isinstance(value, int) or isinstance(value, bool) or not (100_000 <= value <= 1_000_000):
            _add(
                report,
                node.line,
                "error",
                "auto-compact-window",
                "autoCompactWindow must be an integer between 100000 and 1000000",
            )


def _check_bool_fields(report: AgentReport, mapping: dict[str, YVal]) -> None:
    for key in ("background", "omitClaudeMd"):
        node = mapping.get(key)
        if node is None:
            continue
        if not isinstance(unwrap(node), bool):
            _add(report, node.line, "error", "bool-field", f"{key} must be true or false")


def _iter_tool_entries(node: YVal) -> list[tuple[int, str]]:
    value = node.value
    out: list[tuple[int, str]] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, YVal):
                raw = unwrap(item)
                if raw is None:
                    continue
                if isinstance(raw, str):
                    out.append((item.line, raw))
                else:
                    out.append((item.line, str(raw)))
        return out
    raw = unwrap(node)
    if isinstance(raw, str):
        for part in _split_tool_list(raw):
            out.append((node.line, part))
    return out


def _split_tool_list(text: str) -> list[str]:
    """Split a comma-separated tool list, keeping commas inside parentheses."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
            buf.append(ch)
            continue
        if ch == ")":
            depth = max(depth - 1, 0)
            buf.append(ch)
            continue
        if ch == "," and depth == 0:
            name = "".join(buf).strip()
            if name:
                parts.append(name)
            buf = []
            continue
        buf.append(ch)
    name = "".join(buf).strip()
    if name:
        parts.append(name)
    return parts


def _check_tools(report: AgentReport, mapping: dict[str, YVal], key: str) -> None:
    node = mapping.get(key)
    if node is None:
        return
    raw = unwrap(node)
    if not isinstance(raw, (str, list)):
        _add(report, node.line, "error", "tools-type", f"{key} must be a comma-separated string or a YAML list")
        return
    entries = _iter_tool_entries(node)
    if key == "tools" and not entries:
        _add(
            report,
            node.line,
            "error",
            "tools-empty",
            "tools is empty; a subagent whose tools list resolves to nothing usually fails to launch",
        )
        return
    for line, entry in entries:
        if _is_known_tool(entry):
            continue
        suggestion = _suggest_tool(entry)
        if suggestion:
            message = f"unknown tool '{entry}' in {key}; did you mean '{suggestion}'?"
        else:
            message = f"unknown tool '{entry}' in {key}"
        _add(report, line, "warning", "tool-unknown", message)


def _is_known_tool(entry: str) -> bool:
    if entry in KNOWN_TOOLS:
        return True
    if AGENT_SPAWN.match(entry) or TASK_SPAWN.match(entry) or BASH_SPEC.match(entry):
        return True
    if MCP_TOOL.match(entry) or entry.startswith("mcp__"):
        return True
    return False


def _suggest_tool(entry: str) -> Optional[str]:
    # Case-only typo: bash -> Bash
    for tool in KNOWN_TOOLS:
        if tool.lower() == entry.lower():
            return tool
    best = None
    best_dist = 3
    for tool in KNOWN_TOOLS:
        dist = _levenshtein(entry, tool)
        if dist < best_dist:
            best_dist = dist
            best = tool
    return best


def _check_skills(report: AgentReport, mapping: dict[str, YVal]) -> None:
    node = mapping.get("skills")
    if node is None:
        return
    raw = unwrap(node)
    names: list[tuple[int, str]] = []
    if isinstance(node.value, list):
        for item in node.value:
            if isinstance(item, YVal) and isinstance(unwrap(item), str):
                names.append((item.line, unwrap(item)))
    elif isinstance(raw, str):
        for part in raw.split(","):
            if part.strip():
                names.append((node.line, part.strip()))
    else:
        _add(report, node.line, "error", "skills-type", "skills must be a list or a comma-separated string of skill names")
        return
    root = _project_root(report.path)
    for line, skill in names:
        if not _skill_exists(root, skill):
            _add(
                report,
                line,
                "warning",
                "skill-missing",
                f"skill '{skill}' was not found under .claude/skills or skills/; it will be skipped at startup if it is not a user or plugin skill",
            )


def _project_root(agent_path: Path) -> Path:
    path = agent_path.parent
    for candidate in [path, *path.parents]:
        if candidate.name == "agents" and candidate.parent.name == ".claude":
            return candidate.parent.parent
        if (candidate / ".claude").is_dir() or (candidate / ".git").exists():
            return candidate
    return agent_path.parent


def _skill_exists(root: Path, name: str) -> bool:
    candidates = [
        root / ".claude" / "skills" / name / "SKILL.md",
        root / ".claude" / "skills" / f"{name}.md",
        root / "skills" / name / "SKILL.md",
        Path.home() / ".claude" / "skills" / name / "SKILL.md",
    ]
    return any(path.is_file() for path in candidates)


def _check_hooks(report: AgentReport, mapping: dict[str, YVal]) -> None:
    node = mapping.get("hooks")
    if node is None:
        return
    raw = node.value
    if not isinstance(raw, dict):
        _add(report, node.line, "error", "hooks-type", "hooks must be a mapping of event name to a list of matchers")
        return
    root = _project_root(report.path)
    for event, event_node in raw.items():
        if event not in HOOK_EVENTS:
            suggestion = _closest(event, HOOK_EVENTS)
            extra = f"; did you mean '{suggestion}'?" if suggestion else ""
            _add(
                report,
                event_node.line,
                "warning",
                "hook-event",
                f"unknown hook event '{event}'{extra}",
            )
        matchers = event_node.value if isinstance(event_node, YVal) else event_node
        if not isinstance(matchers, list):
            _add(report, event_node.line, "error", "hooks-type", f"hooks.{event} must be a list of matcher entries")
            continue
        for matcher in matchers:
            _check_hook_entry(report, matcher, root)


def _check_hook_entry(report: AgentReport, matcher: YVal, root: Path) -> None:
    value = matcher.value if isinstance(matcher, YVal) else matcher
    if not isinstance(value, dict):
        _add(report, matcher.line, "error", "hooks-type", "each hook matcher must be a mapping with a 'hooks' list")
        return
    hooks_node = value.get("hooks")
    if hooks_node is None:
        _add(report, matcher.line, "error", "hooks-type", "hook matcher is missing a 'hooks' list")
        return
    hooks = hooks_node.value if isinstance(hooks_node, YVal) else hooks_node
    if not isinstance(hooks, list):
        _add(report, hooks_node.line, "error", "hooks-type", "'hooks' must be a list")
        return
    for hook in hooks:
        _check_single_hook(report, hook, root)


def _check_single_hook(report: AgentReport, hook: YVal, root: Path) -> None:
    value = hook.value if isinstance(hook, YVal) else hook
    if not isinstance(value, dict):
        _add(report, hook.line, "error", "hooks-type", "each hook must be a mapping with type and command")
        return
    type_node = value.get("type")
    hook_type = unwrap(type_node) if type_node is not None else None
    if hook_type is not None and hook_type not in HOOK_TYPES:
        _add(
            report,
            type_node.line if type_node else hook.line,
            "error",
            "hook-type",
            f"hook type '{hook_type}' is not one of: {', '.join(sorted(HOOK_TYPES))}",
        )
    command_node = value.get("command")
    if hook_type in (None, "command") and command_node is None:
        _add(report, hook.line, "error", "hook-command-missing", "command hook is missing a 'command' field")
        return
    if command_node is None:
        return
    command = unwrap(command_node)
    if not isinstance(command, str) or command.strip() == "":
        _add(report, command_node.line, "error", "hook-command-missing", "hook command must be a non-empty string")
        return
    if _looks_like_path(command):
        resolved = _resolve_hook_command(command, root, report.path)
        if resolved is None:
            _add(
                report,
                command_node.line,
                "error",
                "hook-command-missing",
                f"hook command '{command}' does not exist (resolved from the project root and the agent file's folder)",
            )
            return
        if os.name != "nt" and not os.access(resolved, os.X_OK):
            _add(
                report,
                command_node.line,
                "warning",
                "hook-not-executable",
                f"hook command '{command}' exists but is not executable; Claude Code will fail the hook instead of running it",
            )


def _looks_like_path(command: str) -> bool:
    first = command.strip().split()[0] if command.strip() else ""
    if first.startswith(("./", "../", "/", "\\")) or first.startswith(".\\"):
        return True
    if "/" in first or "\\" in first:
        return True
    return first.endswith((".sh", ".py", ".js", ".ts", ".bash", ".zsh", ".ps1"))


def _resolve_hook_command(command: str, root: Path, agent_path: Path) -> Optional[Path]:
    first = command.strip().split()[0]
    candidates = [
        Path(first),
        root / first,
        agent_path.parent / first,
        root / Path(first).name,
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _check_mcp_servers(report: AgentReport, mapping: dict[str, YVal]) -> None:
    node = mapping.get("mcpServers")
    if node is None:
        return
    raw = node.value
    if not isinstance(raw, list):
        _add(report, node.line, "error", "mcp-server", "mcpServers must be a list of server names or inline definitions")
        return
    for item in raw:
        value = item.value if isinstance(item, YVal) else item
        line = item.line if isinstance(item, YVal) else node.line
        if isinstance(value, str):
            if not value.strip():
                _add(report, line, "error", "mcp-server", "mcpServers entry is an empty server name")
            continue
        if isinstance(value, dict) and len(value) == 1:
            _check_inline_mcp(report, value, line)
            continue
        if isinstance(value, dict):
            # Some authors write a single inline server as a map of fields
            # without a wrapping name; that will not load.
            _add(
                report,
                line,
                "error",
                "mcp-server",
                "inline mcpServers entry must be a single-key mapping of server name to a config object",
            )
            continue
        _add(report, line, "error", "mcp-server", "mcpServers entry must be a server name or a single-key mapping")


def _check_inline_mcp(report: AgentReport, value: dict[str, YVal], line: int) -> None:
    name, config_node = next(iter(value.items()))
    config = config_node.value if isinstance(config_node, YVal) else config_node
    if not isinstance(config, dict):
        _add(report, config_node.line, "error", "mcp-server", f"inline MCP server '{name}' config must be a mapping")
        return
    server_type = unwrap(config["type"]) if "type" in config else "stdio"
    if server_type not in {"stdio", "http", "sse", "ws"}:
        _add(
            report,
            config["type"].line if "type" in config else config_node.line,
            "error",
            "mcp-server",
            f"MCP server '{name}' has unknown type '{server_type}'; use stdio, http, sse or ws",
        )
        return
    if server_type == "stdio":
        command = unwrap(config["command"]) if "command" in config else None
        if not isinstance(command, str) or not command.strip():
            _add(
                report,
                config_node.line,
                "error",
                "mcp-server",
                f"stdio MCP server '{name}' is missing a command",
            )
    else:
        url = unwrap(config["url"]) if "url" in config else None
        if not isinstance(url, str) or not url.strip():
            _add(
                report,
                config_node.line,
                "error",
                "mcp-server",
                f"{server_type} MCP server '{name}' is missing a url",
            )


def _check_experimental(report: AgentReport, mapping: dict[str, YVal]) -> None:
    node = mapping.get("experimental")
    if node is None:
        return
    raw = node.value
    if not isinstance(raw, dict):
        _add(report, node.line, "error", "experimental", "experimental must be a mapping (for example cacheTtl: 1h)")
        return
    for key, child in raw.items():
        if key != "cacheTtl":
            _add(
                report,
                child.line,
                "warning",
                "experimental",
                f"unknown experimental key '{key}'; only cacheTtl is currently read from subagent files",
            )
            continue
        value = unwrap(child)
        if value not in CACHE_TTL_VALUES:
            _add(
                report,
                child.line,
                "error",
                "cache-ttl",
                "experimental.cacheTtl must be '5m' or '1h'; any other value is ignored",
            )


def _check_body(report: AgentReport, fm: Frontmatter) -> None:
    body = fm.body.strip()
    if not body:
        _add(
            report,
            fm.body_start,
            "warning",
            "empty-body",
            "system prompt body is empty; the subagent will start with almost no instructions of its own",
        )
        return
    tokens = estimate_tokens(fm.body)
    if tokens > LONG_BODY_TOKENS:
        _add(
            report,
            fm.body_start,
            "warning",
            "body-long",
            f"system prompt is about {tokens} tokens; large prompts shrink the subagent's remaining context window",
        )


def _check_filename(report: AgentReport, name: Optional[str]) -> None:
    if not name:
        return
    stem = report.path.stem
    if stem != name:
        _add(
            report,
            1,
            "info",
            "name-file-mismatch",
            f"file is named '{report.path.name}' but the name field is '{name}'; Claude Code uses the name field, not the filename",
        )


def _check_duplicate_names(result: LintResult) -> None:
    groups: dict[tuple[Path, str], list[AgentReport]] = {}
    by_name: dict[str, list[AgentReport]] = {}
    for report in result.agents:
        if not report.name:
            continue
        root = _agents_root(report.path)
        groups.setdefault((root, report.name), []).append(report)
        by_name.setdefault(report.name, []).append(report)
    for (root, name), reports in groups.items():
        if len(reports) < 2:
            continue
        for report in reports:
            report.findings.append(
                Finding(
                    report.path,
                    1,
                    "error",
                    "duplicate-name",
                    f"name '{name}' is declared in {len(reports)} files under {root}; "
                    "Claude Code loads only one of them, chosen by filesystem read order",
                    agent=name,
                )
            )
    # Same name in different .claude/agents trees: the closer path wins.
    for name, reports in by_name.items():
        trees: dict[Path, list[AgentReport]] = {}
        for report in reports:
            trees.setdefault(_agents_root(report.path), []).append(report)
        if len(trees) < 2:
            continue
        for report in reports:
            if any(f.rule == "duplicate-name" for f in report.findings):
                continue
            others = sorted({str(_agents_root(other.path)) for other in reports if other is not report})
            report.findings.append(
                Finding(
                    report.path,
                    1,
                    "warning",
                    "name-shadow",
                    f"name '{name}' also exists under {', '.join(others)}; Claude Code uses the definition closest to the working directory",
                    agent=name,
                )
            )


def _agents_root(path: Path) -> Path:
    for candidate in [path.parent, *path.parent.parents]:
        if candidate.name == "agents" and candidate.parent.name == ".claude":
            return candidate
        if candidate.name == "agents":
            return candidate
    return path.parent


def _check_description_budget(result: LintResult) -> None:
    total = 0
    for report in result.agents:
        if report.description:
            total += estimate_tokens(report.description)
    if total <= DESCRIPTION_BUDGET_TOKENS:
        return
    path = result.agents[0].path if result.agents else Path(".")
    result.extra.append(
        Finding(
            path,
            1,
            "warning",
            "description-budget",
            f"combined subagent descriptions are about {total} tokens; Claude Code warns at startup when the total exceeds {DESCRIPTION_BUDGET_TOKENS:,} (built-ins excluded). Move detail into each system prompt",
        )
    )


def _closest(value: str, options: set[str]) -> Optional[str]:
    best = None
    best_dist = 4
    for option in options:
        dist = _levenshtein(value, option)
        if option.lower() == value.lower():
            return option
        if dist < best_dist:
            best_dist = dist
            best = option
    return best


RULES = [
    ("frontmatter-missing", "warning", "The first line is not '---', so Claude Code treats the file as documentation"),
    ("frontmatter", "error", "Frontmatter opened with '---' was never closed"),
    ("frontmatter-parse", "error", "YAML frontmatter does not parse; Claude Code skips the file"),
    ("name-missing", "error", "No name field; the file will not load as a subagent"),
    ("name-format", "error", "Name starts with '-', contains ':', or is not a string"),
    ("name-length", "error", "Name is longer than 256 characters"),
    ("name-builtin", "warning", "Name overrides a built-in Claude Code subagent"),
    ("description-missing", "error", "A named file has no description, so Claude Code skips it silently"),
    ("description-short", "warning", "Description is too short to guide delegation"),
    ("description-budget", "warning", "Combined descriptions exceed the 15,000-token startup budget"),
    ("unknown-field", "warning", "Frontmatter field Claude Code does not recognize and will ignore"),
    ("permission-mode", "error", "permissionMode is not a valid mode"),
    ("memory-value", "error", "memory is not user, project or local"),
    ("isolation-value", "error", "isolation is not worktree"),
    ("color-value", "error", "color is not a supported colour name"),
    ("effort-value", "error", "effort is not a supported level"),
    ("model-value", "error", "model is not a string"),
    ("model-unknown", "warning", "model is not a known alias or claude-… id"),
    ("max-turns", "error", "maxTurns is not a positive integer"),
    ("auto-compact-window", "error", "autoCompactWindow is outside 100000–1000000, or written as 200k"),
    ("bool-field", "error", "background or omitClaudeMd is not a boolean"),
    ("tools-type", "error", "tools or disallowedTools has the wrong type"),
    ("tools-empty", "error", "tools list is empty"),
    ("tool-unknown", "warning", "Tool name is not a known built-in (with typo suggestion)"),
    ("skills-type", "error", "skills has the wrong type"),
    ("skill-missing", "warning", "Referenced skill was not found in the project"),
    ("hooks-type", "error", "hooks block is not a mapping of event to matcher lists"),
    ("hook-event", "warning", "Unknown hook event name"),
    ("hook-type", "error", "Hook type is not command, prompt or agent"),
    ("hook-command-missing", "error", "Hook command is missing or the script path does not exist"),
    ("hook-not-executable", "warning", "Hook script exists but is not executable"),
    ("mcp-server", "error", "mcpServers entry is malformed or missing command/url"),
    ("experimental", "warning", "Unknown key under experimental"),
    ("cache-ttl", "error", "experimental.cacheTtl is not 5m or 1h"),
    ("empty-body", "warning", "System prompt body is empty"),
    ("body-long", "warning", "System prompt is very large"),
    ("duplicate-name", "error", "Two files in the same agents tree share a name; only one will load"),
    ("name-shadow", "warning", "The same name exists in another agents tree and may be shadowed"),
    ("name-file-mismatch", "info", "Filename does not match the name field"),
    ("encoding", "error", "File is not valid UTF-8"),
    ("io", "error", "File could not be read"),
]
