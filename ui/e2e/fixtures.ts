// What every end-to-end test stands on: a real `runcomposer serve`, started
// for that test alone on a free port, over a config in a temp directory —
// and the UI's own messages, so a test names a button the way the UI does
// instead of repeating its text.
//
// Nothing here waits on the clock. The server is ready when it answers its
// health endpoint; a run is over when the API says so.

import { execFile, spawn } from "node:child_process";
import type { ChildProcess } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { test as base, expect } from "@playwright/test";
import type { APIRequestContext, Page } from "@playwright/test";

import { createTranslator } from "../src/lib/i18n.ts";
import type { Translator, Vars } from "../src/lib/i18n.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
export const REPO = resolve(HERE, "..", "..");
export const ROBOT_SHOP = join(REPO, "examples", "robot-shop", "tests");
const EXEC_FILE = join(REPO, "src", "runcomposer_exec.py");
const VENV_PYTHON = join(REPO, ".venv", "bin", "python");
export const PYTHON = process.env.RUNCOMPOSER_PYTHON ?? (existsSync(VENV_PYTHON) ? VENV_PYTHON : "python3");

const run = promisify(execFile);

// -- messages ------------------------------------------------------------------------

const translators = new Map<string, Translator>();

/** The UI's own text for a key — tests read it from the locale file the UI ships. */
export function msg(key: string, vars: Vars = {}, locale = "en"): string {
  let translator = translators.get(locale);
  if (!translator) {
    const messages = JSON.parse(readFileSync(join(HERE, "..", "src", "locales", `${locale}.json`), "utf-8")) as Record<string, unknown>;
    translator = createTranslator(messages, locale);
    translators.set(locale, translator);
  }
  const text = translator.t(key, vars);
  if (text === key) throw new Error(`no message for ${key} in ${locale}`);
  return text;
}

// -- the server -----------------------------------------------------------------------

const DEMO_TAXONOMY = join(REPO, "src", "runcomposer", "demo", "taxonomy.yaml");
const STORE = { sqlite: { path: "runcomposer.db" } };

// A runner that is a command line: it reports every item it is given as
// passed. Its per-run parameters are declared in the config — one of each
// kind, so the form the UI renders from a declaration is exercised whole.
const SCRIPTED = {
  plugin: "command",
  format: "runcomposer-verdicts",
  command: [
    PYTHON,
    "-c",
    "import json, os, sys\n" +
      "ids = [line for line in open(sys.argv[1], encoding='utf-8').read().splitlines() if line]\n" +
      "verdicts = [{'name': name, 'status': 'PASS', 'duration_ms': 5} for name in ids]\n" +
      "json.dump({'format': 'runcomposer-verdicts', 'verdicts': verdicts}, open(os.path.join(sys.argv[2], 'results.json'), 'w'))\n",
    "{ids_file}",
    "{out_dir}",
  ],
  parameters: [
    { name: "environment", kind: "choice", label: "Target environment", choices: ["staging", "preview"], required: true, help: "Where the shop under test runs." },
    { name: "build", kind: "string", label: "Build number", required: true },
    { name: "retries", kind: "integer", label: "Retry count", default: 0 },
    { name: "headless", kind: "boolean", label: "Run headless", default: true },
    { name: "browsers", kind: "multi_choice", label: "Browsers to use", choices: ["chromium", "firefox", "webkit"], default: ["chromium"] },
  ],
};

/** Deployments the tests run against. Paths in a config are relative to the config file. */
export const CONFIGS = {
  // The bundled demo corpus (60 items). `demo` answers at once; `slow` takes
  // long enough per item to be watched and cancelled; `scripted` declares one
  // parameter of every kind; `strict` refuses every dispatch through a
  // failing readiness hook; `offline` reports itself unhealthy, with a reason.
  demo: {
    store: STORE,
    core: { artifact_dir: "artifacts" },
    runners: {
      demo: {},
      slow: { plugin: "demo", delay_s: 0.4 },
      scripted: SCRIPTED,
      strict: { plugin: "robot-pool", suite_root: ROBOT_SHOP, pre_run_hooks: ["exit 3"] },
      offline: { plugin: "robot-pool", suite_root: "no-such-directory" },
    },
  },
  // Real Robot Framework over the example suite, fanned out over two partitions.
  robot: {
    store: STORE,
    core: { artifact_dir: "artifacts", taxonomy_file: DEMO_TAXONOMY },
    sources: { robotframework: { root: ROBOT_SHOP } },
    runners: { "robot-pool": { suite_root: ROBOT_SHOP, partitions: ["chromium", "firefox"], max_workers: 2 } },
  },
  // The server's default language is German.
  german: { store: STORE, core: { locale_default: "de" }, runners: { demo: {} } },
  // Results may be pushed without a token, and no runner is configured:
  // a selection can only be exported.
  open: { store: STORE, core: { ingestion: { tokens: "disabled" } }, runners: {} },
  // A corpus of thousands, from a generated manifest (written by `largeManifest`).
  large: { store: STORE, sources: { manifest: { path: "corpus.json" } }, runners: { demo: {} } },
} as const;

