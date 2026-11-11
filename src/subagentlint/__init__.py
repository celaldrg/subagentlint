"""Lint Claude Code subagent files before they fail silently."""

from .core import Finding, LintResult, lint_path, lint_paths

__version__ = "0.1.0"
__all__ = ["Finding", "LintResult", "lint_path", "lint_paths", "__version__"]
