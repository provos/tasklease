<script lang="ts">
  import { onMount } from "svelte";
  import { SvelteSet } from "svelte/reactivity";
  import HourlyChart from "./HourlyChart.svelte";
  import { age, count, dateTime, duration, hourLabel, json } from "./format.ts";
  import {
    health,
    type Level,
    type TopicHealth,
    type WorkerState,
  } from "./health.ts";
  import {
    decodeSnapshot,
    hourBuckets,
    statsTopics,
    topics,
    type Snapshot,
  } from "./model.ts";

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
  let allAlerts = $state(false);
  const alertLimit = 6;
  const expandedQueues = new SvelteSet<string>();
  const expandedReplies = new SvelteSet<string>();
  let alive = false;
  let sequence = 0;
  let controller: AbortController | undefined;
  const pageSize = 20;

  const board = $derived(snapshot ? health(snapshot) : null);
  const problems = $derived(board?.alerts.length ?? 0);
  const boardLevel = $derived<Level>(
    board?.alerts.some((alert) => alert.level === "critical")
      ? "critical"
      : problems
        ? "warning"
        : "ok",
  );
  const singleDomain = $derived(
    snapshot?.domains.length === 1 ? snapshot.domains[0]!.domain : null,
  );
  const machinesUp = $derived(
    board?.machines.filter((m) => m.state === "busy" || m.state === "live")
      .length ?? 0,
  );
  const machinesTracked = $derived(
    board?.machines.filter((m) => m.state !== "inactive").length ?? 0,
  );
  const inactiveMachines = $derived(
    board?.machines.filter((m) => m.state === "inactive") ?? [],
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
        waitSum: 0,
        waitCount: 0,
        waitMax: 0,
        runSum: 0,
        runCount: 0,
        runMax: 0,
      },
    ),
  );

  const stateLabels: Record<WorkerState, string> = {
    busy: "busy",
    lapsed: "lease lapsed",
    live: "idle",
    waiting: "waiting for capacity",
    silent: "silent",
    down: "down",
    inactive: "inactive",
  };
  const stateLevels: Record<WorkerState, Level> = {
    busy: "ok",
    lapsed: "warning",
    live: "ok",
    waiting: "idle",
    silent: "warning",
    down: "critical",
    inactive: "idle",
  };

  function updated(timestamp: number, current: number): string {
    return current - timestamp < 5_000 ? "just now" : age(timestamp, current);
  }

  function oldest(topic: TopicHealth): string {
    return topic.oldestAvailableAt === null
      ? "—"
      : duration(topic.generatedAt - topic.oldestAvailableAt);
  }

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
    if (
      requestedHours === undefined ||
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
  <title
    >{problems ? `(${problems}) tasklease monitor` : "tasklease monitor"}</title
  >
  <meta
    name="description"
    content="Read-only local view of tasklease queues, machines, and requests."
  />
</svelte:head>

{#snippet dot(level: Level)}<span class="dot {level}" aria-hidden="true"
  ></span>{/snippet}

<div class="shell">
  <header class="topbar">
    <span class="brand"
      ><span class="brand-mark" aria-hidden="true"
        ><i></i><i></i><i></i><i></i></span
      >tasklease</span
    >
    {#if board}
      <span class="pill {boardLevel}" role="status"
        >{@render dot(boardLevel)}{problems
          ? `${problems} ${problems === 1 ? "problem" : "problems"}`
          : "All healthy"}</span
      >
      <span class="context">
        {#if singleDomain}<span class="mono">{singleDomain}</span>{:else}{count(
            snapshot?.domains.length ?? 0,
          )} domains{/if}
        <span>{count(board.waiting)} waiting · {count(board.held)} running</span
        >
        {#if machinesTracked}<span
            >{machinesUp}/{machinesTracked} machines up</span
          >{/if}
      </span>
    {/if}
    <div class="refresh">
      <p class="refresh-status" aria-live="polite">
        <span
          class="status-dot"
          class:stale={refreshError || snapshot?.errors?.length}
        ></span>{lastUpdated === null
          ? loading
            ? "Connecting…"
            : "Waiting for a snapshot"
          : `Updated ${updated(lastUpdated, now)}`}
      </p>
      <form
        onsubmit={(event) => {
          event.preventDefault();
          applyHours();
        }}
      >
        <label for="hours">History <span class="muted">(h)</span></label>
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
          ><span class:spinning={loading} aria-hidden="true">↻</span><span
            class="visually-hidden">{loading ? "Refreshing" : "Refresh"}</span
          ></button
        >
      </form>
    </div>
  </header>
  {#if inputError}<p id="hours-error" class="field-error" role="alert">
      {inputError}
    </p>{/if}

  <main id="overview">
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

    {#if snapshot && board}
      {#if board.alerts.length}
        <section class="alerts" aria-label="Problems">
          <ul>
            {#each allAlerts ? board.alerts : board.alerts.slice(0, alertLimit) as alert (alert.key)}
              <li class={alert.level}>
                {@render dot(alert.level)}<strong class="mono"
                  >{alert.subject}</strong
                ><span>{alert.detail}</span>
              </li>
            {/each}
            {#if board.alerts.length > alertLimit}<li class="more">
                <button
                  type="button"
                  onclick={() => {
                    allAlerts = !allAlerts;
                  }}
                  >{allAlerts
                    ? "Show fewer"
                    : `Show ${board.alerts.length - alertLimit} more`}</button
                >
              </li>{/if}
          </ul>
        </section>
      {/if}

      <div class="board">
        <section class="panel" aria-labelledby="topics-heading">
          <div class="panel-heading">
            <h2 id="topics-heading">
              Topics <span class="counter">{board.topics.length}</span>
            </h2>
            <p class="section-note">Most urgent first · expand for tasks</p>
          </div>
          {#if board.topics.length || board.otherTopics.length}
            <div class="topic-head" aria-hidden="true">
              <span>Topic</span><span>Workers</span><span>Waiting</span><span
                >Running</span
              ><span>Oldest</span><span>Avg wait</span>
            </div>
            {#each [...board.topics, ...board.otherTopics] as topic, index (topic.key)}
              {#if index === board.topics.length}
                <p class="topic-group">Replies and undeclared topics</p>
              {/if}
              <details
                class="topic {topic.level}"
                class:secondary-topic={index >= board.topics.length}
                open={expandedQueues.has(topic.key)}
                ontoggle={(event) => {
                  if (event.currentTarget.open) expandedQueues.add(topic.key);
                  else expandedQueues.delete(topic.key);
                }}
              >
                <summary title={singleDomain ? undefined : topic.domain}
                  ><span class="topic-name"
                    >{@render dot(topic.level)}<strong class="mono"
                      >{topic.topic}</strong
                    >{#if topic.reasons.length}<span class="reason"
                        >{topic.reasons.join(" · ")}</span
                      >{/if}{#if !singleDomain}<span class="domain mono"
                        >{topic.domain}</span
                      >{/if}</span
                  ><span class="num" data-label="Workers"
                    >{topic.role === "work"
                      ? `${topic.serving}/${topic.seen}`
                      : "—"}</span
                  ><span class="num" data-label="Waiting"
                    >{count(topic.available)}</span
                  ><span class="num" data-label="Running"
                    >{count(topic.held)}</span
                  ><span class="num" data-label="Oldest">{oldest(topic)}</span
                  ><span class="num" data-label="Avg wait"
                    >{duration(topic.averageWaitMs)}</span
                  ></summary
                >
                {#if expandedQueues.has(topic.key)}<div class="topic-content">
                    {#if topic.tasks.length}
                      <div class="table-scroll">
                        <table
                          class="stack"
                          aria-label={`${topic.domain} ${topic.topic} tasks`}
                        >
                          <thead
                            ><tr
                              ><th>Task / creator</th><th>State</th><th
                                >Owner</th
                              ><th>Lease</th><th>Own count</th></tr
                            ></thead
                          ><tbody
                            >{#each topic.tasks as task}{@const lapsed =
                                task.state === "held" &&
                                task.lease_until <= topic.generatedAt}<tr
                                ><td class="wide"
                                  ><code>{task.id}</code><span class="secondary"
                                    >Created by {task.creator} · {age(
                                      task.created_at,
                                      topic.generatedAt,
                                    )}</span
                                  ></td
                                ><td data-label="State"
                                  ><span
                                    class="tag"
                                    class:held={task.state === "held"}
                                    class:warning={lapsed}
                                    >{lapsed
                                      ? "lease lapsed"
                                      : task.state}</span
                                  ></td
                                ><td data-label="Owner"
                                  >{#if task.owner}<strong
                                      >{task.owner.worker}</strong
                                    ><span class="secondary mono"
                                      >{task.owner.identity}</span
                                    >{:else}<span class="muted">Unowned</span
                                    >{/if}</td
                                ><td
                                  data-label="Lease"
                                  title={dateTime(task.lease_until)}
                                  >{task.state === "held"
                                    ? lapsed
                                      ? `Lapsed ${duration(topic.generatedAt - task.lease_until)} ago`
                                      : `${duration(task.lease_until - topic.generatedAt)} left`
                                    : "—"}</td
                                ><td data-label="Own count"
                                  ><span
                                    class:warn-text={task.owned_count > 1}
                                    title={task.owned_count > 1
                                      ? "Owned more than once: an earlier lease expired"
                                      : undefined}
                                    >{count(task.owned_count)}</span
                                  ></td
                                ></tr
                              >{/each}</tbody
                          >
                        </table>
                      </div>
                      {#if topic.truncated}<p class="limit-note">
                          Showing the first 100 of {count(
                            topic.available + topic.held,
                          )} tasks.
                        </p>{/if}
                    {:else}<p class="inline-empty">
                        This topic has no tasks.
                      </p>{/if}
                  </div>{/if}
              </details>
            {/each}
          {:else}<div class="empty">
              <h3>Your queues are clear</h3>
              <p>Tasks, recent pollers, and declared topics appear here.</p>
            </div>{/if}
        </section>

        <section class="panel" aria-labelledby="machines-heading">
          <div class="panel-heading">
            <h2 id="machines-heading">
              Machines <span class="counter">{machinesTracked}</span>
            </h2>
            <p class="section-note">Workers on declared topics</p>
          </div>
          {#if board.undeclaredDomains.length}<p class="hint">
              No declared topics in {board.undeclaredDomains.join(", ")}.
              Declare work topics with <code>tl topic declare</code> to track machines.
            </p>{/if}
          <ul class="machines">
            {#each board.machines.filter((m) => m.state !== "inactive") as machine (machine.identity)}
              <li>
                <div class="machine-line">
                  {@render dot(machine.level)}<strong class="mono"
                    >{machine.identity}</strong
                  ><span class="machine-state {machine.level}"
                    >{machine.state === "busy"
                      ? `busy · ${machine.workers.filter((w) => w.state === "busy").length} running`
                      : machine.state === "live"
                        ? `idle · polled ${age(machine.lastPolledAt, machine.generatedAt)}`
                        : `${machine.state} · quiet ${duration(machine.generatedAt - machine.lastPolledAt)}`}</span
                  >
                </div>
                <div class="chips">
                  {#each machine.workers as worker (worker.domain + worker.worker)}
                    <span
                      class="chip {stateLevels[worker.state]}"
                      title={`${singleDomain ? "" : `${worker.domain} · `}${worker.topics.join(", ")} · last poll ${age(worker.lastPolledAt, worker.generatedAt)}${worker.task ? ` · ${worker.task.topic} task` : ""}`}
                      ><strong>{worker.worker}</strong>
                      {worker.state === "busy" && worker.task
                        ? `busy ${duration(worker.generatedAt - (worker.task.owned_at ?? worker.generatedAt))}`
                        : stateLabels[worker.state]}
                      <span class="chip-topic mono"
                        >{worker.topics.join(", ")}</span
                      ></span
                    >
                  {/each}
                </div>
              </li>
            {:else}
              <li class="inline-empty">
                No workers have polled a declared topic.
              </li>
            {/each}
          </ul>
          {#if inactiveMachines.length}<p class="limit-note">
              Inactive for a day: {inactiveMachines
                .map((m) => m.identity)
                .join(", ")}
            </p>{/if}
        </section>
      </div>

      <section class="panel" aria-labelledby="requests-heading">
        <div class="panel-heading">
          <h2 id="requests-heading">
            Pending requests <span class="counter">{board.requests.length}</span
            >
          </h2>
          <p class="section-note">From agents on this machine</p>
        </div>
        {#if board.requests.length}<div class="table-scroll">
            <table class="stack requests" aria-label="Pending local requests">
              <thead
                ><tr
                  ><th>Request</th><th>Topic</th><th>Status</th><th
                    >Submitted</th
                  ></tr
                ></thead
              ><tbody
                >{#each board.requests as item (item.request.agent_id + item.request.request_id)}<tr
                    ><td class="wide"
                      ><span class="summary">{item.summary}</span><span
                        class="secondary mono"
                        title={item.request.request_id}
                        >{item.request.agent_id} · {item.request.request_id.slice(
                          0,
                          8,
                        )}</span
                      ></td
                    ><td data-label="Topic"
                      ><strong class="mono">{item.request.topic}</strong
                      >{#if !singleDomain}<span class="secondary mono"
                          >{item.request.domain}</span
                        >{/if}</td
                    ><td data-label="Status" class="status {item.level}"
                      >{@render dot(item.level)}{item.status}</td
                    ><td
                      data-label="Submitted"
                      title={dateTime(item.request.created_at)}
                      >{age(item.request.created_at, now)}</td
                    ></tr
                  >{/each}</tbody
              >
            </table>
          </div>{:else}<p class="inline-empty">
            No pending requests on this machine.
          </p>{/if}
      </section>

      <section class="panel" aria-labelledby="inbox-heading">
        <div class="panel-heading">
          <h2 id="inbox-heading">
            Inbox <span class="counter">{snapshot.local.replies.length}</span>
          </h2>
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
                >{#if expandedReplies.has(replyKey)}<div class="reply-content">
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
      </section>

      <section class="panel" aria-labelledby="activity-heading">
        <div class="panel-heading">
          <h2 id="activity-heading">
            Throughput &amp; latency <span class="muted"
              >· last {loadedHours} h</span
            >
          </h2>
          {#if choices.length}<label class="topic-picker"
              >Topic<select
                value={stats?.key ?? ""}
                onchange={(event) => {
                  selectedStats = event.currentTarget.value;
                }}
                >{#each choices as choice}<option value={choice.key}
                    >{singleDomain
                      ? ""
                      : `${choice.domain} · `}{choice.topic}</option
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
                      ><th>Avg / max wait</th><th>Avg / max run</th></tr
                    ></thead
                  ><tbody
                    >{#each reverseHourly as row}<tr
                        ><td title={dateTime(row.hour)}
                          >{hourLabel(row.hour)}</td
                        ><td>{count(row.created)}</td><td
                          >{count(row.consumed)}</td
                        ><td>{count(row.reowned)}</td><td
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
            <h3>No activity to chart</h3>
            <p>
              Hourly throughput and latency appear as tasks move through your
              topics.
            </p>
          </div>{/if}
      </section>
      <footer>
        tasklease · local monitor <span
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
  :global(:root) {
    color-scheme: light dark;
    --bg: #f5f7fb;
    --surface: #fff;
    --surface-subtle: #fafbfe;
    --surface-sunken: #f5f7fc;
    --fill: #f1f4f9;
    --border: #e0e6ef;
    --border-strong: #d9e0ec;
    --divider: #edf1f7;
    --text: #24314b;
    --text-2: #42516e;
    --text-3: #5b6982;
    --muted: #68768d;
    --faint: #7c8ba4;
    --icon: #a8b4ce;
    --accent: #3859d6;
    --accent-text: #4769ce;
    --accent-soft: #edf1ff;
    --accent-icon: #7b8bc8;
    --accent-shadow: #3859d614;
    --on-accent: #fff;
    --held-text: #5871b5;
    --held-bg: #edf1fb;
    --ok-text: #557865;
    --ok-bg: #edf6f0;
    --ok-dot: #299d80;
    --warn-text: #a27b35;
    --warn-bg: #fcf5e6;
    --warn-dot: #c18736;
    --notice-text: #805d23;
    --notice-bg: #fff9eb;
    --notice-border: #efdaa9;
    --error-text: #a04d21;
    --critical-text: #b42318;
    --critical-bg: #fdecea;
    --critical-dot: #d92d20;
    --shadow: #26395603;
    --series-1: #3859d6;
    --series-2: #168071;
    --chart-grid: #e6eaf1;
  }
  @media (prefers-color-scheme: dark) {
    :global(:root) {
      --bg: #0e131b;
      --surface: #151b26;
      --surface-subtle: #19202d;
      --surface-sunken: #111722;
      --fill: #222a39;
      --border: #273042;
      --border-strong: #323c51;
      --divider: #212939;
      --text: #e3e8f1;
      --text-2: #cbd3e1;
      --text-3: #a7b2c5;
      --muted: #8c97ab;
      --faint: #7a869c;
      --icon: #4e5a71;
      --accent: #4a68e6;
      --accent-text: #9db0ff;
      --accent-soft: #24305a;
      --accent-icon: #8597dc;
      --accent-shadow: transparent;
      --held-text: #a7b8ee;
      --held-bg: #232f4d;
      --ok-text: #93cfaa;
      --ok-bg: #1b3126;
      --ok-dot: #3cc39f;
      --warn-text: #e5bb6b;
      --warn-bg: #352a14;
      --warn-dot: #e0a347;
      --notice-text: #f0cf8a;
      --notice-bg: #2a2212;
      --notice-border: #574420;
      --error-text: #f39a72;
      --critical-text: #ff9e93;
      --critical-bg: #3a1815;
      --critical-dot: #f0624f;
      --shadow: transparent;
      --series-1: #7d95ff;
      --series-2: #3fc2a7;
      --chart-grid: #262f41;
    }
  }

  :global(*) {
    box-sizing: border-box;
  }
  :global(html) {
    scroll-behavior: smooth;
  }
  :global(body) {
    margin: 0;
    color: var(--text);
    background: var(--bg);
    font-family:
      Inter,
      ui-sans-serif,
      system-ui,
      -apple-system,
      BlinkMacSystemFont,
      "Segoe UI",
      sans-serif;
    font-size: 14px;
    line-height: 1.45;
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
    outline-offset: 3px;
  }
  :global(:focus-visible) {
    outline: 2px solid var(--accent);
  }
  :global(button) {
    cursor: pointer;
  }
  :global(button:disabled) {
    cursor: default;
    opacity: 0.55;
  }
  :global(h1),
  :global(h2),
  :global(h3),
  :global(p) {
    margin: 0;
  }
  :global(h2) {
    font-size: 0.95rem;
    font-weight: 650;
    letter-spacing: -0.015em;
  }
  :global(h3) {
    font-size: 0.85rem;
    font-weight: 650;
  }
  .shell {
    max-width: 1440px;
    margin: 0 auto;
    padding: 0 32px;
  }
  .visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }

  /* Top bar: one line of chrome. */
  .topbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 18px;
    min-height: 56px;
    padding: 8px 0;
    border-bottom: 1px solid var(--border);
  }
  .brand {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: 1.05rem;
    font-weight: 750;
    letter-spacing: -0.04em;
  }
  .brand-mark {
    display: grid;
    grid-template-columns: repeat(2, 6px);
    gap: 2px;
    padding: 6px;
    background: var(--accent);
    border-radius: 7px;
  }
  .brand-mark i {
    width: 6px;
    height: 6px;
    background: var(--on-accent);
    border-radius: 1.5px;
  }
  .brand-mark i:nth-child(2) {
    opacity: 0.5;
  }
  .pill {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 4px 11px;
    border-radius: 999px;
    font-size: 0.8rem;
    font-weight: 650;
  }
  .pill.critical {
    color: var(--critical-text);
    background: var(--critical-bg);
  }
  .pill.warning {
    color: var(--warn-text);
    background: var(--warn-bg);
  }
  .pill.ok {
    color: var(--ok-text);
    background: var(--ok-bg);
  }
  .context {
    display: inline-flex;
    flex-wrap: wrap;
    gap: 4px 14px;
    color: var(--muted);
    font-size: 0.78rem;
  }
  .context > :global(*) + :global(*)::before {
    content: "·";
    margin-right: 14px;
    color: var(--icon);
  }
  .refresh {
    display: flex;
    align-items: center;
    gap: 14px;
    margin-left: auto;
  }
  .refresh-status {
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--muted);
    font-size: 0.75rem;
    white-space: nowrap;
  }
  .status-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--ok-dot);
  }
  .status-dot.stale {
    background: var(--warn-dot);
  }
  form {
    display: flex;
    align-items: center;
    gap: 7px;
  }
  form label {
    font-size: 0.75rem;
    color: var(--muted);
  }
  form input {
    width: 62px;
    height: 30px;
    padding: 4px 8px;
    border: 1px solid var(--border-strong);
    border-radius: 6px;
    background: var(--surface);
    color: var(--text);
  }
  button {
    min-height: 30px;
    padding: 4px 10px;
    border: 1px solid var(--border-strong);
    border-radius: 6px;
    background: var(--surface);
    color: var(--text-2);
    font-size: 0.75rem;
    font-weight: 600;
  }
  .primary {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 34px;
    color: var(--on-accent);
    background: var(--accent);
    border-color: var(--accent);
    box-shadow: 0 2px 4px var(--accent-shadow);
  }
  .primary span[aria-hidden] {
    font-size: 1.05rem;
    line-height: 1;
  }
  .field-error {
    margin-top: 6px;
    color: var(--error-text);
    font-size: 0.75rem;
  }
  main {
    padding-top: 14px;
  }
  .notice {
    display: flex;
    gap: 6px 12px;
    flex-wrap: wrap;
    padding: 10px 14px;
    margin-bottom: 12px;
    border: 1px solid var(--notice-border);
    border-radius: 8px;
    font-size: 0.8rem;
  }
  .notice.warning {
    color: var(--notice-text);
    background: var(--notice-bg);
  }
  .notice ul {
    margin: 0;
    padding-left: 18px;
    overflow-wrap: anywhere;
  }

  /* Status dots shared by alerts, topics, machines and requests. */
  .dot {
    flex: none;
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--icon);
  }
  .dot.critical {
    background: var(--critical-dot);
  }
  .dot.warning {
    background: var(--warn-dot);
  }
  .dot.ok {
    background: var(--ok-dot);
  }

  /* Alerts: one line per problem, most severe first. */
  .alerts {
    margin-bottom: 12px;
    border: 1px solid var(--border);
    border-radius: 9px;
    background: var(--surface);
    overflow: hidden;
  }
  .alerts ul {
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .alerts li {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 4px 10px;
    padding: 7px 14px;
    border-left: 3px solid var(--warn-dot);
    font-size: 0.8rem;
    color: var(--text-3);
  }
  .alerts li + li {
    border-top: 1px solid var(--divider);
  }
  .alerts li.critical {
    border-left-color: var(--critical-dot);
  }
  .alerts li .dot {
    align-self: center;
  }
  .alerts li.more {
    border-left-color: transparent;
    padding: 5px 14px;
  }
  .alerts li.more button {
    min-height: 24px;
    padding: 1px 9px;
    font-size: 0.72rem;
  }
  .alerts strong {
    color: var(--text);
    font-size: 0.78rem;
    font-weight: 650;
  }

  /* Topics and machines side by side on wide screens. */
  .board {
    display: grid;
    grid-template-columns: minmax(0, 3fr) minmax(300px, 2fr);
    gap: 12px;
    align-items: start;
  }
  .panel {
    margin-bottom: 12px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 9px;
    box-shadow: 0 2px 3px var(--shadow);
    overflow: hidden;
  }
  .panel-heading {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    padding: 10px 16px;
    border-bottom: 1px solid var(--divider);
  }
  .section-note {
    color: var(--muted);
    font-size: 0.72rem;
    text-align: right;
  }
  .counter {
    display: inline-block;
    padding: 0 6px;
    margin-left: 4px;
    color: var(--muted);
    background: var(--fill);
    font-size: 0.7rem;
    font-weight: 500;
    vertical-align: middle;
    border-radius: 5px;
    letter-spacing: 0;
  }
  .muted {
    color: var(--muted);
    font-weight: 400;
  }
  .mono,
  code {
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
    overflow-wrap: anywhere;
  }
  code {
    color: var(--text-3);
    font-size: 0.72rem;
  }
  .secondary {
    display: block;
    color: var(--muted);
    font-size: 0.7rem;
    margin-top: 2px;
    overflow-wrap: anywhere;
  }
  .hint {
    padding: 10px 16px;
    color: var(--muted);
    font-size: 0.75rem;
    border-bottom: 1px solid var(--divider);
  }
  .empty {
    padding: 22px 16px 26px;
    text-align: center;
  }
  .empty h3 {
    color: var(--text-3);
    margin-bottom: 4px;
  }
  .empty p,
  .inline-empty {
    color: var(--muted);
    font-size: 0.75rem;
  }
  .inline-empty {
    padding: 12px 16px;
    list-style: none;
  }
  .limit-note {
    padding: 8px 16px;
    border-top: 1px solid var(--divider);
    color: var(--muted);
    font-size: 0.72rem;
  }

  /* Topic rows. */
  .topic-head,
  .topic > summary {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 64px 60px 60px 70px 70px;
    gap: 10px;
    align-items: center;
  }
  .topic-head {
    padding: 6px 16px;
    color: var(--muted);
    background: var(--surface-subtle);
    border-bottom: 1px solid var(--divider);
    font-size: 0.68rem;
    font-weight: 600;
  }
  .topic-head > span:not(:first-child) {
    text-align: right;
  }
  .topic-head > span:first-child {
    padding-left: 18px;
  }
  details > summary {
    cursor: pointer;
    list-style: none;
  }
  details > summary::-webkit-details-marker {
    display: none;
  }
  .topic {
    border-bottom: 1px solid var(--divider);
  }
  .topic:last-child {
    border-bottom: none;
  }
  .topic > summary {
    min-height: 36px;
    padding: 6px 16px;
    font-size: 0.78rem;
  }
  .topic > summary:hover,
  .reply > summary:hover {
    background: var(--surface-subtle);
  }
  .topic.critical > summary {
    background: color-mix(in srgb, var(--critical-bg) 55%, transparent);
  }
  .topic-name {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 2px 10px;
    min-width: 0;
  }
  .topic-name .dot {
    align-self: center;
  }
  .topic-name strong {
    color: var(--text);
    font-size: 0.76rem;
    font-weight: 600;
  }
  .reason {
    font-size: 0.72rem;
    color: var(--warn-text);
  }
  .topic.critical .reason {
    color: var(--critical-text);
    font-weight: 600;
  }
  .domain {
    flex-basis: 100%;
    padding-left: 18px;
    color: var(--muted);
    font-size: 0.68rem;
  }
  .num {
    text-align: right;
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .topic-group {
    padding: 6px 16px;
    color: var(--muted);
    background: var(--surface-subtle);
    border-bottom: 1px solid var(--divider);
    font-size: 0.68rem;
    font-weight: 600;
  }
  .secondary-topic > summary {
    opacity: 0.75;
  }
  .topic-content {
    border-top: 1px solid var(--divider);
    background: var(--surface-subtle);
  }

  /* Tables. */
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
    padding: 6px 16px;
    color: var(--muted);
    background: var(--surface-sunken);
    border-bottom: 1px solid var(--divider);
    font-size: 0.68rem;
    font-weight: 600;
    white-space: nowrap;
  }
  td {
    padding: 7px 16px;
    border-bottom: 1px solid var(--divider);
    vertical-align: middle;
    color: var(--text-3);
  }
  tr:last-child td {
    border-bottom: 0;
  }
  td strong {
    color: var(--text-2);
    font-weight: 600;
  }
  .tag {
    display: inline-block;
    padding: 2px 7px;
    border-radius: 5px;
    color: var(--ok-text);
    background: var(--ok-bg);
    font-size: 0.7rem;
    white-space: nowrap;
  }
  .tag.held {
    color: var(--held-text);
    background: var(--held-bg);
  }
  .tag.warning {
    color: var(--warn-text);
    background: var(--warn-bg);
  }
  .warn-text {
    color: var(--warn-text);
    font-weight: 650;
  }
  .requests .summary {
    color: var(--text);
    font-weight: 550;
  }
  .status {
    white-space: nowrap;
  }
  .status .dot {
    margin-right: 7px;
  }
  .status.critical {
    color: var(--critical-text);
    font-weight: 600;
  }
  .status.warning {
    color: var(--warn-text);
  }

  /* Machines. */
  .machines {
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .machines > li {
    padding: 8px 16px;
    border-bottom: 1px solid var(--divider);
  }
  .machines > li:last-child {
    border-bottom: 0;
  }
  .machine-line {
    display: flex;
    align-items: center;
    gap: 9px;
    font-size: 0.78rem;
  }
  .machine-line strong {
    color: var(--text);
    font-weight: 650;
  }
  .machine-state {
    margin-left: auto;
    color: var(--muted);
    font-size: 0.72rem;
  }
  .machine-state.critical {
    color: var(--critical-text);
    font-weight: 600;
  }
  .machine-state.warning {
    color: var(--warn-text);
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    margin-top: 6px;
    padding-left: 17px;
  }
  .chip {
    display: inline-flex;
    align-items: baseline;
    gap: 5px;
    max-width: 100%;
    overflow: hidden;
    white-space: nowrap;
    padding: 2px 8px;
    border: 1px solid var(--border);
    border-left-width: 3px;
    border-radius: 5px;
    color: var(--muted);
    font-size: 0.7rem;
  }
  .chip strong {
    color: var(--text-2);
    font-weight: 600;
    white-space: nowrap;
  }
  .chip.ok {
    border-left-color: var(--ok-dot);
  }
  .chip.warning {
    border-left-color: var(--warn-dot);
    color: var(--warn-text);
  }
  .chip.critical {
    border-left-color: var(--critical-dot);
    color: var(--critical-text);
  }
  .chip-topic {
    min-width: 0;
    color: var(--faint);
    font-size: 0.66rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Inbox. */
  .checkbox {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--muted);
    font-size: 0.72rem;
  }
  .checkbox input {
    accent-color: var(--accent);
  }
  .reply {
    margin: 0 16px;
    border-bottom: 1px solid var(--divider);
  }
  .reply > summary {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 14px;
    padding: 8px 2px;
    font-size: 0.72rem;
  }
  .reply > summary strong {
    font-weight: 600;
    color: var(--text-3);
  }
  .reply-received {
    display: inline-flex;
    align-items: center;
    gap: 12px;
    color: var(--muted);
    font-size: 0.72rem;
    white-space: nowrap;
  }
  .chevron {
    display: inline-block;
    color: var(--muted);
    font-size: 1.1rem;
    line-height: 1;
  }
  details[open] > summary .chevron {
    transform: rotate(90deg);
  }
  .reply-content {
    padding-bottom: 12px;
  }
  .reply-content dl {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 22px;
    margin: 0 0 10px;
    font-size: 0.72rem;
  }
  .reply-content dt {
    color: var(--muted);
    margin-bottom: 2px;
  }
  .reply-content dd {
    margin: 0;
    color: var(--text-3);
  }
  pre {
    margin: 0;
    padding: 12px 14px;
    max-height: 320px;
    overflow: auto;
    color: var(--text-3);
    background: var(--surface-sunken);
    border: 1px solid var(--border);
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
    padding: 10px 16px 12px;
  }
  .pagination > span {
    margin-right: auto;
    color: var(--muted);
    font-size: 0.72rem;
  }
  .pagination button {
    min-height: 26px;
    padding: 2px 9px;
    font-size: 0.7rem;
  }

  /* Activity. */
  .topic-picker {
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--muted);
    font-size: 0.72rem;
  }
  select {
    max-width: 320px;
    width: 100%;
    padding: 5px 8px;
    color: var(--text-3);
    background: var(--surface-subtle);
    border: 1px solid var(--border-strong);
    border-radius: 6px;
    font-size: 0.72rem;
  }
  .activity-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    padding: 12px 16px 0;
  }
  .chart-card {
    min-width: 0;
    padding: 0 16px 14px;
  }
  .chart-card:first-child {
    border-right: 1px solid var(--divider);
    padding-left: 0;
  }
  .chart-card:last-child {
    padding-right: 0;
  }
  .chart-card h3 {
    color: var(--text-3);
    font-size: 0.74rem;
    font-weight: 550;
  }
  .chart-totals {
    display: flex;
    gap: 20px;
    margin: 6px 0 10px;
  }
  .chart-totals span {
    color: var(--muted);
    font-size: 0.72rem;
  }
  .chart-totals strong {
    margin-right: 5px;
    color: var(--text-2);
    font-size: 1.05rem;
    font-weight: 600;
    letter-spacing: -0.04em;
  }
  .activity-summary {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 22px;
    padding: 10px 16px;
    border-top: 1px solid var(--divider);
    color: var(--muted);
    font-size: 0.72rem;
  }
  .activity-summary strong {
    color: var(--text-3);
    font-weight: 600;
    margin-right: 4px;
  }
  .activity-summary small {
    margin-left: auto;
    font-size: 0.7rem;
  }
  .hourly-data {
    border-top: 1px solid var(--divider);
  }
  .hourly-data > summary {
    padding: 9px 16px;
    color: var(--faint);
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

  footer {
    display: flex;
    justify-content: space-between;
    gap: 20px;
    margin: 18px 0 26px;
    color: var(--muted);
    font-size: 0.7rem;
  }
  .initial-loading {
    padding: 100px 20px 140px;
    text-align: center;
    color: var(--muted);
  }
  .initial-loading h2 {
    margin: 16px 0 6px;
    color: var(--text-3);
  }
  .initial-loading p {
    font-size: 0.8rem;
  }
  .loading-ring {
    display: inline-block;
    width: 28px;
    height: 28px;
    border: 2px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 1s linear infinite;
  }
  .spinning {
    display: inline-block;
    animation: spin 1s linear infinite;
  }
  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  @media (max-width: 1100px) {
    .board {
      grid-template-columns: minmax(0, 1fr);
      gap: 0;
    }
  }
  @media (max-width: 760px) {
    .shell {
      padding: 0 14px;
    }
    .refresh {
      width: 100%;
      margin-left: 0;
      justify-content: space-between;
    }
    .context > :global(*) + :global(*)::before {
      display: none;
    }
    .section-note {
      display: none;
    }
    .topic-head {
      display: none;
    }
    .topic > summary {
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 6px 8px;
      padding: 9px 14px;
    }
    .topic-name {
      grid-column: 1 / -1;
    }
    .num {
      text-align: left;
      font-size: 0.72rem;
    }
    .num::before {
      content: attr(data-label);
      display: block;
      color: var(--muted);
      font-size: 0.62rem;
    }
    .activity-grid {
      grid-template-columns: 1fr;
      padding: 12px 14px 0;
    }
    .chart-card:first-child {
      border-right: 0;
      border-bottom: 1px solid var(--divider);
      padding: 0 0 14px;
    }
    .chart-card:last-child {
      padding: 14px 0;
    }
    .activity-summary small {
      flex-basis: 100%;
      margin-left: 0;
    }
    .panel-heading {
      flex-wrap: wrap;
    }
    .topic-picker select {
      max-width: none;
    }
    .reply {
      margin: 0 14px;
    }
    footer {
      flex-direction: column;
      gap: 6px;
    }
    /* Data tables become one card per row, with each cell labelled. */
    table.stack thead {
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip-path: inset(50%);
      white-space: nowrap;
    }
    table.stack,
    .stack tbody,
    .stack td {
      display: block;
    }
    .stack tr {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 8px 14px;
      padding: 10px 14px;
      border-bottom: 1px solid var(--divider);
    }
    .stack tr:last-child {
      border-bottom: 0;
    }
    .stack td {
      min-width: 0;
      padding: 0;
      border: 0;
    }
    .stack td.wide {
      grid-column: 1 / -1;
    }
    .stack td[data-label]::before {
      content: attr(data-label);
      display: block;
      margin-bottom: 2px;
      color: var(--muted);
      font-size: 0.66rem;
    }
    .status {
      white-space: normal;
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
