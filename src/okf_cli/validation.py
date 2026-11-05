"""OKF v0.2 conformance errors and advisory validation warnings."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from ruamel.yaml.comments import CommentedMap

from okf_cli.diagnostics import Diagnostic
from okf_cli.inspection import citation_diagnostics, link_diagnostics, links_for_file
from okf_cli.markdown import ParsedMarkdown, resolve_destination
from okf_cli.metadata import (
    STATUSES,
    is_stale,
    parse_date,
    parse_datetime,
    path_fields,
    status,
)
from okf_cli.model import RESERVED_NAMES, Document, concept_id, iter_markdown, parse_document


def _warning(file: str, code: str, message: str, field: str | None = None) -> Diagnostic:
    return Diagnostic("warning", code, file, message, field)


def _error(file: str, code: str, message: str, field: str | None = None) -> Diagnostic:
    return Diagnostic("error", code, file, message, field)


def _valid_actor(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and (value.startswith(("human:", "process:")) or "/" in value)
    )


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_metadata(file: str, metadata: Mapping[str, Any], today: date) -> list[Diagnostic]:
    diagnostics = _validate_core(file, metadata, today)
    diagnostics.extend(_validate_generated(file, metadata.get("generated")))
    diagnostics.extend(_validate_verified(file, metadata.get("verified")))
    diagnostics.extend(_validate_sources(file, metadata.get("sources")))
    diagnostics.extend(_validate_usage_windows(file, metadata))
    if metadata.get("type") == "Attested Computation":
        diagnostics.extend(_validate_attested(file, metadata))
    return diagnostics


def _validate_core(file: str, metadata: Mapping[str, Any], today: date) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    if not _non_empty_string(metadata.get("type")):
        diagnostics.append(_error(file, "invalid-type", "type must be a non-empty string", "type"))
    for recommended in ("title", "description"):
        if recommended not in metadata:
            diagnostics.append(
                _warning(
                    file,
                    f"missing-{recommended}",
                    f"recommended field is absent: {recommended}",
                    recommended,
                )
            )
        elif not isinstance(metadata[recommended], str):
            diagnostics.append(
                _warning(
                    file, f"invalid-{recommended}", f"{recommended} should be a string", recommended
                )
            )
    if "tags" in metadata and (
        not isinstance(metadata["tags"], list)
        or not all(isinstance(tag, str) and tag for tag in metadata["tags"])
    ):
        diagnostics.append(
            _warning(file, "invalid-tags", "tags should be a list of non-empty strings", "tags")
        )
    if "status" in metadata and metadata["status"] not in STATUSES:
        diagnostics.append(
            _warning(
                file, "invalid-status", "status should be draft, stable, or deprecated", "status"
            )
        )
    if "stale_after" in metadata and parse_date(metadata["stale_after"]) is None:
        diagnostics.append(
            _warning(file, "invalid-stale-after", "stale_after should be YYYY-MM-DD", "stale_after")
        )
    elif is_stale(metadata, today):
        diagnostics.append(_warning(file, "stale", "concept is stale", "stale_after"))
    return diagnostics


def _validate_generated(file: str, generated: Any) -> list[Diagnostic]:
    if generated is None:
        return []
    if not isinstance(generated, Mapping):
        return [_warning(file, "invalid-generated", "generated should be a mapping", "generated")]
    diagnostics: list[Diagnostic] = []
    if not _valid_actor(generated.get("by")):
        diagnostics.append(
            _warning(
                file,
                "invalid-generated-by",
                "generated.by should identify an actor",
                "generated.by",
            )
        )
    if "at" in generated and parse_datetime(generated.get("at")) is None:
        diagnostics.append(
            _warning(
                file,
                "invalid-generated-at",
                "generated.at should be ISO 8601",
                "generated.at",
            )
        )
    return diagnostics


def _validate_verified(file: str, verified: Any) -> list[Diagnostic]:
    if verified is None:
        return []
    events = verified if isinstance(verified, list) else [verified]
    if not events or not all(isinstance(event, Mapping) for event in events):
        return [
            _warning(file, "invalid-verified", "verified should contain event mappings", "verified")
        ]
    diagnostics: list[Diagnostic] = []
    for index, event in enumerate(events):
        if not _valid_actor(event.get("by")):
            diagnostics.append(
                _warning(
                    file,
                    "invalid-verifier",
                    "verification event should identify an actor",
                    f"verified[{index}].by",
                )
            )
        if parse_datetime(event.get("at")) is None:
            diagnostics.append(
                _warning(
                    file,
                    "invalid-verification-date",
                    "verification event at should be ISO 8601",
                    f"verified[{index}].at",
                )
            )
    return diagnostics


def _validate_sources(file: str, sources: Any) -> list[Diagnostic]:
    if sources is None:
        return []
    if not isinstance(sources, list):
        return [_warning(file, "invalid-sources", "sources should be a list", "sources")]
    diagnostics: list[Diagnostic] = []
    ids: set[str] = set()
    for index, source in enumerate(sources):
        field = f"sources[{index}]"
        if not isinstance(source, Mapping):
            diagnostics.append(
                _warning(file, "invalid-source", "source should be a mapping", field)
            )
            continue
        if not _non_empty_string(source.get("resource")):
            diagnostics.append(
                _warning(
                    file,
                    "missing-source-resource",
                    "source resource is required",
                    f"{field}.resource",
                )
            )
        source_id = source.get("id")
        if source_id is not None:
            if not isinstance(source_id, str) or not source_id:
                diagnostics.append(
                    _warning(
                        file,
                        "invalid-source-id",
                        "source ID should be a non-empty string",
                        f"{field}.id",
                    )
                )
            elif source_id in ids:
                diagnostics.append(
                    _warning(
                        file,
                        "duplicate-source-id",
                        f"duplicate source ID: {source_id}",
                        f"{field}.id",
                    )
                )
            else:
                ids.add(source_id)
        usage_count = source.get("usage_count")
        if usage_count is not None and (
            not isinstance(usage_count, int) or isinstance(usage_count, bool) or usage_count < 0
        ):
            diagnostics.append(
                _warning(
                    file,
                    "invalid-usage-count",
                    "usage_count should be a non-negative integer",
                    f"{field}.usage_count",
                )
            )
        if "last_modified" in source and parse_date(source["last_modified"]) is None:
            diagnostics.append(
                _warning(
                    file,
                    "invalid-last-modified",
                    "last_modified should be YYYY-MM-DD",
                    f"{field}.last_modified",
                )
            )
        if "author" in source and not _valid_actor(source["author"]):
            diagnostics.append(
                _warning(
                    file,
                    "invalid-source-author",
                    "source author should identify an actor",
                    f"{field}.author",
                )
            )
    return diagnostics


def _validate_usage_windows(file: str, metadata: Mapping[str, Any]) -> list[Diagnostic]:
    sources = metadata.get("sources")
    windows = [("usage_window", metadata.get("usage_window"))]
    if isinstance(sources, list):
        windows.extend(
            (f"sources[{index}].usage_window", source.get("usage_window"))
            for index, source in enumerate(sources)
            if isinstance(source, Mapping)
        )
    diagnostics: list[Diagnostic] = []
    for field, window in windows:
        if window is None:
            continue
        start = parse_date(window.get("from")) if isinstance(window, Mapping) else None
        end = parse_date(window.get("to")) if isinstance(window, Mapping) else None
        if start is None or end is None:
            diagnostics.append(
                _warning(
                    file,
                    "invalid-usage-window",
                    "usage window requires from and to dates",
                    field,
                )
            )
        elif start > end:
            diagnostics.append(
                _warning(
                    file,
                    "invalid-usage-window-order",
                    "usage window from date should not be after to date",
                    field,
                )
            )
    return diagnostics


def _validate_attested(file: str, metadata: Mapping[str, Any]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    if not _non_empty_string(metadata.get("runtime")):
        diagnostics.append(
            _warning(file, "missing-runtime", "Attested Computation requires runtime", "runtime")
        )
    diagnostics.extend(_validate_parameters(file, metadata.get("parameters", [])))
    executor = metadata.get("executor")
    if not isinstance(executor, Mapping):
        diagnostics.append(
            _warning(file, "missing-executor", "executor should be a mapping", "executor")
        )
    else:
        if not _non_empty_string(executor.get("resource")):
            diagnostics.append(
                _warning(
                    file,
                    "invalid-executor-resource",
                    "executor.resource is required",
                    "executor.resource",
                )
            )
        receipt = executor.get("receipt")
        if (
            not isinstance(receipt, list)
            or not receipt
            or not all(isinstance(field, str) and field for field in receipt)
        ):
            diagnostics.append(
                _warning(
                    file,
                    "invalid-receipt",
                    "executor.receipt should list returned fields",
                    "executor.receipt",
                )
            )
    attester = metadata.get("attester")
    if not isinstance(attester, Mapping) or not isinstance(attester.get("resource"), str):
        diagnostics.append(
            _warning(file, "missing-attester", "attester.resource is required", "attester.resource")
        )
    return diagnostics


def _validate_parameters(file: str, parameters: Any) -> list[Diagnostic]:
    if not isinstance(parameters, list):
        return [_warning(file, "invalid-parameters", "parameters should be a list", "parameters")]
    diagnostics: list[Diagnostic] = []
    names: set[str] = set()
    for index, parameter in enumerate(parameters):
        field = f"parameters[{index}]"
        if not isinstance(parameter, Mapping) or not all(
            key in parameter for key in ("name", "type", "required")
        ):
            diagnostics.append(
                _warning(
                    file,
                    "invalid-parameter",
                    "parameter requires name, type, and required",
                    field,
                )
            )
            continue
        if not isinstance(parameter["name"], str) or not parameter["name"]:
            diagnostics.append(
                _warning(
                    file, "invalid-parameter-name", "parameter name is invalid", f"{field}.name"
                )
            )
        elif parameter["name"] in names:
            diagnostics.append(
                _warning(
                    file,
                    "duplicate-parameter",
                    f"duplicate parameter: {parameter['name']}",
                    f"{field}.name",
                )
            )
        else:
            names.add(parameter["name"])
        if not isinstance(parameter["type"], str) or not parameter["type"]:
            diagnostics.append(
                _warning(
                    file, "invalid-parameter-type", "parameter type is invalid", f"{field}.type"
                )
            )
        if not isinstance(parameter["required"], bool):
            diagnostics.append(
                _warning(
                    file,
                    "invalid-parameter-required",
                    "parameter required should be boolean",
                    f"{field}.required",
                )
            )
    return diagnostics


def _validate_reserved(root: Path, target: Path, document: Document) -> list[Diagnostic]:
    relative = target.relative_to(root).as_posix()
    diagnostics: list[Diagnostic] = []
    if document.error:
        diagnostics.append(_error(relative, "invalid-reserved-file", document.error))
    elif document.frontmatter is not None:
        if target.name == "log.md" or target != root / "index.md":
            diagnostics.append(
                _error(
                    relative,
                    "reserved-frontmatter",
                    "frontmatter is not allowed in this reserved file",
                )
            )
        elif set(document.frontmatter) - {"okf_version"}:
            diagnostics.append(
                _error(
                    relative,
                    "invalid-index-frontmatter",
                    "root index frontmatter may contain only okf_version",
                )
            )
        elif not isinstance(document.frontmatter.get("okf_version"), str):
            diagnostics.append(
                _warning(
                    relative,
                    "invalid-okf-version",
                    "okf_version should be a version string",
                    "okf_version",
                )
            )
    headings = ParsedMarkdown(document.text_body).headings()
    if target.name == "index.md" and not any(heading.text for heading in headings):
        diagnostics.append(
            _error(relative, "invalid-index", "index.md should contain a section heading")
        )
    if target.name == "log.md":
        dates = [heading.text for heading in headings if heading.level == 2]
        if not any(heading.level == 1 and heading.text for heading in headings) or not dates:
            diagnostics.append(
                _error(
                    relative,
                    "invalid-log",
                    "log.md should contain a title and date-grouped entries",
                )
            )
        diagnostics.extend(
            _error(
                relative,
                "invalid-log-date",
                f"log date heading must use YYYY-MM-DD: {heading}",
            )
            for heading in dates
            if parse_date(heading) is None or len(heading) != 10
        )
    return diagnostics


def _validate_inline_computation(
    file: str, metadata: Mapping[str, Any], body: str
) -> list[Diagnostic]:
    fenced_blocks = ParsedMarkdown(body).fenced_blocks_in_section("Computation") or 0
    external_computation = metadata.get("computation")
    if external_computation is None and fenced_blocks != 1:
        return [
            _warning(
                file,
                "invalid-inline-computation",
                "inline computation requires one fenced block under # Computation",
                "computation",
            )
        ]
    if external_computation is not None and fenced_blocks:
        return [
            _warning(
                file,
                "duplicate-computation",
                "use a computation path or an inline fence, not both",
                "computation",
            )
        ]
    return []


def _validate_path_targets(
    root: Path, target: Path, file: str, metadata: CommentedMap
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for ref in path_fields(metadata, root, target):
        resolved = resolve_destination(root, target, ref.value).resolved
        if resolved is not None and not resolved.exists():
            diagnostics.append(
                _warning(
                    file,
                    "missing-path-target",
                    f"path-valued field target does not exist: {ref.value}",
                    ref.name,
                )
            )
    return diagnostics


def _validate_concept(root: Path, target: Path, today: date) -> list[Diagnostic]:
    file = concept_id(root, target)
    document = parse_document(target, require_frontmatter=True)
    if document.error or document.frontmatter is None:
        return [_error(file, "invalid-frontmatter", document.error or "invalid frontmatter")]
    metadata = document.frontmatter
    diagnostics = validate_metadata(file, metadata, today)
    if metadata.get("type") == "Attested Computation":
        diagnostics.extend(_validate_inline_computation(file, metadata, document.text_body))
    diagnostics.extend(_validate_path_targets(root, target, file, metadata))
    return diagnostics


def _deprecated_link_warnings(root: Path, targets: list[Path]) -> list[Diagnostic]:
    # A link target can sit outside the validated path, so its status is looked up on demand.
    status_by_path: dict[str, str | None] = {}

    def target_status(relative: str) -> str | None:
        if relative not in status_by_path:
            linked = root / relative
            document = (
                parse_document(linked)
                if linked.suffix == ".md" and linked.name not in RESERVED_NAMES and linked.is_file()
                else None
            )
            status_by_path[relative] = (
                status(document.frontmatter)
                if document is not None and document.frontmatter is not None
                else None
            )
        return status_by_path[relative]

    diagnostics: list[Diagnostic] = []
    for target in targets:
        if target.name in RESERVED_NAMES:
            continue
        document = parse_document(target)
        if document.frontmatter is None or status(document.frontmatter) != "stable":
            continue
        for link in links_for_file(root, target):
            if link.resolved_path and target_status(link.resolved_path) == "deprecated":
                diagnostics.append(
                    _warning(
                        concept_id(root, target),
                        "stable-links-deprecated",
                        f"stable concept links to deprecated concept: {link.destination}",
                    )
                )
    return diagnostics


def validate_bundle(root: Path, path: Path | None, today: date) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    targets = list(iter_markdown(root, path))
    for target in targets:
        if target.name in RESERVED_NAMES:
            diagnostics.extend(_validate_reserved(root, target, parse_document(target)))
        else:
            diagnostics.extend(_validate_concept(root, target, today))
    diagnostics.extend(link_diagnostics(root, path))
    diagnostics.extend(citation_diagnostics(root, path))
    diagnostics.extend(_deprecated_link_warnings(root, targets))
    return sorted(
        diagnostics,
        key=lambda item: (
            item.file,
            item.line or 0,
            0 if item.severity == "error" else 1,
            item.code,
        ),
    )
