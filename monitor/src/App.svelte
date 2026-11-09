<script lang="ts">
  import { onMount } from "svelte";
  import { SvelteSet } from "svelte/reactivity";
  import HourlyChart from "./HourlyChart.svelte";
  import { age, count, dateTime, duration, hourLabel, json } from "./format";
  import {
    decodeSnapshot,
    hourBuckets,
    statsTopics,
    topics,
    workers,
    type Snapshot,
  } from "./model";

  let snapshot = $state<Snapshot | null>(null);
  let loading = $state(false);
  let refreshError = $state<string | null>(null);
  let inputError = $state<string | null>(null);
  let requestedHours = $state<number | undefined>(24);
  let loadedHours = $state(24);
  // Keep plots tied to loaded data while retries use the latest valid choice.
  let desiredHours = 24;
  let lastUpdated = $state<number | null>(null);
  let now = $state(Date.now());
  let selectedStats = $state("");
  let replyPage = $state(1);
  let showUnmatched = $state(false);
  let hourlyOpen = $state(false);
  const expandedQueues = new SvelteSet<string>();
  const expandedReplies = new SvelteSet<string>();
  let alive = false;
  let sequence = 0;
  let controller: AbortController | undefined;
  const pageSize = 20;

  const queueRows = $derived(topics(snapshot));
  const workerRows = $derived(workers(queueRows));
  const available = $derived(
    queueRows.reduce((total, topic) => total + topic.available, 0),
  );
  const held = $derived(
    queueRows.reduce((total, topic) => total + topic.held, 0),
  );
  const workerCount = $derived(
    new Set(
      workerRows.map((worker) =>
        JSON.stringify([worker.identity, worker.worker]),
      ),
    ).size,
  );
  const pending = $derived(
    (snapshot?.local.requests ?? [])
      .filter((request) => request.state !== "answered")
      .sort((a, b) => b.created_at - a.created_at),
  );
  const replies = $derived(
    (snapshot?.local.replies ?? [])
      .filter((reply) => !showUnmatched || reply.request_id === null)
      .sort((a, b) => b.received_at - a.received_at),
  );
  const unmatchedCount = $derived(
    snapshot?.local.replies.filter((reply) => reply.request_id === null)
      .length ?? 0,
  );
  const pages = $derived(Math.max(1, Math.ceil(replies.length / pageSize)));
  const currentPage = $derived(Math.min(replyPage, pages));
  const visibleReplies = $derived(
    replies.slice((currentPage - 1) * pageSize, currentPage * pageSize),
  );
  const choices = $derived(statsTopics(snapshot));
  const stats = $derived(
    choices.find((choice) => choice.key === selectedStats) ?? choices[0],
  );
  const hourly = $derived(
    hourBuckets(
      stats?.rows ?? [],
      stats?.generated_at ?? snapshot?.generated_at ?? now,
      loadedHours,
    ),
  );
  const reverseHourly = $derived([...hourly].reverse());
  const totals = $derived(
    hourly.reduce(
      (total, row) => ({
        created: total.created + row.created,
        consumed: total.consumed + row.consumed,
        reowned: total.reowned + row.reowned,
        empty: total.empty + row.empty_polls,
        waitSum: total.waitSum + row.wait_ms_sum,
        waitCount: total.waitCount + row.wait_count,
        waitMax: Math.max(total.waitMax, row.wait_ms_max),
        runSum: total.runSum + row.run_ms_sum,
        runCount: total.runCount + row.run_count,
        runMax: Math.max(total.runMax, row.run_ms_max),
      }),
      {
        created: 0,
        consumed: 0,
        reowned: 0,
        empty: 0,
        waitSum: 0,
        waitCount: 0,
        waitMax: 0,
        runSum: 0,
        runCount: 0,
        runMax: 0,
      },
    ),
  );

  async function refresh(hours: number): Promise<void> {
    if (!Number.isInteger(hours) || hours < 1 || hours > 720) {
      inputError = "Choose a whole number of hours from 1 to 720.";
      return;
    }
    controller?.abort();
    const active = ++sequence;
    const request = new AbortController();
    controller = request;
    loading = true;
    let timedOut = false;
    const timeout = window.setTimeout(() => {
      timedOut = true;
      request.abort();
    }, 25_000);
    try {
      const response = await fetch(`/api/snapshot?hours=${hours}`, {
        signal: request.signal,
        cache: "no-store",
        credentials: "omit",
        headers: { Accept: "application/json" },
      });
      if (!response.ok)
        throw new Error(`Snapshot request failed (HTTP ${response.status}).`);
      const value: unknown = await response.json();
      const result = decodeSnapshot(value);
      if (alive && active === sequence) {
        const queueKeys = new Set(
          topics(result).map((topic) =>
            JSON.stringify([topic.domain, topic.topic]),
          ),
        );
        const replyKeys = new Set(
          result.local.replies.map((reply) =>
            JSON.stringify([reply.agent_id, reply.task_id]),
          ),
        );
        for (const key of expandedQueues)
          if (!queueKeys.has(key)) expandedQueues.delete(key);
        for (const key of expandedReplies)
          if (!replyKeys.has(key)) expandedReplies.delete(key);
        snapshot = result;
        loadedHours = hours;
        lastUpdated = Date.now();
        now = lastUpdated;
        refreshError = null;
      }
    } catch (error) {
      if (alive && active === sequence) {
        refreshError = timedOut
          ? "The refresh timed out."
          : error instanceof Error
            ? error.message
            : "The snapshot could not be refreshed.";
      }
    } finally {
      window.clearTimeout(timeout);
      if (alive && active === sequence) loading = false;
    }
  }

  function applyHours(): void {
    if (requestedHours === undefined) {
      inputError = "Choose a whole number of hours from 1 to 720.";
      return;
    }
    if (
      !Number.isInteger(requestedHours) ||
      requestedHours < 1 ||
      requestedHours > 720
    ) {
      inputError = "Choose a whole number of hours from 1 to 720.";
      return;
    }
    inputError = null;
    desiredHours = requestedHours;
    void refresh(desiredHours);
  }

  onMount(() => {
    alive = true;
    void refresh(24);
    // Hidden tabs do not poll; a tab that becomes visible refreshes at once.
    const polling = window.setInterval(() => {
      if (!loading && !document.hidden) void refresh(desiredHours);
    }, 30_000);
    const onVisible = () => {
      if (!loading && !document.hidden) void refresh(desiredHours);
    };
    document.addEventListener("visibilitychange", onVisible);
    const clock = window.setInterval(() => {
      now = Date.now();
    }, 10_000);
    return () => {
      alive = false;
      controller?.abort();
      window.clearInterval(polling);
      window.clearInterval(clock);
      document.removeEventListener("visibilitychange", onVisible);
    };
  });
