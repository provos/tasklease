import { describe, expect, it } from "vitest";
import { validateMonitorSnapshotResponse } from "../../../service/src/generated/validators.js";
import { decodeSnapshot, HOUR } from "../model";
import { respond } from "./plugin";
import { mockSnapshot, SNAPSHOT_SCENARIOS } from "./snapshot";

const NOW = 1_762_600_000_000;
const PAGE = "http://localhost:5173/";

describe("mock snapshots", () => {
  it.each(SNAPSHOT_SCENARIOS)(
    "%s decodes and each domain matches the service schema",
    (scenario) => {
      for (const hours of [1, 24, 720]) {
        const snapshot = decodeSnapshot(
          JSON.parse(JSON.stringify(mockSnapshot(scenario, hours, NOW))),
        );
        for (const domain of snapshot.domains) {
          expect(
            validateMonitorSnapshotResponse(domain),
            JSON.stringify(validateMonitorSnapshotResponse.errors),
          ).toBe(true);
          const current = Math.floor(NOW / HOUR) * HOUR;
          for (const row of domain.stats) {
            expect(row.hour).toBeLessThanOrEqual(current);
            expect(row.hour).toBeGreaterThan(current - hours * HOUR);
          }
        }
        // `tl monitor` omits answered requests from the local list.
        expect(
          snapshot.local.requests.every((r) => r.state !== "answered"),
        ).toBe(true);
      }
    },
  );

  it("is deterministic for the same time", () => {
    for (const scenario of SNAPSHOT_SCENARIOS) {
      expect(mockSnapshot(scenario, 24, NOW)).toEqual(
        mockSnapshot(scenario, 24, NOW),
      );
    }
  });

  it("covers the states the dashboard must render", () => {
    expect(mockSnapshot("empty", 24, NOW).domains[0]?.topics).toEqual([]);
    expect(mockSnapshot("degraded", 24, NOW).errors).toHaveLength(2);
    const large = mockSnapshot("large", 24, NOW);
    const topics = large.domains.flatMap((domain) => domain.topics);
    expect(large.domains.length).toBeGreaterThan(1);
    expect(topics.some((topic) => topic.truncated)).toBe(true);
    expect(topics.some((topic) => topic.topic.length === 128)).toBe(true);
    expect(large.local.replies.length).toBeGreaterThan(20);
  });
});

describe("mock API responses", () => {
  it("passes other paths through", () => {
    expect(respond("/", PAGE, "busy", NOW)).toBeNull();
    expect(respond("/assets/index.js", PAGE, "busy", NOW)).toBeNull();
  });

  it("serves the default scenario and the one in the page URL", () => {
    const busy = respond("/api/snapshot?hours=24", PAGE, "busy", NOW)!;
    expect(busy.status).toBe(200);
    expect(busy.body).toBe(JSON.stringify(mockSnapshot("busy", 24, NOW)));
    const empty = respond(
      "/api/snapshot?hours=6",
      `${PAGE}?scenario=empty&delay=2500`,
      "busy",
      NOW,
    )!;
    expect(empty.body).toBe(JSON.stringify(mockSnapshot("empty", 6, NOW)));
    expect(empty.delayMs).toBe(2500);
  });

  it("fails like the monitor when the service is unreachable", () => {
    const result = respond(
      "/api/snapshot",
      `${PAGE}?scenario=unreachable`,
      "busy",
      NOW,
    )!;
    expect(result.status).toBe(502);
    expect(result.contentType).toMatch(/^text\/plain/);
  });

  it("rejects bad hours and unknown scenarios, and caps delays", () => {
    for (const hours of ["0", "721", "1.5", "x"]) {
      expect(
        respond(`/api/snapshot?hours=${hours}`, PAGE, "busy", NOW)!.status,
      ).toBe(400);
    }
    const unknown = respond(
      "/api/snapshot",
      `${PAGE}?scenario=nope`,
      "busy",
      NOW,
    )!;
    expect(unknown.status).toBe(400);
    expect(unknown.body).toContain("busy, empty, degraded, large, unreachable");
    expect(
      respond("/api/snapshot", `${PAGE}?delay=99999`, "busy", NOW)!.delayMs,
    ).toBe(30_000);
    expect(respond("/api/snapshot", "not a url", "busy", NOW)!.status).toBe(
      200,
    );
  });
});
