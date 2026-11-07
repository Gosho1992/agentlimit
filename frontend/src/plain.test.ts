import { describe, expect, it } from "vitest";
import { narrate, overall } from "./plain";

const ev = (event_type: string, payload: any = {}) =>
  ({ event_type, payload, session_id: "s", timestamp_utc: "2026-11-07T00:00:00Z", prev_hash: "", entry_hash: Math.random().toString(), signature: "" });

describe("overall(): never more confident than the evidence", () => {
  it("KILL without a verdict is 'Stopping…', not 'stopped'", () => {
    const s = overall({ state: "KILL", agent_status: "running", termination: null });
    expect(s.label).toBe("Stopping…");
    expect(s.say).not.toMatch(/checked 6 times/);
  });
  it("KILL_FAILED is never shown as stopped for good", () => {
    const s = overall({ state: "KILL", termination: { verdict: "KILL_FAILED", checks: {} }, lifecycle: {} });
    expect(s.label).toBe("Stop not confirmed");
    expect(s.signal).not.toBe("green");
  });
  it("KILL_CONFIRMED is 'Stopped for good'", () => {
    expect(overall({ state: "KILL", termination: { verdict: "KILL_CONFIRMED", checks: {} } }).label).toBe("Stopped for good");
  });
  it("a completed run does not claim the answer was checked", () => {
    const s = overall({ state: "NORMAL", agent_status: "completed", lifecycle: {} });
    expect(s.label).toBe("Finished");
    expect(s.say).not.toMatch(/independent|correct/i);
  });
  it("THROTTLE claims a cheaper model only when the signed event shows one", () => {
    const snap = { state: "THROTTLE", agent_status: "running" };
    const noDown = overall(snap, [ev("throttle_applied", { verified: true, model_downgrade: "unavailable (x)" })]);
    expect(noDown.say).toMatch(/isn't available/);
    const down = overall(snap, [ev("throttle_applied", { verified: true, model_downgrade: { from: "a", to: "b" } })]);
    expect(down.say).toMatch(/cheaper model b/);
    const failed = overall(snap, [ev("throttle_applied", { verified: false })]);
    expect(failed.label).toBe("Slow-down not confirmed");
  });
  it("PAUSE says 'Frozen' only for a kernel-confirmed freeze", () => {
    const snap = { state: "PAUSE", agent_status: "running" };
    expect(overall(snap, [ev("pause_applied", { mode: "KERNEL_FREEZE", verified_frozen: true })]).label).toBe("Frozen");
    expect(overall(snap, [ev("pause_applied", { mode: "STEP_BOUNDARY", verified_frozen: false })]).label).toBe("Paused");
  });
});

describe("narrate()", () => {
  it("a KILL transition is a decision, not a confirmation", () => {
    expect(narrate(ev("state_transition", { to: "KILL", cause: "hard_limit:wall_clock" }))!.text).toMatch(/Stopping and checking/);
  });
  it("an applied-but-unverified fix is not called confirmed", () => {
    expect(narrate(ev("recommendation_applied", { applied: true, verified: false }))!.text).toMatch(/not independently confirmed/);
  });
});

describe("failed stop followed by verified clean-up", () => {
  it("is a warning, never a clean 'Stopped for good'", () => {
    const s = overall({ state: "KILL", termination: { verdict: "KILL_FAILED", checks: {} },
      lifecycle: { workload_gone: true, sandbox_removed: true } });
    expect(s.label).toBe("Stopped, with a warning");
    expect(s.signal).toBe("yellow");
  });
});
