import { defineConfig, devices } from "@playwright/test";

// End-to-end tests drive the BUILT UI as `runcomposer serve` serves it — the
// bundle committed under src/runcomposer/ui_dist, not a dev server. Every
// test starts its own server on a free port in a temp directory (see
// e2e/fixtures.ts), so tests share nothing and can run in parallel.
//
// No retries: a test that only passes the second time is a flaky test, and
// the point of this suite is to notice.
export default defineConfig({
  testDir: "./e2e",
  outputDir: "./test-results",
  timeout: 120_000,
  expect: { timeout: 20_000 },
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: process.env.CI ? 2 : 4,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  use: {
    ...devices["Desktop Chrome"],
    viewport: { width: 1440, height: 900 },
    locale: "en-US",
    timezoneId: "UTC",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
