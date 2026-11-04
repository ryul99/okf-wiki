# OKF Wiki CLI

`okf` manages file-based [Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md) v0.2 bundles while
keeping Markdown files authoritative. It creates and inspects concepts, safely
updates YAML frontmatter, writes concept bodies, validates bundles, searches
content, checks links and citations, reports trust and lifecycle state, and
performs recoverable concept moves.

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

## Codex and Claude Code plugins

The base `okf-wiki` plugin helps Codex and Claude Code inspect, update,
validate, and safely move OKF concepts using the installed `okf` command. It
does not install hooks.

Install it from the `ryul99-ai-tools` marketplace:

```sh
# Codex
codex plugin marketplace add ryul99/ai-tools
codex plugin add okf-wiki@ryul99-ai-tools

# Claude Code
claude plugin marketplace add ryul99/ai-tools
claude plugin install okf-wiki@ryul99-ai-tools
```

Codex and Claude Code users can optionally install the autosave companion
after installing the base plugin:

```sh
# Codex
codex plugin add okf-wiki-autosave@ryul99-ai-tools

# Claude Code
claude plugin install okf-wiki-autosave@ryul99-ai-tools
```

After installing the companion in Codex, open `/hooks` and trust its hook
definition. The companion uses the active matching Codex or Claude Code
subscription login to journal meaningful work after main-agent turns. It
creates or updates concepts under `worklog/` by default, groups entries under
per-day headings, and tags worklogs with `autosave-worklog`. Every write is
validated and rolled back if validation fails; `index.md` and `log.md` are
never modified.

Autosave requires `okf` and the host CLI (`codex` or `claude`) on `PATH`, plus
an OKF bundle in the current directory hierarchy or an `OKF_ROOT` value.

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

## Inspect tags

List the distinct tags used in a bundle, optionally scoped to a concept or
directory:

```sh
uv run okf tag list
uv run okf tag list tables
```

Tags are sorted and printed one per line. Add `--counts` to show the number of
concepts using each tag, or `--format json` for machine-readable output:

```sh
uv run okf tag list --counts
uv run okf tag list --format json
```

## Write concept bodies

Replace a concept's Markdown body from a file or stdin. The frontmatter bytes
are kept exactly as they are:

```sh
uv run okf show notes/handoff --body-only > handoff.md
# Edit handoff.md, then write it back.
uv run okf body set notes/handoff --from-file handoff.md

printf '# Handoff\n\nNext: add tests.\n' | uv run okf body set notes/handoff
```

Pass `--if-match` with the `identity` reported by `okf show --format json` to
refuse a write when the file changed since it was read, and
`--warnings-as-errors` to refuse a write that introduces new link or citation
warnings.

## Interrupted move recovery

Move transactions use hidden `.okf-transaction-*` directories inside the
bundle. If a process stops after a transaction is staged or partially applied,
the next mutating command automatically restores every original file, removes
the newly created destination, and cleans up the transaction directory. If its
recovery manifest is damaged, mutation is refused and the transaction directory
is retained for manual recovery.

The CLI never initializes Git, stores knowledge in a database, or launches an
editor.
