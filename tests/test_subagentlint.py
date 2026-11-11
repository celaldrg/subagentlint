import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from subagentlint.cli import main
from subagentlint.core import estimate_tokens, lint_path, lint_paths
from subagentlint.yaml_lite import YamlError, parse_yaml, unwrap


ROOT = Path(__file__).resolve().parents[1]
OK = ROOT / "examples" / "ok"
BROKEN = ROOT / "examples" / "broken"


def rules_of(result, name=None):
    reports = result.agents if hasattr(result, "agents") else [result]
    findings = []
    for report in reports:
        findings.extend(report.findings)
    if hasattr(result, "extra"):
        findings.extend(result.extra)
    if name is not None:
        findings = [f for f in findings if f.agent == name or Path(f.path).name.startswith(name)]
    return {(f.rule, f.level) for f in findings}


def findings_for(result, rule):
    items = []
    reports = result.agents if hasattr(result, "agents") else [result]
    for report in reports:
        items.extend(report.findings)
    if hasattr(result, "extra"):
        items.extend(result.extra)
    return [f for f in items if f.rule == rule]


class YamlTests(unittest.TestCase):
    def test_plain_and_quoted_and_list(self):
        node = parse_yaml(
            "name: code-reviewer\n"
            "description: \"Expert: use after changes.\"\n"
            "tools: [Read, Grep, Bash]\n"
            "maxTurns: 12\n"
            "omitClaudeMd: true\n"
        )
        data = unwrap(node)
        self.assertEqual(data["name"], "code-reviewer")
        self.assertEqual(data["description"], "Expert: use after changes.")
        self.assertEqual(unwrap(data["tools"] if False else node.value["tools"]), ["Read", "Grep", "Bash"])
        self.assertEqual(data["maxTurns"], 12)
        self.assertIs(data["omitClaudeMd"], True)

    def test_block_map_list_and_comment(self):
        node = parse_yaml(
            "skills:\n"
            "  - api-conventions  # project skill\n"
            "  - error-handling\n"
            "hooks:\n"
            "  PreToolUse:\n"
            "    - matcher: Bash\n"
            "      hooks:\n"
            "        - type: command\n"
            "          command: ./scripts/check.sh\n"
        )
        data = unwrap(node)
        self.assertEqual(data["skills"], ["api-conventions", "error-handling"])
        self.assertEqual(data["hooks"]["PreToolUse"][0]["matcher"], "Bash")
        self.assertEqual(
            data["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
            "./scripts/check.sh",
        )

    def test_rejects_unquoted_colon_space(self):
        with self.assertRaises(YamlError) as ctx:
            parse_yaml("description: Use this: now\n")
        self.assertIn("': '", ctx.exception.message)

    def test_rejects_tabs(self):
        with self.assertRaises(YamlError):
            parse_yaml("name: ok\n\tdescription: tabbed\n")

    def test_duplicate_key(self):
        with self.assertRaises(YamlError) as ctx:
            parse_yaml("name: a\nname: b\n")
        self.assertIn("duplicate key", ctx.exception.message)

    def test_flow_map_and_literal_block(self):
        node = parse_yaml(
            "server: {type: stdio, command: npx}\n"
            "prompt: |\n"
            "  line one\n"
            "  line two\n"
        )
        data = unwrap(node)
        self.assertEqual(data["server"]["type"], "stdio")
        self.assertEqual(data["prompt"], "line one\nline two\n")


class ExampleTests(unittest.TestCase):
    def test_ok_example_is_clean(self):
        result = lint_paths([OK])
        self.assertEqual(len(result.agents), 1)
        self.assertEqual(result.errors, 0)
        self.assertEqual(result.warnings, 0)
        self.assertEqual(result.agents[0].name, "code-reviewer")

    def test_broken_example_hits_core_rules(self):
        result = lint_paths([BROKEN])
        rules = {f.rule for f in result.findings}
        for needed in (
            "unknown-field",
            "tool-unknown",
            "description-short",
            "hook-command-missing",
            "empty-body",
            "duplicate-name",
            "name-format",
            "description-missing",
            "auto-compact-window",
            "frontmatter-missing",
        ):
            self.assertIn(needed, rules, f"missing rule {needed} in {rules}")
        self.assertGreaterEqual(result.errors, 4)

    def test_typo_field_suggests_camel_case(self):
        hits = findings_for(lint_paths([BROKEN]), "unknown-field")
        self.assertTrue(any("permissionMode" in f.message for f in hits))

    def test_tool_typo_suggests_grep(self):
        hits = findings_for(lint_paths([BROKEN]), "tool-unknown")
        self.assertTrue(any("Grep" in f.message and "Grap" in f.message for f in hits))


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.agents = self.root / ".claude" / "agents"
        self.agents.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name: str, text: str, rel: str | None = None) -> Path:
        path = self.agents / (rel or name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_description_is_error(self):
        path = self.write(
            "x.md",
            "---\nname: silent\ntools: Read\n---\n\nBody.\n",
        )
        report = lint_path(path)
        self.assertIn(("description-missing", "error"), rules_of(report))

    def test_name_starting_with_hyphen_is_error(self):
        path = self.write(
            "x.md",
            "---\nname: -helper\ndescription: A helper that never loads because of its name prefix issue.\ntools: Read\n---\n\nBody.\n",
        )
        report = lint_path(path)
        self.assertIn(("name-format", "error"), rules_of(report))

    def test_name_with_colon_is_error(self):
        path = self.write(
            "x.md",
            "---\nname: plugin:reviewer\ndescription: A helper that never loads because the name contains a colon reserved for plugins.\ntools: Read\n---\n\nBody.\n",
        )
        report = lint_path(path)
        self.assertIn(("name-format", "error"), rules_of(report))

    def test_invalid_permission_mode(self):
        path = self.write(
            "x.md",
            "---\nname: perms\ndescription: Tests an invalid permission mode value that must be rejected as an error.\npermissionMode: yolo\n---\n\nBody.\n",
        )
        report = lint_path(path)
        self.assertIn(("permission-mode", "error"), rules_of(report))

    def test_auto_compact_window_suffix(self):
        path = self.write(
            "x.md",
            "---\nname: compact\ndescription: Tests a compact window written with a k suffix, which Claude Code ignores.\nautoCompactWindow: 200k\n---\n\nBody.\n",
        )
        report = lint_path(path)
        self.assertIn(("auto-compact-window", "error"), rules_of(report))

    def test_auto_compact_window_range(self):
        path = self.write(
            "x.md",
            "---\nname: compact\ndescription: Tests a compact window that is far below the documented minimum of 100000.\nautoCompactWindow: 50\n---\n\nBody.\n",
        )
        report = lint_path(path)
        self.assertIn(("auto-compact-window", "error"), rules_of(report))

    def test_builtin_name_warning(self):
        path = self.write(
            "x.md",
            "---\nname: Explore\ndescription: Overrides the built-in Explore subagent with a custom project definition.\nmodel: haiku\n---\n\nBody.\n",
        )
        report = lint_path(path)
        self.assertIn(("name-builtin", "warning"), rules_of(report))

    def test_unknown_model_warning(self):
        path = self.write(
            "x.md",
            "---\nname: modeller\ndescription: Uses a model alias that is not one of the documented family aliases.\nmodel: gpt-next\n---\n\nBody.\n",
        )
        report = lint_path(path)
        self.assertIn(("model-unknown", "warning"), rules_of(report))

    def test_known_model_ids_are_ok(self):
        path = self.write(
            "x.md",
            "---\nname: modeller\ndescription: Uses a full Claude model id, which the linter should accept without warning.\nmodel: claude-opus-5-5\n---\n\nBody.\n",
        )
        report = lint_path(path)
        self.assertNotIn("model-unknown", {f.rule for f in report.findings})

    def test_skill_missing_and_present(self):
        skill = self.root / ".claude" / "skills" / "api-conventions" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: api-conventions\ndescription: conventions\n---\n\nBody.\n")
        path = self.write(
            "x.md",
            "---\nname: api-dev\ndescription: Implements API endpoints following team conventions and shared error patterns.\nskills:\n  - api-conventions\n  - missing-skill\n---\n\nBody.\n",
        )
        report = lint_path(path)
        hits = [f for f in report.findings if f.rule == "skill-missing"]
        self.assertEqual(len(hits), 1)
        self.assertIn("missing-skill", hits[0].message)

    def test_hook_command_missing(self):
        path = self.write(
            "x.md",
            "---\nname: db-reader\ndescription: Execute read-only database queries when asked to inspect production-like data.\ntools: Bash\nhooks:\n  PreToolUse:\n    - matcher: Bash\n      hooks:\n        - type: command\n          command: ./scripts/missing.sh\n---\n\nBody.\n",
        )
        report = lint_path(path)
        self.assertIn(("hook-command-missing", "error"), rules_of(report))

    @unittest.skipIf(os.name == "nt", "execute bit is not meaningful on Windows")
    def test_hook_not_executable(self):
        script = self.root / "scripts" / "check.sh"
        script.parent.mkdir()
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(script.stat().st_mode & ~stat.S_IXUSR & ~stat.S_IXGRP & ~stat.S_IXOTH)
        path = self.write(
            "x.md",
            "---\nname: db-reader\ndescription: Execute read-only database queries when asked to inspect production-like data.\ntools: Bash\nhooks:\n  PreToolUse:\n    - matcher: Bash\n      hooks:\n        - type: command\n          command: ./scripts/check.sh\n---\n\nBody.\n",
        )
        report = lint_path(path)
        self.assertIn(("hook-not-executable", "warning"), rules_of(report))

    def test_mcp_stdio_missing_command(self):
        path = self.write(
            "x.md",
            "---\nname: browser\ndescription: Tests features in a real browser using an inline Playwright MCP server.\nmcpServers:\n  - playwright:\n      type: stdio\n      args: [\"-y\", \"@playwright/mcp@latest\"]\n---\n\nBody.\n",
        )
        report = lint_path(path)
        self.assertIn(("mcp-server", "error"), rules_of(report))

    def test_mcp_http_missing_url(self):
        path = self.write(
            "x.md",
            "---\nname: remote\ndescription: Talks to a remote MCP server over HTTP but forgets to set the url field.\nmcpServers:\n  - docs:\n      type: http\n---\n\nBody.\n",
        )
        report = lint_path(path)
        self.assertIn(("mcp-server", "error"), rules_of(report))

    def test_named_mcp_reference_ok(self):
        path = self.write(
            "x.md",
            "---\nname: github-helper\ndescription: Reuses an already configured GitHub MCP server by name only.\nmcpServers:\n  - github\n---\n\nBody.\n",
        )
        report = lint_path(path)
        self.assertFalse(any(f.rule == "mcp-server" for f in report.findings))

    def test_cache_ttl_and_unknown_experimental(self):
        path = self.write(
            "x.md",
            "---\nname: auditor\ndescription: Audits a large repository and reports what it finds, with a bad cache ttl.\nexperimental:\n  cacheTtl: 10m\n  other: 1\n---\n\nBody.\n",
        )
        report = lint_path(path)
        self.assertIn(("cache-ttl", "error"), rules_of(report))
        self.assertIn(("experimental", "warning"), rules_of(report))

    def test_agent_spawn_syntax_is_known_tool(self):
        path = self.write(
            "x.md",
            "---\nname: coordinator\ndescription: Coordinates work across specialized agents without flagging Agent() syntax.\ntools: Agent(worker, researcher), Read, Bash\n---\n\nBody.\n",
        )
        report = lint_path(path)
        self.assertFalse(any(f.rule == "tool-unknown" for f in report.findings))

    def test_duplicate_names_in_same_tree(self):
        self.write(
            "a.md",
            "---\nname: twin\ndescription: First copy of a subagent that shares its name with another file in this tree.\n---\n\nBody.\n",
        )
        self.write(
            "b.md",
            "---\nname: twin\ndescription: Second copy of a subagent that shares its name with another file in this tree.\n---\n\nBody.\n",
            rel="nested/b.md",
        )
        result = lint_paths([self.root])
        self.assertGreaterEqual(len(findings_for(result, "duplicate-name")), 2)

    def test_name_file_mismatch_is_info(self):
        path = self.write(
            "reviewer.md",
            "---\nname: code-reviewer\ndescription: Expert code review specialist used after writing or modifying code.\n---\n\nBody.\n",
        )
        report = lint_path(path)
        self.assertIn(("name-file-mismatch", "info"), rules_of(report))

    def test_bool_field(self):
        path = self.write(
            "x.md",
            "---\nname: flagger\ndescription: Checks that omitClaudeMd must be a boolean, not a string placeholder.\nomitClaudeMd: yes-please\n---\n\nBody.\n",
        )
        report = lint_path(path)
        self.assertIn(("bool-field", "error"), rules_of(report))

    def test_ignore_rule(self):
        path = self.write(
            "x.md",
            "---\nname: typo\ndescription: Has an unknown field that should be ignored when the rule is skipped.\npermission_mode: default\n---\n\nBody.\n",
        )
        result = lint_paths([path], ignore=["unknown-field", "name-file-mismatch"])
        self.assertFalse(any(f.rule == "unknown-field" for f in result.findings))

    def test_description_budget(self):
        long_desc = "A" * (15_000 * 4 + 20)
        self.write(
            "x.md",
            f"---\nname: bulky\ndescription: \"{long_desc}\"\n---\n\nBody.\n",
        )
        result = lint_paths([self.root])
        self.assertTrue(any(f.rule == "description-budget" for f in result.extra))

    def test_discovery_skips_node_modules(self):
        hidden = self.root / "node_modules" / ".claude" / "agents"
        hidden.mkdir(parents=True)
        (hidden / "secret.md").write_text(
            "---\nname: secret\ndescription: Should not be discovered inside node_modules even though it looks valid enough.\n---\n\nBody.\n"
        )
        result = lint_paths([self.root])
        names = [a.name for a in result.agents]
        self.assertNotIn("secret", names)

    def test_yaml_line_numbers_point_at_the_field(self):
        path = self.write(
            "x.md",
            "---\nname: lined\ndescription: Enough characters to avoid a short-description warning here.\npermissionMode: nope\n---\n\nBody.\n",
        )
        report = lint_path(path)
        hits = [f for f in report.findings if f.rule == "permission-mode"]
        self.assertEqual(hits[0].line, 4)


class CliTests(unittest.TestCase):
    def test_ok_exit_zero(self):
        self.assertEqual(main([str(OK)]), 0)

    def test_broken_exit_one(self):
        self.assertEqual(main([str(BROKEN)]), 1)

    def test_missing_path_exit_two(self):
        self.assertEqual(main(["/tmp/subagentlint-does-not-exist-xyz"]), 2)

    def test_no_agents_exit_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(main([tmp]), 2)

    def test_json_format(self):
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main([str(OK), "--format", "json"])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["summary"]["agents"], 1)
        self.assertEqual(payload["agents"][0]["name"], "code-reviewer")

    def test_github_format_and_strict(self):
        import io
        from contextlib import redirect_stdout

        # A file with only warnings should fail under --strict
        with tempfile.TemporaryDirectory() as tmp:
            agents = Path(tmp) / ".claude" / "agents"
            agents.mkdir(parents=True)
            (agents / "short.md").write_text(
                "---\nname: short\ndescription: Too short.\n---\n\nBody.\n",
                encoding="utf-8",
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main([tmp, "--strict", "--format", "github"])
            self.assertEqual(code, 1)
            self.assertIn("::warning file=", buf.getvalue())

    def test_list_rules(self):
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--list-rules"])
        self.assertEqual(code, 0)
        self.assertIn("duplicate-name", buf.getvalue())

    def test_version_and_estimate(self):
        self.assertGreaterEqual(estimate_tokens("abcd"), 1)
        self.assertEqual(estimate_tokens(""), 0)


if __name__ == "__main__":
    unittest.main()
