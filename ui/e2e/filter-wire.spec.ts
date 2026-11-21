// A filter that is a single pattern travels as its quoted text: the server
// reads a lone string as filter text, and a pattern with parentheses in it
// would otherwise reach it only through the fallback for strings that are
// not filter text — with a warning on every preview.

import { addRule, currentRunId, expect, expectPreview, runButton, test } from "./fixtures.ts";

test("a pattern with parentheses previews and runs as exactly that pattern, with no warning", async ({ page, open, api }) => {
  await open("#/compose");
  await addRule(page, "regex:^Cart(V2)?$");
  await expectPreview(page, 9, 60);
  await expect(page.locator(".preview .notice-warn")).toHaveCount(0);

  await runButton(page, 9).click();
  const runId = await currentRunId(page);
  const run = await api.get<{ selection: { tag_filter: unknown }; planned_count: number }>(`/runs/${runId}`);
  // The run spec stores the pattern itself — never the quotes it travelled in.
  expect(run.selection.tag_filter).toBe("regex:^Cart(V2)?$");
  expect(run.planned_count).toBe(9);
});
