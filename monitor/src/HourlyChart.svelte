<script lang="ts">
  import { count, duration, hourLabel } from "./format";
  import type { HourlyStats } from "./model";

  let { rows, kind }: { rows: HourlyStats[]; kind: "throughput" | "latency" } =
    $props();

  const width = 640;
  const height = 170;
  const left = 44;
  const right = 16;
  const top = 16;
  const bottom = 32;

  const series = $derived(
    kind === "throughput"
      ? [
          {
            name: "Created",
            color: "#3859d6",
            values: rows.map((r) => r.created),
          },
          {
            name: "Consumed",
            color: "#168071",
            values: rows.map((r) => r.consumed),
          },
        ]
      : [
          {
            name: "Average wait",
            color: "#3859d6",
            values: rows.map((r) =>
              r.wait_count ? r.wait_ms_sum / r.wait_count : 0,
            ),
          },
          {
            name: "Average run",
            color: "#168071",
            values: rows.map((r) =>
              r.run_count ? r.run_ms_sum / r.run_count : 0,
            ),
          },
        ],
  );
  const maximum = $derived(Math.max(1, ...series.flatMap((s) => s.values)));
  const labels = $derived(
    rows.length > 1
      ? [...new Set([0, Math.floor((rows.length - 1) / 2), rows.length - 1])]
      : [0],
  );

  function x(index: number): number {
    return rows.length < 2
      ? (width + left - right) / 2
      : left + (index * (width - left - right)) / (rows.length - 1);
  }

  function y(value: number): number {
    return top + (height - top - bottom) * (1 - value / maximum);
  }

  function points(values: number[]): string {
    return values.map((value, index) => `${x(index)},${y(value)}`).join(" ");
  }
</script>

<div class="chart">
  <svg
    viewBox={`0 0 ${width} ${height}`}
    role="img"
    aria-label={kind === "throughput"
      ? "Hourly created and consumed tasks"
      : "Hourly average wait and run times"}
  >
    <title>{kind === "throughput" ? "Task throughput" : "Task latency"}</title>
    <desc
      >Hourly values across the selected period. The current hour is partial.
      Exact values are available in the hourly data table.</desc
    >
    {#each [0, 0.5, 1] as fraction}
      <line
        x1={left}
        x2={width - right}
        y1={y(maximum * fraction)}
        y2={y(maximum * fraction)}
        stroke="#e6eaf1"
      />
      <text x={left - 8} y={y(maximum * fraction) + 4} text-anchor="end"
        >{kind === "latency"
          ? duration(maximum * fraction)
          : count(maximum * fraction)}</text
      >
    {/each}
    {#each series as item}
      <polyline
        points={points(item.values)}
        fill="none"
        stroke={item.color}
        stroke-width="2.5"
        stroke-linejoin="round"
        vector-effect="non-scaling-stroke"
      />
      {#if rows.length === 1}
        <circle
          cx={x(0)}
          cy={y(item.values[0] ?? 0)}
          r="3.5"
          fill={item.color}
        />
      {/if}
    {/each}
    {#each labels as index}
      {#if rows[index]}
        <text
          x={x(index)}
          y={height - 7}
          text-anchor={index === 0 && rows.length > 1
            ? "start"
            : index === rows.length - 1 && rows.length > 1
              ? "end"
              : "middle"}>{hourLabel(rows[index].hour)}</text
        >
      {/if}
    {/each}
  </svg>
  <div class="legend">
    {#each series as item}
      <span><i style={`background:${item.color}`}></i>{item.name}</span>
    {/each}
  </div>
</div>

<style>
  .chart {
    min-width: 0;
  }
  svg {
    display: block;
    width: 100%;
    overflow: visible;
  }
  text {
    fill: #6d778d;
    font:
      10px system-ui,
      sans-serif;
  }
  .legend {
    display: flex;
    justify-content: center;
    gap: 1.4rem;
    color: #59657c;
    font-size: 0.75rem;
  }
  .legend span {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
  }
  i {
    width: 1rem;
    height: 3px;
    border-radius: 2px;
  }
</style>
