<script lang="ts">
  import { count, duration } from "./format.ts";
  import { topicLatencies, type StatsTopic } from "./model.ts";

  let {
    topics,
    showDomain,
    onSelect,
  }: {
    topics: StatsTopic[];
    showDomain: boolean;
    onSelect: (key: string) => void;
  } = $props();

  const rows = $derived(topicLatencies(topics));
  const maximum = $derived(
    rows.reduce(
      (max, { completion }) =>
        completion
          ? Math.max(max, completion.waitMs + completion.afterClaimMs)
          : max,
      0,
    ),
  );
</script>

<figure>
  <figcaption>Average time to complete</figcaption>
  <div class="legend">
    <span><i class="queue"></i>Initial queue wait</span>
    <span><i class="after"></i>After first claim</span>
    <small>Sorted by average queue wait · Current hour is partial</small>
  </div>
  {#if maximum > 0}
    <div class="scale" aria-hidden="true">
      <span>0</span><span>{duration(maximum)}</span>
    </div>
  {/if}
  <ul>
    {#each rows as row (row.key)}
      <li>
        <div class="row-heading">
          <button type="button" onclick={() => onSelect(row.key)}>
            {#if showDomain}<span>{`${row.domain} · `}</span>{/if}{row.topic}
          </button>
          <span>
            {count(row.waiting)} waiting now{#if row.oldestWaitingMs !== null}{` · oldest task ${duration(row.oldestWaitingMs)}`}{/if}
          </span>
        </div>
        {#if row.completion}
          {#if maximum > 0}
            <div class="track" aria-hidden="true">
              <div
                class="queue"
                style:width={`${(row.completion.waitMs / maximum) * 100}%`}
              ></div>
              <div
                class="after"
                style:width={`${(row.completion.afterClaimMs / maximum) * 100}%`}
              ></div>
            </div>
          {/if}
          <div class="timing">
            <span
              >{duration(row.completion.waitMs + row.completion.afterClaimMs)} avg
              total</span
            >
            <span title={`${count(row.completion.waitMs)} ms`}
              ><i class="queue"></i>{duration(row.completion.waitMs)} queued</span
            >
            <span title={`${count(row.completion.afterClaimMs)} ms`}
              ><i class="after"></i>{duration(row.completion.afterClaimMs)} after
              first claim</span
            >
            <span
              >{count(row.completion.count)} of {count(row.consumed)} measured</span
            >
          </div>
        {:else}
          <div class="timing">
            No measured completions{#if row.consumed}
              · {count(row.consumed)} consumed{/if}
          </div>
        {/if}
      </li>
    {/each}
  </ul>
</figure>

<style>
  figure {
    margin: 0;
    padding: 12px 16px 16px;
  }
  figcaption {
    color: var(--text-2);
    font-size: 0.85rem;
    font-weight: 600;
  }
  .legend,
  .row-heading,
  .timing,
  .scale {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 3px 14px;
    color: var(--muted);
    font-size: 0.72rem;
  }
  .legend {
    margin: 5px 0 12px;
  }
  .legend small {
    margin-left: auto;
    font-size: inherit;
  }
  .row-heading,
  .scale {
    justify-content: space-between;
  }
  .scale {
    font-variant-numeric: tabular-nums;
  }
  ul {
    margin: 0;
    padding: 0;
    list-style: none;
  }
  li {
    padding: 9px 0;
    border-top: 1px solid var(--divider);
  }
  button {
    max-width: 100%;
    padding: 0;
    border: 0;
    background: none;
    color: var(--accent-text);
    font:
      600 0.78rem ui-monospace,
      monospace;
    text-align: left;
    overflow-wrap: anywhere;
    cursor: pointer;
  }
  button:hover {
    text-decoration: underline;
  }
  button span {
    color: var(--muted);
    font-weight: 400;
  }
  .track {
    display: flex;
    height: 10px;
    margin: 6px 0 3px;
    border-radius: 3px;
    overflow: hidden;
    background: var(--surface-subtle);
  }
  .queue {
    background: var(--series-1);
  }
  .after {
    background: var(--series-2);
  }
  i {
    display: inline-block;
    width: 8px;
    height: 8px;
    margin-right: 4px;
    border-radius: 2px;
  }
  .timing {
    margin-top: 3px;
  }
</style>
