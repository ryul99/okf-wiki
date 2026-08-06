from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def run_okf(
    *arguments: str,
    cwd: Path,
    expected: int = 0,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("OKF_ACTOR", None)
    if environment:
        env.update(environment)
    result = subprocess.run(
        [sys.executable, "-m", "okf_cli", *arguments],
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected, result.stderr + result.stdout
    return result


def init_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    run_okf("init", str(bundle), cwd=tmp_path)
    return bundle


def test_help_describes_each_command(tmp_path: Path) -> None:
    top_level = run_okf("--help", cwd=tmp_path).stdout
    for description in (
        "Initialize a new OKF bundle.",
        "Create a new concept.",
        "List concepts in the bundle.",
        "Show a concept's contents.",
        "Search concept metadata and content.",
        "Read or update concept metadata.",
        "Add or remove concept tags.",
        "Manage a concept's sources.",
        "Record a concept verification.",
        "Manage concept lifecycle state.",
        "Inspect links between concepts.",
        "Inspect source citations.",
        "Validate an OKF bundle or path.",
        "Move a concept and update its links.",
    ):
        assert description in top_level

    nested_commands = {
        "meta": (
            "Get metadata from a concept.",
            "Set a metadata value on a concept.",
            "Remove a metadata value from a concept.",
        ),
        "tag": ("Add tags to a concept.", "Remove tags from a concept."),
        "source": (
            "List sources for a concept.",
            "Add a source to a concept.",
            "Remove a source from a concept.",
        ),
        "lifecycle": (
            "Set a concept's lifecycle status.",
            "Set when a concept becomes stale.",
            "Clear when a concept becomes stale.",
            "Report concept lifecycle state.",
        ),
        "links": (
            "List links from a concept.",
            "List links to a concept.",
            "Check links for problems.",
        ),
        "citations": ("Check citations for problems.",),
    }
    for command, descriptions in nested_commands.items():
        output = run_okf(command, "--help", cwd=tmp_path).stdout
        for description in descriptions:
            assert description in output

    assert "Create a new concept." in run_okf("new", "--help", cwd=tmp_path).stdout


def write_concept(bundle: Path, relative: str, frontmatter: str, body: str = "") -> Path:
    path = bundle / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter.rstrip()}\n---\n{body}", encoding="utf-8")
    return path


def test_init_new_list_show_and_global_options(tmp_path: Path) -> None:
    bundle = init_bundle(tmp_path)
    run_okf(
        "--root",
        str(bundle),
        "new",
        "tables/orders",
        "--type",
        "BigQuery Table",
        "--title",
        "Orders",
        "--description",
        "Completed orders.",
        "--tag",
        "sales",
        cwd=tmp_path,
    )
    source = bundle / "tables/orders.md"
    assert source.read_text() == (
        "---\n"
        "type: BigQuery Table\n"
        "title: Orders\n"
        "description: Completed orders.\n"
        "tags:\n"
        "- sales\n"
        "---\n\n"
    )

    source.write_bytes(source.read_bytes() + b"# Schema\r\n\r\nBody bytes.\r\n")
    body = run_okf("show", "tables/orders", "--body-only", cwd=bundle).stdout
    assert body == "\n# Schema\n\nBody bytes.\n"
    shown = json.loads(
        run_okf("list", "--format", "json", "--today", "2026-08-06", cwd=bundle).stdout
    )
    assert shown[0]["id"] == "tables/orders"
    assert shown[0]["trust"] == "unverified"
    assert shown[0]["freshness"] == "fresh"

    frontmatter = run_okf(
        "show", "tables/orders", "--frontmatter-only", "--format", "yaml", cwd=bundle
    ).stdout
    assert "type: BigQuery Table" in frontmatter
    shown_json = json.loads(run_okf("show", "tables/orders", "--format", "json", cwd=bundle).stdout)
    assert shown_json["frontmatter"]["type"] == "BigQuery Table"
    assert "Body bytes." in shown_json["body"]


def test_metadata_mutations_preserve_body_and_unknown_fields(tmp_path: Path) -> None:
    bundle = init_bundle(tmp_path)
    concept = write_concept(
        bundle,
        "notes/example.md",
        "type: Note\ncustom: keep # comment\ngenerated:\n  by: agent/v1",
        "\n# Authored\n\nKeep **exactly** this.\n",
    )
    original_body = concept.read_bytes().split(b"---\n", 2)[2]

    dry_run = run_okf(
        "meta",
        "set",
        "notes/example",
        "generated.at",
        "2026-08-06",
        "--type",
        "date",
        "--dry-run",
        cwd=bundle,
    )
    assert "+  at: 2026-08-06" in dry_run.stdout
    assert b"at:" not in concept.read_bytes()

    run_okf(
        "meta",
        "set",
        "notes/example",
        "generated.at",
        "2026-08-06",
        "--type",
        "date",
        cwd=bundle,
    )
    changed = json.loads(
        run_okf(
            "meta",
            "set",
            "notes/example",
            "enabled",
            "true",
            "--type",
            "boolean",
            "--format",
            "json",
            cwd=bundle,
        ).stdout
    )
    assert changed == {
        "path": "notes/example",
        "key": "enabled",
        "value": True,
        "changed": True,
    }
    run_okf("tag", "add", "notes/example", "one", "two", "one", cwd=bundle)
    run_okf("tag", "remove", "notes/example", "missing", cwd=bundle)
    assert concept.read_bytes().split(b"---\n", 2)[2] == original_body
    text = concept.read_text()
    assert "custom: keep # comment" in text
    assert text.index("custom:") < text.index("generated:")

    ambiguous = run_okf("meta", "set", "notes/example", "enabled", "true", cwd=bundle, expected=2)
    assert "explicit --type" in ambiguous.stderr
    run_okf("meta", "unset", "notes/example", "type", cwd=bundle, expected=2)


def test_provenance_verification_lifecycle_and_filters(tmp_path: Path) -> None:
    bundle = init_bundle(tmp_path)
    run_okf(
        "new",
        "metric",
        "--type",
        "Metric",
        "--title",
        "Revenue",
        "--description",
        "Revenue metric.",
        cwd=bundle,
    )
    run_okf(
        "source",
        "add",
        "metric",
        "--resource",
        "https://example.test/policy",
        "--id",
        "policy",
        "--usage-count",
        "12",
        "--usage-from",
        "2026-01-01",
        "--usage-to",
        "2026-06-30",
        cwd=bundle,
    )
    run_okf(
        "verify",
        "metric",
        "--by",
        "process:nightly",
        "--at",
        "2026-07-01T00:00:00Z",
        cwd=bundle,
    )
    run_okf(
        "verify",
        "metric",
        "--by",
        "human:reviewer",
        "--at",
        "2026-07-02T00:00:00Z",
        cwd=bundle,
    )
    run_okf("lifecycle", "set-status", "metric", "stable", cwd=bundle)
    run_okf("lifecycle", "set-stale-after", "metric", "2026-08-01", cwd=bundle)

    records = json.loads(
        run_okf(
            "list",
            "--trust",
            "human-reviewed",
            "--stale",
            "--today",
            "2026-08-06",
            "--format",
            "json",
            cwd=bundle,
        ).stdout
    )
    assert [record["id"] for record in records] == ["metric"]
    report = json.loads(
        run_okf(
            "lifecycle", "report", "--today", "2026-08-06", "--format", "json", cwd=bundle
        ).stdout
    )
    assert report[0]["latest_verification"].startswith("2026-07-02")
    sources = json.loads(run_okf("source", "list", "metric", "--format", "json", cwd=bundle).stdout)
    assert sources[0]["usage_window"] == {"from": "2026-01-01", "to": "2026-06-30"}


def test_search_links_citations_and_validation_exit_codes(tmp_path: Path) -> None:
    bundle = init_bundle(tmp_path)
    write_concept(
        bundle,
        "target.md",
        "type: Reference\ntitle: Target\ndescription: Link target.",
        "\n# Target\n",
    )
    write_concept(
        bundle,
        "source.md",
        "type: Note\ntitle: Source\ndescription: Searchable note.\n"
        "sources:\n  - id: used\n    resource: https://example.test\n"
        "  - id: unused\n    resource: https://example.test/unused",
        "\nA literal Needle links to [target](target.md).[^used]\n\n"
        "```md\n[ignored](missing.md)\n```\n\n"
        "[broken](missing.md)\n\n[^used]: Evidence\n",
    )
    results = json.loads(run_okf("search", "needle", "--format", "json", cwd=bundle).stdout)
    body_match = next(result for result in results if result["field"] == "body")
    assert body_match["line"] == 12

    outgoing = json.loads(
        run_okf("links", "outgoing", "source", "--format", "json", cwd=bundle).stdout
    )
    assert [record["destination"] for record in outgoing] == ["target.md", "missing.md"]
    assert outgoing[0]["line"] == 12
    diagnostics = json.loads(run_okf("links", "check", "--format", "json", cwd=bundle).stdout)[
        "diagnostics"
    ]
    assert [item["code"] for item in diagnostics] == ["broken-link"]
    run_okf("links", "check", "--warnings-as-errors", cwd=bundle, expected=1)

    citations = json.loads(run_okf("citations", "check", "--format", "json", cwd=bundle).stdout)[
        "diagnostics"
    ]
    assert any(item["code"] == "unused-source" for item in citations)
    validation = json.loads(
        run_okf("validate", "--format", "json", "--today", "2026-08-06", cwd=bundle).stdout
    )
    assert validation["valid"] is True
    assert validation["warnings"] >= 2
    one_concept = json.loads(run_okf("validate", "target", "--format", "json", cwd=bundle).stdout)
    assert one_concept["valid"] is True

    (bundle / "invalid.md").write_text("No frontmatter.\n", encoding="utf-8")
    invalid = json.loads(run_okf("validate", "--format", "json", cwd=bundle, expected=1).stdout)
    assert invalid["valid"] is False
    assert any(item["code"] == "invalid-frontmatter" for item in invalid["diagnostics"])


def test_move_dry_run_and_transactional_rewrites(tmp_path: Path) -> None:
    bundle = init_bundle(tmp_path)
    write_concept(
        bundle,
        "a/other.md",
        "type: Note\ntitle: Other\ndescription: Other concept.",
        "\nOther.\n",
    )
    moved = write_concept(
        bundle,
        "a/item.md",
        "type: Note\ntitle: Item\ndescription: Item concept.\n"
        "resource: other.md\ncustom: untouched",
        "\n[Other](other.md?view=1#top) and [absolute](/a/other.md).\n\n"
        '[Self reference][self]\n\n[self]: <item.md#section> "title"\n\n'
        "```md\n[code](item.md)\n```\n",
    )
    incoming = write_concept(
        bundle,
        "refs/from.md",
        "type: Note\ntitle: Incoming\ndescription: Incoming link.\nresource: ../a/item.md",
        "\nSee [item](../a/item.md?raw=1#section).\n",
    )
    (bundle / "index.md").write_text(
        '---\nokf_version: "0.2"\n---\n\n# Concepts\n\n[Item](/a/item.md)\n', encoding="utf-8"
    )

    dry = json.loads(
        run_okf(
            "move",
            "a/item",
            "new/item",
            "--dry-run",
            "--format",
            "json",
            cwd=bundle,
        ).stdout
    )
    assert dry["source"] == "a/item"
    assert any(change["kind"] == "frontmatter" for change in dry["changes"])
    assert moved.exists()
    assert not (bundle / "new").exists()

    run_okf("move", "a/item", "new/item", cwd=bundle)
    destination = bundle / "new/item.md"
    assert destination.exists()
    assert not moved.exists()
    destination_text = destination.read_text()
    assert "resource: ../a/other.md" in destination_text
    assert "[Other](../a/other.md?view=1#top)" in destination_text
    assert "[absolute](/a/other.md)" in destination_text
    assert '[self]: <item.md#section> "title"' in destination_text
    assert "[code](item.md)" in destination_text
    assert "custom: untouched" in destination_text
    incoming_text = incoming.read_text()
    assert "resource: ../new/item.md" in incoming_text
    assert "[item](../new/item.md?raw=1#section)" in incoming_text
    assert "[Item](/new/item.md)" in (bundle / "index.md").read_text()
    assert not list(bundle.glob(".okf-transaction-*"))
    run_okf("validate", cwd=bundle)


def test_next_mutation_recovers_interrupted_move(tmp_path: Path) -> None:
    bundle = init_bundle(tmp_path)
    concept = write_concept(
        bundle,
        "item.md",
        "type: Note\ntitle: Original\ndescription: Original concept.",
        "\nOriginal body.\n",
    )
    original = concept.read_bytes()
    transaction = bundle / ".okf-transaction-test"
    backup = transaction / "backups/item.md"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(original)
    (transaction / "manifest.json").write_text(
        json.dumps({"originals": ["item.md"], "created": ["new/item.md"], "created_dirs": ["new"]}),
        encoding="utf-8",
    )
    concept.write_text("partially replaced", encoding="utf-8")
    created = bundle / "new/item.md"
    created.parent.mkdir()
    created.write_text("partial destination", encoding="utf-8")

    result = run_okf("tag", "add", "item", "recovered", cwd=bundle)
    assert "recovered interrupted transaction" in result.stderr
    assert "Original body." in concept.read_text()
    assert "- recovered" in concept.read_text()
    assert not created.exists()
    assert not transaction.exists()


def test_reserved_and_escape_paths_are_usage_errors(tmp_path: Path) -> None:
    bundle = init_bundle(tmp_path)
    run_okf("new", "index", "--type", "Note", cwd=bundle, expected=2)
    run_okf("new", "../outside", "--type", "Note", cwd=bundle, expected=2)
    run_okf("show", "missing", cwd=bundle, expected=3)

    dotted = json.loads(
        run_okf(
            "new",
            "release.v1",
            "--type",
            "Note",
            "--format",
            "json",
            cwd=bundle,
        ).stdout
    )
    assert dotted["path"] == "release.v1.md"
    assert (bundle / "release.v1.md").is_file()

    dry_target = tmp_path / "dry-bundle"
    run_okf("init", str(dry_target), "--dry-run", cwd=tmp_path)
    assert not dry_target.exists()


def test_reserved_and_attested_computation_structure(tmp_path: Path) -> None:
    bundle = init_bundle(tmp_path)
    write_concept(
        bundle,
        "compute.md",
        "type: Attested Computation\ntitle: Compute\ndescription: A sanctioned computation.\n"
        "runtime: python\nexecutor:\n  resource: runner.py\n  receipt: [result]\n"
        "attester:\n  resource: attest.py",
        "\n# Notes\n\nNo computation fence.\n",
    )
    (bundle / "log.md").write_text(
        "# Directory Update Log\n\n## August 6\n* Updated.\n", encoding="utf-8"
    )
    result = json.loads(run_okf("validate", "--format", "json", cwd=bundle, expected=1).stdout)
    codes = {item["code"] for item in result["diagnostics"]}
    assert "invalid-inline-computation" in codes
    assert "invalid-log-date" in codes
