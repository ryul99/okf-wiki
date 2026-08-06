"""Command-line interface for OKF bundles."""

from __future__ import annotations

import argparse
import difflib
import io
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Never

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from okf_cli import __version__
from okf_cli.errors import OkfError, OperationalError, UsageError
from okf_cli.inspection import all_links, citation_diagnostics, link_diagnostics, links_for_file
from okf_cli.metadata import (
    Filters,
    dotted_get,
    dotted_set,
    dotted_unset,
    is_stale,
    latest_verification,
    normalize_verified,
    parse_date,
    status,
    trust_tier,
)
from okf_cli.model import (
    Document,
    atomic_write,
    concept_id,
    concept_path,
    discover_root,
    iter_concepts,
    parse_document,
    require_mutable_document,
    safe_resolve,
    yaml_plain,
)
from okf_cli.move import build_move_plan, execute_move, recover_transactions
from okf_cli.validation import validate_bundle

Handler = Callable[[argparse.Namespace], int]


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise UsageError(message)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _today(args: argparse.Namespace) -> date:
    value = getattr(args, "today", None)
    if value is None:
        return date.today()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise UsageError("--today must use YYYY-MM-DD") from exc


def _root(args: argparse.Namespace, *, inspection: bool = False) -> Path:
    return discover_root(Path.cwd(), getattr(args, "root", None), inspection=inspection)


def _format(args: argparse.Namespace) -> str:
    return str(getattr(args, "format", "text"))


def _json(value: Any) -> None:
    print(json.dumps(yaml_plain(value), indent=2, ensure_ascii=False, sort_keys=False))


def _yaml_text(value: Any) -> str:
    output = io.StringIO()
    yaml = YAML()
    yaml.width = 4096
    yaml.dump(value, output)
    return output.getvalue()


def _display_change(
    document: Document, proposed: bytes, dry_run: bool, *, payload: Any = None
) -> None:
    if dry_run:
        if payload is not None:
            _json(payload)
            return
        before = document.raw.decode("utf-8").splitlines(keepends=True)
        after = proposed.decode("utf-8").splitlines(keepends=True)
        sys.stdout.writelines(
            difflib.unified_diff(
                before, after, fromfile=str(document.path), tofile=str(document.path)
            )
        )
    else:
        atomic_write(document.path, proposed, expected_identity=document.identity)


def _prepare_mutation(args: argparse.Namespace) -> Path:
    root = _root(args)
    if getattr(args, "dry_run", False):
        pending = sorted(root.glob(".okf-transaction-*"))
        if pending:
            raise OperationalError(
                "an interrupted transaction must be recovered by a non-dry-run mutation first"
            )
        return root
    recovered = recover_transactions(root)
    if recovered:
        print(f"recovered interrupted transaction: {', '.join(recovered)}", file=sys.stderr)
    return root


def _target_path(root: Path, value: str | None) -> Path | None:
    if value is None:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = safe_resolve(root, candidate)
    if not candidate.exists() and candidate.suffix != ".md":
        markdown_candidate = safe_resolve(root, Path(f"{candidate}.md"))
        if markdown_candidate.exists():
            candidate = markdown_candidate
    if not candidate.exists():
        raise OperationalError(f"path does not exist: {value}")
    return candidate


def _filters(args: argparse.Namespace) -> Filters:
    due = getattr(args, "due_before", None)
    try:
        due_date = date.fromisoformat(due) if due else None
    except ValueError as exc:
        raise UsageError("--due-before must use YYYY-MM-DD") from exc
    return Filters(
        getattr(args, "filter_type", None),
        getattr(args, "tag", None),
        getattr(args, "status", None),
        getattr(args, "trust", None),
        getattr(args, "stale", False),
        due_date,
    )


