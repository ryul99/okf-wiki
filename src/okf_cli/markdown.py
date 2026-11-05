"""Markdown structure located by marko, with source spans into the original body text."""

from __future__ import annotations

import os
import re
from bisect import bisect_left
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from marko import Markdown, block, inline
from marko.element import Element
from marko.ext.footnote import FootnoteDef, FootnoteRef
from marko.helpers import MarkoExtension, normalize_label


class _AdjacentFootnoteDef(FootnoteDef):
    # GFM ends a footnote at the next definition line; marko would read it as lazy continuation.
    override = True
    breaks_paragraph = True


_MARKDOWN = Markdown(extensions=["footnote", MarkoExtension(elements=[_AdjacentFootnoteDef])])


@dataclass(frozen=True)
class Link:
    destination: str
    start: int
    end: int
    line: int


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    line: int


@dataclass(frozen=True)
class ResolvedLink:
    resolved: Path | None
    external: bool
    outside: bool


def _walk(element: Element) -> Iterator[Element]:
    yield element
    children = getattr(element, "children", None)
    if isinstance(children, list):
        for child in children:
            yield from _walk(child)


class ParsedMarkdown:
    """One marko parse of a body, exposing the parts OKF inspects as original-text offsets."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.document = _MARKDOWN.parse(text)
        # marko parses CRLF as LF, so every span after a CRLF is short by one per CRLF.
        self._crlf_offsets = [
            match.start() - count for count, match in enumerate(re.finditer("\r\n", text))
        ]

    def _offset(self, position: int) -> int:
        return position + bisect_left(self._crlf_offsets, position)

    def _span(self, span: tuple[int, int]) -> tuple[int, int]:
        return self._offset(span[0]), self._offset(span[1])

    def _start(self, element: Element) -> int:
        return self._offset(element.source_span[0]) if element.source_span else 0

    def _line(self, offset: int) -> int:
        return self.text.count("\n", 0, offset) + 1

    def _link(self, start: int, end: int) -> Link | None:
        if self.text.startswith("<", start) and self.text[end - 1 : end] == ">":
            start, end = start + 1, end - 1
        if end <= start:
            return None
        return Link(self.text[start:end], start, end, self._line(start))

    def _reference_label(self, link: inline.Link) -> str | None:
        if not link.syntax_spans:
            return None
        opening = self._span(link.syntax_spans[0])
        closing = self._span(link.syntax_spans[-1])
        suffix = self.text[closing[0] : closing[1]]
        if suffix.startswith("][") and len(suffix) > 3:
            return normalize_label(suffix[2:-1])
        return normalize_label(self.text[opening[1] : closing[0]])

    def links(self) -> list[Link]:
        """Inline link destinations plus the definitions that reference links use."""

        links: list[Link] = []
        used_labels: set[str] = set()
        definitions: dict[str, block.LinkRefDef] = {}
        for element in _walk(self.document):
            if isinstance(element, inline.Link):
                if element.dest_span is not None:
                    link = self._link(*self._span(element.dest_span))
                    if link is not None:
                        links.append(link)
                elif (label := self._reference_label(element)) is not None:
                    used_labels.add(label)
            elif isinstance(element, block.LinkRefDef):
                definitions.setdefault(element.label, element)
        for label, definition in definitions.items():
            if label not in used_labels or definition.source_span is None:
                continue
            start, end = self._span(definition.source_span)
            source = self.text[start:end]
            position = source.find(definition.dest, source.find("]:") + 2)
            if position >= 0:
                link = self._link(start + position, start + position + len(definition.dest))
                if link is not None:
                    links.append(link)
        return sorted(links, key=lambda link: link.start)

    def footnotes(self) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
        """Footnote uses and definitions as (label, line), labels kept as written."""

        uses: list[tuple[int, str]] = []
        definitions: list[tuple[int, str]] = []
        for element in _walk(self.document):
            if element.source_span is None:
                continue
            start, end = self._span(element.source_span)
            if isinstance(element, FootnoteRef):
                uses.append((start, self.text[start + 2 : end - 1]))
            elif isinstance(element, FootnoteDef):
                source = self.text[start:end]
                label_start = len(source) - len(source.lstrip(" ")) + 2
                definitions.append((start, source[label_start : source.index("]:")]))
            elif isinstance(element, inline.RawText):
                # Without a definition marko leaves [^label] as text, but it still cites a source.
                uses.extend(
                    (match.start(), match.group(1))
                    for match in FootnoteRef.pattern.finditer(self.text, start, end)
                )
        return (
            [(label, self._line(offset)) for offset, label in sorted(uses)],
            [(label, self._line(offset)) for offset, label in sorted(definitions)],
        )

    def headings(self) -> list[Heading]:
        """Top-level headings with their text as written, closing markers excluded."""

        return [
            Heading(element.level, self._heading_text(element), self._line(self._start(element)))
            for element in self.document.children
            if isinstance(element, block.Heading | block.SetextHeading)
        ]

    def fenced_blocks_in_section(self, title: str) -> int | None:
        """Count fenced code blocks under the level-1 heading `title`, or None if it is absent."""

        count: int | None = None
        for element in self.document.children:
            if isinstance(element, block.Heading | block.SetextHeading) and element.level == 1:
                if count is not None:
                    break
                if self._heading_text(element) == title:
                    count = 0
            elif count is not None:
                count += sum(isinstance(item, block.FencedCode) for item in _walk(element))
        return count

    def _heading_text(self, heading: block.Heading | block.SetextHeading) -> str:
        spans = [child.source_span for child in heading.children if child.source_span]
        return self.text[self._offset(spans[0][0]) : self._offset(spans[-1][1])] if spans else ""


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
    return _encode_like(path_part, rewritten) + suffix


def _encode_like(original: str, path: str) -> str:
    # Destinations are resolved decoded, so a rewrite must restore the author's encoding style.
    if unquote(original) != original:
        return quote(path, safe="/")
    # Raw style stays raw, but whitespace would end the Markdown destination.
    return "".join(quote(character) if character.isspace() else character for character in path)


def apply_replacements(text: str, replacements: list[tuple[int, int, str]]) -> str:
    result = text
    for start, end, replacement in sorted(replacements, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result
