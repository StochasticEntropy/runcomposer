import { describe, expect, it, vi } from "vitest";

import { ApiError, Latest, asApiError, createClient, isAbort } from "./request.ts";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

describe("request", () => {
  it("returns the body of a successful answer", async () => {
    const { request } = createClient(async () => json(200, { ok: true }));
    await expect(request("/api/v1/health")).resolves.toEqual({ ok: true });
  });

  it("turns an error answer into an ApiError with code, detail and what else came along", async () => {
    const { request } = createClient(async () =>
      json(409, { detail: "the corpus changed", code: "dispatch-refused", run_kept: false, run_id: null }),
    );
    const error = await request("/api/v1/runs", { method: "POST", body: {} }).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 409, code: "dispatch-refused", detail: "the corpus changed", extra: { run_kept: false, run_id: null } });
  });

  it("carries field errors and the refused parameter", async () => {
    const invalid = createClient(async () =>
      json(400, { detail: "invalid request — title: bad", code: "invalid-request", fields: [{ field: "title", message: "bad" }] }),
    );
    const first = (await invalid.request("/x", { method: "POST", body: {} }).catch((caught: unknown) => caught)) as ApiError;
    expect(first.fields).toEqual([{ field: "title", message: "bad" }]);
    const parameter = createClient(async () => json(400, { detail: "parameter 'seed' must be a string", code: "invalid-parameter", parameter: "seed" }));
    const second = (await parameter.request("/x", { method: "POST", body: {} }).catch((caught: unknown) => caught)) as ApiError;
    expect(second.extra.parameter).toBe("seed");
  });

  it("never shows a non-string detail as [object Object]", async () => {
    const { request } = createClient(async () => json(422, { detail: [{ loc: ["body", "title"], msg: "bad" }] }));
    const error = (await request("/x").catch((caught: unknown) => caught)) as ApiError;
    expect(error.detail).toContain('"msg":"bad"');
    expect(error.detail).not.toContain("[object Object]");
    expect(error.code).toBe("http-422");
  });

  it("survives an error page that is not JSON", async () => {
    const { request } = createClient(async () => new Response("<html>Bad Gateway</html>", { status: 502, statusText: "Bad Gateway" }));
    const error = (await request("/x").catch((caught: unknown) => caught)) as ApiError;
    expect(error).toMatchObject({ status: 502, code: "http-502", detail: "Bad Gateway" });
  });

  it("reports an unreachable server with the code `network`", async () => {
    const { request } = createClient(async () => {
      throw new TypeError("Failed to fetch");
    });
    const error = (await request("/x").catch((caught: unknown) => caught)) as ApiError;
    expect(error).toMatchObject({ status: 0, code: "network" });
  });

  it("answers a 204 with nothing", async () => {
    const { request } = createClient(async () => new Response(null, { status: 204 }));
    await expect(request("/x", { method: "DELETE", expect: "none" })).resolves.toBeUndefined();
  });

  it("shares one request between identical reads in flight, but never a write", async () => {
    let release: (response: Response) => void = () => undefined;
    const fetchImpl = vi.fn(() => new Promise<Response>((resolve) => (release = resolve)));
    const { request } = createClient(fetchImpl);
    const first = request("/api/v1/runs");
    const second = request("/api/v1/runs");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    release(json(200, { runs: [] }));
    await expect(Promise.all([first, second])).resolves.toEqual([{ runs: [] }, { runs: [] }]);
    // Once it is answered, the next read is a new request.
    const third = request("/api/v1/runs");
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    release(json(200, { runs: [] }));
    await third;

    const posting = createClient(vi.fn(async () => json(200, {})));
    await Promise.all([posting.request("/x", { method: "POST", body: {} }), posting.request("/x", { method: "POST", body: {} })]);
  });

  it("aborts the shared fetch only when every caller has left", async () => {
    let seen: AbortSignal | undefined;
    const fetchImpl = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      seen = init?.signal ?? undefined;
      return new Promise<Response>(() => undefined);
    });
    const { request } = createClient(fetchImpl);
    const one = new AbortController();
    const two = new AbortController();
    const first = request("/x", { signal: one.signal }).catch((caught: unknown) => caught);
    const second = request("/x", { signal: two.signal }).catch((caught: unknown) => caught);
    one.abort();
    expect(isAbort(await first)).toBe(true);
    expect(seen?.aborted).toBe(false);
    two.abort();
    expect(isAbort(await second)).toBe(true);
    expect(seen?.aborted).toBe(true);
  });
});

describe("only the latest call counts", () => {
  it("delivers only the answer to the most recent call", async () => {
    const latest = new Latest();
    let releaseFirst: (value: string) => void = () => undefined;
    const first = latest.run(() => new Promise<string>((resolve) => (releaseFirst = resolve)));
    const second = latest.run(async () => "second");
    await expect(second).resolves.toEqual({ current: true, value: "second" });
    releaseFirst("first");
    await expect(first).resolves.toEqual({ current: false });
  });

  it("swallows the failure of a superseded call and reports the current one", async () => {
    const latest = new Latest();
    const first = latest.run(async (signal) => {
      await new Promise((resolve) => setTimeout(resolve, 5));
      if (signal.aborted) throw new DOMException("aborted", "AbortError");
      return "first";
    });
    const second = latest.run(async () => {
      throw new ApiError(500, "internal-error", "boom");
    });
    await expect(second).rejects.toMatchObject({ code: "internal-error" });
    await expect(first).resolves.toEqual({ current: false });
  });

  it("cancel makes whatever is in flight not count", async () => {
    const latest = new Latest();
    const pending = latest.run(async () => "value");
    latest.cancel();
    await expect(pending).resolves.toEqual({ current: false });
  });
});

describe("asApiError", () => {
  it("passes an ApiError through and wraps anything else", () => {
    const original = new ApiError(404, "unknown-run", "unknown run id");
    expect(asApiError(original)).toBe(original);
    expect(asApiError(new Error("boom"))).toMatchObject({ code: "client-error", detail: "boom" });
    expect(asApiError("text")).toMatchObject({ code: "client-error", detail: "text" });
  });
});
