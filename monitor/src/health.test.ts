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
      ["critical", "verify/macos"],
    ]);
    const windows = result.alerts.find((a) => a.subject === "verify/windows")!;
    expect(windows.detail).toBe("no live worker · 2 tasks waiting");
    expect(
      result.alerts.find((a) => a.subject === "verify/macos")!.detail,
    ).toBe("no live worker; lease lapsed on 1 task · 1 task waiting");
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
      ["verify/macos", "critical"],
      ["verify/linux", "ok"],
      ["advise", "ok"],
      ["build/windows", "idle"],
    ]);
    expect(result.otherTopics.map((t) => [t.topic, t.role])).toEqual([
      ["verified/agent-claude-a1", "reply"],
    ]);
    expect(result.waiting).toBe(5);
    expect(result.held).toBe(3);
  });

  it("joins each pending request to its task", () => {
    const requests = board("busy").requests.map((r) => [
      r.request.topic,
      r.level,
      r.status,
      r.summary,
    ]);
    expect(requests).toEqual([
      ["verify/linux", "critical", "Moved to verify/linux/error", "pr 1030"],
      [
        "verify/windows",
        "critical",
        "Queued 1 of 2, no live worker",
        "pr 1031 · sha 3497557d · tier pr",
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

  it("keeps a running request matched after another fencing-ID rotation", () => {
    const value = mockSnapshot("building", 24, NOW);
    const request = value.local.requests.find(
      (r) => r.topic === "build/windows",
    )!;
    const task = value.domains[0].topics.find((t) => t.topic === request.topic)!
      .tasks[0];
    expect(task.origin_id).toBe(request.origin_id);
    expect(task.id).not.toBe(request.origin_id);
    task.id = "t_01ARZ3NDEKTSV4RRFFQ69G5FAV";
    expect(
      health(value).requests.find((r) => r.request === request)!.status,
    ).toBe("Running on build-a on windows-1, lease 7 min left");
  });

  it("finds an error successor even when the original topic is absent", () => {
    const value = mockSnapshot("busy", 24, NOW);
    value.domains[0].topics = value.domains[0].topics.filter(
      (topic) => topic.topic !== "verify/linux",
    );
    expect(
      health(value).requests.find((r) => r.summary === "pr 1030"),
    ).toMatchObject({
      level: "critical",
      status: "Moved to verify/linux/error",
    });
  });

  it("reports a lapsed request when the service makes its task available", () => {
    const value = mockSnapshot("building", 24, NOW);
    const request = value.local.requests.find(
      (r) => r.topic === "build/windows",
    )!;
    const topic = value.domains[0].topics.find(
      (t) => t.topic === request.topic,
    )!;
    topic.tasks[0].lease_until = NOW - 60_000;
    topic.tasks[0].state = "available";
    topic.available = 1;
    topic.held = 0;
    topic.oldest_available_at = topic.tasks[0].created_at;
    const result = health(value);
    expect(result.requests.find((r) => r.request === request)).toMatchObject({
      level: "warning",
      status: "Lease lapsed 1 min ago (build-a on windows-1)",
    });
    expect(
      result.topics.find((t) => t.topic === "build/windows")!.reasons,
    ).toContain("lease lapsed on 1 task");
    expect(
      result.machines
        .flatMap((machine) => machine.workers)
        .find((w) => w.identity === "windows-1" && w.worker === "build-a")!
        .state,
    ).toBe("lapsed");
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
