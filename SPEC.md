# OKF Wiki CLI Specification

## 1. Overview

The OKF Wiki CLI is a small command-line tool for managing an Open Knowledge
Format (OKF) bundle as a file-based wiki.

Markdown files are the source of truth. People and agents edit Markdown bodies
with their normal filesystem tools or write them through the CLI. The CLI is
responsible for bundle discovery, YAML frontmatter operations, body writes,
validation, search, link inspection, safe concept moves, and lifecycle
reporting. It must not introduce a database, server, editor, or proprietary
storage format.

The examples in this specification use `okf` as the executable name.

## 2. Goals

The CLI should:

1. Make it easy to create a conformant OKF bundle and concept documents.
2. Let people and agents inspect and update YAML frontmatter and Markdown
   bodies safely.
3. Validate OKF v0.2 structure and metadata without rejecting permitted
   extensions.
4. Provide fast file, metadata, full-text, and link discovery.
5. Move individual concepts safely while preserving internal references.
6. Surface provenance, trust, lifecycle, and freshness information.
7. Work well in shell scripts and agent workflows through stable JSON output
   and exit codes.
8. Keep every bundle fully usable with ordinary filesystem and Git tools when
   the CLI is unavailable.

## 3. Non-goals

The CLI will not:

- Provide a web UI, local server, TUI, or built-in editor.
- Open an external editor.
- Generate or update the authored content of `index.md` or `log.md`.
- Move directories or arbitrary non-concept files.
- Wrap Git operations such as diff, history, commit, push, or pull.
- Store bundle content in a database or maintain a required persistent index.
- Define a central type registry or reject producer-defined types and fields.
- Execute computations, executors, or attesters.
- Define the runtime receipt or attestation protocol deferred by OKF v0.2.
- Package, synchronize, publish, or remotely host bundles.

When an excluded operation is needed, the user or agent should modify the files
directly or use the existing specialized tool for the task, then run
`okf validate`.

## 4. Design principles

### 4.1 Files are authoritative

The CLI reads the bundle from disk on each invocation. Any cache must be
optional, disposable, and reproducible from the files. Deleting a cache must
never lose knowledge.

### 4.2 Structural refactoring is explicit and mechanical

A concept move is a structural refactoring, not a content-authoring operation.
It must be requested explicitly through `okf move`, limited to one concept per
invocation, and fully described by `--dry-run` before the user or agent chooses
to execute it.

The move may update only:

- Parsed Markdown link destinations affected by the move.
- Standard OKF frontmatter fields whose values resolve to affected internal
  paths.
- The filesystem path of the requested concept.

It must not infer, rewrite, or improve prose, headings, link labels, code
examples, or arbitrary path-like text.

### 4.3 Frontmatter changes are narrow and lossless

A frontmatter mutation must:

- Preserve the Markdown body byte-for-byte.
- Preserve unknown frontmatter keys.
- Avoid changing unrelated frontmatter values.
- Preserve key ordering and YAML comments where the implementation library
  makes this practical.
- Write the file atomically.
- Refuse the update if the current frontmatter cannot be parsed.

Every mutating command must support `--dry-run` and show the complete proposed
change without writing any file.

For `okf move`, path-valued fields affected by the move are related values and
may change. All other frontmatter values remain subject to the preservation
rules above.

### 4.4 Validation follows OKF's tolerance rules

The validator distinguishes conformance errors from advisory warnings. It must
not report the following as errors:

- Missing optional fields or metadata families.
- Unknown `type` values.
- Unknown frontmatter keys.
- Broken internal links.
- Missing `index.md` files.

### 4.5 Human and agent parity

Commands should be usable interactively, but must not require prompts. All
required values must have flag-based forms, and machine-readable output must be
available through `--format json`.

## 5. Bundle and concept model

### 5.1 Bundle root