def _record(root: Path, path: Path, metadata: Mapping[str, Any], today: date) -> dict[str, Any]:
    stale = is_stale(metadata, today)
    return {
        "id": concept_id(root, path),
        "type": metadata.get("type"),
        "title": metadata.get("title") or path.stem,
        "status": status(metadata),
        "trust": trust_tier(metadata),
        "freshness": "stale" if stale else "fresh",
        "stale_after": yaml_plain(metadata.get("stale_after")),
        "tags": yaml_plain(metadata.get("tags", [])),
    }


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path or getattr(args, "root", None) or ".").expanduser().resolve()
    index = target / "index.md"
    if index.exists():
        raise UsageError(f"refusing to overwrite existing file: {index}")
    content = f'---\nokf_version: "{args.version}"\n---\n\n# Concepts\n'.encode()
    if args.dry_run:
        if _format(args) == "json":
            _json({"path": str(index), "content": content.decode(), "created": False})
        else:
            sys.stdout.buffer.write(content)
    else:
        target.mkdir(parents=True, exist_ok=True)
        atomic_write(index, content)
        if _format(args) == "json":
            _json({"path": str(index), "version": args.version, "created": True})
        else:
            print(f"initialized OKF bundle at {target}")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    root = _prepare_mutation(args)
    path = concept_path(root, args.concept)
    if path.exists():
        raise UsageError(f"refusing to overwrite existing file: {path}")
    if not args.concept_type.strip():
        raise UsageError("--type must be non-empty")
    metadata = CommentedMap()
    metadata["type"] = args.concept_type
    for name in ("title", "description", "resource"):
        value = getattr(args, name)
        if value is not None:
            metadata[name] = value
    if args.tags:
        metadata["tags"] = CommentedSeq(dict.fromkeys(args.tags))
    if args.status is not None:
        metadata["status"] = args.status
    if args.generated_by is not None:
        metadata["generated"] = CommentedMap({"by": args.generated_by, "at": _utc_now()})
    document = Document(path, b"", metadata, b"\n", b"")
    proposed = document.render()
    if args.dry_run:
        if _format(args) == "json":
            _json(
                {
                    "path": path.relative_to(root).as_posix(),
                    "content": proposed.decode(),
                    "generated_by": args.generated_by,
                    "created": False,
                }
            )
        else:
            sys.stdout.buffer.write(proposed)
    else:
        atomic_write(path, proposed)
        if _format(args) == "json":
            _json(
                {
                    "id": concept_id(root, path),
                    "path": path.relative_to(root).as_posix(),
                    "created": True,
                    "generated_by": args.generated_by,
                }
            )
        else:
            print(concept_id(root, path))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    root = _root(args, inspection=True)
    target = _target_path(root, args.path)
    today = _today(args)
    filters = _filters(args)
    records: list[dict[str, Any]] = []
    for path in iter_concepts(root, target):
        document = parse_document(path, require_frontmatter=True)
        if document.frontmatter is not None and filters.matches(document.frontmatter, today):
            records.append(_record(root, path, document.frontmatter, today))
    if _format(args) == "json":
        _json(records)
    else:
        for item in records:
            print(
                f"{item['id']}\t{item['type'] or '-'}\t{item['title']}\t"
                f"{item['status']}\t{item['trust']}\t{item['freshness']}"
            )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    root = _root(args, inspection=True)
    path = concept_path(root, args.concept, allow_reserved=True)
    document = parse_document(path)
    if args.frontmatter_only:
        if document.frontmatter is None:
            raise OperationalError(f"{path}: no frontmatter")
        if _format(args) == "json":
            _json(document.frontmatter)
        else:
            sys.stdout.write(_yaml_text(document.frontmatter))
    elif args.body_only:
        if _format(args) == "json":
            _json({"id": concept_id(root, path), "body": document.text_body})
        else:
            sys.stdout.buffer.write(document.body)
    else:
        if _format(args) == "json":
            _json(
                {
                    "id": concept_id(root, path),
                    "frontmatter": document.frontmatter,
                    "body": document.text_body,
                    "source": document.raw.decode("utf-8"),
                }
            )
        else:
            sys.stdout.buffer.write(document.raw)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    root = _root(args, inspection=True)
    today = _today(args)
    filters = _filters(args)
    needle = args.query if args.case_sensitive else args.query.casefold()
    results: list[dict[str, Any]] = []
    for path in iter_concepts(root):
        document = parse_document(path, require_frontmatter=True)
        metadata = document.frontmatter
        if metadata is None or not filters.matches(metadata, today):
            continue
        candidates: list[tuple[str, str]] = [("id", concept_id(root, path))]
        for field in ("type", "title", "description"):
            if isinstance(metadata.get(field), str):
                candidates.append((field, metadata[field]))
        tags = metadata.get("tags", [])
        if isinstance(tags, list):
            candidates.extend(("tags", tag) for tag in tags if isinstance(tag, str))
        for field, text in candidates:
            haystack = text if args.case_sensitive else text.casefold()
            position = haystack.find(needle)
            if position >= 0:
                results.append(
                    {
                        "id": concept_id(root, path),
                        "field": field,
                        "excerpt": text[max(0, position - 40) : position + len(args.query) + 40],
                        "line": None,
                    }
                )
        if not args.metadata_only:
            first_body_line = document.prefix.count(b"\n") + 1
            for line_number, line in enumerate(
                document.text_body.splitlines(), start=first_body_line
            ):
                haystack = line if args.case_sensitive else line.casefold()
                position = haystack.find(needle)
                if position >= 0:
                    results.append(
                        {
                            "id": concept_id(root, path),
                            "field": "body",
                            "excerpt": line[
                                max(0, position - 40) : position + len(args.query) + 40
                            ],
                            "line": line_number,
                        }
                    )
    if _format(args) == "json":
        _json(results)
    else:
        for result in results:
            location = f":{result['line']}" if result["line"] is not None else ""
            print(f"{result['id']}{location}\t{result['field']}\t{result['excerpt']}")
    return 0


