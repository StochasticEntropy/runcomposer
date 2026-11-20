// One message that survives exactly one navigation: "run started" is said on
// the page the run opens on, not on the page that was left.

export interface Flash {
  tone: "ok" | "warn" | "info";
  text: string;
}

let pending: Flash | null = null;

export function setFlash(flash: Flash): void {
  pending = flash;
}

export function takeFlash(): Flash | null {
  const flash = pending;
  pending = null;
  return flash;
}
