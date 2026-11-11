"""Command-line interface for subagentlint."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .core import RULES, LintResult, lint_paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="subagentlint",
        description="Lint Claude Code subagent files (.claude/agents) before they fail silently.",
    )
    parser.add_argument("paths", nargs="*", default=["."], help="Files or folders to scan (default: current directory)")
    parser.add_argument("--format", choices=("text", "json", "github"), default="text", help="Output format")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as failures")
    parser.add_argument("--ignore", action="append", default=[], metavar="RULE", help="Skip a rule (repeatable)")
    parser.add_argument("--user", action="store_true", help="Also scan ~/.claude/agents")
    parser.add_argument("--list-rules", action="store_true", help="Print the rule table and exit")
    parser.add_argument("--version", action="version", version=f"subagentlint {__version__}")
    args = parser.parse_args(argv)

    if args.list_rules:
        _print_rules()
        return 0

    paths = [Path(p) for p in args.paths]
    missing = [p for p in paths if not p.exists()]
    if missing:
        for path in missing:
            print(f"subagentlint: path not found: {path}", file=sys.stderr)
        return 2

    result = lint_paths(paths, include_user=args.user, ignore=args.ignore)
    if not result.agents:
        print("subagentlint: no subagent files found", file=sys.stderr)
        return 2

    if args.format == "json":
        json.dump(result.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    elif args.format == "github":
        _print_github(result)
    else:
        _print_text(result)

    if result.errors:
        return 1
    if args.strict and result.warnings:
        return 1
    return 0


def _print_rules() -> None:
    width = max(len(rule) for rule, _level, _msg in RULES)
    print(f"{'Rule'.ljust(width)}  Level    What it catches")
    print(f"{'-' * width}  -------- {'-' * 40}")
    for rule, level, message in RULES:
        print(f"{rule.ljust(width)}  {level.ljust(8)} {message}")


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _print_text(result: LintResult) -> None:
    for report in result.agents:
        loc = _rel(report.path)
        label = report.name or loc
        token_bit = f" (~{report.tokens} tokens)"
        if report.errors:
            status = "FAIL"
        elif report.warnings:
            status = "warn"
        else:
            status = "ok  "
        print(f"{status}  {label}{token_bit}")
        for finding in report.findings:
            print(
                f"      {_rel(finding.path)}:{finding.line}  {finding.level}  {finding.rule}  {finding.message}"
            )
    for finding in result.extra:
        print(f"      {_rel(finding.path)}:{finding.line}  {finding.level}  {finding.rule}  {finding.message}")
    print()
    print(
        f"{len(result.agents)} agent(s), {result.errors} error(s), {result.warnings} warning(s)"
    )


def _print_github(result: LintResult) -> None:
    for finding in result.findings:
        level = "notice" if finding.level == "info" else finding.level
        path = _rel(finding.path).replace("\\", "/")
        print(f"::{level} file={path},line={finding.line}::{finding.rule}: {finding.message}")
    print(f"{len(result.agents)} agent(s), {result.errors} error(s), {result.warnings} warning(s)")