def _parse_meta_value(raw: str, value_type: str | None) -> Any:
    if value_type is None:
        lowered = raw.lower()
        ambiguous = (
            lowered in {"true", "false", "null", "yes", "no"}
            or raw.startswith(("{", "["))
            or parse_date(raw) is not None
        )
        try:
            float(raw)
            ambiguous = True
        except ValueError:
            pass
        if ambiguous:
            raise UsageError("ambiguous scalar requires explicit --type")
        return raw
    if value_type == "string":
        return raw
    if value_type == "number":
        try:
            number = (
                float(raw)
                if any(character in raw.lower() for character in (".", "e"))
                else int(raw)
            )
        except ValueError as exc:
            raise UsageError("value is not a valid number") from exc
        return number
    if value_type == "boolean":
        if raw.lower() not in {"true", "false"}:
            raise UsageError("boolean value must be true or false")
        return raw.lower() == "true"
    if value_type == "date":
        parsed = parse_date(raw)
        if parsed is None:
            raise UsageError("date value must use YYYY-MM-DD")
        return parsed
    try:
        parsed_json = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsageError(f"invalid JSON value: {exc.msg}") from exc
    if not isinstance(parsed_json, dict | list):
        raise UsageError("--type json requires an object or array")
    return parsed_json


def cmd_meta_get(args: argparse.Namespace) -> int:
    root = _root(args, inspection=True)
    document = require_mutable_document(root, args.concept)
    assert document.frontmatter is not None
    value = document.frontmatter if args.key is None else dotted_get(document.frontmatter, args.key)
    if _format(args) == "json":
        _json(value)
    elif isinstance(value, Mapping | list):
        sys.stdout.write(_yaml_text(value))
    else:
        print(yaml_plain(value))
    return 0


