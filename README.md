# OKF Wiki CLI

`okf` manages file-based [Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md) v0.2 bundles while
keeping Markdown files authoritative. It creates and inspects concepts, safely
updates YAML frontmatter, validates bundles, searches content, checks links and
citations, reports trust and lifecycle state, and performs recoverable concept
moves.

## Installation

Install the `okf` command directly from GitHub with `uv`:

```sh
uv tool install git+https://github.com/ryul99/okf-wiki.git
```

Alternatively, install it with pip into the active Python environment:

```sh
python -m pip install git+https://github.com/ryul99/okf-wiki.git
```

After installation, verify the command is available:

```sh
okf --help
```

## Codex and Claude Code skill

The `okf-wiki` plugin helps Codex and Claude Code inspect, update, validate,
and safely move OKF concepts using the installed `okf` command.

Install it from the `ryul99-ai-tools` marketplace:

```sh
# Codex
codex plugin marketplace add ryul99/ai-tools
codex plugin add okf-wiki@ryul99-ai-tools

# Claude Code
claude plugin marketplace add ryul99/ai-tools
claude plugin install okf-wiki@ryul99-ai-tools
```

Claude Code users can optionally install the autosave companion:

```sh
claude plugin install okf-wiki-autosave@ryul99-ai-tools
```

The base `okf-wiki` plugin installs no hooks. The companion uses the active
Claude subscription login to maintain existing shared concepts tagged
`worklog-managed` after turns; it does not create per-session documents.

Run the agent inside an OKF bundle, or optionally set its default wiki root:

```sh
export OKF_ROOT=/path/to/wiki
```

Then ask it to search concepts, edit metadata, check links and citations,
validate the bundle, or move a concept safely. If no bundle or `OKF_ROOT` is
available, the skill asks for the wiki root.

## Development

This project uses Python 3.11 or newer and [`uv`](https://docs.astral.sh/uv/).

```sh
uv sync --all-groups
uv run okf --help
uv run pytest
uv run pre-commit run --all-files
```

## Quick start

```sh
uv run okf init knowledge
cd knowledge
uv run okf new tables/orders --type "BigQuery Table" --title "Orders"
uv run okf validate
```

Every mutating command accepts `--dry-run`. Read and validation commands, as
well as move plans, support stable machine-readable output with
`--format json`.

## Interrupted move recovery

Move transactions use hidden `.okf-transaction-*` directories inside the
bundle. If a process stops after a transaction is staged or partially applied,
the next mutating command automatically restores every original file, removes
the newly created destination, and cleans up the transaction directory. If its
recovery manifest is damaged, mutation is refused and the transaction directory
is retained for manual recovery.

The CLI never initializes Git, stores knowledge in a database, launches an
editor, or rewrites authored prose.
