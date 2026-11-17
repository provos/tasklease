# Lifecycle verification

The executable reference model in `conformance/lifecycle_model.py` interprets
spec §§2–5 independently of the Worker and FakeBoard. Hypothesis generates
histories of Create, Own, Renew, Read, Delete, Consume, Cancel, clock advancement
and retries. Each response, the complete live task set, task metadata and payloads
are compared with the model after each operation. Retired fencing IDs remain
available for later commands.

Run both implementations with `make test-model`. These tests also run as part of
`make conformance` and `make check`. Service test files run in sequence to limit
competing workloads during wall-clock lease and long-poll tests; explicit request
races inside each test remain concurrent. For an offline run:

```sh
cd python
uv run pytest ../conformance/test_lifecycle_model.py --backend=fake
```

The default budget is 20 histories of up to 30 generated actions per backend,
plus initialization and a deterministic boundary history. Hypothesis stores and
minimizes failures in its ignored `.hypothesis` directory. Each history gets a
fresh domain and identities. IDs are bound to the backend's returned values;
the model checks freshness and uses those values for the specified ordering
among tasks created in the same millisecond. Test-only seeded random sources
make ID ordering reproducible during replay and shrinking; suffixes remain
non-monotonic so the model still checks tie-breaking by ID.

The clock advances without sleeping, including to just before, exactly at and
just after lease and idempotency-retention boundaries. FakeBoard uses its
injected clock. The Worker uses a separate local process whose entrypoint is
`service/test/model-worker.ts`; its clock shim applies to the unmodified
production implementation. Its control route and RPC exist only in this test
entrypoint, served on loopback. The route checks POST before parsing JSON and
validates its input, including the ULID timestamp range. The ordinary conformance
process continues to test the shipped bundle using real time. Model histories in
one process run serially because the clock shim is isolate-wide.

Lost-response tests compose the existing `LostResponse` transport with a
recording transport. The observer checks the hidden committed response's HTTP
status and body to bind fresh IDs, while the caller receives a transport error.
The next retry is a new operation: losing a reply does not roll back a committed
mutation.

Each generated history begins with competing tasks on two topics, varying list order
and the clock around lease expiry. The current scope is the task lifecycle with finite integer payloads, two
topics, three non-operator identities, exact topic grants, two worker labels,
two idempotency keys and at most two Consume successors. Own varies both worker
labels, single-topic and multi-topic selectors, list order and mixed grants, and Renew checks that changing the label keeps ownership metadata intact.
Read grants vary by actor, covering denied reads and access through Own alone.
A separate observer can read every generated topic, including denied
destinations, to detect partial writes on refusal. Generated cases include expiry
before Renew, re-ownership, stale-ID mutations, strict Delete cutoffs, origin-preserving
fan-out, cancellation and keyed replay after removal. Existing schema, operator,
transaction-rollback and concurrent-race tests remain complementary.

These are implementation conformance tests, not an exhaustive proof. They do
not establish long-poll liveness, thread scheduling, cryptographic ID uniqueness,
R2 behavior or exactly-once external side effects. The model treats fresh IDs as
an assumption that it checks on observed executions. Server time is controlled
and nondecreasing. Poller telemetry and statistics are outside this model;
notably an empty Own may record activity without changing task state. A Renew
retry may change its deadline because it uses the new operation's server time.

## Bounded protocol model

`formal/TaskLease.tla` separately models atomic storage transitions and response
delivery. A committed request can lose its reply; another identity can act before
a delayed reply arrives. Clients learn IDs through delivered Create, Own, Renew
and Consume responses. Live tasks can be re-owned after expiry, and stale IDs
remain known to their previous holders.

TLC exhaustively explores five finite configurations:

| Configuration | Workers | Fresh-ID budget | Initial tasks | Topics | Time horizon |
| --- | ---: | ---: | ---: | ---: | ---: |
| `TaskLease.cfg` | 2 | 3 | 1 | 2 | 2 |
| `Fanout.cfg` | 1 | 4 | 1 | 2 | 1 |
| `Retention.cfg` | 1 | 2 | 2 | 1 | 2 |
| `MultipleRoots.cfg` | 1 | 4 | 2 | 1 | 1 |
| `MultiTopic.cfg` | 1 | 3 | 2 | 2 | 1 |

Each configuration starts empty. The ID budget counts every Create, Own and
Consume insertion, including retired IDs. Fresh IDs are chosen nondeterministically
from an ordered finite set, so tie-breaking does not assume insertion order.
The fan-out configuration has enough IDs for Create, Own and two successors.
The multi-topic configuration can create competing roots on either topic and
claim from any nonempty subset. It explores ties, differing creation times
and unavailable tasks. It checks that a ready worker with a fresh ID can
claim whenever any selected topic has available work. The initial-task budget
also counts removed tasks. The retention configuration
permits a second initial task after key expiry. The multiple-root configuration
permits competing available tasks before Own, and cancellation of one initial
task while another origin has descendants. In this configuration the requester
is also the worker, so creator equality cannot accidentally protect descendants
when their initial-task flag is ignored. A Cancel after Consume is modeled
as a refused response with no task changes.
Retention is normalized to one time unit, with replay still live at equality.
Time is nondecreasing; lease durations are one or two units. New tasks use
`now - 1` to abstract their immediate availability.

The invariants check retired IDs never becoming live, one live version per
lineage, one initial task per origin, at most one end per lineage, immutable
lineage metadata, inherited origins and non-initial descendants. Action
properties check that replay and stale-ID refusal preserve core state, live
idempotency mappings retain their original acceptance time, cancellation
preserves descendants and Own selects the oldest available task across the
selected topics with ID tie-breaking. Task insertion, key publication and Consume successors
are atomic model steps; the existing SQLite rollback tests check that assumption
against the implementation.

Use Java 11+ and the pinned official TLC 1.7.4 JAR:

```sh
curl -fL https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar \
  -o /tmp/tasklease-tla2tools-1.7.4.jar
TLA2TOOLS_JAR=/tmp/tasklease-tla2tools-1.7.4.jar make model-check
```

The runner checks SHA-256
`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`
before executing the JAR. It downloads nothing, uses one TLC worker and a 1 GiB
heap, limits each run to five minutes, and keeps generated state/trace files in
a temporary directory. `make model-check` also requires six deliberately broken
variants to fail their named checks: keeping an old ID live after Own, giving a
Consume successor a forged origin, cancelling a descendant, claiming out of
order, checking age only within the claimed topic, and requiring every selected
topic to have work before claiming. The multi-topic mutations exercise both
competing tasks and selections that include an empty topic. A parse error or unrelated
failure does not count as detecting a mutation. Each anchor must occur exactly
once within its named TLA+ action, independent of definition order. Missing JARs,
Java startup failures and checker timeouts produce concise error diagnostics.

This command is separate from `make check` so ordinary development and release
checks do not require Java or a downloaded JAR. Run both before submitting a
verification change. Passing TLC establishes the listed properties for these
finite abstractions and bounds. It is not a refinement proof of the SQL or an
unbounded theorem. The model omits payloads, authorization, operator override,
worker labels, conditional Delete, multiple idempotency keys and cross-domain
or cross-creator key namespaces, listing, object restarts, long polling and Python
thread scheduling. Those are covered in part by implementation tests; no liveness or
fairness guarantee is asserted. Budget exhaustion can leave no enabled action,
so deadlock checking is explicitly disabled for these safety configurations.