def cmd_meta_set(args: argparse.Namespace) -> int:
    root = _prepare_mutation(args)
    document = require_mutable_document(root, args.concept)
    assert document.frontmatter is not None
    value = _parse_meta_value(args.value, args.value_type)
    if args.key == "type" and (not isinstance(value, str) or not value.strip()):
        raise UsageError("type must remain a non-empty string")
    dotted_set(document.frontmatter, args.key, value)
    proposed = document.render()
    _display_change(
        document,
        proposed,
        args.dry_run,
        payload={"path": concept_id(root, document.path), "key": args.key, "value": value}
        if args.dry_run and _format(args) == "json"
        else None,
    )
    if not args.dry_run and _format(args) == "json":
        _json(
            {
                "path": concept_id(root, document.path),
                "key": args.key,
                "value": value,
                "changed": proposed != document.raw,
            }
        )
    return 0


def cmd_meta_unset(args: argparse.Namespace) -> int:
    if args.key == "type" or args.key.startswith("type."):
        raise UsageError("the required type field cannot be removed")
    root = _prepare_mutation(args)
    document = require_mutable_document(root, args.concept)
    assert document.frontmatter is not None
    changed = dotted_unset(document.frontmatter, args.key)
    proposed = document.render()
    _display_change(
        document,
        proposed,
        args.dry_run,
        payload={"path": concept_id(root, document.path), "key": args.key, "changed": changed}
        if args.dry_run and _format(args) == "json"
        else None,
    )
    if not args.dry_run and _format(args) == "json":
        _json({"path": concept_id(root, document.path), "key": args.key, "changed": changed})
    return 0


def cmd_tag(args: argparse.Namespace) -> int:
    root = _prepare_mutation(args)
    document = require_mutable_document(root, args.concept)
    assert document.frontmatter is not None
    existing = document.frontmatter.get("tags", [])
    if not isinstance(existing, list) or not all(isinstance(tag, str) for tag in existing):
        raise OperationalError("existing tags field is not a list of strings")
    tags = list(existing)
    if args.tag_action == "add":
        tags.extend(tag for tag in args.tags if tag not in tags)
    else:
        removal = set(args.tags)
        tags = [tag for tag in tags if tag not in removal]
    if tags:
        document.frontmatter["tags"] = CommentedSeq(tags)
    else:
        document.frontmatter.pop("tags", None)
    proposed = document.render()
    _display_change(
        document,
        proposed,
        args.dry_run,
        payload={"path": concept_id(root, document.path), "tags": tags}
        if args.dry_run and _format(args) == "json"
        else None,
    )
    if not args.dry_run and _format(args) == "json":
        _json({"path": concept_id(root, document.path), "tags": tags})
    return 0


def cmd_source_list(args: argparse.Namespace) -> int:
    root = _root(args, inspection=True)
    document = require_mutable_document(root, args.concept)
    assert document.frontmatter is not None
    sources = document.frontmatter.get("sources", [])
    if _format(args) == "json":
        _json(sources)
    elif sources:
        sys.stdout.write(_yaml_text(sources))
    return 0


def cmd_source_add(args: argparse.Namespace) -> int:
    root = _prepare_mutation(args)
    document = require_mutable_document(root, args.concept)
    assert document.frontmatter is not None
    if not args.resource.strip():
        raise UsageError("--resource must be non-empty")
    sources = document.frontmatter.get("sources", [])
    if not isinstance(sources, list):
        raise OperationalError("existing sources field is not a list")
    if args.source_id is not None and any(
        isinstance(source, Mapping) and source.get("id") == args.source_id for source in sources
    ):
        raise UsageError(f"duplicate source ID: {args.source_id}")
    source = CommentedMap()
    for field, value in (
        ("id", args.source_id),
        ("resource", args.resource),
        ("title", args.title),
        ("author", args.author),
        ("usage_count", args.usage_count),
        ("last_modified", args.last_modified),
    ):
        if value is not None:
            source[field] = value
    if (args.usage_from is None) != (args.usage_to is None):
        raise UsageError("--usage-from and --usage-to must be supplied together")
    if args.usage_from is not None:
        for value in (args.usage_from, args.usage_to):
            if parse_date(value) is None:
                raise UsageError("usage window dates must use YYYY-MM-DD")
        source["usage_window"] = CommentedMap({"from": args.usage_from, "to": args.usage_to})
        if date.fromisoformat(args.usage_from) > date.fromisoformat(args.usage_to):
            raise UsageError("--usage-from must not be after --usage-to")
    if args.last_modified is not None and parse_date(args.last_modified) is None:
        raise UsageError("--last-modified must use YYYY-MM-DD")
    if args.usage_count is not None and args.usage_count < 0:
        raise UsageError("--usage-count must be non-negative")
    updated = CommentedSeq(sources)
    updated.append(source)
    document.frontmatter["sources"] = updated
    proposed = document.render()
    _display_change(
        document,
        proposed,
        args.dry_run,
        payload={"path": concept_id(root, document.path), "added": source}
        if args.dry_run and _format(args) == "json"
        else None,
    )
    if not args.dry_run and _format(args) == "json":
        _json({"path": concept_id(root, document.path), "added": source})
    return 0


