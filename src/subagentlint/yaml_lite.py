"""A small YAML 1.2 subset parser with line numbers. No third-party deps.

Supports block maps and sequences, flow lists/maps, quoted and plain
scalars, literal (|) and folded (>) blocks, and comments. Tabs in
indentation are rejected. This is enough for Claude Code subagent
frontmatter; it is not a complete YAML implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


class YamlError(Exception):
    def __init__(self, line: int, message: str) -> None:
        self.line = line
        self.message = message
        super().__init__(f"line {line}: {message}")


@dataclass
class YVal:
    value: Any
    line: int

    def unwrap(self) -> Any:
        return unwrap(self)


def unwrap(node: Any) -> Any:
    if isinstance(node, YVal):
        return unwrap(node.value)
    if isinstance(node, dict):
        return {k: unwrap(v) for k, v in node.items()}
    if isinstance(node, list):
        return [unwrap(v) for v in node]
    return node


def key_line(mapping: dict[str, YVal], key: str) -> int:
    val = mapping.get(key)
    return val.line if isinstance(val, YVal) else 1


@dataclass
class _Line:
    no: int
    indent: int
    raw: str
    content: str


def parse_yaml(text: str) -> YVal:
    lines = _preprocess(text)
    if not lines:
        return YVal(value=None, line=1)
    value, index = _parse_node(lines, 0, min_indent=0)
    if index < len(lines):
        leftover = lines[index]
        raise YamlError(leftover.no, "unexpected content after the end of the document")
    return value


def _preprocess(text: str) -> list[_Line]:
    result: list[_Line] = []
    for no, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip(" \t"))]:
            raise YamlError(no, "tabs are not allowed in YAML indentation")
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        content = raw[indent:]
        content = _strip_unquoted_comment(content)
        if content.strip() == "":
            continue
        result.append(_Line(no=no, indent=indent, raw=raw, content=content.rstrip()))
    return result


def _strip_unquoted_comment(content: str) -> str:
    in_single = False
    in_double = False
    escape = False
    for i, ch in enumerate(content):
        if in_double:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_double = False
            continue
        if in_single:
            if ch == "'":
                in_single = False
            continue
        if ch == '"':
            in_double = True
            continue
        if ch == "'":
            in_single = True
            continue
        if ch == "#" and (i == 0 or content[i - 1].isspace()):
            return content[:i].rstrip()
    return content


def _parse_node(lines: list[_Line], index: int, min_indent: int) -> tuple[YVal, int]:
    if index >= len(lines):
        raise YamlError(lines[-1].no if lines else 1, "unexpected end of YAML")
    line = lines[index]
    if line.indent < min_indent:
        raise YamlError(line.no, "unexpected dedent")
    content = line.content
    if content.startswith("- ") or content == "-":
        return _parse_seq(lines, index, line.indent)
    if _looks_like_key(content):
        return _parse_map(lines, index, line.indent)
    return _parse_scalar_line(line), index + 1


def _looks_like_key(content: str) -> bool:
    if content.startswith("- ") or content == "-":
        return False
    if content.startswith("{") or content.startswith("["):
        return False
    return _split_key(content) is not None


def _split_key(content: str) -> Optional[tuple[str, str]]:
    in_single = False
    in_double = False
    escape = False
    for i, ch in enumerate(content):
        if in_double:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_double = False
            continue
        if in_single:
            if ch == "'":
                in_single = False
            continue
        if ch == '"':
            in_double = True
            continue
        if ch == "'":
            in_single = True
            continue
        if ch == ":" and (i + 1 == len(content) or content[i + 1] in " \t#"):
            key = content[:i].strip()
            value = content[i + 1 :].strip()
            if not key:
                return None
            return key, value
    return None


def _parse_map(lines: list[_Line], index: int, indent: int) -> tuple[YVal, int]:
    data: dict[str, YVal] = {}
    start = lines[index].no
    while index < len(lines):
        line = lines[index]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise YamlError(line.no, "unexpected indentation")
        if line.content.startswith("- ") or line.content == "-":
            break
        split = _split_key(line.content)
        if split is None:
            raise YamlError(line.no, "expected a mapping entry")
        raw_key, rest = split
        key = _decode_key(raw_key, line.no)
        if key in data:
            raise YamlError(line.no, f"duplicate key '{key}'")
        if rest in ("|", ">", "|-", "|+", ">-", ">+"):
            value, index = _parse_block_scalar(lines, index + 1, indent, rest, line.no)
        elif rest == "":
            value, index = _parse_map_value(lines, index + 1, indent, line.no)
        else:
            value = _parse_flow_or_plain(rest, line.no)
            index += 1
        # Attach the key's line on the value so findings point at the field.
        value.line = line.no
        data[key] = value
    return YVal(value=data, line=start), index


def _parse_map_value(lines: list[_Line], index: int, parent_indent: int, key_line_no: int) -> tuple[YVal, int]:
    if index >= len(lines) or lines[index].indent <= parent_indent:
        return YVal(value=None, line=key_line_no), index
    child = lines[index]
    if child.content.startswith("- ") or child.content == "-":
        return _parse_seq(lines, index, child.indent)
    if _looks_like_key(child.content):
        return _parse_map(lines, index, child.indent)
    return _parse_scalar_line(child), index + 1


def _parse_seq(lines: list[_Line], index: int, indent: int) -> tuple[YVal, int]:
    items: list[YVal] = []
    start = lines[index].no
    while index < len(lines):
        line = lines[index]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise YamlError(line.no, "unexpected indentation")
        if not (line.content.startswith("- ") or line.content == "-"):
            break
        rest = line.content[1:].strip()
        if rest in ("|", ">", "|-", "|+", ">-", ">+"):
            value, index = _parse_block_scalar(lines, index + 1, indent, rest, line.no)
        elif rest == "":
            if index + 1 < len(lines) and lines[index + 1].indent > indent:
                value, index = _parse_node(lines, index + 1, min_indent=indent + 1)
            else:
                value = YVal(value=None, line=line.no)
                index += 1
        elif _split_key(rest) is not None:
            # Inline map on the dash line, plus possible continuation.
            fake = _Line(no=line.no, indent=line.indent + 2, raw=line.raw, content=rest)
            patched = [fake] + [
                _Line(ln.no, ln.indent, ln.raw, ln.content)
                for ln in lines[index + 1 :]
            ]
            value, consumed = _parse_map(patched, 0, fake.indent)
            # consumed includes the fake first line; remaining real lines are consumed-1
            index = index + consumed
        else:
            value = _parse_flow_or_plain(rest, line.no)
            index += 1
        items.append(value)
    return YVal(value=items, line=start), index


def _parse_block_scalar(
    lines: list[_Line], index: int, parent_indent: int, style: str, start_line: int
) -> tuple[YVal, int]:
    chomped: list[str] = []
    block_indent: Optional[int] = None
    while index < len(lines):
        line = lines[index]
        if line.indent <= parent_indent:
            break
        if block_indent is None:
            block_indent = line.indent
        padding = line.indent - block_indent
        chomped.append((" " * max(padding, 0)) + line.content)
        index += 1
    text = "\n".join(chomped)
    if style.startswith("|"):
        value = text + ("\n" if text else "")
    else:
        folded = " ".join(part.strip() for part in text.split("\n") if part.strip())
        value = folded
    return YVal(value=value, line=start_line), index


def _parse_scalar_line(line: _Line) -> YVal:
    return _parse_flow_or_plain(line.content, line.no)


def _parse_flow_or_plain(text: str, line_no: int) -> YVal:
    text = text.strip()
    if text.startswith("["):
        return YVal(value=_parse_flow_list(text, line_no), line=line_no)
    if text.startswith("{"):
        return YVal(value=_parse_flow_map(text, line_no), line=line_no)
    return YVal(value=_parse_scalar(text, line_no), line=line_no)


def _parse_scalar(text: str, line_no: int) -> Any:
    if text == "" or text in ("~", "null", "Null", "NULL"):
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        return _unquote(text, line_no)
    # Unquoted values must not contain an unquoted ": " which YAML rejects.
    if ": " in text:
        raise YamlError(
            line_no,
            "plain value contains ': ', which YAML rejects; wrap the value in quotes",
        )
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if _is_int(text):
        return int(text)
    if _is_float(text):
        return float(text)
    return text


def _is_int(text: str) -> bool:
    if text.startswith(("+", "-")):
        return text[1:].isdigit() and text[1:] != ""
    return text.isdigit()


def _is_float(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return any(ch in text for ch in ".eE")


def _unquote(text: str, line_no: int) -> str:
    quote = text[0]
    body = text[1:-1]
    if quote == "'":
        return body.replace("''", "'")
    out: list[str] = []
    escape = False
    escapes = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}
    for ch in body:
        if escape:
            out.append(escapes.get(ch, ch))
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        out.append(ch)
    if escape:
        raise YamlError(line_no, "unterminated escape in double-quoted string")
    return "".join(out)


def _decode_key(raw: str, line_no: int) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        return _unquote(raw, line_no)
    return raw


def _split_flow(text: str, line_no: int) -> list[str]:
    if len(text) < 2 or text[0] not in "[{" or text[-1] not in "]}":
        raise YamlError(line_no, "invalid flow collection")
    inner = text[1:-1].strip()
    if not inner:
        return []
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    in_single = False
    in_double = False
    escape = False
    for ch in inner:
        if in_double:
            buf.append(ch)
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_double = False
            continue
        if in_single:
            buf.append(ch)
            if ch == "'":
                in_single = False
            continue
        if ch == '"':
            in_double = True
            buf.append(ch)
            continue
        if ch == "'":
            in_single = True
            buf.append(ch)
            continue
        if ch in "[{":
            depth += 1
            buf.append(ch)
            continue
        if ch in "]}":
            depth -= 1
            buf.append(ch)
            continue
        if ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if buf or inner.endswith(","):
        leftover = "".join(buf).strip()
        if leftover:
            parts.append(leftover)
    if in_single or in_double or depth != 0:
        raise YamlError(line_no, "unbalanced quotes or brackets in flow collection")
    return parts


def _parse_flow_list(text: str, line_no: int) -> list[YVal]:
    if not text.endswith("]"):
        raise YamlError(line_no, "unclosed flow list")
    return [_parse_flow_or_plain(part, line_no) for part in _split_flow(text, line_no)]


def _parse_flow_map(text: str, line_no: int) -> dict[str, YVal]:
    if not text.endswith("}"):
        raise YamlError(line_no, "unclosed flow map")
    data: dict[str, YVal] = {}
    for part in _split_flow(text, line_no):
        split = _split_key(part)
        if split is None:
            raise YamlError(line_no, f"expected key: value in flow map, got '{part}'")
        key = _decode_key(split[0], line_no)
        data[key] = _parse_flow_or_plain(split[1], line_no)
    return data
