"""Link and citation inspection shared by commands and validation."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from okf_cli.diagnostics import Diagnostic
from okf_cli.markdown import footnotes, resolve_destination, scan_links
from okf_cli.model import RESERVED_NAMES, Document, concept_id, iter_markdown, parse_document


@dataclass(frozen=True)
class LinkRecord:
    source: str
    line: int
    destination: str
    resolved_path: str | None
    external: bool
    broken: bool
    outside_bundle: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def links_for_file(root: Path, path: Path) -> list[LinkRecord]:
    return links_for_document(root, parse_document(path))


def links_for_document(root: Path, document: Document) -> list[LinkRecord]:
    path = document.path
    text = document.text_body
    body_line_offset = document.prefix.count(b"\n")
    records: list[LinkRecord] = []
    source_name = (
        concept_id(root, path)
        if path.name not in RESERVED_NAMES
        else path.relative_to(root).as_posix()
    )
    for link in scan_links(text):
        resolved = resolve_destination(root, path, link.destination)
        relative = (
            resolved.resolved.relative_to(root).as_posix()
            if resolved.resolved is not None
            else None
        )
        records.append(
            LinkRecord(
                source_name,
                link.line + body_line_offset,
                link.destination,
                relative,
                resolved.external,
                not resolved.external
                and (resolved.resolved is None or not resolved.resolved.exists()),
                resolved.outside,
            )
        )
    return records


def all_links(root: Path, path: Path | None = None) -> list[LinkRecord]:
    return [
        record for source in iter_markdown(root, path) for record in links_for_file(root, source)
    ]


def _link_diagnostic(record: LinkRecord) -> Diagnostic | None:
    if record.outside_bundle:
        return Diagnostic(
            "warning",
            "link-outside-bundle",
            record.source,
            f"internal link escapes the bundle: {record.destination}",
            line=record.line,
        )
    if record.broken:
        return Diagnostic(
            "warning",
            "broken-link",
            record.source,
            f"internal link target does not exist: {record.destination}",
            line=record.line,
        )
    return None


def link_diagnostics(root: Path, path: Path | None = None) -> list[Diagnostic]:
    return [
        diagnostic
        for record in all_links(root, path)
        if (diagnostic := _link_diagnostic(record)) is not None
    ]


def document_link_diagnostics(root: Path, document: Document) -> list[Diagnostic]:
    return [
        diagnostic
        for record in links_for_document(root, document)
        if (diagnostic := _link_diagnostic(record)) is not None
    ]


def citation_diagnostics(root: Path, path: Path | None = None) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for source in iter_markdown(root, path):
        if source.name in RESERVED_NAMES:
            continue
        document = parse_document(source, require_frontmatter=True)
        diagnostics.extend(document_citation_diagnostics(root, document))
    return diagnostics


def document_citation_diagnostics(root: Path, document: Document) -> list[Diagnostic]:
    if document.frontmatter is None:
        return []
    diagnostics: list[Diagnostic] = []
    file_name = concept_id(root, document.path)
    source_entries = document.frontmatter.get("sources", [])
    ids: list[str] = []
    if isinstance(source_entries, list):
        for entry in source_entries:
            if isinstance(entry, Mapping) and isinstance(entry.get("id"), str):
                ids.append(entry["id"])
    uses, definitions = footnotes(document.text_body)
    body_line_offset = document.prefix.count(b"\n")
    uses = [(label, line + body_line_offset) for label, line in uses]
    definitions = [(label, line + body_line_offset) for label, line in definitions]
    id_counts = Counter(ids)
    definition_counts = Counter(label for label, _ in definitions)
    used_labels = {label for label, _ in uses}
    defined_labels = {label for label, _ in definitions}
    known_ids = set(ids)
    for source_id, count in id_counts.items():
        if count > 1:
            diagnostics.append(
                Diagnostic(
                    "warning",
                    "duplicate-source-id",
                    file_name,
                    f"duplicate source ID: {source_id}",
                )
            )
    for label, count in definition_counts.items():
        if count > 1:
            line = next(line for item, line in definitions if item == label)
            diagnostics.append(
                Diagnostic(
                    "warning",
                    "duplicate-footnote-definition",
                    file_name,
                    f"duplicate footnote definition: {label}",
                    line=line,
                )
            )
    for label, line in sorted((*uses, *definitions), key=lambda item: item[1]):
        if label not in known_ids:
            diagnostics.append(
                Diagnostic(
                    "warning",
                    "unmatched-footnote",
                    file_name,
                    f"footnote has no matching source ID: {label}",
                    line=line,
                )
            )
    for source_id in sorted(known_ids - (used_labels | defined_labels)):
        diagnostics.append(
            Diagnostic(
                "warning",
                "unused-source",
                file_name,
                f"source ID is not referenced by a body footnote: {source_id}",
            )
        )
    return diagnostics
