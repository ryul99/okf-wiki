"""Source-span Markdown links and footnotes without body reformatting."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

FENCE_RE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})")
REFERENCE_RE = re.compile(r"^[ ]{0,3}\[([^\]^][^\]]*)\]:[ \t]*(?:<([^>\n]+)>|([^ \t\n]+))")
FOOTNOTE_USE_RE = re.compile(r"\[\^([^\]]+)\]")
FOOTNOTE_DEF_RE = re.compile(r"^[ ]{0,3}\[\^([^\]]+)\]:", re.MULTILINE)


@dataclass(frozen=True)
class Link:
    destination: str
    start: int
    end: int
    line: int


@dataclass(frozen=True)
class ResolvedLink:
    resolved: Path | None
    external: bool
    outside: bool


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _active_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    offset = 0
    fence: str | None = None
    html_block = False
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip(" ")
        fence_match = FENCE_RE.match(line)
        excluded = False
        if fence is not None:
            excluded = True
            if re.match(
                rf"^[ ]{{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$", line.rstrip("\r\n")
            ):
                fence = None
        elif fence_match:
            fence = fence_match.group(1)
            excluded = True
        elif line.startswith(("    ", "\t")):
            excluded = True
        elif html_block:
            excluded = True
            if ">" in line:
                html_block = False
        elif stripped.startswith(("<script", "<style", "<pre", "<!--")):
            excluded = True
            html_block = not (
                "</script>" in line or "</style>" in line or "</pre>" in line or "-->" in line
            )
        if not excluded:
            ranges.append((offset, offset + len(line)))
        offset += len(line)
    return ranges


def _is_active(offset: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= offset < end for start, end in ranges)


def scan_links(text: str) -> list[Link]:
    active = _active_ranges(text)
    links: list[Link] = []
    definitions: dict[str, Link] = {}
    definition_spans: set[tuple[int, int]] = set()
    offset = 0
    for line in text.splitlines(keepends=True):
        match = REFERENCE_RE.match(line)
        if match and _is_active(offset + match.start(), active):
            group = 2 if match.group(2) is not None else 3
            start = offset + match.start(group)
            end = offset + match.end(group)
            label = " ".join(match.group(1).split()).casefold()
            definitions[label] = Link(text[start:end], start, end, _line_number(text, start))
            definition_spans.add((offset + match.start(), offset + match.end()))
        offset += len(line)

    used_references: set[str] = set()
    reference_use = re.compile(r"(?<!!)\[([^\]^\n]+)\](?:\[([^\]\n]*)\])?")
    for match in reference_use.finditer(text):
        if not _is_active(match.start(), active) or any(
            start <= match.start() < end for start, end in definition_spans
        ):
            continue
        trailing = text[match.end() : match.end() + 1]
        if match.group(2) is None and trailing == "(":
            continue
        label = match.group(2) if match.group(2) else match.group(1)
        normalized = " ".join(label.split()).casefold()
        if normalized in definitions:
            used_references.add(normalized)
    links.extend(definitions[label] for label in definitions if label in used_references)

    index = 0
    length = len(text)
    while index < length:
        if text[index] != "[" or not _is_active(index, active):
            index += 1
            continue
        if index > 0 and text[index - 1] in ("!", "\\"):
            index += 1
            continue
        if index + 1 < length and text[index + 1] == "^":
            index += 1
            continue
        close = index + 1
        bracket_depth = 1
        while close < length and bracket_depth:
            if text[close] == "\\":
                close += 2
                continue
            if text[close] == "[":
                bracket_depth += 1
            elif text[close] == "]":
                bracket_depth -= 1
            close += 1
        if bracket_depth or close >= length:
            index += 1
            continue
        opening = close
        while opening < length and text[opening] in " \t\n":
            opening += 1
        if opening >= length or text[opening] != "(":
            index += 1
            continue
        cursor = opening + 1
        while cursor < length and text[cursor] in " \t\n":
            cursor += 1
        if cursor >= length:
            break
        if text[cursor] == "<":
            start = cursor + 1
            end = text.find(">", start)
            if end < 0:
                index += 1
                continue
        else:
            start = cursor
            depth = 0
            while cursor < length:
                char = text[cursor]
                if char == "\\":
                    cursor += 2
                    continue
                if char == "(":
                    depth += 1
                elif char == ")":
                    if depth == 0:
                        break
                    depth -= 1
                elif char in " \t\n" and depth == 0:
                    break
                cursor += 1
            end = cursor
        if end > start and not any(a <= index < b for a, b in definition_spans):
            links.append(Link(text[start:end], start, end, _line_number(text, start)))
        index = max(close + 1, end)
    return sorted(links, key=lambda link: link.start)


def split_destination(destination: str) -> tuple[str, str]:
    fragment_at = destination.find("#")
    query_at = destination.find("?")
    positions = [position for position in (fragment_at, query_at) if position >= 0]
    suffix_at = min(positions) if positions else len(destination)
    return destination[:suffix_at], destination[suffix_at:]


def resolve_destination(root: Path, source: Path, destination: str) -> ResolvedLink:
    path_part, _ = split_destination(destination)
    parsed = urlsplit(path_part)
    if not path_part or path_part.startswith("#") or parsed.scheme or path_part.startswith("//"):
        return ResolvedLink(None, True, False)
    decoded = unquote(path_part)
    candidate = root / decoded.lstrip("/") if decoded.startswith("/") else source.parent / decoded
    resolved = candidate.resolve(strict=False)
    outside = not resolved.is_relative_to(root.resolve())
    return ResolvedLink(None if outside else resolved, False, outside)


def rewrite_destination(
    root: Path, old_source: Path, new_source: Path, destination: str, target: Path
) -> str:
    path_part, suffix = split_destination(destination)
    if path_part.startswith("/"):
        rewritten = "/" + target.relative_to(root).as_posix()
    else:
        rewritten = os.path.relpath(target, new_source.parent).replace(os.sep, "/")
        if path_part.startswith("./") and not rewritten.startswith("../"):
            rewritten = "./" + rewritten
    return rewritten + suffix


def apply_replacements(text: str, replacements: list[tuple[int, int, str]]) -> str:
    result = text
    for start, end, replacement in sorted(replacements, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def footnotes(text: str) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    active = _active_ranges(text)
    definitions: list[tuple[str, int]] = []
    uses: list[tuple[str, int]] = []
    definition_offsets: set[int] = set()
    for match in FOOTNOTE_DEF_RE.finditer(text):
        if _is_active(match.start(), active):
            definitions.append((match.group(1), _line_number(text, match.start())))
            definition_offsets.add(text.rfind("\n", 0, match.start()) + 1)
    for match in FOOTNOTE_USE_RE.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        if _is_active(match.start(), active) and line_start not in definition_offsets:
            uses.append((match.group(1), _line_number(text, match.start())))
    return uses, definitions
