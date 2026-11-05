"""Metadata derivation, filtering, and dotted-path operations."""

from __future__ import annotations

import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ruamel.yaml.comments import CommentedMap

from okf_cli.errors import UsageError
from okf_cli.markdown import resolve_destination

STATUSES = ("draft", "stable", "deprecated")
LIFECYCLE_KEYS = ("status", "stale_after")
TRUST_TIERS = ("unverified", "machine-confirmed", "human-reviewed")


def parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None
    return None


def trust_tier(metadata: Mapping[str, Any]) -> str:
    verified = normalize_verified(metadata.get("verified"))
    events = [event for event in verified if isinstance(event, Mapping)]
    if not events:
        return "unverified"
    if any(str(event.get("by", "")).startswith("human:") for event in events):
        return "human-reviewed"
    return "machine-confirmed"


def status(metadata: Mapping[str, Any]) -> str:
    value = metadata.get("status", "stable")
    return str(value) if value is not None else "stable"


def is_stale(metadata: Mapping[str, Any], today: date) -> bool:
    stale_after = parse_date(metadata.get("stale_after"))
    return stale_after is not None and today >= stale_after


def latest_verification(metadata: Mapping[str, Any]) -> datetime | None:
    events = normalize_verified(metadata.get("verified"))
    dates = [
        parsed
        for event in events
        if isinstance(event, Mapping)
        if (parsed := parse_datetime(event.get("at"))) is not None
    ]
    normalized = [
        value if value.tzinfo is not None else value.replace(tzinfo=UTC) for value in dates
    ]
    return max(normalized) if normalized else None


@dataclass(frozen=True)
class Filters:
    type: str | None = None
    tag: str | None = None
    status: str | None = None
    trust: str | None = None
    stale: bool = False
    due_before: date | None = None

    def matches(self, metadata: Mapping[str, Any], today: date) -> bool:
        if self.type is not None and metadata.get("type") != self.type:
            return False
        tags = metadata.get("tags", [])
        if self.tag is not None and (not isinstance(tags, list) or self.tag not in tags):
            return False
        if self.status is not None and status(metadata) != self.status:
            return False
        if self.trust is not None and trust_tier(metadata) != self.trust:
            return False
        if self.stale and not is_stale(metadata, today):
            return False
        stale_after = parse_date(metadata.get("stale_after"))
        return not (
            self.due_before is not None and (stale_after is None or stale_after > self.due_before)
        )


@dataclass(frozen=True)
class PathField:
    """A path-valued frontmatter field, addressable for both reading and rewriting."""

    name: str
    parent: MutableMapping[str, Any]
    key: str

    @property
    def value(self) -> str:
        return str(self.parent[self.key])


def _is_path_like(value: str) -> bool:
    if urlsplit(value).scheme or value.startswith("//"):
        return False
    return (
        value.startswith(("/", "./", "../"))
        or "/" in value
        or bool(re.search(r"\.[A-Za-z0-9]{1,8}(?:[?#].*)?$", value))
    ) and not any(character.isspace() for character in value)


def _is_path_value(root: Path, source: Path, value: str) -> bool:
    if _is_path_like(value):
        return True
    # A name the shape rule misses, such as config.properties, is still a path to a real file.
    resolved = resolve_destination(root, source, value).resolved
    return resolved is not None and resolved.is_file()


def path_fields(metadata: MutableMapping[str, Any], root: Path, source: Path) -> list[PathField]:
    """Path-valued fields of the concept at `source`, resolved against `root`."""

    candidates = [PathField("resource", metadata, "resource")]
    sources = metadata.get("sources")
    if isinstance(sources, list):
        candidates.extend(
            PathField(f"sources[{index}].resource", source, "resource")
            for index, source in enumerate(sources)
            if isinstance(source, MutableMapping)
        )
    candidates.append(PathField("computation", metadata, "computation"))
    for family in ("executor", "attester"):
        value = metadata.get(family)
        if isinstance(value, MutableMapping):
            candidates.append(PathField(f"{family}.resource", value, "resource"))
    return [
        field
        for field in candidates
        if isinstance(field.parent.get(field.key), str)
        and _is_path_value(root, source, field.value)
    ]


def dotted_get(metadata: Mapping[str, Any], key: str) -> Any:
    current: Any = metadata
    for component in key.split("."):
        if not isinstance(current, Mapping) or component not in current:
            raise UsageError(f"metadata key does not exist: {key}")
        current = current[component]
    return current


def dotted_set(metadata: MutableMapping[str, Any], key: str, value: Any) -> None:
    components = key.split(".")
    if any(not component for component in components):
        raise UsageError(f"invalid dotted metadata key: {key}")
    current = metadata
    for component in components[:-1]:
        existing = current.get(component)
        if existing is None:
            child: MutableMapping[str, Any] = CommentedMap()
            current[component] = child
            current = child
        elif isinstance(existing, MutableMapping):
            current = existing
        else:
            raise UsageError(f"metadata path is not a mapping: {component}")
    current[components[-1]] = value


def dotted_unset(metadata: MutableMapping[str, Any], key: str) -> bool:
    components = key.split(".")
    current: MutableMapping[str, Any] = metadata
    for component in components[:-1]:
        existing = current.get(component)
        if not isinstance(existing, MutableMapping):
            return False
        current = existing
    return current.pop(components[-1], None) is not None


def normalize_verified(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [value] if isinstance(value, Mapping) else []