Commands locate the bundle root by walking upward from the current directory.
A directory is considered an explicit OKF root when its root `index.md`
contains `okf_version`. The `--root <path>` option overrides discovery.

Commands that only inspect files may also operate on a directory without a
root `index.md`; in that case, the specified or current directory is treated as
the bundle root.

### 5.2 Concept ID

A concept ID is the POSIX-style path of a concept file relative to the bundle
root with the `.md` suffix removed. For example:

```text
tables/orders.md -> tables/orders
```

`index.md` and `log.md` are reserved files and never concept IDs.

Commands accepting a concept should support both a concept ID and an explicit
`.md` path. Output must use the normalized concept ID unless a path format is
explicitly requested.

### 5.3 Trust and freshness

The CLI derives trust tiers from `verified`:

- No `verified`: `unverified`
- Verification only by non-`human:` actors: `machine-confirmed`
- At least one verifier beginning with `human:`: `human-reviewed`

A concept is stale when the current date is on or after `stale_after`. Commands
must offer `--today YYYY-MM-DD` for deterministic tests and automation.

## 6. Command specification

### 6.1 `okf init`

Create the minimal files needed to mark a directory as an OKF bundle.

```bash
okf init [path] [--version 0.2]
```

Behavior:

- Create the target directory if necessary.
- Create a root `index.md` containing the `okf_version` frontmatter and an empty
  `# Concepts` section.
- Default to OKF version `0.2`.
- Refuse to overwrite an existing `index.md`.
- Do not initialize Git or create a predefined content hierarchy.

Example output:

```markdown
---
okf_version: "0.2"
---

# Concepts
```

The user or agent adds the index body directly when a navigational listing is
needed.

### 6.2 `okf new`

Create a concept document with valid frontmatter and an empty body.

```bash
okf new <concept-id> --type <type> [options]
```

Supported options:

- `--title <text>`
- `--description <text>`
- `--resource <uri-or-path>`
- `--tag <tag>`, repeatable
- `--status draft|stable|deprecated`
- `--generated-by <actor>`
- `--dry-run`

Behavior:

- Require a non-empty `type`.
- Create missing parent directories.
- Append `.md` when the concept ID omits it.
- Reject reserved filenames and paths outside the bundle root.
- Refuse to overwrite an existing file.
- If `--generated-by` is supplied, write `generated.by` and the current UTC
  time as `generated.at`.
- Write an empty Markdown body after the closing frontmatter delimiter.
- Do not update `index.md` or `log.md`.

The CLI must not invent a default actor. An actor may instead be supplied by an
optional user-level configuration or environment variable, but the effective
value must be visible in `--dry-run` output.

### 6.3 `okf list`

List concepts using frontmatter without printing their bodies.

```bash
okf list [path] [filters]
```

Filters:

- `--type <type>`
- `--tag <tag>`
- `--status draft|stable|deprecated`
- `--trust unverified|machine-confirmed|human-reviewed`
- `--stale`
- `--due-before YYYY-MM-DD`

Default text output should include concept ID, type, title, status, trust tier,
and freshness. `--format json` returns structured records.

### 6.4 `okf show`

Read a concept without modifying it.

```bash
okf show <concept-id>
```

Text output is the complete source file. JSON output contains the concept ID,
`identity` (the SHA-256 of the file's bytes, for use with
`okf body set --if-match`), the parsed frontmatter, the body, and the complete
source.

Use `okf meta get` to read only the frontmatter and `okf body get` to read only
the body.

### 6.5 `okf search`

Search concept IDs, metadata, and Markdown bodies.

```bash
okf search <query> [filters]
```

Searchable fields:

- Concept ID
- `type`, `title`, `description`, and `tags`
- Markdown body text

The command supports the same metadata filters as `okf list`, plus:

- `--metadata-only` to exclude body text
- `--case-sensitive`
- `--limit <n>` to return at most `n` results

Queries are treated as literal text. Regular-expression syntax is out of scope
for the core CLI.

