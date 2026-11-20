// "Show the answer to THIS question, or say there is none yet."
//
// A plan preview is the answer to (selection, runner, parameters). Any of
// them may change while a request is under way. `KeyedRequest` keeps the
// question as a key next to every answer: an answer is *current* only while
// its key is the key of what is on screen. A new question aborts the request
// for the old one, and an answer that arrives late is dropped — the same
// discipline as the selection preview (compose.ts), for any request.

import { Latest, asApiError } from "./request.ts";
import type { ApiError } from "./request.ts";

export interface KeyedState<T> {
  /** The question on screen right now ("" when there is none). */
  key: string;
  status: "idle" | "loading" | "ready" | "error";
  result: T | null;
  /** The question `result` answers. */
  resultKey: string;
  error: ApiError | null;
}

export const idleKeyed = <T>(): KeyedState<T> => ({ key: "", status: "idle", result: null, resultKey: "", error: null });

/** The result on screen answers the question on screen. */
export const isCurrent = <T>(state: KeyedState<T>): boolean =>
  state.status === "ready" && state.result !== null && state.resultKey === state.key;

export class KeyedRequest<T> {
  private state: KeyedState<T> = idleKeyed<T>();
  private readonly latest = new Latest();
  private timer: ReturnType<typeof setTimeout> | null = null;
  private task: ((signal: AbortSignal) => Promise<T>) | null = null;

  constructor(
    private readonly onChange: (state: KeyedState<T>) => void,
    private readonly debounceMs = 200,
  ) {}

  get current(): KeyedState<T> {
    return this.state;
  }

  private set(next: KeyedState<T>): void {
    this.state = next;
    this.onChange(next);
  }

  /** The question is now `key`; `task` answers it. `null` means: no question. */
  request(key: string | null, task?: (signal: AbortSignal) => Promise<T>): void {
    if (key !== null && key === this.state.key && this.state.status !== "error") return;
    this.clearTimer();
    this.latest.cancel();
    if (key === null || !task) {
      this.task = null;
      if (this.state.status !== "idle" || this.state.key !== "") this.set(idleKeyed<T>());
      return;
    }
    this.task = task;
    // An answer to another question is not shown while this one is asked.
    this.set({ key, status: "loading", result: null, resultKey: "", error: null });
    if (this.debounceMs === 0) void this.send(key, task);
    else this.timer = setTimeout(() => void this.send(key, task), this.debounceMs);
  }

  retry(): void {
    if (!this.task || this.state.key === "") return;
    this.clearTimer();
    this.set({ ...this.state, status: "loading", error: null });
    void this.send(this.state.key, this.task);
  }

  private async send(key: string, task: (signal: AbortSignal) => Promise<T>): Promise<void> {
    this.timer = null;
    try {
      const outcome = await this.latest.run(task);
      if (!outcome.current || key !== this.state.key) return;
      this.set({ key, status: "ready", result: outcome.value, resultKey: key, error: null });
    } catch (error) {
      if (key !== this.state.key) return;
      this.set({ key, status: "error", result: null, resultKey: "", error: asApiError(error) });
    }
  }

  private clearTimer(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }

  dispose(): void {
    this.clearTimer();
    this.latest.cancel();
    this.task = null;
    this.state = idleKeyed<T>();
  }
}