export type ConfigName = keyof typeof CONFIGS;

/** 3,000 items of the web-shop world in 60 suites, with a tag per suite. */
export function largeManifest(): string {
  const areas = ["Payments", "Checkout", "Cart", "Catalog", "Auth"];
  const items = [];
  for (let index = 0; index < 3000; index += 1) {
    const area = areas[index % areas.length]!;
    const suite = Math.floor(index / 50) + 1;
    items.push({
      id: `Shop.${area}.Suite${suite}.T${String(index + 1).padStart(4, "0")}`,
      name: `${area} check ${index + 1}`,
      tags: [area, `${area}-${suite}`, index % 4 === 0 ? "Smoke" : "Regression", `Sprint-${12 + (index % 3)}`],
      hierarchy: ["Shop", area, `Suite${suite}`],
    });
  }
  return JSON.stringify({ items });
}

function freePort(): Promise<number> {
  return new Promise((resolvePort, reject) => {
    const probe = createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const address = probe.address();
      const port = typeof address === "object" && address ? address.port : 0;
      probe.close(() => resolvePort(port));
    });
  });
}

export class Server {
  readonly api: string;
  private logText = "";

  private constructor(
    readonly url: string,
    readonly directory: string,
    private readonly child: ChildProcess,
  ) {
    this.api = `${url}/api/v1`;
    child.stdout?.on("data", (chunk: Buffer) => (this.logText += chunk.toString()));
    child.stderr?.on("data", (chunk: Buffer) => (this.logText += chunk.toString()));
  }

  get log(): string {
    return this.logText;
  }

  static async start(config: ConfigName): Promise<Server> {
    const directory = mkdtempSync(join(tmpdir(), "runcomposer-e2e-"));
    // YAML is a superset of JSON: the config is written as JSON and read as YAML.
    writeFileSync(join(directory, "config.yaml"), JSON.stringify(CONFIGS[config], null, 2));
    if (config === "large") writeFileSync(join(directory, "corpus.json"), largeManifest());
    const port = await freePort();
    const child = spawn(PYTHON, ["-m", "runcomposer.cli", "serve", "--port", String(port), "--config", join(directory, "config.yaml"), "--log-level", "warning"], {
      cwd: directory,
      stdio: ["ignore", "pipe", "pipe"],
    });
    const server = new Server(`http://127.0.0.1:${port}`, directory, child);
    await server.ready();
    return server;
  }

  /** Resolves once the server answers its health endpoint. */
  private async ready(): Promise<void> {
    let exited: number | null | undefined;
    this.child.once("exit", (code) => (exited = code));
    await expect
      .poll(
        async () => {
          if (exited !== undefined) throw new Error(`runcomposer serve exited with ${exited}:\n${this.logText}`);
          try {
            return (await fetch(`${this.api}/health`)).status;
          } catch {
            return 0;
          }
        },
        { timeout: 60_000, intervals: [50, 100, 200], message: "runcomposer serve did not come up" },
      )
      .toBe(200);
  }

  async stop(): Promise<void> {
    if (this.child.exitCode === null) {
      const gone = new Promise((done) => this.child.once("exit", done));
      this.child.kill(); // the default signal asks it to terminate
      // A graceful stop waits for requests in flight, and a page that was
      // following a run leaves a long poll behind that the server would sit
      // out. The test is over and its directory is thrown away: do not wait.
      const timer = setTimeout(() => this.child.kill(9), 1500);
      await gone;
      clearTimeout(timer);
    }
    rmSync(this.directory, { recursive: true, force: true });
  }
}

// -- talking to the API from a test -------------------------------------------------------

export interface RunRow {
  id: string;
  title: string;
  state: string;
  completion: string | null;
  planned_count: number;
  verdict_summary: Record<string, number>;
}

export class Api {
  constructor(
    private readonly request: APIRequestContext,
    private readonly server: Server,
  ) {}

  private async json<T>(method: "get" | "post" | "delete", path: string, data?: unknown): Promise<T> {
    const response = await this.request[method](`${this.server.api}${path}`, data === undefined ? undefined : { data });
    if (!response.ok()) throw new Error(`${method.toUpperCase()} ${path} → ${response.status()} ${await response.text()}`);
    return response.status() === 204 ? (undefined as T) : ((await response.json()) as T);
  }