Results include the concept ID and a short matching excerpt. JSON results must
identify the matched field and, for body matches, the line number.

Results follow concept ID order. With `--limit`, the command returns the first
`n` results in that order and, when more matches exist, writes a note to
standard error. JSON output remains an array of results.

### 6.6 `okf meta`

Inspect or mutate YAML frontmatter only.

```bash
okf meta get <concept-id> [key]
okf meta set <concept-id> <key> <value> [--type string|number|boolean|date|json]
okf meta unset <concept-id> <key>
```

Without a key, `meta get` returns the complete frontmatter, printed as YAML in
text output.

Requirements:

- Support dotted paths such as `generated.by`.
- Parse `--type json` as a JSON object or array for structured values.
- Require explicit `--type` for ambiguous scalar values.
- Prevent removal or emptying of the required `type` field.
- Prevent frontmatter mutation on reserved files through this command.
- Preserve the body byte-for-byte.

Higher-level commands below are preferred for metadata families with semantic
rules.

### 6.7 `okf body`

Read or replace a concept's Markdown body.

```bash
okf body get <concept-id>
okf body set <concept-id> [--from-file <path>] [--if-match <sha256>] [--warnings-as-errors]
```

`body get` prints the body exactly as stored. JSON output contains the concept
ID, `identity`, and the body.

`body set` reads the new body from the file named by `--from-file`, or
otherwise from standard input, and requires valid UTF-8. Without
`--from-file`, it refuses to run when standard input is a terminal, so it never
waits for interactive input. The body is written exactly as supplied.

A person or agent can therefore save the output of `body get` to a file, edit
the file, and write it back with `body set --from-file`. Writing back an
unedited copy leaves the concept unchanged.

`body set` requirements:

- Preserve the frontmatter bytes exactly; the frontmatter is not re-serialized.
- Reject reserved files and concepts whose frontmatter cannot be parsed.
- With `--if-match`, refuse the write unless the SHA-256 of the current file
  equals the supplied value.
- Before writing, run link and citation checks on the proposed body and report
  the warnings the current body does not already have. Warnings are matched by
  code and message because line numbers shift. With `--warnings-as-errors`,
  new warnings leave the file unchanged and the command exits with code `1`.
- Write atomically with concurrent-change detection.

`body set --dry-run` prints a unified diff. With `--format json`, `body set`
returns `path`, `changed` (whether the file was or would be changed),
`identity` (the SHA-256 of the file after the command), and `warnings`; a dry
run also returns the proposed `body`.

### 6.8 `okf tag`

Manage the frontmatter `tags` list.

```bash
okf tag list [path] [--counts]
okf tag add <concept-id> <tag>...
okf tag remove <concept-id> <tag>...
```

`tag list` returns distinct tags in lexical order. The optional path limits the
scan to a concept or directory. Default text output prints one tag per line;
`--counts` reports the number of concepts using each tag. JSON output is an
array of tag strings by default and an array of objects containing `tag` and
`count` when `--counts` is used. An empty result is successful and produces no
text, or an empty JSON array.

Tags are deduplicated while preserving their existing order. Removing a tag
that is not present is a successful no-op.

### 6.9 `okf source`

Manage provenance entries without editing body citations.

```bash
okf source list <concept-id>
okf source add <concept-id> --resource <value> [options]
okf source remove <concept-id> (--id <id> | --resource <value>)
```

Optional fields for `source add`:

- `--id <id>`
- `--title <text>`
- `--author <actor>`
- `--usage-count <integer>`
- `--last-modified YYYY-MM-DD`
- `--usage-from YYYY-MM-DD --usage-to YYYY-MM-DD`

The command validates field shapes and rejects duplicate source IDs within a
concept. It does not add, remove, or rewrite footnotes in the Markdown body.

### 6.10 `okf verify`

Append a verification event to frontmatter.

```bash
okf verify <concept-id> --by <actor> [--at <ISO-8601-datetime>]
```

