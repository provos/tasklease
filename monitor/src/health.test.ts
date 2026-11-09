import { describe, expect, it } from "vitest";
import { health, summarize, type Health } from "./health.ts";
import { mockSnapshot } from "./mock/snapshot.ts";

const NOW = 1_762_600_000_000;

function board(scenario: Parameters<typeof mockSnapshot>[0]): Health {
  return health(mockSnapshot(scenario, 24, NOW));
}

describe("operator health", () => {
  it("raises the busy board's incident, most severe first", () => {
    const result = board("busy");
    expect(result.alerts.map((alert) => [alert.level, alert.subject])).toEqual([
      ["critical", "windows-1 down"],
      ["critical", "verify/linux/error"],
      ["critical", "verify/windows"],
      ["warning", "verify/macos"],
      ["warning", "agent-codex-b7 request to advise"],
    ]);
    const windows = result.alerts.find((a) => a.subject === "verify/windows")!;
    expect(windows.detail).toBe("no live worker · 2 tasks waiting");
    expect(
      result.alerts.find((a) => a.subject === "verify/macos")!.detail,
    ).toBe("lease lapsed on 1 task");
  });

  it("classifies machines and workers from polls and held leases", () => {
    const machines = Object.fromEntries(
      board("busy").machines.map((m) => [
        m.identity,
        [m.state, m.workers.map((w) => `${w.worker}:${w.state}`)],
      ]),
    );
    expect(machines).toEqual({
      "windows-1": ["down", ["build-a:down", "verify-a:down"]],
      "mac-1": ["live", ["verify-a:lapsed"]],
      adviser: ["busy", ["opus:busy"]],
      "linux-1": ["busy", ["verify-a:busy", "verify-b:busy"]],
    });
  });

  it("does not mistake a long build for a dead machine", () => {
    const result = board("building");
    expect(result.alerts).toEqual([]);
    const windows = result.machines.find((m) => m.identity === "windows-1")!;
    expect(windows.state).toBe("busy");
    expect(
      Object.fromEntries(windows.workers.map((w) => [w.worker, w.state])),
    ).toEqual({ "build-a": "busy", "verify-a": "waiting" });
    expect(
      result.topics.find((t) => t.topic === "verify/windows"),
    ).toMatchObject({ level: "ok", serving: 1, seen: 1 });
  });

  it("sorts topics by severity and keeps replies out of the work totals", () => {
    const result = board("busy");
    expect(result.topics.map((t) => [t.topic, t.level])).toEqual([
      ["verify/linux/error", "critical"],
      ["verify/windows", "critical"],
      ["verify/macos", "warning"],
      ["verify/linux", "ok"],
      ["advise", "ok"],
      ["build/windows", "idle"],
    ]);
    expect(result.otherTopics.map((t) => [t.topic, t.role])).toEqual([
      ["verified/agent-claude-a1", "reply"],
    ]);
    expect(result.waiting).toBe(4);
    expect(result.held).toBe(4);
  });

  it("joins each pending request to its task", () => {
    const requests = board("busy").requests.map((r) => [
      r.request.topic,
      r.level,
      r.status,
      r.summary,
    ]);
    expect(requests).toEqual([
      [
        "verify/windows",
        "critical",
        "Queued 1 of 2, no live worker",
        "pr 1031 · sha 3497557d · tier pr",
      ],
      [
        "advise",
        "warning",
        "Submission unconfirmed for 7 min",
        "question “Is a 30-minute solve-lane hold OK for the G-9 batch?”",
      ],
      [
        "verify/linux",
        "ok",
        "Running on verify-a on linux-1, lease 4 min left",
        "pr 1031 · sha 3497557d · tier pr",
      ],
    ]);
    expect(board("building").requests.map((r) => r.status)).toEqual([
      "Running on build-a on windows-1, lease 7 min left",
      "Queued 1 of 1",
      "Running on verify-a on linux-1, lease 8 min left",
    ]);
  });

  it("is quiet on an empty board and flags undeclared domains", () => {
    const empty = board("empty");
    expect(empty.alerts).toEqual([]);
    expect(empty.undeclaredDomains).toEqual([
      "github.com/provos/fluxshoteditor",
    ]);
    expect(board("busy").undeclaredDomains).toEqual([]);
  });

  it("flags an unserved backlog in the large scenario", () => {
    const flood = board("large").topics.find(
      (t) => t.topic === "verify/flood",
    )!;
    expect(flood.level).toBe("critical");
    expect(flood.reasons).toEqual(["no live worker"]);
  });

  it("summarizes request data on one line", () => {
    expect(summarize({ pr: 7, draft: false, nested: { a: 1 } })).toBe(
      "pr 7 · draft false",
    );
    expect(summarize({ nested: { a: 1 } })).toBe("{…}");
    expect(summarize({ note: "x".repeat(70) })).toBe(`note ${"x".repeat(57)}…`);
  });
});
