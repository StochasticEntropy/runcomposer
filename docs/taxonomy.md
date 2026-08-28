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
  taxonomy_file: taxonomy.yaml     # path relative to the config file's directory
```

With no `taxonomy_file` configured, runcomposer serves the bundled demo
taxonomy, so the panel is never empty on a fresh install. The file is served
at `GET /api/v1/taxonomy`, parsed and otherwise untouched — the UI receives
exactly what you wrote.

---

## The shape

One top-level key, `taxonomy:`, holding a list of nodes. A node has three
possible keys:

| Key | Required | Meaning |
|---|---|---|
| `label` | yes | The text shown in the tree. Displayed verbatim — it is your string, not a translation key, and it is the same in every locale. Keep sibling labels distinct; the UI keys nodes by label. |
| `filter` | no | **One** pattern string in the tag-filter grammar. A node that has it is a **leaf**: clickable, and clicking it puts that pattern into the filter builder. |
| `children` | no | A list of further nodes. Nesting is arbitrarily deep. |

A node with `children` and no `filter` is a **group** — a heading that only
opens. A node with `filter` and no `children` is a plain leaf, at any level
including the top. A node may carry both: it is then clickable *and* has
children under it.

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

## What a click does

Clicking a leaf appends its pattern to the filter builder as one rule, where
it stays editable. Clicking a second leaf appends a second rule; how
the two combine is the builder's own glue (AND by default), which the user can
change there. Nothing about the tree is recorded in the run spec: the spec
keeps the resulting `selection.tag_filter`, not the node it came from.

---

## When the panel comes up empty

Nothing validates this file. It is parsed and served as it is, so a document
in the wrong shape produces a perfectly successful request and a panel with
nothing in it — no error in the UI, nothing in the log but a `200`. If the
taxonomy panel is empty, read what is actually being served:

```bash
curl -s localhost:8100/api/v1/taxonomy
```

It must be a mapping with a `taxonomy` key holding a **list of nodes**; the UI
reads `body.taxonomy` and renders an empty tree when that key is missing. The
two silent causes:

1. the top-level key is something else — `nodes:`, `tree:`, `groups:`. The
   request returns `200` with your document in it, and nothing renders.
2. a node spells the pattern differently — `tags:`, `pattern:`, `rule:`. Only
   `filter` is read, so the node renders as a non-clickable group instead of a
   leaf.

The neighbouring mistakes fail loudly instead, which is worth knowing so you
can tell them apart: a `taxonomy_file` that does not exist relative to the
config file, an empty file, and a file whose top level is a bare list all make
`GET /api/v1/taxonomy` answer `500`, and the UI shows an error banner. The
bundled demo taxonomy is served only when `taxonomy_file` is unset — never as
a fallback for a file that is broken.

`GET /api/v1/taxonomy` re-reads the file per request, so an edit shows up on
the next page load. Treat that as a convenience rather than a promise —
hot-reload is explicitly out of scope (DESIGN.md §13), and nothing else in the
config behaves this way.
