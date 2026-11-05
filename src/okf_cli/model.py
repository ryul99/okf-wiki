"""Bundle discovery, safe paths, and lossless frontmatter boundaries."""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

from okf_cli.errors import OperationalError, UsageError

RESERVED_NAMES = {"index.md", "log.md"}
TRANSACTION_PREFIX = ".okf-transaction-"


def content_identity(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ensure_unchanged(path: Path, expected_identity: str) -> None:
    try:
        current = content_identity(path.read_bytes())
    except OSError as exc:
        raise OperationalError(f"cannot recheck {path}: {exc}") from exc
    if current != expected_identity:
        raise OperationalError(f"concurrent change detected: {path}")


def _yaml() -> YAML:
    parser = YAML(typ="rt")
    parser.preserve_quotes = True
    parser.width = 4096
    return parser


@dataclass
class Document:
    path: Path
    raw: bytes
    frontmatter: CommentedMap | None
    body: bytes
    prefix: bytes
    error: str | None = None

    @property
    def identity(self) -> str:
        return content_identity(self.raw)

    @property
    def text_body(self) -> str:
        try:
            return self.body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OperationalError(f"{self.path}: body is not valid UTF-8") from exc

    def render(self) -> bytes:
        if self.frontmatter is None:
            return self.raw
        stream = io.StringIO()
        _yaml().dump(self.frontmatter, stream)
        yaml_bytes = stream.getvalue().encode("utf-8")
        return b"---\n" + yaml_bytes + b"---\n" + self.body


def parse_document(path: Path, *, require_frontmatter: bool = False) -> Document:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise OperationalError(f"cannot read {path}: {exc}") from exc
    return parse_bytes(path, raw, require_frontmatter=require_frontmatter)


def parse_bytes(path: Path, raw: bytes, *, require_frontmatter: bool = False) -> Document:
    if not raw.startswith(b"---\n") and not raw.startswith(b"---\r\n"):
        error = "missing YAML frontmatter" if require_frontmatter else None
        return Document(path, raw, None, raw, b"", error)

    lines = raw.splitlines(keepends=True)
    closing = None
    yaml_start = len(lines[0])
    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip(b"\r\n") == b"---":
            closing = index
            break
    if closing is None:
        return Document(path, raw, None, raw, b"", "unterminated YAML frontmatter")

    close_start = sum(len(line) for line in lines[:closing])
    body_start = close_start + len(lines[closing])
    yaml_raw = raw[yaml_start:close_start]
    try:
        parsed = _yaml().load(yaml_raw.decode("utf-8"))
    except (UnicodeDecodeError, YAMLError) as exc:
        return Document(path, raw, None, raw[body_start:], raw[:body_start], f"invalid YAML: {exc}")
    if parsed is None:
        parsed = CommentedMap()
    if not isinstance(parsed, CommentedMap):
        return Document(
            path, raw, None, raw[body_start:], raw[:body_start], "frontmatter must be a mapping"
        )
    return Document(path, raw, parsed, raw[body_start:], raw[:body_start])


def has_root_marker(directory: Path) -> bool:
    index = directory / "index.md"
    if not index.is_file():
        return False
    doc = parse_document(index)
    return doc.frontmatter is not None and "okf_version" in doc.frontmatter


def discover_root(start: Path, override: str | Path | None, *, inspection: bool = False) -> Path:
    if override is not None:
        root = Path(override).expanduser().resolve()
        if not root.is_dir():
            raise OperationalError(f"bundle root is not a directory: {root}")
        return root

    current = start.resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if has_root_marker(candidate):
            return candidate
    if inspection:
        return current
    raise UsageError("no OKF bundle found; run 'okf init' or pass --root")


def safe_resolve(root: Path, path: Path) -> Path:
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(root.resolve()):
        raise UsageError(f"path escapes bundle root: {path}")
    return resolved


def concept_path(root: Path, value: str, *, allow_reserved: bool = False) -> Path:
    normalized = value.replace("\\", "/")
    if not normalized or normalized in {".", "..", "/"}:
        raise UsageError("concept ID must be non-empty")
    pure = PurePosixPath(normalized)
    if pure.suffix != ".md":
        pure = PurePosixPath(f"{pure}.md")
    raw_path = Path(normalized)
    if raw_path.is_absolute():
        path = safe_resolve(root, Path(f"{raw_path}.md") if raw_path.suffix != ".md" else raw_path)
    else:
        path = safe_resolve(root, root.joinpath(*pure.parts))
    if not allow_reserved and path.name in RESERVED_NAMES:
        raise UsageError(f"reserved file is not a concept: {value}")
    return path


def concept_id(root: Path, path: Path) -> str:
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    return relative[:-3] if relative.endswith(".md") else relative


def iter_markdown(root: Path, path: Path | None = None) -> Iterator[Path]:
    target = (path or root).resolve()
    if target.is_file():
        if target.suffix.lower() == ".md":
            yield target
        return
    found: list[Path] = []
    for directory, dirs, files in os.walk(target, followlinks=False):
        directory_path = Path(directory)
        dirs[:] = sorted(
            d
            for d in dirs
            if not d.startswith(TRANSACTION_PREFIX)
            and (directory_path / d).resolve().is_relative_to(root.resolve())
        )
        for name in files:
            candidate = directory_path / name
            if name.endswith(".md") and candidate.resolve().is_relative_to(root.resolve()):
                found.append(candidate)
    yield from sorted(found, key=lambda item: item.relative_to(root).as_posix())


def iter_concepts(root: Path, path: Path | None = None) -> Iterator[Path]:
    for candidate in iter_markdown(root, path):
        if candidate.name not in RESERVED_NAMES:
            yield candidate


def atomic_write(path: Path, data: bytes, *, expected_identity: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if expected_identity is not None:
        ensure_unchanged(path, expected_identity)
    file_descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            if path.exists():
                os.fchmod(stream.fileno(), path.stat().st_mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        temporary_path.unlink(missing_ok=True)
        raise OperationalError(f"cannot write {path}: {exc}") from exc


def require_mutable_document(root: Path, value: str) -> Document:
    path = concept_path(root, value)
    if not path.is_file():
        raise OperationalError(f"concept does not exist: {value}")
    doc = parse_document(path, require_frontmatter=True)
    if doc.error or doc.frontmatter is None:
        raise OperationalError(f"{path}: {doc.error or 'invalid frontmatter'}")
    return doc


def yaml_plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): yaml_plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [yaml_plain(item) for item in value]
    if isinstance(value, set | tuple):
        return [yaml_plain(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)
