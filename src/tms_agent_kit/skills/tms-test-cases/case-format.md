# Case file format

A case file is Markdown with optional YAML frontmatter. The parser is strict: unknown headings, text outside sections and unknown frontmatter keys are errors with line numbers.

## Frontmatter

```yaml
---
tms: testit                 # read-only from here ...
id: 6f1c2e3a-...
global_id: 1042
version_id: 9a7b...
version_number: 7
modified_date: '2026-11-01T10:15:00Z'   # ... to here
type: TestCases             # TestCases | CheckLists | SharedSteps
state: Ready                # NotReady | Ready | NeedsWork
priority: Medium            # Lowest | Low | Medium | High | Highest
duration_ms: 300000         # > 0 for TestCases
tags: [auth, smoke]
attributes:
  Component: Login          # project attributes by name
automated: false            # read-only
---
```

- All keys are optional for a new case. Defaults: `TestCases`, `NotReady`, `Medium`. `duration_ms` is required for `TestCases`.
- `type`, `state` and `priority` are case-insensitive.
- `attributes` uses the attribute names that pulled cases show. The tool does not check them; an unknown name is sent as is and the TMS may reject it.
- `lossy: true` marks a file the tool cannot represent exactly. Edits to the affected fields are blocked; leave such fields alone.

## Body

```markdown
# <case name>

## Description

Free text.

## Preconditions

### Step 1

#### Action

...

## Steps

### Step 1

#### Action

...

#### Expected

...

#### Test data

...

#### Comments

...

### Step 2

Shared step: 1003

## Postconditions

### Step 1

#### Action

...
```

- The `# ` heading is the case name. It must be the first heading.
- Section order is fixed: Description, Preconditions, Steps, Postconditions. Omit empty sections, except Steps.
- Steps are numbered in file order. The number in `### Step N` is informational; pull rewrites it.
- Step fields: `Action`, `Expected`, `Test data`, `Comments`, in any order. Text in a step must be under a field heading. Omit empty fields.
- A shared step is a step whose whole body is `Shared step: <globalId>`.
- Text is Markdown and is stored in the TMS as is. Lists, code, bold and links are fine.
- A text line starting with `#` is written as `\#...`, so it cannot become a heading. Lines inside code fences are not escaped.
- Cases edited in the TMS web editor may contain HTML (`<p class="tiptap-text">`). Keep it valid when editing; do not convert it unless asked.

## Files and folders

- Name: `<globalId>-<slug>.md`, set by pull and push. A new case can have any name; push renames it.
- A new file named `<globalId>-...` with the global id of an existing case is rejected: it looks like that case with its frontmatter lost.
- The folder is the section. A folder that is not a section yet is created as one on push, with its missing parents. Folder names must survive a round trip (no trailing dots or spaces).