  get = <T>(path: string) => this.json<T>("get", path);
  post = <T>(path: string, data: unknown = {}) => this.json<T>("post", path, data);

  /** Compose a run and, with a runner, dispatch it. */
  async compose(body: { selection: unknown; title?: string; labels?: Record<string, string>; dispatch?: unknown }): Promise<RunRow> {
    return (await this.post<{ run: RunRow }>("/runs", body)).run;
  }

  run = (id: string) => this.get<RunRow & { verdicts: { item_id: string; status: string; shard: string; live: boolean }[] }>(`/runs/${id}`);

  async runs(): Promise<RunRow[]> {
    return (await this.get<{ runs: RunRow[] }>("/runs?limit=200")).runs;
  }

  /** Wait — on the API, not on the clock — until the run is in one of these states. */
  async waitFor(id: string, states: string[], timeout = 90_000): Promise<RunRow> {
    let latest: RunRow | undefined;
    await expect
      .poll(async () => (latest = await this.run(id)).state, { timeout, intervals: [100, 250, 500], message: `run ${id} never became ${states.join("/")}` })
      .toMatch(new RegExp(`^(${states.join("|")})$`));
    return latest!;
  }

  /** A finished run from the instant `demo` runner. Seed `b` fails some items, `a` passes these. */
  async finishedRun(filter: unknown, seed: string, extra: { title?: string; labels?: Record<string, string> } = {}): Promise<RunRow> {
    const run = await this.compose({ selection: { tag_filter: filter }, ...extra, dispatch: { runner: "demo", parameters: { seed } } });
    return this.waitFor(run.id, ["COMPLETE"]);
  }

  /** Execute a downloaded spec with the vendored consumer (simulated) and return the bundle's files. */
  async simulate(specFile: string, dispatchId: string, seed = "a"): Promise<{ name: string; mimeType: string; buffer: Buffer }[]> {
    const out = join(dirname(specFile), "bundle");
    await run(PYTHON, [EXEC_FILE, specFile, "--out", out, "--simulate", "--seed", seed, "--dispatch", dispatchId]);
    const { readdirSync } = await import("node:fs");
    return readdirSync(out)
      .sort()
      .map((name) => ({ name, mimeType: "application/json", buffer: readFileSync(join(out, name)) }));
  }
}

// -- fixtures -------------------------------------------------------------------------------

interface Fixtures {
  /** Which deployment this test runs against; override with `test.use({ config: … })`. */
  config: ConfigName;
  server: Server;
  api: Api;
  /** Open the UI at a hash route and wait until it has booted. */
  open: (hash?: string) => Promise<void>;
}

export const test = base.extend<Fixtures>({
  config: ["demo", { option: true }],

  server: async ({ config }, use, testInfo) => {
    const server = await Server.start(config);
    await use(server);
    if (testInfo.status !== testInfo.expectedStatus) await testInfo.attach("server.log", { body: server.log, contentType: "text/plain" });
    await server.stop();
  },

  api: async ({ request, server }, use) => {
    await use(new Api(request, server));
  },

  open: async ({ page, server }, use) => {
    // A page that logs an error or throws has failed, whatever else it shows.
    const problems: string[] = [];
    page.on("pageerror", (error) => problems.push(`pageerror: ${error.message}`));
    page.on("console", (message) => {
      if (message.type() === "error" && !/Failed to load resource/.test(message.text())) problems.push(`console: ${message.text()}`);
    });
    await use(async (hash = "") => {
      await page.goto(`${server.url}/${hash}`);
      await expect(page.locator("main")).toBeVisible();
    });
    expect(problems, "the page reported errors").toEqual([]);
  },
});

export { expect };

// -- things several tests do ---------------------------------------------------------------

/** Type a tag or pattern into the root group's add field and press Enter. */
export async function addRule(page: Page, pattern: string, locale = "en"): Promise<void> {
  const field = page.getByRole("combobox", { name: msg("filter.addTag", {}, locale), exact: true });
  await field.fill(pattern);
  await field.press("Enter");
}

/** The preview is current and says it matches `count` of `total`. */
export async function expectPreview(page: Page, count: number, total: number, locale = "en"): Promise<void> {
  await expect(page.getByTestId("preview-count")).toContainText(msg("preview.matched", { count, total }, locale));
  await expect(page.locator(".preview")).toHaveAttribute("aria-busy", "false");
}

export const runButton = (page: Page, count: number, locale = "en") =>
  page.getByRole("button", { name: msg("compose.run", { count }, locale), exact: true });

/** The run id of the run page the browser is on. */
export async function currentRunId(page: Page): Promise<string> {
  await expect(page).toHaveURL(/#\/runs\/[0-9A-Z]{20,}/);
  return /#\/runs\/([0-9A-Z]+)/.exec(page.url())![1]!;
}
