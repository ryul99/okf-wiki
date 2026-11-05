"""OKF v0.2 conformance errors and advisory validation warnings."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from okf_cli.inspection import citation_diagnostics, link_diagnostics, links_for_file
from okf_cli.metadata import STATUSES, is_stale, parse_date, parse_datetime, status
from okf_cli.model import RESERVED_NAMES, concept_id, iter_markdown, parse_document


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    file: str
    message: str
    field: str | None = None
    line: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


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


def _path_like(value: str) -> bool:
    if urlsplit(value).scheme or value.startswith("//"):
        return False
    return (
        value.startswith(("/", "./", "../"))
        or "/" in value
        or bool(re.search(r"\.[A-Za-z0-9]{1,8}(?:[?#].*)?$", value))
    ) and not any(character.isspace() for character in value)


def path_fields(metadata: Mapping[str, Any]) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    if isinstance(metadata.get("resource"), str):
        fields.append(("resource", metadata["resource"]))
    sources = metadata.get("sources")
    if isinstance(sources, list):
        for index, source in enumerate(sources):
            if isinstance(source, Mapping) and isinstance(source.get("resource"), str):
                fields.append((f"sources[{index}].resource", source["resource"]))
    for name in ("computation",):
        if isinstance(metadata.get(name), str):
            fields.append((name, metadata[name]))
    for family in ("executor", "attester"):
        value = metadata.get(family)
        if isinstance(value, Mapping) and isinstance(value.get("resource"), str):
            fields.append((f"{family}.resource", value["resource"]))
    return fields


def validate_metadata(file: str, metadata: Mapping[str, Any], today: date) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    type_value = metadata.get("type")
    if not isinstance(type_value, str) or not type_value.strip():
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

    generated = metadata.get("generated")
    if generated is not None:
        if not isinstance(generated, Mapping):
            diagnostics.append(
                _warning(file, "invalid-generated", "generated should be a mapping", "generated")
            )
        else:
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

    verified = metadata.get("verified")
    if verified is not None:
        events = verified if isinstance(verified, list) else [verified]
        if not events or not all(isinstance(event, Mapping) for event in events):
            diagnostics.append(
                _warning(
                    file, "invalid-verified", "verified should contain event mappings", "verified"
                )
            )
        else:
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

    sources = metadata.get("sources")
    if sources is not None:
        if not isinstance(sources, list):
            diagnostics.append(
                _warning(file, "invalid-sources", "sources should be a list", "sources")
            )
        else:
            ids: set[str] = set()
            for index, source in enumerate(sources):
                field = f"sources[{index}]"
                if not isinstance(source, Mapping):
                    diagnostics.append(
                        _warning(file, "invalid-source", "source should be a mapping", field)
                    )
                    continue
                if (
                    not isinstance(source.get("resource"), str)
                    or not source.get("resource", "").strip()
                ):
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
                    not isinstance(usage_count, int)
                    or isinstance(usage_count, bool)
                    or usage_count < 0
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
    for window_field, window in [
        ("usage_window", metadata.get("usage_window")),
        *(
            (f"sources[{index}].usage_window", source.get("usage_window"))
            for index, source in enumerate(sources or [])
            if isinstance(source, Mapping)
        ),
    ]:
        if window is not None and (
            not isinstance(window, Mapping)
            or parse_date(window.get("from")) is None
            or parse_date(window.get("to")) is None
        ):
            diagnostics.append(
                _warning(
                    file,
                    "invalid-usage-window",
                    "usage window requires from and to dates",
                    window_field,
                )
            )
        elif isinstance(window, Mapping):
            start = parse_date(window.get("from"))
            end = parse_date(window.get("to"))
            if start is not None and end is not None and start > end:
                diagnostics.append(
                    _warning(
                        file,
                        "invalid-usage-window-order",
                        "usage window from date should not be after to date",
                        window_field,
                    )
                )

    if type_value == "Attested Computation":
        diagnostics.extend(_validate_attested(file, metadata))
    return diagnostics


def _validate_attested(file: str, metadata: Mapping[str, Any]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    if not isinstance(metadata.get("runtime"), str) or not metadata.get("runtime", "").strip():
        diagnostics.append(
            _warning(file, "missing-runtime", "Attested Computation requires runtime", "runtime")
        )
    parameters = metadata.get("parameters", [])
    if not isinstance(parameters, list):
        diagnostics.append(
            _warning(file, "invalid-parameters", "parameters should be a list", "parameters")
        )
    else:
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
    executor = metadata.get("executor")
    if not isinstance(executor, Mapping):
        diagnostics.append(
            _warning(file, "missing-executor", "executor should be a mapping", "executor")
        )
    else:
        if (
            not isinstance(executor.get("resource"), str)
            or not executor.get("resource", "").strip()
        ):
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


def validate_bundle(root: Path, path: Path | None, today: date) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    targets = list(iter_markdown(root, path))
    for target in targets:
        relative = target.relative_to(root).as_posix()
        document = parse_document(target, require_frontmatter=target.name not in RESERVED_NAMES)
        if target.name in RESERVED_NAMES:
            if document.error:
                diagnostics.append(_error(relative, "invalid-reserved-file", document.error))
            elif document.frontmatter is not None:
                root_index = target == root / "index.md"
                if target.name == "log.md" or not root_index:
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
            body = document.text_body
            if target.name == "index.md" and not re.search(r"(?m)^#{1,6}[ \t]+\S", body):
                diagnostics.append(
                    _error(relative, "invalid-index", "index.md should contain a section heading")
                )
            if target.name == "log.md":
                headings = re.findall(r"(?m)^##[ \t]+(.+?)[ \t]*$", body)
                invalid_headings = [
                    heading
                    for heading in headings
                    if parse_date(heading) is None or len(heading) != 10
                ]
                if not re.search(r"(?m)^#[ \t]+\S", body) or not headings:
                    diagnostics.append(
                        _error(
                            relative,
                            "invalid-log",
                            "log.md should contain a title and date-grouped entries",
                        )
                    )
                for heading in invalid_headings:
                    diagnostics.append(
                        _error(
                            relative,
                            "invalid-log-date",
                            f"log date heading must use YYYY-MM-DD: {heading}",
                        )
                    )
            continue
        file = concept_id(root, target)
        if document.error or document.frontmatter is None:
            diagnostics.append(
                _error(file, "invalid-frontmatter", document.error or "invalid frontmatter")
            )
            continue
        diagnostics.extend(validate_metadata(file, document.frontmatter, today))
        if document.frontmatter.get("type") == "Attested Computation":
            computation_section = re.search(
                r"(?ms)^#[ \t]+Computation[ \t]*\r?$\n(.*?)(?=^#[ \t]+|\Z)",
                document.text_body,
            )
            fenced_blocks = (
                re.findall(
                    r"(?ms)^[ ]{0,3}(`{3,}|~{3,})[^\n]*\n.*?^\1[ \t]*$",
                    computation_section.group(1),
                )
                if computation_section
                else []
            )
            external_computation = document.frontmatter.get("computation")
            if external_computation is None and len(fenced_blocks) != 1:
                diagnostics.append(
                    _warning(
                        file,
                        "invalid-inline-computation",
                        "inline computation requires one fenced block under # Computation",
                        "computation",
                    )
                )
            elif external_computation is not None and fenced_blocks:
                diagnostics.append(
                    _warning(
                        file,
                        "duplicate-computation",
                        "use a computation path or an inline fence, not both",
                        "computation",
                    )
                )
        for field, value in path_fields(document.frontmatter):
            if not _path_like(value):
                continue
            path_value = value.split("#", 1)[0].split("?", 1)[0]
            candidate = (
                root / path_value.lstrip("/")
                if path_value.startswith("/")
                else target.parent / path_value
            )
            resolved = candidate.resolve(strict=False)
            if resolved.is_relative_to(root.resolve()) and not resolved.exists():
                diagnostics.append(
                    _warning(
                        file,
                        "missing-path-target",
                        f"path-valued field target does not exist: {value}",
                        field,
                    )
                )

    for item in link_diagnostics(root, path):
        diagnostics.append(
            Diagnostic(item.severity, item.code, item.file, item.message, line=item.line)
        )
    for item in citation_diagnostics(root, path):
        diagnostics.append(
            Diagnostic(item.severity, item.code, item.file, item.message, line=item.line)
        )

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
    return sorted(
        diagnostics,
        key=lambda item: (
            item.file,
            item.line or 0,
            0 if item.severity == "error" else 1,
            item.code,
        ),
    )
