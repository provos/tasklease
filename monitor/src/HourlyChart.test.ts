import { render } from "svelte/server";
import { describe, expect, it } from "vitest";
import fixture from "../../schema/examples/monitor.snapshot/valid/response.json";
import HourlyChart from "./HourlyChart.svelte";
import { hourBuckets, type HourlyStats } from "./model";

const base = fixture.body.stats[0];

function plottedSeries(body: string): number[][][] {
  return [...body.matchAll(/<polyline\b[^>]*\bpoints="([^"]*)"/g)].map(
    (match) =>
      match[1]
        ? match[1].split(/\s+/).map((point) => point.split(",").map(Number))
        : [],
  );
}

describe("HourlyChart server rendering", () => {
  it.each(["throughput", "latency"] as const)(
    "renders an accessible empty %s chart without invalid coordinates",
    async (kind) => {
      const { body } = await render(HourlyChart, { props: { kind, rows: [] } });

      expect(body).toContain('role="img"');
      expect(body).toContain(
        kind === "throughput"
          ? 'aria-label="Hourly created and consumed tasks"'
          : 'aria-label="Hourly average wait and run times"',
      );
      expect(body).toContain("The current hour is partial.");
      expect(body).toContain(
        "Exact values are available in the hourly data table.",
      );
      expect(plottedSeries(body)).toEqual([[], []]);
      expect(body).not.toMatch(/NaN|Infinity/);
    },
  );

  it.each(["throughput", "latency"] as const)(
    "centers a single zero-activity fixture hour in a %s chart",
    async (kind) => {
      const rows = hourBuckets(
        fixture.body.stats,
        fixture.body.generated_at,
        1,
      );
      const { body } = await render(HourlyChart, { props: { kind, rows } });

      expect(plottedSeries(body)).toEqual([[[334, 138]], [[334, 138]]]);
      expect([...body.matchAll(/<circle\b/g)]).toHaveLength(2);
      expect(body).not.toMatch(/NaN|Infinity/);
    },
  );

  it("plots nonzero created and consumed fixture statistics separately", async () => {
    const rows: HourlyStats[] = [
      { ...base, created: 4, consumed: 2 },
      { ...base, hour: base.hour + 3_600_000, created: 0, consumed: 4 },
    ];
    const { body } = await render(HourlyChart, {
      props: { kind: "throughput", rows },
    });

    expect(body).toContain("<title>Task throughput</title>");
    expect(body).toContain("Created");
    expect(body).toContain("Consumed");
    expect(plottedSeries(body)).toEqual([
      [
        [44, 16],
        [624, 138],
      ],
      [
        [44, 77],
        [624, 16],
      ],
    ]);
  });

  it("plots per-hour average latencies and handles zero observation counts", async () => {
    const rows: HourlyStats[] = [
      {
        ...base,
        wait_ms_sum: 800,
        wait_count: 4,
        run_ms_sum: 400,
        run_count: 2,
      },
      {
        ...base,
        hour: base.hour + 3_600_000,
        wait_ms_sum: 500,
        wait_count: 0,
        run_ms_sum: 1000,
        run_count: 1,
      },
    ];
    const { body } = await render(HourlyChart, {
      props: { kind: "latency", rows },
    });
    const [wait, run] = plottedSeries(body);

    expect(body).toContain("<title>Task latency</title>");
    expect(body).toContain("Average wait");
    expect(body).toContain("Average run");
    expect(wait[0][0]).toBe(44);
    expect(wait[0][1]).toBeCloseTo(113.6);
    expect(wait[1]).toEqual([624, 138]);
    expect(run[0][1]).toBeCloseTo(113.6);
    expect(run[1]).toEqual([624, 16]);
    expect(body).not.toMatch(/NaN|Infinity/);
  });
});