</script>

<svelte:head>
  <title>Tasklease monitor</title>
  <meta
    name="description"
    content="Read-only local view of tasklease queues, workers, and replies."
  />
</svelte:head>

<div class="shell">
  <header class="masthead">
    <a class="brand" href="#overview" aria-label="Tasklease overview"
      ><span class="brand-mark" aria-hidden="true"
        ><i></i><i></i><i></i><i></i></span
      >tasklease<span class="local-label">LOCAL MONITOR</span></a
    >
    <span class="read-only"><span aria-hidden="true">◈</span> Read only</span>
  </header>

  <main id="overview">
    <section class="page-heading" aria-labelledby="page-title">
      <div>
        <p class="eyebrow">YOUR SYSTEM, IN VIEW</p>
        <h1 id="page-title">Task board</h1>
        <p class="subtitle">
          Queues, workers, and replies across your machines.
        </p>
      </div>
      <div class="refresh-controls">
        <form
          onsubmit={(event) => {
            event.preventDefault();
            applyHours();
          }}
        >
          <label for="hours">History <span class="muted">(hours)</span></label>
          <input
            id="hours"
            type="number"
            min="1"
            max="720"
            step="1"
            required
            bind:value={requestedHours}
            onchange={applyHours}
            aria-describedby={inputError ? "hours-error" : undefined}
            aria-invalid={inputError ? "true" : undefined}
          />
          <button class="primary" type="submit" disabled={loading}
            ><span class:spinning={loading} aria-hidden="true">↻</span>{loading
              ? "Refreshing"
              : "Refresh"}</button
          >
        </form>
        <p class="refresh-status" aria-live="polite">
          <span
            class:warning-dot={refreshError || snapshot?.errors?.length}
            class="status-dot"
          ></span>{lastUpdated === null
            ? loading
              ? "Connecting to your local monitor…"
              : "Waiting for a snapshot"
            : `Updated ${age(lastUpdated, now)} · auto refresh 30s`}
        </p>
        {#if inputError}<p id="hours-error" class="field-error" role="alert">
            {inputError}
          </p>{/if}
      </div>
    </section>

    {#if refreshError}
      <div class="notice warning" role="alert">
        <strong
          >{snapshot
            ? "Snapshot is stale."
            : "Unable to load the task board."}</strong
        ><span
          >{refreshError}
          {snapshot
            ? "The last successful snapshot remains below."
            : "Check the local monitor and try refreshing."}</span
        >
      </div>
    {/if}
    {#if snapshot?.errors?.length}
      <div class="notice warning" role="status">
        <strong>Some sources are unavailable.</strong>
        <ul>
          {#each snapshot.errors as error}<li>{error}</li>{/each}
        </ul>
      </div>
    {/if}

    {#if snapshot}
      <section class="metrics" aria-label="System overview">
        <article class="metric">
          <span>Available tasks</span><strong>{count(available)}</strong><small
            >Ready for a worker</small
          >
        </article>
        <article class="metric">
          <span>Held tasks</span><strong>{count(held)}</strong><small
            >Leases currently active</small
          >
        </article>
        <article class="metric">
          <span>Workers seen</span><strong>{count(workerCount)}</strong><small
            >Across {count(snapshot.domains.length)}
            {snapshot.domains.length === 1 ? "domain" : "domains"}</small
          >
        </article>
        <article class="metric">
          <span>Pending requests</span><strong>{count(pending.length)}</strong
          ><small>Saved on this machine</small>
        </article>
      </section>

      <section class="panel" aria-labelledby="workers-heading">
        <div class="section-heading">
          <div>
            <p class="eyebrow">THE MACHINES DOING THE WORK</p>
            <h2 id="workers-heading">
              Workers <span class="counter">{workerRows.length}</span>
            </h2>
          </div>
          <p class="section-note">Pollers seen in the last 7 days</p>
        </div>
        {#if workerRows.length}
          <div class="table-scroll">
            <table aria-label="Workers by domain and topic">
              <thead
                ><tr
                  ><th>Worker / identity</th><th>Domain / topic</th><th
                    >Last poll</th
                  ><th>Last successful Own</th></tr
                ></thead
              ><tbody>
                {#each workerRows as worker}
                  <tr
                    ><td
                      ><strong class="worker-label"
                        ><span class="worker-icon" aria-hidden="true">▣</span
                        >{worker.worker}</strong
                      ><span class="secondary mono">{worker.identity}</span></td
                    ><td
                      ><strong class="mono">{worker.topic}</strong><span
                        class="secondary mono">{worker.domain}</span
                      ></td
                    ><td title={dateTime(worker.last_polled_at)}
                      >{age(worker.last_polled_at, now)}</td
                    ><td title={dateTime(worker.last_owned_at)}
                      >{worker.last_owned_at === null
                        ? "Never"
                        : age(worker.last_owned_at, now)}</td
                    ></tr
                  >
                {/each}
              </tbody>
            </table>
          </div>
        {:else}<div class="empty">
            <span aria-hidden="true">▣</span>
            <h3>No workers seen yet</h3>
            <p>Workers appear here after they poll a topic you can view.</p>
          </div>{/if}
      </section>

      <section class="panel" aria-labelledby="queues-heading">
        <div class="section-heading">
          <div>
            <p class="eyebrow">WHAT IS WAITING, WHAT IS RUNNING</p>
            <h2 id="queues-heading">
              Queues <span class="counter">{queueRows.length}</span>
            </h2>
          </div>
          <p class="section-note">Expand a topic to inspect task metadata</p>
        </div>
        {#if queueRows.length}
          <div class="queue-head" aria-hidden="true">
            <span>Domain / topic</span><span>Available</span><span>Held</span
            ><span>Oldest available</span>
          </div>
          {#each queueRows as topic (JSON.stringify( [topic.domain, topic.topic] ))}
            {@const queueKey = JSON.stringify([topic.domain, topic.topic])}
            <details
              class="queue"
              ontoggle={(event) => {
                if (event.currentTarget.open) expandedQueues.add(queueKey);
                else expandedQueues.delete(queueKey);
              }}
            >
              <summary
                ><span class="queue-name"
                  ><span class="chevron" aria-hidden="true">›</span><span
                    ><strong class="mono">{topic.topic}</strong><span
                      class="secondary mono"
                      >{topic.domain}{#if topic.declared}<span class="declared"
                          >declared</span
                        >{/if}</span
                    ></span
                  ></span
                ><span class="queue-number"
                  ><span class="mobile-label">Available </span><span
                    class:has-tasks={topic.available > 0}
                    class="badge">{count(topic.available)}</span
                  ></span
                ><span class="queue-number"
                  ><span class="mobile-label">Held </span><span
                    class="badge neutral">{count(topic.held)}</span
                  ></span
                ><span
                  class="oldest"
                  title={dateTime(topic.oldest_available_at)}
                  ><span class="mobile-label"
                    >Oldest
                  </span>{topic.oldest_available_at === null
                    ? "—"
                    : duration(now - topic.oldest_available_at)}</span
                ></summary
              >
              {#if expandedQueues.has(queueKey)}<div class="queue-content">
                  {#if topic.tasks.length}
                    <div class="table-scroll">
                      <table
                        class="task-table"
                        aria-label={`${topic.domain} ${topic.topic} task metadata`}
                      >
                        <thead
                          ><tr
                            ><th>Task / creator</th><th>State</th><th>Owner</th
                            ><th>Lease</th><th>Own count</th></tr
                          ></thead
                        ><tbody
                          >{#each topic.tasks as task}<tr
                              ><td
                                ><code>{task.id}</code><span class="secondary"
                                  >Created by {task.creator} · {age(
                                    task.created_at,
                                    now,
                                  )}</span
                                ></td
                              ><td
                                ><span
                                  class:held={task.state === "held"}
                                  class="task-state">{task.state}</span
                                ></td
                              ><td
                                >{#if task.owner}<strong
                                    >{task.owner.worker}</strong
                                  ><span class="secondary mono"
                                    >{task.owner.identity}</span
                                  >{:else}<span class="muted">Unowned</span
                                  >{/if}</td
                              ><td title={dateTime(task.lease_until)}
                                >{task.state === "held"
                                  ? task.lease_until > now
                                    ? `${duration(task.lease_until - now)} left`
                                    : "Expired since snapshot"
                                  : "Available"}</td
                              ><td>{count(task.owned_count)}</td></tr
                            >{/each}</tbody
                        >
                      </table>
                    </div>
                    {#if topic.truncated}<p class="limit-note">
                        Showing the first 100 tasks. More tasks are present in
                        this queue.
                      </p>{/if}
                  {:else}<p class="inline-empty">
                      This topic has no tasks.
                    </p>{/if}
                </div>{/if}
            </details>
          {/each}
        {:else}<div class="empty">
            <span aria-hidden="true">≋</span>
            <h3>Your queues are clear</h3>
            <p>Tasks, recent pollers, and declared topics will appear here.</p>
          </div>{/if}
      </section>

      <section class="panel" aria-labelledby="activity-heading">
        <div class="section-heading">
          <div>
            <p class="eyebrow">A PICTURE OF THE LAST {loadedHours} HOURS</p>
            <h2 id="activity-heading">Throughput &amp; latency</h2>
          </div>
          {#if choices.length}<label class="topic-picker"
              >Topic<select
                value={stats?.key ?? ""}
                onchange={(event) => {
                  selectedStats = event.currentTarget.value;
                }}
                >{#each choices as choice}<option value={choice.key}
                    >{choice.domain} · {choice.topic}</option
                  >{/each}</select
              ></label
            >{/if}
        </div>
        {#if stats}
          <div class="activity-grid">
            <article class="chart-card">
              <h3>Task throughput</h3>
              <div class="chart-totals">
                <span><strong>{count(totals.created)}</strong> created</span
                ><span><strong>{count(totals.consumed)}</strong> consumed</span>
              </div>
              <HourlyChart rows={hourly} kind="throughput" />
            </article>
            <article class="chart-card">
              <h3>Time to work &amp; finish</h3>
              <div class="chart-totals">
                <span
                  ><strong
                    >{duration(
                      totals.waitCount
                        ? totals.waitSum / totals.waitCount
                        : null,
                    )}</strong
                  > average wait</span
                ><span
                  ><strong
                    >{duration(
                      totals.runCount ? totals.runSum / totals.runCount : null,
                    )}</strong
                  > average run</span
                >
              </div>
              <HourlyChart rows={hourly} kind="latency" />
            </article>
          </div>
          <div class="activity-summary">
            <span><strong>{count(totals.reowned)}</strong> re-owns</span><span
              ><strong>{count(totals.empty)}</strong> empty polls</span
            ><span
              ><strong
                >{duration(totals.waitCount ? totals.waitMax : null)}</strong
              > max wait</span
            ><span
              ><strong
                >{duration(totals.runCount ? totals.runMax : null)}</strong
              > max run</span
            ><small>Local times · current hour is partial</small>
          </div>
          <details class="hourly-data" bind:open={hourlyOpen}>
            <summary
              >Hourly data <span class="muted"
                >({loadedHours} {loadedHours === 1 ? "hour" : "hours"})</span
              ></summary
            >{#if hourlyOpen}<div class="table-scroll">
                <table aria-label="Exact hourly activity and latency">
                  <thead
                    ><tr
                      ><th>Hour</th><th>Created</th><th>Consumed</th><th
                        >Re-owns</th
                      ><th>Empty polls</th><th>Avg / max wait</th><th
                        >Avg / max run</th
                      ></tr
                    ></thead
                  ><tbody
                    >{#each reverseHourly as row}<tr
                        ><td title={dateTime(row.hour)}
                          >{hourLabel(row.hour)}</td
                        ><td>{count(row.created)}</td><td
                          >{count(row.consumed)}</td
                        ><td>{count(row.reowned)}</td><td
                          >{count(row.empty_polls)}</td
                        ><td
                          >{duration(
                            row.wait_count
                              ? row.wait_ms_sum / row.wait_count
                              : null,
                          )} / {duration(
                            row.wait_count ? row.wait_ms_max : null,
                          )}</td
                        ><td
                          >{duration(
                            row.run_count
                              ? row.run_ms_sum / row.run_count
                              : null,
                          )} / {duration(
                            row.run_count ? row.run_ms_max : null,
                          )}</td
                        ></tr
                      >{/each}</tbody
                  >
                </table>
              </div>{/if}
          </details>
        {:else}<div class="empty">
            <span aria-hidden="true">⌁</span>
            <h3>No activity to chart</h3>
            <p>
              Hourly throughput and latency appear as tasks move through your
              topics.
            </p>
          </div>{/if}
      </section>

      <section class="panel" aria-labelledby="local-heading">
        <div class="section-heading">
          <div>
            <p class="eyebrow">DURABLE RECORDS, CLOSE TO HOME</p>
            <h2 id="local-heading">This machine</h2>
          </div>
          <p class="section-note">All local agent sessions</p>
        </div>
        <div class="subsection">
          <h3>
            Pending requests <span class="counter">{pending.length}</span>
          </h3>
          {#if pending.length}<div class="table-scroll">
              <table aria-label="Pending local requests">
                <thead
                  ><tr
                    ><th>Request / agent</th><th>Domain / topic</th><th
                      >State</th
                    ><th>Submitted</th></tr
                  ></thead
                ><tbody
                  >{#each pending as request}<tr
                      ><td
                        ><strong class="mono">{request.request_id}</strong><span
                          class="secondary mono">{request.agent_id}</span
                        ></td
                      ><td
                        ><strong class="mono">{request.topic}</strong><span
                          class="secondary mono">{request.domain}</span
                        ></td
                      ><td
                        ><span
                          class:unknown={request.state === "submission_unknown"}
                          class="task-state"
                          >{request.state === "submission_unknown"
                            ? "Submission unknown"
                            : "Pending"}</span
                        ></td
                      ><td title={dateTime(request.created_at)}
                        >{age(request.created_at, now)}</td
                      ></tr
                    >{/each}</tbody
                >
              </table>
            </div>{:else}<p class="inline-empty">
              No pending requests on this machine.
            </p>{/if}
        </div>
        <div class="subsection inbox">
          <div class="inbox-heading">
            <h3>
              Inbox <span class="counter">{snapshot.local.replies.length}</span>
            </h3>
            <label class="checkbox"
              ><input
                type="checkbox"
                bind:checked={showUnmatched}
                onchange={() => {
                  replyPage = 1;
                }}
              />Unmatched only
              <span class="counter">{unmatchedCount}</span></label
            >
          </div>
          {#if visibleReplies.length}<div class="reply-list">
              {#each visibleReplies as reply (JSON.stringify( [reply.agent_id, reply.task_id] ))}{@const replyKey =
                  JSON.stringify([reply.agent_id, reply.task_id])}
                <details
                  class="reply"
                  ontoggle={(event) => {
                    if (event.currentTarget.open) expandedReplies.add(replyKey);
                    else expandedReplies.delete(replyKey);
                  }}
                >
                  <summary
                    ><span
                      ><strong class="mono"
                        >{reply.request_id ?? "Unmatched reply"}</strong
                      ><span class="secondary"
                        >Agent {reply.agent_id} · from {reply.creator}</span
                      ></span
                    ><span
                      class="reply-received"
                      title={dateTime(reply.received_at)}
                      >{age(reply.received_at, now)}<span
                        class="chevron"
                        aria-hidden="true">›</span
                      ></span
                    ></summary
                  >{#if expandedReplies.has(replyKey)}<div
                      class="reply-content"
                    >
                      <dl>
                        <div>
                          <dt>Topic</dt>
                          <dd class="mono">{reply.topic}</dd>
                        </div>
                        <div>
                          <dt>Task ID</dt>
                          <dd class="mono">{reply.task_id}</dd>
                        </div>
                        <div>
                          <dt>Received</dt>
                          <dd>{dateTime(reply.received_at)}</dd>
                        </div>
                      </dl>
                      <pre>{json(reply.data)}</pre>
                    </div>{/if}
                </details>{/each}
            </div>
            <nav class="pagination" aria-label="Inbox pages">
              <span
                >{(currentPage - 1) * pageSize + 1}–{Math.min(
                  currentPage * pageSize,
                  replies.length,
                )} of {replies.length}</span
              ><button
                type="button"
                disabled={currentPage <= 1}
                onclick={() => {
                  replyPage = currentPage - 1;
                }}>Previous</button
              ><button
                type="button"
                disabled={currentPage >= pages}
                onclick={() => {
                  replyPage = currentPage + 1;
                }}>Next</button
              >
            </nav>{:else}<p class="inline-empty">
              {showUnmatched
                ? "No unmatched replies."
                : "Your inbox is empty. Replies saved by an agent appear here."}
            </p>{/if}
        </div>
      </section>
      <footer>
        Tasklease · local monitor <span
          >Snapshot {dateTime(snapshot.generated_at)}</span
        >
      </footer>
    {:else if loading}
      <div class="initial-loading" role="status">
        <span class="loading-ring" aria-hidden="true"></span>
        <h2>Loading your task board</h2>
        <p>Reading domain snapshots and local agent records.</p>
      </div>
    {:else}<div class="initial-loading">
        <h2>No snapshot yet</h2>
        <p>Refresh to connect to your local monitor.</p>
      </div>{/if}
  </main>
</div>

<style>
  :global(*) {
    box-sizing: border-box;
  }
  :global(html) {
    scroll-behavior: smooth;
  }
  :global(body) {
    margin: 0;
    color: #24314b;
    background: #f5f7fb;
    font-family:
      Inter,
      ui-sans-serif,
      system-ui,
      -apple-system,
      BlinkMacSystemFont,
      "Segoe UI",
      sans-serif;
    font-size: 14px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }
  :global(button),
  :global(input),
  :global(select) {
    font: inherit;
  }
  :global(button),
  :global(input),
  :global(select),
  :global(summary) {
    outline-offset: 4px;
  }
  :global(:focus-visible) {
    outline: 2px solid #3859d6;
  }
  :global(button) {
    cursor: pointer;
  }
  :global(button:disabled) {
    cursor: default;
    opacity: 0.55;
  }
  :global(a) {
    color: inherit;
  }
  :global(h1),
  :global(h2),
  :global(h3),
  :global(p) {
    margin: 0;
  }
  :global(h2) {
    font-size: 1.1rem;
    font-weight: 650;
    letter-spacing: -0.025em;
  }
  :global(h3) {
    font-size: 0.9rem;
    font-weight: 650;
  }
  .shell {
    max-width: 1440px;
    margin: 0 auto;
    padding: 0 48px;
  }
  .masthead {
    display: flex;
    align-items: center;
    justify-content: space-between;
    min-height: 88px;
    border-bottom: 1px solid #e0e5ef;
    gap: 20px;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 10px;
    text-decoration: none;
    font-size: 1.2rem;
    font-weight: 750;
    letter-spacing: -0.04em;
  }
  .brand-mark {
    display: grid;
    grid-template-columns: repeat(2, 7px);
    gap: 3px;
    padding: 8px;
    background: #3859d6;
    border-radius: 9px;
  }
  .brand-mark i {
    width: 7px;
    height: 7px;
    background: #fff;
    border-radius: 1.5px;
  }
  .brand-mark i:nth-child(2) {
    opacity: 0.5;
  }
  .local-label {
    margin-left: 12px;
    padding-left: 18px;
    border-left: 1px solid #d5dce9;
    font-size: 0.7rem;
    font-weight: 650;
    letter-spacing: 0.14em;
    color: #68768d;
  }
  .read-only {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 0.75rem;
    color: #68768d;
  }
  main {
    padding-top: 38px;
  }
  .page-heading {
    display: flex;
    justify-content: space-between;
    align-items: end;
    gap: 24px;
    margin-bottom: 28px;
  }
  .eyebrow {
    color: #68768d;
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.13em;
    margin-bottom: 7px;
  }
  h1 {
    font-size: clamp(1.8rem, 3vw, 2.3rem);
    line-height: 1.2;
    font-weight: 650;
    letter-spacing: -0.055em;
  }
  .subtitle {
    margin-top: 10px;
    color: #68768d;
    font-size: 0.85rem;
  }
  .refresh-controls {
    text-align: right;
  }
  form {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  form label {
    font-size: 0.75rem;
  }
  form input {
    width: 74px;
    height: 36px;
    padding: 7px 9px;
    border: 1px solid #d9e0ec;
    border-radius: 7px;
    background: #fff;
    color: #24314b;
  }
  button {
    min-height: 34px;
    padding: 6px 12px;
    border: 1px solid #dce2ed;
    border-radius: 7px;
    background: #fff;
    color: #42516e;
    font-size: 0.75rem;
    font-weight: 600;
  }
  .primary {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 7px;
    min-width: 100px;
    min-height: 36px;
    color: #fff;
    background: #3859d6;
    border-color: #3859d6;
    box-shadow: 0 2px 4px #3859d614;
  }
  .primary span {
    font-size: 1.2rem;
    line-height: 1;
  }
  .refresh-status {
    display: flex;
    align-items: center;
    justify-content: end;
    gap: 6px;
    margin-top: 9px;
    color: #68768d;
    font-size: 0.72rem;
  }
  .status-dot {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: #299d80;
  }
  .status-dot.warning-dot {
    background: #c18736;
  }
  .field-error {
    margin-top: 6px;
    color: #a04d21;
    font-size: 0.75rem;
  }
  .notice {
    display: flex;
    gap: 6px 12px;
    flex-wrap: wrap;
    padding: 14px 18px;
    margin-bottom: 20px;
    border: 1px solid #efdaa9;
    border-radius: 9px;
    font-size: 0.8rem;
  }
  .notice.warning {
    color: #805d23;
    background: #fff9eb;
  }
  .notice ul {
    margin: 0;
    padding-left: 18px;
    overflow-wrap: anywhere;
  }
  .metrics {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 17px;
    margin-bottom: 28px;
  }
  .metric {
    padding: 19px 22px;
    background: #fff;
    border: 1px solid #e0e6ef;
    border-radius: 11px;
    box-shadow: 0 2px 3px #26395603;
  }
  .metric > span {
    color: #6e7b92;
    font-size: 0.75rem;
  }
  .metric strong {
    display: block;
    margin: 6px 0 5px;
    font-size: 2rem;
    font-weight: 620;
    letter-spacing: -0.05em;
    line-height: 1.3;
  }
  .metric small {
    color: #68768d;
    font-size: 0.72rem;
  }
  .panel {
    margin-bottom: 24px;
    background: #fff;
    border: 1px solid #e0e6ef;
    border-radius: 11px;
    box-shadow: 0 2px 3px #26395603;
    overflow: hidden;
  }
  .section-heading {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 18px;
    padding: 22px 24px 19px;
  }
  .section-heading .eyebrow {
    margin-bottom: 5px;
    font-size: 0.72rem;
  }
  .section-note {
    font-size: 0.72rem;
    color: #68768d;
    text-align: right;
  }
  .counter {
    display: inline-block;
    padding: 1px 7px;
    margin-left: 5px;
    color: #68768d;
    background: #f1f4f9;
    font-size: 0.72rem;
    font-weight: 500;
    vertical-align: middle;
    border-radius: 5px;
    letter-spacing: 0;
  }
  .table-scroll {
    overflow-x: auto;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    text-align: left;
    font-size: 0.75rem;
  }
  th {
    padding: 10px 24px;
    color: #68768d;
    background: #fafbfe;
    border-top: 1px solid #eef1f6;
    border-bottom: 1px solid #e9edf4;
    font-size: 0.7rem;
    font-weight: 600;
    white-space: nowrap;
  }
  td {
    padding: 14px 24px;
    border-bottom: 1px solid #eff2f7;
    vertical-align: middle;
    color: #5b6982;
  }
  tr:last-child td {
    border-bottom: 0;
  }
  td strong {
    color: #3e4c66;
    font-weight: 550;
  }
  .secondary {
    display: block;
    color: #68768d;
    font-size: 0.72rem;
    margin-top: 3px;
    overflow-wrap: anywhere;
  }
  .mono,
  code {
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
    overflow-wrap: anywhere;
  }
  code {
    color: #66748d;
    font-size: 0.72rem;
  }
  .worker-label {
    display: inline-flex;
    gap: 8px;
    align-items: center;
  }
  .worker-icon {
    color: #7b8bc8;
    font-size: 1rem;
  }
  .empty {
    padding: 30px 24px 38px;
    text-align: center;
    border-top: 1px solid #f0f2f7;
  }
  .empty > span {
    display: inline-block;
    margin-bottom: 8px;
    color: #a8b4ce;
    font-size: 1.5rem;
  }
  .empty h3 {
    color: #52617b;
    margin-bottom: 5px;
    font-size: 0.85rem;
  }
  .empty p {
    color: #68768d;
    font-size: 0.75rem;
  }
  .queue-head,
  .queue > summary {
    display: grid;
    grid-template-columns: minmax(220px, 1fr) 110px 90px 150px;
    gap: 14px;
    align-items: center;
  }
  .queue-head {
    padding: 10px 24px;
    color: #68768d;
    background: #fafbfe;
    border-top: 1px solid #eef1f6;
    border-bottom: 1px solid #e9edf4;
    font-size: 0.7rem;
  }
  .queue-head > span:first-child {
    padding-left: 21px;
  }
  details > summary {
    cursor: pointer;
    list-style: none;
  }
  details > summary::-webkit-details-marker {
    display: none;
  }
  .queue {
    border-bottom: 1px solid #edf1f7;
  }
  .queue:last-child {
    border-bottom: none;
  }
  .queue > summary {
    padding: 16px 24px;
    font-size: 0.75rem;
  }
  .queue > summary:hover,
  .reply > summary:hover {
    background: #fafbff;
  }
  .queue-name {
    display: flex;
    align-items: center;
    gap: 11px;
    min-width: 0;
  }
  .queue-name strong {
    color: #4b5b78;
    font-size: 0.74rem;
    font-weight: 550;
  }
  .chevron {
    display: inline-block;
    color: #68768d;
    font-size: 1.2rem;
    line-height: 1;
  }
  details[open] > summary .chevron {
    transform: rotate(90deg);
  }
  .declared {
    display: inline-block;
    padding: 0 4px;
    margin-left: 8px;
    border: 1px solid #e3e8f1;
    border-radius: 3px;
    color: #68768d;
    font-family: system-ui, sans-serif;
    font-size: 0.72rem;
  }
  .badge {
    display: inline-block;
    min-width: 25px;
    padding: 2px 7px;
    background: #f3f5f9;
    color: #68768d;
    text-align: center;
    border-radius: 5px;
    font-size: 0.72rem;
    font-weight: 600;
  }
  .badge.has-tasks {
    color: #4769ce;
    background: #edf1ff;
  }
  .badge.neutral {
    color: #71819d;
    background: #f0f3f8;
  }
  .oldest {
    color: #68768d;
    font-size: 0.7rem;
  }
  .mobile-label {
    display: none;
  }
  .queue-content {
    border-top: 1px solid #e9eef6;
    background: #fcfdff;
  }
  .task-table th {
    background: #f5f7fc;
  }
  .task-state {
    display: inline-block;
    padding: 3px 7px;
    border-radius: 5px;
    color: #557865;
    background: #edf6f0;
    font-size: 0.7rem;
    white-space: nowrap;
  }
  .task-state.held {
    color: #5871b5;
    background: #edf1fb;
  }
  .task-state.unknown {
    color: #a27b35;
    background: #fcf5e6;
  }
  .limit-note {
    padding: 10px 24px;
    border-top: 1px solid #edf1f7;
    color: #68768d;
    font-size: 0.72rem;
  }
  .inline-empty {
    padding: 18px 24px;
    color: #68768d;
    font-size: 0.75rem;
  }
  .topic-picker {
    display: flex;
    align-items: center;
    gap: 8px;
    color: #68768d;
    font-size: 0.72rem;
  }
  select {
    max-width: 350px;
    width: 100%;
    padding: 6px 9px;
    color: #566784;
    background: #fbfcff;
    border: 1px solid #dfe5ef;
    border-radius: 6px;
    font-size: 0.72rem;
  }
  .activity-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    padding: 0 24px;
  }
  .chart-card {
    min-width: 0;
    padding: 0 15px 16px;
  }
  .chart-card:first-child {
    border-right: 1px solid #edf1f7;
    padding-left: 0;
    padding-right: 24px;
  }
  .chart-card:last-child {
    padding-left: 24px;
    padding-right: 0;
  }
  .chart-card h3 {
    color: #65738b;
    font-size: 0.74rem;
    font-weight: 550;
  }
  .chart-totals {
    display: flex;
    gap: 20px;
    margin: 9px 0 14px;
  }
  .chart-totals span {
    color: #68768d;
    font-size: 0.72rem;
  }
  .chart-totals strong {
    margin-right: 5px;
    color: #4b5b76;
    font-size: 1.1rem;
    font-weight: 600;
    letter-spacing: -0.04em;
  }
  .activity-summary {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 24px;
    padding: 14px 24px;
    border-top: 1px solid #edf1f7;
    color: #68768d;
    font-size: 0.72rem;
  }
  .activity-summary strong {
    color: #5c6c89;
    font-weight: 600;
    margin-right: 4px;
  }
  .activity-summary small {
    margin-left: auto;
    font-size: 0.7rem;
    color: #68768d;
  }
  .hourly-data {
    border-top: 1px solid #edf1f7;
  }
  .hourly-data > summary {
    padding: 11px 24px;
    color: #7c8ba4;
    font-size: 0.72rem;
  }
  .hourly-data > summary:before {
    content: "+";
    margin-right: 7px;
  }
  .hourly-data[open] > summary:before {
    content: "−";
  }
  .hourly-data .table-scroll {
    max-height: 340px;
  }
  .hourly-data th {
    position: sticky;
    top: 0;
  }
  .subsection {
    border-top: 1px solid #edf1f7;
  }
  .subsection > h3 {
    padding: 18px 24px 14px;
    font-size: 0.78rem;
    color: #5e6e87;
  }
  .inbox-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 18px 24px 14px;
    gap: 18px;
  }
  .inbox-heading h3 {
    font-size: 0.78rem;
    color: #5e6e87;
  }
  .checkbox {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: #68768d;
    font-size: 0.72rem;
  }
  .checkbox input {
    accent-color: #3859d6;
  }
  .checkbox .counter {
    margin-left: 0;
  }
  .reply {
    margin: 0 24px;
    border-top: 1px solid #edf1f7;
  }
  .reply > summary {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 14px;
    padding: 12px 2px;
    font-size: 0.7rem;
  }
  .reply > summary strong {
    font-weight: 550;
    color: #5a6b89;
  }
  .reply-received {
    display: inline-flex;
    align-items: center;
    gap: 14px;
    color: #68768d;
    font-size: 0.72rem;
    white-space: nowrap;
  }
  .reply-content {
    padding-bottom: 16px;
  }
  .reply-content dl {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 22px;
    margin: 0 0 12px;
    font-size: 0.72rem;
  }
  .reply-content dt {
    color: #68768d;
    margin-bottom: 2px;
  }
  .reply-content dd {
    margin: 0;
    color: #60708b;
  }
  pre {
    margin: 0;
    padding: 14px 16px;
    max-height: 320px;
    overflow: auto;
    color: #65728d;
    background: #f6f8fc;
    border: 1px solid #e8edf5;
    border-radius: 7px;
    font-size: 0.7rem;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .pagination {
    display: flex;
    align-items: center;
    justify-content: end;
    gap: 8px;
    padding: 12px 24px 18px;
  }
  .pagination > span {
    margin-right: auto;
    color: #68768d;
    font-size: 0.72rem;
  }
  .pagination button {
    min-height: 28px;
    padding: 3px 9px;
    font-size: 0.7rem;
  }
  footer {
    display: flex;
    justify-content: space-between;
    gap: 20px;
    margin: 30px 0 32px;
    color: #68768d;
    font-size: 0.7rem;
  }
  .muted {
    color: #68768d;
  }
  .initial-loading {
    padding: 100px 20px 140px;
    text-align: center;
    color: #68768d;
  }
  .initial-loading h2 {
    margin: 16px 0 6px;
    color: #66758e;
  }
  .initial-loading p {
    font-size: 0.8rem;
  }
  .loading-ring {
    display: inline-block;
    width: 28px;
    height: 28px;
    border: 2px solid #dee4f3;
    border-top-color: #3859d6;
    border-radius: 50%;
    animation: spin 1s linear infinite;
  }
  .spinning {
    animation: spin 1s linear infinite;
  }
  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
  @media (max-width: 1050px) {
    .shell {
      padding: 0 28px;
    }
    .queue-head,
    .queue > summary {
      grid-template-columns: minmax(180px, 1fr) 90px 65px 115px;
      gap: 10px;
    }
    .metric {
      padding: 17px;
    }
    .section-heading {
      align-items: start;
    }
    .topic-picker {
      flex-direction: column;
      align-items: start;
      max-width: 45%;
    }
  }
  @media (max-width: 760px) {
    .shell {
      padding: 0 18px;
    }
    .masthead {
      min-height: 70px;
    }
    .local-label {
      display: none;
    }
    main {
      padding-top: 26px;
    }
    .page-heading {
      align-items: start;
      flex-direction: column;
      gap: 20px;
    }
    .refresh-controls {
      width: 100%;
      text-align: left;
    }
    .refresh-controls form {
      justify-content: start;
    }
    .refresh-status {
      justify-content: start;
    }
    .metrics {
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .metric {
      padding: 15px 17px;
    }
    .metric strong {
      font-size: 1.8rem;
    }
    .section-heading {
      padding: 18px;
      flex-wrap: wrap;
      gap: 10px;
    }
    .section-note {
      text-align: left;
    }
    th,
    td {
      padding-left: 18px;
      padding-right: 18px;
    }
    .queue-head {
      display: none;
    }
    .queue > summary {
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      padding: 14px 18px;
      gap: 10px;
    }
    .queue-name {
      grid-column: 1 / -1;
    }
    .queue-number:first-of-type {
      padding-left: 22px;
    }
    .mobile-label {
      display: inline;
      color: #68768d;
      font-size: 0.7rem;
    }
    .queue-number {
      white-space: nowrap;
    }
    .oldest {
      font-size: 0.72rem;
      white-space: nowrap;
    }
    .activity-grid {
      grid-template-columns: 1fr;
      padding: 0 18px;
    }
    .chart-card:first-child {
      border-right: 0;
      border-bottom: 1px solid #edf1f7;
      padding: 0 0 20px;
    }
    .chart-card:last-child {
      padding: 20px 0;
    }
    .activity-summary {
      padding: 14px 18px;
      gap: 10px 18px;
    }
    .activity-summary small {
      flex-basis: 100%;
      margin-left: 0;
    }
    .topic-picker {
      max-width: 100%;
      width: 100%;
    }
    .topic-picker select {
      max-width: none;
    }
    .subsection > h3,
    .inbox-heading {
      padding-left: 18px;
      padding-right: 18px;
    }
    .reply {
      margin: 0 18px;
    }
    .reply > summary {
      align-items: start;
    }
    .reply-received {
      max-width: 100px;
      white-space: normal;
      text-align: right;
    }
    .inbox-heading {
      align-items: start;
      flex-wrap: wrap;
      gap: 8px;
    }
    footer {
      flex-direction: column;
      gap: 6px;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    :global(html) {
      scroll-behavior: auto;
    }
    .loading-ring,
    .spinning {
      animation: none;
    }
  }
</style>
