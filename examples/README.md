# Examples

- **`webshop-regression.runspec.yaml`** — a complete, dispatchable runspec 1.0
  document composed against the bundled demo corpus. It is kept *true*: a test
  re-compiles its `tag_filter` against the corpus and asserts the embedded
  `materialized.item_ids` and `source.snapshot` match exactly. Try:

  ```
  runcomposer validate --for-dispatch examples/webshop-regression.runspec.yaml
  ```

- **`manifest-pytest.json`** — a pytest-flavored manifest for the `manifest`
  TestSource (DESIGN.md §6.1): item ids are pytest nodeids, opaque to the
  core. Together with the Robot-Framework-shaped ids in the demo corpus
  (`Shop.Payments.Cards.T001`), this demonstrates the framework-agnostic
  claim rather than asserting it. Try:

  ```
  runcomposer catalog --manifest examples/manifest-pytest.json
  ```
