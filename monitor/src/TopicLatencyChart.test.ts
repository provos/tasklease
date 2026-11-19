import { render } from "svelte/server";
import { describe, expect, it } from "vitest";
import fixture from "../../schema/examples/monitor.snapshot/valid/response-completion-latency.json";
import TopicLatencyChart from "./TopicLatencyChart.svelte";
import { type HourlyStats, type StatsTopic } from "./model";

const base = fixture.body.stats[0];

function topic(name: string, rows: HourlyStats[]): StatsTopic {
  return {
    key: name,
    domain: "acme/pipeline",
    topic: name,
    generated_at: fixture.body.generated_at,
    rows,
    available: 0,
    oldest_available_at: null,
  };
}

describe("TopicLatencyChart server rendering", () => {
  it("uses one scale across topics and shows backlog when a bar has no observations", async () => {
    const { body } = await render(TopicLatencyChart, {
      props: {
        topics: [
          topic("first", [{ ...base, consumed: 2 }]),
          topic("second", [
            {
              ...base,
              completion_latency: {
                wait_ms_sum: 500,
                after_claim_ms_sum: 1000,
                count: 1,
              },
            },
          ]),
          {
            ...topic("waiting", []),
            available: 101,
            oldest_available_at: fixture.body.generated_at - 3_600_000,
          },
        ],
        showDomain: true,
        onSelect: () => {},
      },
    });
    const widths = [...body.matchAll(/style="width:\s*([\d.]+)%/g)].map(
      (match) => Number(match[1]),
    );
    expect(widths).toHaveLength(4);
    expect(widths[0] + widths[1]).toBeCloseTo(100);
    expect(widths[2] + widths[3]).toBeCloseTo(50);
    expect(body).toContain("acme/pipeline · ");
    expect(body).toContain("1 of 2 measured");
    expect(body).toContain("101 waiting now");
    expect(body).toContain("oldest task 1 h");
    expect(body).toContain("No measured completions");
  });

  it("renders measured zero latency on an all-zero scale", async () => {
    const { body } = await render(TopicLatencyChart, {
      props: {
        topics: [
          topic("instant", [
            {
              ...base,
              completion_latency: {
                wait_ms_sum: 0,
                after_claim_ms_sum: 0,
                count: 1,
              },
            },
          ]),
        ],
        showDomain: false,
        onSelect: () => {},
      },
    });
    expect(body).toMatch(/0 ms avg\s+total/);
    expect(body).not.toMatch(/NaN|Infinity|No measured completions/);
  });
});