def cmd_source_remove(args: argparse.Namespace) -> int:
    root = _prepare_mutation(args)
    document = require_mutable_document(root, args.concept)
    assert document.frontmatter is not None
    sources = document.frontmatter.get("sources", [])
    if not isinstance(sources, list):
        raise OperationalError("existing sources field is not a list")
    updated = [
        source
        for source in sources
        if not (
            isinstance(source, Mapping)
            and (
                (args.source_id is not None and source.get("id") == args.source_id)
                or (args.resource is not None and source.get("resource") == args.resource)
            )
        )
    ]
    if updated:
        document.frontmatter["sources"] = CommentedSeq(updated)
    else:
        document.frontmatter.pop("sources", None)
    proposed = document.render()
    _display_change(
        document,
        proposed,
        args.dry_run,
        payload={"path": concept_id(root, document.path), "removed": len(sources) - len(updated)}
        if args.dry_run and _format(args) == "json"
        else None,
    )
    if not args.dry_run and _format(args) == "json":
        _json(
            {
                "path": concept_id(root, document.path),
                "removed": len(sources) - len(updated),
            }
        )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    root = _prepare_mutation(args)
    document = require_mutable_document(root, args.concept)
    assert document.frontmatter is not None
    if not args.by.strip():
        raise UsageError("--by must be non-empty")
    at = args.at or _utc_now()
    try:
        parsed_at = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UsageError("--at must be an ISO 8601 datetime") from exc
    if parsed_at.tzinfo is None:
        at = parsed_at.replace(tzinfo=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    events = normalize_verified(document.frontmatter.get("verified"))
    events.append(CommentedMap({"by": args.by, "at": at}))
    document.frontmatter["verified"] = CommentedSeq(events)
    proposed = document.render()
    _display_change(
        document,
        proposed,
        args.dry_run,
        payload={"path": concept_id(root, document.path), "verification": events[-1]}
        if args.dry_run and _format(args) == "json"
        else None,
    )
    if not args.dry_run and _format(args) == "json":
        _json({"path": concept_id(root, document.path), "verification": events[-1]})
    return 0


def cmd_lifecycle_mutate(args: argparse.Namespace) -> int:
    root = _prepare_mutation(args)
    document = require_mutable_document(root, args.concept)
    assert document.frontmatter is not None
    if args.lifecycle_action == "set-status":
        document.frontmatter["status"] = args.value
    elif args.lifecycle_action == "set-stale-after":
        if parse_date(args.value) is None:
            raise UsageError("stale date must use YYYY-MM-DD")
        document.frontmatter["stale_after"] = args.value
    else:
        document.frontmatter.pop("stale_after", None)
    proposed = document.render()
    _display_change(
        document,
        proposed,
        args.dry_run,
        payload={"path": concept_id(root, document.path), "metadata": document.frontmatter}
        if args.dry_run and _format(args) == "json"
        else None,
    )
    if not args.dry_run and _format(args) == "json":
        _json({"path": concept_id(root, document.path), "metadata": document.frontmatter})
    return 0


def cmd_lifecycle_report(args: argparse.Namespace) -> int:
    root = _root(args, inspection=True)
    filters = Filters(stale=args.stale, due_before=_filters(args).due_before)
    today = _today(args)
    records: list[dict[str, Any]] = []
    for path in iter_concepts(root):
        document = parse_document(path, require_frontmatter=True)
        if document.frontmatter is None or not filters.matches(document.frontmatter, today):
            continue
        latest = latest_verification(document.frontmatter)
        records.append(
            {
                "id": concept_id(root, path),
                "status": status(document.frontmatter),
                "stale_after": yaml_plain(document.frontmatter.get("stale_after")),
                "latest_verification": latest.isoformat() if latest else None,
            }
        )
    if _format(args) == "json":
        _json(records)
    else:
        for item in records:
            print(
                f"{item['id']}\t{item['status']}\t{item['stale_after'] or '-'}\t"
                f"{item['latest_verification'] or '-'}"
            )
    return 0


def cmd_links(args: argparse.Namespace) -> int:
    root = _root(args, inspection=True)
    if args.links_action == "outgoing":
        path = concept_path(root, args.concept)
        records = links_for_file(root, path)
    elif args.links_action == "backlinks":
        target = concept_path(root, args.concept)
        records = [
            record
            for record in all_links(root)
            if record.resolved_path == target.relative_to(root).as_posix()
        ]
    else:
        records = []
        diagnostics = link_diagnostics(root, _target_path(root, args.path))
        if _format(args) == "json":
            _json({"diagnostics": [item.as_dict() for item in diagnostics]})
        else:
            for item in diagnostics:
                print(f"{item.severity}: {item.file}:{item.line or '-'}: {item.message}")
        return 1 if args.warnings_as_errors and diagnostics else 0
    if _format(args) == "json":
        _json([record.as_dict() for record in records])
    else:
        for record in records:
            resolved = record.resolved_path or "external"
            print(f"{record.source}:{record.line}\t{record.destination}\t{resolved}")
    return 0


def cmd_citations(args: argparse.Namespace) -> int:
    root = _root(args, inspection=True)
    diagnostics = citation_diagnostics(root, _target_path(root, args.path))
    if _format(args) == "json":
        _json({"diagnostics": [item.as_dict() for item in diagnostics]})
    else:
        for item in diagnostics:
            print(f"{item.severity}: {item.file}:{item.line or '-'}: {item.message}")
    return 1 if args.warnings_as_errors and diagnostics else 0


def cmd_validate(args: argparse.Namespace) -> int:
    root = _root(args, inspection=True)
    diagnostics = validate_bundle(root, _target_path(root, args.path), _today(args))
    errors = [item for item in diagnostics if item.severity == "error"]
    warnings = [item for item in diagnostics if item.severity == "warning"]
    if _format(args) == "json":
        _json(
            {
                "valid": not errors,
                "errors": len(errors),
                "warnings": len(warnings),
                "diagnostics": [item.as_dict() for item in diagnostics],
            }
        )
    else:
        for item in diagnostics:
            location = f":{item.line}" if item.line else ""
            field = f" [{item.field}]" if item.field else ""
            print(f"{item.severity}: {item.file}{location}{field}: {item.message}")
        print(f"{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors or (args.warnings_as_errors and warnings) else 0


def cmd_move(args: argparse.Namespace) -> int:
    root = _prepare_mutation(args)
    plan = build_move_plan(root, args.source, args.destination, _today(args))
    if args.dry_run:
        if _format(args) == "json":
            _json(plan.as_dict())
        else:
            print(f"move {plan.source} -> {plan.destination}")
            for change in plan.changes:
                print(
                    f"{change.file}: {change.kind} {change.location}: "
                    f"{change.before} -> {change.after}"
                )
        return 0
    execute_move(root, plan, _today(args))
    if _format(args) == "json":
        payload = plan.as_dict()
        payload["moved"] = True
        _json(payload)
    else:
        print(f"moved {plan.source} -> {plan.destination}")
    return 0


def _common(
    parser: argparse.ArgumentParser,
    *,
    today: bool = False,
    formats: tuple[str, ...] = ("text", "json"),
) -> None:
    parser.add_argument("--root", default=argparse.SUPPRESS)
    parser.add_argument("--format", choices=formats, default=argparse.SUPPRESS)
    if today:
        parser.add_argument("--today", default=argparse.SUPPRESS)


def _dry(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true")


def _add_filters(parser: argparse.ArgumentParser, *, today: bool = True) -> None:
    parser.add_argument("--type", dest="filter_type")
    parser.add_argument("--tag")
    parser.add_argument("--status", choices=("draft", "stable", "deprecated"))
    parser.add_argument("--trust", choices=("unverified", "machine-confirmed", "human-reviewed"))
    parser.add_argument("--stale", action="store_true")
    parser.add_argument("--due-before")
    _common(parser, today=today)


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="okf", description="Manage an Open Knowledge Format bundle")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--root")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--today")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init")
    init.add_argument("path", nargs="?")
    init.add_argument("--version", default="0.2")
    _common(init)
    _dry(init)
    init.set_defaults(handler=cmd_init)

    new = commands.add_parser("new")
    new.add_argument("concept")
    new.add_argument("--type", dest="concept_type", required=True)
    new.add_argument("--title")
    new.add_argument("--description")
    new.add_argument("--resource")
    new.add_argument("--tag", dest="tags", action="append", default=[])
    new.add_argument("--status", choices=("draft", "stable", "deprecated"))
    new.add_argument("--generated-by", default=os.environ.get("OKF_ACTOR"))
    _common(new)
    _dry(new)
    new.set_defaults(handler=cmd_new)

    listing = commands.add_parser("list")
    listing.add_argument("path", nargs="?")
    _add_filters(listing)
    listing.set_defaults(handler=cmd_list)

    show = commands.add_parser("show")
    show.add_argument("concept")
    selection = show.add_mutually_exclusive_group()
    selection.add_argument("--frontmatter-only", action="store_true")
    selection.add_argument("--body-only", action="store_true")
    _common(show, formats=("text", "yaml", "json"))
    show.set_defaults(handler=cmd_show)

    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--metadata-only", action="store_true")
    search.add_argument("--case-sensitive", action="store_true")
    _add_filters(search)
    search.set_defaults(handler=cmd_search)

    meta = commands.add_parser("meta")
    meta_commands = meta.add_subparsers(dest="meta_action", required=True)
    meta_get = meta_commands.add_parser("get")
    meta_get.add_argument("concept")
    meta_get.add_argument("key", nargs="?")
    _common(meta_get)
    meta_get.set_defaults(handler=cmd_meta_get)
    meta_set = meta_commands.add_parser("set")
    meta_set.add_argument("concept")
    meta_set.add_argument("key")
    meta_set.add_argument("value")
    meta_set.add_argument(
        "--type", dest="value_type", choices=("string", "number", "boolean", "date", "json")
    )
    _common(meta_set)
    _dry(meta_set)
    meta_set.set_defaults(handler=cmd_meta_set)
    meta_unset = meta_commands.add_parser("unset")
    meta_unset.add_argument("concept")
    meta_unset.add_argument("key")
    _common(meta_unset)
    _dry(meta_unset)
    meta_unset.set_defaults(handler=cmd_meta_unset)

    tag = commands.add_parser("tag")
    tag_commands = tag.add_subparsers(dest="tag_action", required=True)
    for action in ("add", "remove"):
        action_parser = tag_commands.add_parser(action)
        action_parser.add_argument("concept")
        action_parser.add_argument("tags", nargs="+")
        _common(action_parser)
        _dry(action_parser)
        action_parser.set_defaults(handler=cmd_tag)

    source = commands.add_parser("source")
    source_commands = source.add_subparsers(dest="source_action", required=True)
    source_list = source_commands.add_parser("list")
    source_list.add_argument("concept")
    _common(source_list)
    source_list.set_defaults(handler=cmd_source_list)
    source_add = source_commands.add_parser("add")
    source_add.add_argument("concept")
    source_add.add_argument("--resource", required=True)
    source_add.add_argument("--id", dest="source_id")
    source_add.add_argument("--title")
    source_add.add_argument("--author")
    source_add.add_argument("--usage-count", type=int)
    source_add.add_argument("--last-modified")
    source_add.add_argument("--usage-from")
    source_add.add_argument("--usage-to")
    _common(source_add)
    _dry(source_add)
    source_add.set_defaults(handler=cmd_source_add)
    source_remove = source_commands.add_parser("remove")
    source_remove.add_argument("concept")
    source_choice = source_remove.add_mutually_exclusive_group(required=True)
    source_choice.add_argument("--id", dest="source_id")
    source_choice.add_argument("--resource")
    _common(source_remove)
    _dry(source_remove)
    source_remove.set_defaults(handler=cmd_source_remove)

    verify = commands.add_parser("verify")
    verify.add_argument("concept")
    verify.add_argument("--by", required=True)
    verify.add_argument("--at")
    _common(verify)
    _dry(verify)
    verify.set_defaults(handler=cmd_verify)

    lifecycle = commands.add_parser("lifecycle")
    lifecycle_commands = lifecycle.add_subparsers(dest="lifecycle_action", required=True)
    set_status = lifecycle_commands.add_parser("set-status")
    set_status.add_argument("concept")
    set_status.add_argument("value", choices=("draft", "stable", "deprecated"))
    _common(set_status)
    _dry(set_status)
    set_status.set_defaults(handler=cmd_lifecycle_mutate)
    set_stale = lifecycle_commands.add_parser("set-stale-after")
    set_stale.add_argument("concept")
    set_stale.add_argument("value")
    _common(set_stale)
    _dry(set_stale)
    set_stale.set_defaults(handler=cmd_lifecycle_mutate)
    clear_stale = lifecycle_commands.add_parser("clear-stale-after")
    clear_stale.add_argument("concept")
    _common(clear_stale)
    _dry(clear_stale)
    clear_stale.set_defaults(handler=cmd_lifecycle_mutate)
    report = lifecycle_commands.add_parser("report")
    report.add_argument("--stale", action="store_true")
    report.add_argument("--due-before")
    _common(report, today=True)
    report.set_defaults(handler=cmd_lifecycle_report)

    links = commands.add_parser("links")
    links_commands = links.add_subparsers(dest="links_action", required=True)
    for action in ("outgoing", "backlinks"):
        action_parser = links_commands.add_parser(action)
        action_parser.add_argument("concept")
        _common(action_parser)
        action_parser.set_defaults(handler=cmd_links)
    links_check = links_commands.add_parser("check")
    links_check.add_argument("path", nargs="?")
    links_check.add_argument("--warnings-as-errors", action="store_true")
    _common(links_check)
    links_check.set_defaults(handler=cmd_links)

    citations = commands.add_parser("citations")
    citations_commands = citations.add_subparsers(dest="citations_action", required=True)
    citations_check = citations_commands.add_parser("check")
    citations_check.add_argument("path", nargs="?")
    citations_check.add_argument("--warnings-as-errors", action="store_true")
    _common(citations_check)
    citations_check.set_defaults(handler=cmd_citations)

    validate = commands.add_parser("validate")
    validate.add_argument("path", nargs="?")
    validate.add_argument("--warnings-as-errors", action="store_true")
    _common(validate, today=True)
    validate.set_defaults(handler=cmd_validate)

    move = commands.add_parser("move")
    move.add_argument("source")
    move.add_argument("destination")
    _common(move, today=True)
    _dry(move)
    move.set_defaults(handler=cmd_move)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        handler: Handler = args.handler
        return handler(args)
    except OkfError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except BrokenPipeError:
        return 0
    except (OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"error: unexpected failure: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
