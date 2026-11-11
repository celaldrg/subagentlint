"""Split a Markdown file into YAML frontmatter and body."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .yaml_lite import YamlError, YVal, parse_yaml


class FrontmatterError(Exception):
    def __init__(self, line: int, message: str, rule: str = "frontmatter") -> None:
        self.line = line
        self.message = message
        self.rule = rule
        super().__init__(message)


@dataclass
class Frontmatter:
    mapping: dict[str, YVal]
    body: str
    body_start: int
    raw: str


def split_frontmatter(text: str) -> tuple[Optional[str], int, str, int]:
    """Return (yaml_text, yaml_start_line, body, body_start_line).

    yaml_text is None when the file has no opening --- on line 1.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, 1, text, 1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            yaml_text = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1 :])
            if body.startswith("\n"):
                # keep a leading blank line out of "empty body" checks later
                pass
            return yaml_text, 2, body, i + 2
    raise FrontmatterError(1, "frontmatter opened with '---' but was never closed", "frontmatter")


def parse_frontmatter(text: str) -> Frontmatter:
    yaml_text, yaml_start, body, body_start = split_frontmatter(text)
    if yaml_text is None:
        raise FrontmatterError(
            1,
            "no YAML frontmatter: the first line must be '---' for this file to load as a subagent",
            "frontmatter-missing",
        )
    try:
        node = parse_yaml(yaml_text)
    except YamlError as exc:
        raise FrontmatterError(
            yaml_start + exc.line - 1,
            exc.message,
            "frontmatter-parse",
        ) from exc
    value = node.value
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise FrontmatterError(
            yaml_start,
            "frontmatter must be a YAML mapping of fields",
            "frontmatter-parse",
        )
    # Shift line numbers from the YAML snippet (line 1 = first yaml line)
    # so they refer to the original file. parse_yaml uses 1-based lines
    # inside the snippet; the snippet starts at yaml_start.
    shifted = _shift_lines(value, yaml_start - 1)
    return Frontmatter(mapping=shifted, body=body, body_start=body_start, raw=yaml_text)


def _shift_lines(value: dict[str, YVal], delta: int) -> dict[str, YVal]:
    def shift(node: YVal) -> YVal:
        inner = node.value
        if isinstance(inner, dict):
            inner = {k: shift(v) for k, v in inner.items()}
        elif isinstance(inner, list):
            inner = [shift(v) if isinstance(v, YVal) else v for v in inner]
        return YVal(value=inner, line=node.line + delta)

    return {k: shift(v) for k, v in value.items()}