`--at` defaults to the current UTC time. The command normalizes a bare
`verified` mapping to a list when appending a second event. It must not delete
earlier events.

### 6.11 `okf lifecycle`

Update or report lifecycle metadata.

```bash
okf lifecycle set <concept-id> status draft|stable|deprecated
okf lifecycle set <concept-id> stale_after YYYY-MM-DD
okf lifecycle unset <concept-id> status|stale_after
okf lifecycle report [--stale] [--due-before YYYY-MM-DD]
```

`set` and `unset` accept only the lifecycle keys `status` and `stale_after`,
named as they appear in frontmatter. `set` rejects a status outside
`draft`, `stable`, and `deprecated` and a stale date that is not
`YYYY-MM-DD`. `unset` removes the key; a concept without `status` is treated
as `stable`.

The report is read-only and includes concept ID, status, stale date, and the
latest verification date when present.

### 6.12 `okf links`

Inspect Markdown links without changing them.

```bash
okf links outgoing <concept-id>
okf links backlinks <concept-id>
okf links check [path]
```

Requirements:

- Resolve bundle-relative and document-relative links.
- Ignore external URLs when checking target existence.
- Exclude links inside fenced code blocks.
- Report source concept, line number, original destination, and resolved path.
- Treat broken links as warnings, consistent with OKF conformance rules.
- Never rewrite a link or file.

### 6.13 `okf move`

Move one concept and mechanically preserve internal references.

```bash
okf move <source-concept> <destination-concept> [--dry-run]
```

The source and destination use normalized concept IDs. The destination may be
in another directory, and missing parent directories are created during the
successful transaction.

The command must perform a complete preflight before writing any file:

1. Resolve and validate the source and destination paths.
2. Reject reserved files, a missing source, an existing destination, a
   destination outside the bundle, and directory moves.
3. Scan every Markdown file in the bundle, including reserved files, for
   internal links affected by the move.
4. Scan standard OKF path-valued frontmatter fields for affected internal
   references.
5. Recalculate relative internal links and path-valued fields inside the moved
   concept so they continue to resolve to their original targets from the new
   directory.
6. Parse and validate every file that would be changed.
7. Build the complete change plan and capture the original content identity of
   every affected file.

The following frontmatter fields are eligible for mechanical path rewriting:

- `resource`
- `sources[].resource`
- `computation`
- `executor.resource`
- `attester.resource`

A field is rewritten only when its value can be unambiguously resolved as an
internal bundle path. External URLs and non-path scope descriptors remain
unchanged.

Markdown rewriting requirements:

- Rewrite a link in any Markdown body when its pre-move resolved target is the
  source concept.
- In the moved concept, rewrite other relative links when necessary to keep
  their pre-move resolved targets unchanged.
- Support inline and reference-style standard Markdown links.
- Preserve whether a destination was bundle-relative or document-relative.
- Preserve link labels, optional titles, query strings, and fragments.
- Apply source-span replacements only to affected destinations so all other
  body bytes remain unchanged.
- Do not rewrite external URLs, links in fenced or indented code blocks, raw
  HTML, or path-like plain text.
- Permit targeted destination rewrites in `index.md` and `log.md`, but do not
  otherwise modify their authored content.

`--dry-run` must print the source and destination plus every proposed file and
field or link change. With `--format json`, it must return a structured move
plan suitable for review by an agent. A dry run never creates directories,
temporary files, or transaction state.

Execution must stage and apply the move and all rewrites as one recoverable
transaction. Immediately after applying it, the command runs the equivalent of
`okf links check` and `okf validate` for affected files. A failure restores the
original files and source location. The command must never report success for
a partially applied move.

Warnings that existed before the move do not block it. A new unresolved link,
ambiguous path, unsupported link form that appears to target the source, or
concurrent file change aborts the move before commit and leaves the bundle
unchanged.

