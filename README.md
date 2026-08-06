# OKF Wiki CLI

`okf` manages file-based [Open Knowledge Format](OKF.md) v0.2 bundles while
keeping Markdown files authoritative. It creates and inspects concepts, safely
updates YAML frontmatter, validates bundles, searches content, checks links and
citations, reports trust and lifecycle state, and performs recoverable concept
moves.

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
