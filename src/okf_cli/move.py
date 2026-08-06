"""Preflighted, recoverable single-concept moves."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import uuid
from collections.abc import MutableMapping
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from okf_cli.errors import OperationalError, UsageError
from okf_cli.markdown import (
    apply_replacements,
    resolve_destination,
    rewrite_destination,
    scan_links,
)
from okf_cli.model import (
    TRANSACTION_PREFIX,
    Document,
    concept_id,
    concept_path,
    iter_markdown,
    parse_document,
)
from okf_cli.validation import Diagnostic, validate_bundle


@dataclass(frozen=True)
class MoveChange:
    file: str
    kind: str
    location: str
    before: str
    after: str


@dataclass
class MovePlan:
    source: str
    destination: str
    changes: list[MoveChange]
    contents: dict[Path, bytes]
    identities: dict[Path, str]
    source_path: Path
    destination_path: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "destination": self.destination,
            "files": sorted(
                {change.file for change in self.changes} | {self.source, self.destination}
            ),
            "changes": [asdict(change) for change in self.changes],
        }


def _path_like(value: str) -> bool:
    return (
        not (urlsplit(value).scheme or value.startswith("//"))
        and not any(character.isspace() for character in value)
        and (value.startswith(("/", "./", "../")) or "/" in value or "." in Path(value).name)
    )


def _field_refs(
    metadata: MutableMapping[str, Any],
) -> list[tuple[str, MutableMapping[str, Any], str]]:
    refs: list[tuple[str, MutableMapping[str, Any], str]] = []
    if isinstance(metadata.get("resource"), str):
        refs.append(("resource", metadata, "resource"))
    sources = metadata.get("sources")
    if isinstance(sources, list):
        for index, source in enumerate(sources):
            if isinstance(source, MutableMapping) and isinstance(source.get("resource"), str):
                refs.append((f"sources[{index}].resource", source, "resource"))
    if isinstance(metadata.get("computation"), str):
        refs.append(("computation", metadata, "computation"))
    for family in ("executor", "attester"):
        value = metadata.get(family)
        if isinstance(value, MutableMapping) and isinstance(value.get("resource"), str):
            refs.append((f"{family}.resource", value, "resource"))
    return refs


def _rewrite_document(
    root: Path, document: Document, source: Path, destination: Path
) -> tuple[bytes, list[MoveChange]]:
    old_file = document.path
    new_file = destination if old_file == source else old_file
    file_label = old_file.relative_to(root).as_posix()
    changes: list[MoveChange] = []
    body = document.text_body
    body_line_offset = document.prefix.count(b"\n")
    replacements: list[tuple[int, int, str]] = []
    for link in scan_links(body):
        resolved = resolve_destination(root, old_file, link.destination)
        if resolved.external or resolved.outside or resolved.resolved is None:
            continue
        target = resolved.resolved
        should_rewrite = target == source
        if old_file == source and not link.destination.split("?", 1)[0].split("#", 1)[0].startswith(
            "/"
        ):
            should_rewrite = True
        if not should_rewrite:
            continue
        new_target = destination if target == source else target
        rewritten = rewrite_destination(root, old_file, new_file, link.destination, new_target)
        if rewritten != link.destination:
            replacements.append((link.start, link.end, rewritten))
            changes.append(
                MoveChange(
                    file_label,
                    "link",
                    f"line {link.line + body_line_offset}",
                    link.destination,
                    rewritten,
                )
            )

    rendered_body = apply_replacements(body, replacements).encode("utf-8")
    if document.frontmatter is not None:
        for field, parent, key in _field_refs(document.frontmatter):
            value = parent[key]
            if not isinstance(value, str) or not _path_like(value):
                continue
            resolved = resolve_destination(root, old_file, value)
            if resolved.external or resolved.outside or resolved.resolved is None:
                continue
            target = resolved.resolved
            relative = not value.split("?", 1)[0].split("#", 1)[0].startswith("/")
            if target != source and not (old_file == source and relative):
                continue
            new_target = destination if target == source else target
            rewritten = rewrite_destination(root, old_file, new_file, value, new_target)
            if rewritten != value:
                parent[key] = rewritten
                changes.append(MoveChange(file_label, "frontmatter", field, value, rewritten))
        document.body = rendered_body
        rendered = document.render()
    else:
        body_start = len(document.raw) - len(document.body)
        rendered = document.raw[:body_start] + rendered_body
    return rendered, changes


def build_move_plan(root: Path, source_value: str, destination_value: str, today: date) -> MovePlan:
    source = concept_path(root, source_value)
    destination = concept_path(root, destination_value)
    if source == destination:
        raise UsageError("source and destination are the same concept")
    if not source.is_file():
        raise UsageError(f"source concept does not exist: {source_value}")
    if destination.exists():
        raise UsageError(f"destination already exists: {destination_value}")
    if source.is_dir() or destination.is_dir():
        raise UsageError("directory moves are not supported")

    contents: dict[Path, bytes] = {}
    identities: dict[Path, str] = {}
    changes: list[MoveChange] = [
        MoveChange(
            source.relative_to(root).as_posix(),
            "move",
            "path",
            source.relative_to(root).as_posix(),
            destination.relative_to(root).as_posix(),
        )
    ]
    for path in iter_markdown(root):
        document = parse_document(path, require_frontmatter=path.name not in {"index.md", "log.md"})
        rendered, file_changes = _rewrite_document(root, document, source, destination)
        if file_changes or path == source:
            if document.error:
                raise OperationalError(f"cannot move because {path} is invalid: {document.error}")
            target = destination if path == source else path
            contents[target] = rendered
            identities[path] = document.identity
            changes.extend(file_changes)

    for changed_path in identities:
        errors = [
            diagnostic
            for diagnostic in validate_bundle(root, changed_path, today)
            if diagnostic.severity == "error"
        ]
        if errors:
            raise OperationalError(
                f"cannot move because {changed_path} is not conformant: {errors[0].message}"
            )
    return MovePlan(
        concept_id(root, source),
        concept_id(root, destination),
        changes,
        contents,
        identities,
        source,
        destination,
    )


def _broken_targets(root: Path) -> set[str]:
    targets: set[str] = set()
    for path in iter_markdown(root):
        document = parse_document(path)
        for link in scan_links(document.text_body):
            resolved = resolve_destination(root, path, link.destination)
            if not resolved.external and (
                resolved.resolved is None or not resolved.resolved.exists()
            ):
                targets.add(
                    str(resolved.resolved) if resolved.resolved is not None else link.destination
                )
    return targets


def recover_transactions(root: Path) -> list[str]:
    recovered: list[str] = []
    for transaction in sorted(root.glob(f"{TRANSACTION_PREFIX}*")):
        manifest_path = transaction / "manifest.json"
        if not manifest_path.is_file():
            shutil.rmtree(transaction)
            recovered.append(transaction.name)
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for relative in manifest["originals"]:
                backup = transaction / "backups" / relative
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, target)
            for relative in manifest["created"]:
                (root / relative).unlink(missing_ok=True)
            for relative in manifest.get("created_dirs", []):
                with contextlib.suppress(OSError):
                    (root / relative).rmdir()
            shutil.rmtree(transaction)
            recovered.append(transaction.name)
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise OperationalError(
                f"automatic recovery failed for {transaction.name}: {exc}"
            ) from exc
    return recovered


def execute_move(root: Path, plan: MovePlan, today: date) -> None:
    before_broken = _broken_targets(root)
    for path, identity in plan.identities.items():
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise OperationalError(f"cannot recheck {path}: {exc}") from exc
        if actual != identity:
            raise OperationalError(f"concurrent change detected: {path}")
    if plan.destination_path.exists():
        raise OperationalError(f"destination appeared during move: {plan.destination_path}")

    transaction = root / f"{TRANSACTION_PREFIX}{uuid.uuid4().hex}"
    backups = transaction / "backups"
    staged = transaction / "staged"
    created_dirs: list[str] = []
    destination_parent = plan.destination_path.parent
    cursor = destination_parent
    while cursor != root and not cursor.exists():
        created_dirs.append(cursor.relative_to(root).as_posix())
        cursor = cursor.parent
    manifest = {
        "originals": [path.relative_to(root).as_posix() for path in plan.identities],
        "created": [plan.destination_path.relative_to(root).as_posix()],
        "created_dirs": created_dirs,
    }
    try:
        transaction.mkdir()
        for path in plan.identities:
            relative = path.relative_to(root)
            backup = backups / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
        for target, content in plan.contents.items():
            stage = staged / target.relative_to(root)
            stage.parent.mkdir(parents=True, exist_ok=True)
            stage.write_bytes(content)
            original = plan.source_path if target == plan.destination_path else target
            stage.chmod(original.stat().st_mode)
        (transaction / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

        for path, identity in plan.identities.items():
            if hashlib.sha256(path.read_bytes()).hexdigest() != identity:
                raise OperationalError(f"concurrent change detected: {path}")
        destination_parent.mkdir(parents=True, exist_ok=True)
        for target in sorted(plan.contents, key=lambda item: item.as_posix()):
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged / target.relative_to(root), target)
        plan.source_path.unlink()

        new_broken = _broken_targets(root) - before_broken
        if new_broken:
            raise OperationalError(f"move introduced an unresolved link: {sorted(new_broken)[0]}")
        errors: list[Diagnostic] = []
        for target in plan.contents:
            errors.extend(
                diagnostic
                for diagnostic in validate_bundle(root, target, today)
                if diagnostic.severity == "error"
            )
        if errors:
            raise OperationalError(f"post-move validation failed: {errors[0].message}")
        shutil.rmtree(transaction)
    except Exception as exc:
        try:
            if transaction.exists():
                recover_transactions(root)
        except OperationalError as recovery_error:
            raise OperationalError(
                f"move failed ({exc}); automatic rollback also failed ({recovery_error})"
            ) from recovery_error
        if isinstance(exc, OperationalError | UsageError):
            raise
        raise OperationalError(f"move failed and was rolled back: {exc}") from exc