The move does not update `generated`, `verified`, `status`, or `stale_after`:
relocation is not a meaningful content change or a new verification event.

### 6.14 `okf citations check`

Check the relationship between body footnotes and `sources[].id` values.

```bash
okf citations check [path]
```

Warnings include:

- A body footnote label with no matching source ID.
- A source ID that is not referenced by a body footnote.
- Duplicate source IDs.
- Duplicate footnote definitions.

An unused source is advisory because a source may support the concept as a
whole rather than a single claim.

### 6.15 `okf validate`

Validate a bundle, directory, or concept.

```bash
okf validate [path] [--format text|json]
```

Conformance errors include:

- A non-reserved Markdown file has missing or invalid YAML frontmatter.
- A concept has a missing, empty, or non-string `type`.
- A reserved `index.md` or `log.md` violates the applicable OKF structure.

Warnings include:

- A present standard field has an invalid shape or value.
- An `Attested Computation` has no valid `runtime` or has an invalid contract.
- Broken internal links.
- Invalid or unmatched citation references.
- A concept is stale.
- A deprecated concept is linked from a stable concept.
- A path-valued field points to a missing internal file.
- Recommended metadata such as `title` or `description` is absent.

Validation of Attested Computation concepts is structural only. The CLI checks
the declared contract, parameter definitions, computation location, and the
presence and shape of executor and attester metadata. It never executes them.

## 7. Shared command behavior

### 7.1 Output formats

Read and validation commands support:

- `--format text`, the human-readable default
- `--format json`, a stable machine-readable representation

JSON output must be written to stdout. Diagnostics not represented in the JSON
document must be written to stderr.

### 7.2 Exit codes

The CLI uses stable exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Success; validation may contain warnings only |
| `1` | Validation or requested check found one or more errors |
| `2` | Invalid command usage or arguments |
| `3` | Filesystem, parsing, or unexpected operational failure |

For `links check` and `citations check`, warnings do not produce exit code `1`
unless `--warnings-as-errors` is explicitly supplied.

### 7.3 Atomic writes and concurrency

Mutating commands must write a temporary file in the same directory and rename
it over the target. Before replacement, the command must ensure that the source
file has not changed since it was read. If it has changed, the operation fails
without overwriting either version.

`okf move` is a multi-file operation and therefore requires a recoverable
transaction strategy:

1. Record content identities for every source file during preflight.
2. Stage every rewritten file before replacing any original.
3. Recheck all recorded identities immediately before commit.
4. Keep recoverable originals until the move and all replacements succeed.
5. Roll back completed steps if any later step fails.

Temporary transaction state must remain inside the bundle, must not be treated
as knowledge content, and must be removed after success or rollback. If the
process is interrupted and automatic rollback cannot finish, the next mutating
command must detect the incomplete transaction and refuse unrelated writes
until recovery is completed. The implementation must document the recovery
command or automatic recovery behavior.

### 7.4 Paths and safety

- No command may read or write through a resolved path outside the bundle root
  unless the user supplied that external path explicitly to a read-only
  command or as the input file of `okf body set --from-file`.
- Symbolic links that escape the bundle are not traversed during bundle scans.
- Metadata and body mutation commands reject reserved files. The only
  reserved-file writes are root `index.md` creation by `okf init` and targeted
  link destination rewrites by `okf move`.
- Destructive metadata operations require an explicit command; there is no
  wildcard mutation mode.

### 7.5 Time handling

- Stored datetimes use ISO 8601 in UTC unless the user explicitly supplies an
  offset.
- Stored lifecycle dates use `YYYY-MM-DD`.
- Date-based reports use the local date by default and accept `--today` for
  deterministic evaluation.

## 8. Configuration

The CLI should require no project-specific configuration. Optional user-level
configuration may provide defaults such as:

- Actor identity for `generated.by` and `verified.by`
- Default output format
- Date display preference

