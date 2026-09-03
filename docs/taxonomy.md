# The taxonomy file

The taxonomy is the curated tree the UI shows on the left: the way *your* team
talks about the corpus, laid over the flat tag space. It is data, not code —
one YAML file you write and runcomposer serves.

This page is the format. For everything around it, see
[ADOPTING.md](../ADOPTING.md); for what a taxonomy is *for*,
[DESIGN.md](../DESIGN.md) §2.

---

## Where it lives

```yaml
core:
  taxonomy_file: taxonomy.yaml     # relative to the config file's directory
```

That is the rule for *every* relative path in a config file — the `core:` keys
and the plugin sections both — so the taxonomy travels with the config
directory rather than depending on where the command was typed
([docs/cli.md](cli.md#--config--the-only-way-to-point-at-another-config-file)).

With no `taxonomy_file` configured, runcomposer serves the bundled demo
taxonomy, so the panel is never empty on a fresh install. The file is served
at `GET /api/v1/taxonomy`, validated (see
[below](#what-is-validated-and-when)) and otherwise untouched — that response
is exactly what you wrote. The UI asks for `?resolve=true` instead, which is
the same tree with every pattern replaced by the catalog tags it covers
([below](#resolution-the-tree-the-ui-renders)).

---

## The shape

One top-level key, `taxonomy:`, holding a list of nodes. A node has three
possible keys:

| Key | Required | Meaning |
|---|---|---|
| `label` | yes | The text shown in the tree. Displayed verbatim — it is your string, not a translation key, and it is the same in every locale. Keep sibling labels distinct — the validator requires it, and it is what lets the resolved tree number nodes by their position in the file. |
| `filter` | no | **One** pattern string in the tag-filter grammar. A node that has it is a **leaf**: clickable, and clicking it puts that pattern into the filter builder. |
| `children` | no | A list of further nodes. Nesting is arbitrarily deep. |

A node with `children` and no `filter` is a **group** — a heading that only
opens. A node with `filter` and no `children` is a plain leaf, at any level
including the top. A node may carry both: it is then clickable *and* has
children under it. A node with **neither** is refused: it would render as a
heading that never opens, which nobody means on purpose.

Keys beyond those three are ignored, so you may annotate nodes for your own
tooling — but see the `pattern:` trap in
[what is validated](#what-is-validated-and-when).

---

## A worked example

```yaml
taxonomy:
  - label: Areas                                     # group: a heading, not clickable
    children:
      - { label: Payments, filter: "Payments" }      # literal tag — matched case-INsensitively
      - { label: Checkout, filter: "prefix:Checkout-" }   # every tag starting "Checkout-" (case-sensitive)
      - label: Cart                                  # both: clickable AND a parent
        filter: "regex:^Cart(V2)?$"
        children:
          - { label: Cart v2 only, filter: "CartV2" }
  - label: Suites
    children:
      - { label: Smoke, filter: "Smoke" }
      - { label: Regression, filter: "Regression" }
      - { label: Payment methods, filter: "regex:^(Visa|Mastercard|Paypal)$" }   # "any of these" — one regex
  - label: Quarantine                                # a leaf needs no children, at any level
    filter: "prefix:Quarantine-"
```

A shipped, working file lives in the package at
`src/runcomposer/demo/taxonomy.yaml` — that is what `runcomposer demo` and a
zero-config `runcomposer serve` render.

---

## The pattern in a leaf

`filter` is a pattern in the same grammar the run spec uses
(DESIGN.md §3.1), matched against an item's **tags** — never its id, name, or
hierarchy:

| Pattern | Matches | Case |
|---|---|---|
| `Payments` | a tag equal to `Payments` | **insensitive** |
| `prefix:Checkout-` | a tag starting with `Checkout-` (sugar for `regex:^Checkout-`) | **sensitive** |
| `regex:^Cart(V2)?$` | a tag matching the expression (`re.search`, so anchor it yourself) | **sensitive** |

That asymmetry is the one thing to remember: literals ignore case, `regex:`
and `prefix:` do not. A tree ported from a case-insensitive tool goes quietly
empty — every leaf clicks, and nothing matches. The opt-out is inline:
`regex:(?i)^checkout-`.

---

## Known limitation: one pattern per leaf, and only a pattern

A leaf carries a single pattern **string**. The tree's consumer is the UI, and
what it does with a leaf is hand `node.filter` to the filter builder as one
pattern — so a list of tags and a filter AST, both perfectly good *spec*
content, are not usable here:

```yaml
# NOT supported — `filter` must be a single pattern string
- { label: Payment methods, filter: ["Visa", "Mastercard"] }
- { label: Payment methods, filter: { op: OR, items: ["Visa", "Mastercard"] } }
```

So "any of these tags" is written as one alternation regex:

```yaml
- { label: Payment methods, filter: "regex:^(Visa|Mastercard|Paypal)$" }
```

Anything with real boolean structure — an AND across two tags, an exclusion —
belongs in the filter builder, where the user composes it, and it is the
composed filter that ends up in `selection.tag_filter`. The tree is navigation
into the builder, not a second selection language.

---

## Resolution: the tree the UI renders

One pattern per leaf keeps the file writable, and it is also what makes a leaf
opaque: `regex:^Cart(V2)?$` renders as **one** row, and the tags it stands for
— `Cart`, `CartV2` — have no row of their own, so neither can be picked. Over
a real corpus that is most of the tag space hidden behind a handful of
patterns.

So the tree is served twice. `GET /api/v1/taxonomy` returns the file, exactly
as before. `GET /api/v1/taxonomy?resolve=true` returns it **resolved against
the catalog** — and that is what the UI asks for:

- every node keeps its own `label` and `filter` and gains **one child per
  concrete catalog tag its own pattern matches**, each a leaf whose `filter`
  is that one tag;
- a node whose whole subtree matches nothing in the current catalog is
  **dropped**, instead of rendering as a leaf that clicks and selects nothing;
- the tags **no** node anywhere claims are gathered under one synthetic node,
  so every tag in the catalog is reachable and a tag introduced tomorrow shows
  up rather than being merely absent.

```console
$ runcomposer taxonomy-check --tree
Areas  (60 item(s), 6 tag(s))
  Cart  regex:^Cart(V2)?$  (9 item(s), 2 tag(s))
    Cart  Cart  (5 item(s), 1 tag(s))
    CartV2  CartV2  (4 item(s), 1 tag(s))
…
```

**The file format does not change.** Resolution is derived, never stored: the
same file over a different catalog resolves differently, and nothing about it
is written back.

### The shape

Resolved nodes speak the same three keys the file is written in — so the
result is itself a valid taxonomy document — plus additive metadata:

| Key | Meaning |
|---|---|
| `label`, `filter`, `children` | As in the file. A tag node's `filter` is that one tag. |
| `id` | Stable key, spelled as the node's path in the document (`taxonomy[0].children[2]`, the same path the [validator's messages](#what-is-validated-and-when) use), with `.tags[n]` for a synthesized tag node. Written nodes are numbered by position in the **file**, so a collapsing sibling does not renumber the rest. |
| `origin` | `file` (written), `tag` (synthesized for one tag), `unassigned` (the synthetic node). |
| `tag_count`, `item_count` | Distinct catalog tags and items under the node — its own pattern and its whole subtree together. Clicking still applies the node's **own** pattern. |

Alongside `taxonomy`, the response carries a `resolved` summary:
`tags_total`, `tags_claimed`, `tags_selectable`, `tags_unassigned`,
`items_total`, `items_claimed`, `nodes_written`, `nodes_dropped`,
`nodes_total`.

### The three rules worth knowing

**A tag a descendant already claims is not repeated on the parent.** It
belongs to the more specific node; listed twice in one branch it reads as two
different things.

```yaml
- label: Cart
  filter: "regex:^Cart(V2)?$"
  children:
    - { label: Cart v2 only, filter: "CartV2" }
```

resolves to `Cart` → [`Cart v2 only`, `Cart`] — `CartV2` stays where you put
it.

**A pattern that resolves to exactly one tag stays a plain leaf.** The node
already *is* that tag's node; a single identical child would be noise.

**A tag some other tag differs from only in case gets an escaped regex, not a
literal.** Literals match case-insensitively ([above](#the-pattern-in-a-leaf)),
so a corpus carrying both `Adapter` and `ADAPTER` would otherwise get two
nodes that both select both. Each gets `regex:^Adapter$` / `regex:^ADAPTER$`
instead, and means itself. (The same escaping covers a tag literally spelled
`regex:…` or `prefix:…`.)

### A catch-all leaf resolves to the whole tag space

A leaf written as `filter: "regex:.*"` — the usual way to make sure nothing is
invisible — claims **every** tag, so after resolution it carries a child for
every tag in the catalog, and the synthetic unassigned node is empty because
nothing is left over. That is faithful, and usually not what you want any
more: resolution already guarantees reachability, so the catch-all can go and
the tags it was covering will appear under the synthetic node instead, where
`taxonomy-check` can also tell you they are unclaimed.

---

## What a click does

Clicking a leaf puts its pattern into the filter builder as one rule, where it
stays editable. Clicking a second leaf adds a second rule; how the two combine
is the builder's own glue (AND by default), which the user can change there.
Nothing about the tree is recorded in the run spec: the spec keeps the
resulting `selection.tag_filter`, not the node it came from.

A leaf is a **switch**, not an append-only button: clicking one whose pattern
is already in the filter takes that pattern back out instead of adding it a
second time, and every leaf currently in the filter is marked in the tree.

The mark follows the **pattern**, not the position. In the resolved tree that
is the only reading that holds: a tag hangs under every node whose pattern
covers it, so one filter condition can legitimately be several rows — all of
them show as active, and clicking any of them switches it off. Two patterns
that merely overlap (a literal and an alternation regex containing it) stay
independent: switching one on does not mark the other.

Since a branch has to be opened to be seen, a **closed** row carries the
number of distinct active patterns switched on somewhere below it, so nothing
active is invisible.

The same conditions are listed above the builder as chips, each with a `✕`
that removes just that one; a chip for a nested group removes the group. The
`Clear` link still empties the whole filter.

---

## What is validated, and when

This file is checked, and a wrong-shaped one fails loudly instead of
rendering as an empty panel. The rules:

| Rule | Refused example |
|---|---|
| the document is a mapping with a top-level `taxonomy` key | `nodes:`, `tree:`, `groups:` at the top; a bare list of nodes; an empty file |
| `taxonomy` holds a **list** of nodes | `taxonomy: {label: Areas}` |
| every node is a mapping with a non-empty **`label`** string | `taxonomy: ["Payments"]` |
| `filter`, when present, is **one pattern string** the grammar can parse | `filter: ["Visa", "MC"]`, `filter: {op: OR, …}`, `filter: "regex:^(unclosed"` |
| `children`, when present, is a **list** | `children: {}` |
| sibling labels are **distinct** | two `- {label: Payments, …}` under one parent |
| every node has `filter`, `children`, or both | `- {label: Payments, pattern: "Payments"}` |

That last rule is what catches the trap: only `filter` is read, so a node
spelled `pattern:`, `tags:` or `rule:` would otherwise render as a
non-clickable heading and look like a *layout* mistake rather than a typo.
Because such a node has no `children` either, it is refused — and the message
names the key you actually wrote:

```
taxonomy.yaml: taxonomy[0].children[0] ('Payments') has neither 'filter' nor
'children', so it would render as a heading that never opens — did you mean
'filter'? (this node's other key(s): ['pattern'])
```

Every message names the offending node by its path in the document, so you can
go straight to the line.

**Checked in two places**, for one reason each:

- **at startup** — a broken taxonomy stops `runcomposer serve` with that
  message and exit code 2, the same way an unknown plugin id does
  (DESIGN.md §8). A server that boots healthy and serves a broken tree is
  precisely what this exists to prevent.
- **on every request** — `GET /api/v1/taxonomy` re-reads the file per request,
  so it can break under a running server. Then the response is a `500`
  carrying the same message, and the UI shows an error banner. Never a `200`
  with an empty tree.

Per-request re-reading means an edit shows up on the next page load. Treat
that as a convenience rather than a promise — hot-reload is explicitly out of
scope (DESIGN.md §13), and nothing else in the config behaves this way. The
bundled demo taxonomy is served only when `taxonomy_file` is unset — never as
a fallback for a file that is broken.

If something still looks wrong, read what is actually being served:

```bash
curl -s localhost:8100/api/v1/taxonomy
```

What remains *legal but useless* is a tree whose patterns match nothing — a
valid document renders a valid, empty-looking tree if every leaf's pattern is
wrong. That is the case-sensitivity trap above, not a shape problem: check a
leaf's pattern with `runcomposer compile 'prefix:Checkout-'`.