Configuration must never define custom conformance rules that make a portable
OKF bundle valid only for one installation. Repository-specific policy belongs
in separate CI scripts or future policy extensions, not in the core CLI.

## 9. Performance expectations

The initial implementation may scan files on every command. It should:

- Stream file discovery rather than loading the entire bundle into memory.
- Parse frontmatter only for metadata listings and filters.
- Read bodies only for full-text search, link analysis, citation checks, full
  validation, and body writes.
- Produce deterministic ordering by normalized concept ID.

An optional cache may be introduced only after measurement shows it is needed.
It must not change command semantics or become required bundle state.

## 10. Recommended implementation order

### Phase 1: File model and read-only inspection

Implement the shared foundation first:

1. Bundle-root discovery and safe path resolution.
2. Concept ID normalization and reserved-file handling.
3. Frontmatter/body boundary parsing that preserves raw body bytes.
4. `okf init`.
5. `okf show` and `okf list` with text and JSON output.

This phase establishes the file model and makes real bundles inspectable before
introducing mutations.

### Phase 2: Concept creation and safe frontmatter and body mutation

1. Atomic writes and concurrent-change detection.
2. `okf new`.
3. `okf meta get`, `set`, and `unset`.
4. `okf tag add` and `remove`.
5. `okf body set`.
6. `--dry-run` for every mutating command.

Tests in this phase must prove that frontmatter mutations leave the Markdown
body byte-for-byte unchanged and preserve unknown fields, and that body writes
leave the frontmatter byte-for-byte unchanged.

### Phase 3: OKF conformance validation

1. Core concept and reserved-file validation.
2. Standard provenance, trust, and lifecycle field validation.
3. Attested Computation structural validation.
4. Text and JSON diagnostics with stable exit codes.
5. `--warnings-as-errors` behavior for CI usage.

Validation should be implemented before higher-level metadata commands so
every later feature can reuse the same rules.

### Phase 4: Discovery and relationship inspection

1. `okf search` with metadata and body matches.
2. `okf links outgoing` and `backlinks`.
3. `okf links check`.
4. `okf citations check`.

All features in this phase are read-only and must report locations precisely
enough for a person or agent to edit the source file directly.

### Phase 5: Safe concept moves

1. Produce a read-only move plan from the link model built in Phase 4.
2. Rewrite incoming links to a moved concept.
3. Recalculate relative links inside the moved concept.
4. Rewrite affected standard path-valued frontmatter fields.
5. Add multi-file staging, concurrent-change detection, rollback, and recovery.
6. Run post-move validation and link checks.
7. Add text and JSON `--dry-run` contract tests.

Begin with one concept per invocation. Directory moves and batch moves remain
out of scope. Tests must verify that only parsed destinations and eligible
frontmatter values change; all surrounding Markdown and metadata must remain
byte-for-byte identical.

### Phase 6: Trust, provenance, and lifecycle workflows

1. `okf source list`, `add`, and `remove`.
2. `okf verify`.
3. `okf lifecycle` mutation and reporting commands.
4. Trust-tier and freshness filters across `list` and `search`.

These commands should reuse the mutation and validation foundations rather
than implementing separate YAML rewrite paths.

### Phase 7: Hardening and scale

1. Large-bundle performance tests using real bundle fixtures.
2. Cross-platform path and newline tests.
3. Crash-safety and concurrent-edit tests.
4. Stable JSON schema documentation and compatibility tests.
5. Optional disposable caching only if profiling demonstrates a need.

## 11. Minimum viable release

The minimum useful release consists of:

```text
okf init
okf new
okf show
okf list
okf meta get|set|unset
okf validate
okf search
okf links outgoing|backlinks|check
okf move
```

This set is sufficient to create a bundle, maintain frontmatter, find
knowledge, inspect relationships, safely reorganize concepts, and verify the
result. Bodies can be edited with ordinary filesystem tools before `okf body`
is available.
