----------------------------- MODULE TaskLease -----------------------------
EXTENDS Naturals, Integers, FiniteSets, TLC

\* Finite safety model of spec §§2–5. One requester, Workers identities,
\* unordered fresh IDs, atomic storage transitions, independently delivered
\* or lost responses. Payloads, scopes, telemetry and long polls are abstracted.
CONSTANTS Workers, Requester, Topics, RootTopics, MaxIds, MaxRoots,
          Horizon, Retention, Holds

Ids == 1..MaxIds
Principals == Workers \cup {Requester}
NoOwner == "no-owner"
ASSUME /\ RootTopics \subseteq Topics /\ RootTopics # {}
       /\ NoOwner \notin Principals
       /\ Holds # {}
       /\ Holds \subseteq 1..Horizon
MaxHold == CHOOSE h \in Holds : \A other \in Holds : other <= h

VARIABLES tasks, births, edges, used, retired, ends, now,
          keyOrigin, keyAt, pending, known
vars == <<tasks, births, edges, used, retired, ends, now,
          keyOrigin, keyAt, pending, known>>
core == <<tasks, births, edges, used, retired, ends, keyOrigin, keyAt>>

LiveIds == {t.id : t \in tasks}
Fresh == Ids \ used
Row(i) == CHOOSE t \in tasks : t.id = i
Birth(i) == CHOOSE t \in births : t.id = i
KeyLive == keyOrigin # 0 /\ now - keyAt <= Retention
NoReply == [waiting |-> FALSE, ids |-> {}]
Reply(ids) == [waiting |-> TRUE, ids |-> ids]
Ready(p) == ~pending[p].waiting

New(i, topic, creator, origin, root) ==
  [id |-> i, lineage |-> i, origin |-> origin, root |-> root,
   topic |-> topic, creator |-> creator, created |-> now,
   lease |-> now - 1, owner |-> NoOwner, count |-> 0]

Selections == (SUBSET Topics) \ {{}}
Eligible(selected) == {t \in tasks : t.topic \in selected /\ t.lease <= now}
Precedes(t, u) == t.created < u.created \/ (t.created = u.created /\ t.id < u.id)
Heads(rows) == {t \in rows : ~\E u \in rows : Precedes(u, t)}
\* Mirror the indexed claim: compare only the head of each selected topic.
Pick(selected) == Heads(UNION {Heads(Eligible({topic})) : topic \in selected})

Init ==
  /\ tasks = {} /\ births = {} /\ edges = {}
  /\ used = {} /\ retired = {}
  /\ ends = [i \in Ids |-> 0]
  /\ now = 0 /\ keyOrigin = 0 /\ keyAt = 0
  /\ pending = [p \in Principals |-> NoReply]
  /\ known = [p \in Principals |-> {}]

Create(i, keyed, topic) ==
  /\ Ready(Requester) /\ i \in Fresh
  /\ Cardinality({b \in births : b.root}) < MaxRoots
  /\ ~keyed \/ ~KeyLive
  /\ LET t == New(i, topic, Requester, i, TRUE) IN
       /\ tasks' = tasks \cup {t}
       /\ births' = births \cup {t}
  /\ used' = used \cup {i}
  /\ keyOrigin' = IF keyed THEN i ELSE keyOrigin
  /\ keyAt' = IF keyed THEN now ELSE keyAt
  /\ pending' = [pending EXCEPT ![Requester] = Reply({i})]
  /\ UNCHANGED <<edges, retired, ends, now, known>>

Replay ==
  /\ Ready(Requester) /\ KeyLive
  /\ pending' = [pending EXCEPT ![Requester] = Reply({keyOrigin})]
  /\ UNCHANGED <<core, now, known>>

Own(w, selected, i, j, hold) ==
  /\ Ready(w) /\ i \in LiveIds /\ j \in Fresh
  /\ LET t == Row(i) IN
       /\ t \in Pick(selected)
       /\ tasks' = (tasks \ {t}) \cup
            {[t EXCEPT !.id = j, !.lease = now + hold,
                        !.owner = w, !.count = @ + 1]}
  /\ used' = used \cup {j} /\ retired' = retired \cup {i}
  /\ pending' = [pending EXCEPT ![w] = Reply({j})]
  /\ UNCHANGED <<births, edges, ends, now, keyOrigin, keyAt, known>>

Renew(w, i, hold) ==
  /\ Ready(w) /\ i \in known[w] \cap LiveIds
  /\ LET t == Row(i) IN
       /\ t.owner = w
       \* No lease-validity guard: a lapsed live ID may still renew.
       /\ tasks' = (tasks \ {t}) \cup {[t EXCEPT !.lease = now + hold]}
  /\ pending' = [pending EXCEPT ![w] = Reply({i})]
  /\ UNCHANGED <<births, edges, used, retired, ends, now,
                 keyOrigin, keyAt, known>>

Consume(w, i, children, destinations) ==
  /\ Ready(w) /\ i \in known[w] \cap LiveIds
  /\ LET t == Row(i)
         next == {New(j, destinations[j], w, t.origin, FALSE) : j \in children}
     IN /\ t.owner = w
        /\ tasks' = (tasks \ {t}) \cup next
        /\ births' = births \cup next
        /\ edges' = edges \cup
             {[parent |-> t.lineage, child |-> j] : j \in children}
        /\ ends' = [ends EXCEPT ![t.lineage] = @ + 1]
  /\ used' = used \cup children /\ retired' = retired \cup {i}
  /\ pending' = [pending EXCEPT ![w] = Reply(children)]
  /\ UNCHANGED <<now, keyOrigin, keyAt, known>>

Delete(p, i) ==
  /\ Ready(p) /\ i \in known[p] \cap LiveIds
  /\ LET t == Row(i) IN
       /\ t.owner = p \/ (t.owner = NoOwner /\ t.creator = p)
       /\ tasks' = tasks \ {t}
       /\ ends' = [ends EXCEPT ![t.lineage] = @ + 1]
  /\ retired' = retired \cup {i}
  /\ pending' = [pending EXCEPT ![p] = Reply({})]
  /\ UNCHANGED <<births, edges, used, now, keyOrigin, keyAt, known>>

Cancel(origin) ==
  /\ Ready(Requester) /\ origin \in known[Requester]
  /\ LET roots == {t \in tasks : t.root /\ t.origin = origin /\
                                  t.creator = Requester}
     IN IF roots = {} THEN
          \* not_found: an origin's descendants remain untouched.
          UNCHANGED <<tasks, retired, ends>>
        ELSE \E t \in roots :
          /\ tasks' = tasks \ {t}
          /\ retired' = retired \cup {t.id}
          /\ ends' = [ends EXCEPT ![t.lineage] = @ + 1]
  /\ pending' = [pending EXCEPT ![Requester] = Reply({})]
  /\ UNCHANGED <<births, edges, used, now, keyOrigin, keyAt, known>>

\* A request using a retired fencing ID returns not_found without a new ID.
Stale(p, i) ==
  /\ Ready(p) /\ i \in known[p] \cap retired
  /\ pending' = [pending EXCEPT ![p] = Reply({})]
  /\ UNCHANGED <<core, now, known>>

Deliver(p) ==
  /\ pending[p].waiting
  /\ known' = [known EXCEPT ![p] = @ \cup pending[p].ids]
  /\ pending' = [pending EXCEPT ![p] = NoReply]
  /\ UNCHANGED <<core, now>>

Lose(p) ==
  /\ pending[p].waiting
  /\ pending' = [pending EXCEPT ![p] = NoReply]
  /\ UNCHANGED <<core, now, known>>

Tick ==
  /\ now < Horizon /\ now' = now + 1
  /\ UNCHANGED <<core, pending, known>>

Next ==
  \/ \E i \in Fresh, keyed \in BOOLEAN, topic \in RootTopics : Create(i, keyed, topic)
  \/ Replay
  \/ \E w \in Workers, selected \in Selections, i \in LiveIds,
         j \in Fresh, hold \in Holds : Own(w, selected, i, j, hold)
  \/ \E w \in Workers, i \in LiveIds, hold \in Holds : Renew(w, i, hold)
  \/ \E w \in Workers, i \in LiveIds, children \in SUBSET Fresh :
       /\ Cardinality(children) <= 2
       /\ \E destinations \in [children -> Topics] :
            Consume(w, i, children, destinations)
  \/ \E p \in Principals, i \in LiveIds : Delete(p, i)
  \/ \E origin \in Ids : Cancel(origin)
  \/ \E p \in Principals, i \in retired : Stale(p, i)
  \/ \E p \in Principals : Deliver(p) \/ Lose(p)
  \/ Tick

Spec == Init /\ [][Next]_vars

TypeOK ==
  /\ used \subseteq Ids /\ retired \subseteq used
  /\ now \in 0..Horizon /\ ends \in [Ids -> 0..2]
  /\ keyOrigin \in Ids \cup {0} /\ keyAt \in 0..Horizon
  /\ pending \in [Principals -> [waiting : BOOLEAN, ids : SUBSET used]]
  /\ known \in [Principals -> SUBSET used]
  /\ tasks \subseteq
       [id : Ids, lineage : Ids, origin : Ids, root : BOOLEAN,
        topic : Topics, creator : Principals, created : 0..Horizon,
        lease : -1..(Horizon + MaxHold), owner : Principals \cup {NoOwner},
        count : 0..MaxIds]
  /\ births \subseteq
       [id : Ids, lineage : Ids, origin : Ids, root : BOOLEAN,
        topic : Topics, creator : Principals, created : 0..Horizon,
        lease : -1..Horizon, owner : {NoOwner}, count : {0}]
  /\ edges \subseteq [parent : Ids, child : Ids]

Fencing == /\ retired \cap LiveIds = {}
           /\ used = retired \cup LiveIds
UniqueLiveIds == \A i \in LiveIds : Cardinality({t \in tasks : t.id = i}) = 1
OneLiveVersion == \A i \in Ids :
  Cardinality({t \in tasks : t.lineage = i}) <= 1
OneInitialTask == \A origin \in Ids :
  Cardinality({t \in tasks : t.root /\ t.origin = origin}) <= 1
AtMostOneEnd == \A i \in Ids : ends[i] <= 1
MetadataPreserved == \A t \in tasks :
  LET b == Birth(t.lineage) IN
    /\ t.origin = b.origin /\ t.topic = b.topic /\ t.creator = b.creator
    /\ t.created = b.created /\ t.root = b.root
DescendantOrigins == \A e \in edges :
  /\ Birth(e.child).origin = Birth(e.parent).origin
  /\ ~Birth(e.child).root
OriginIsInitial == \A b \in births :
  /\ Birth(b.origin).root /\ Birth(b.origin).origin = b.origin
KeyNamesOrigin == keyOrigin = 0 \/
  (Birth(keyOrigin).root /\ Birth(keyOrigin).creator = Requester /\
   Birth(keyOrigin).created = keyAt)

\* Action properties independently constrain preservation across transitions.
ReplayReadOnly == [][Replay => UNCHANGED core]_vars
StaleReadOnly == [][(\E p \in Principals, i \in Ids : Stale(p, i)) =>
                    UNCHANGED core]_vars
RetainLiveKey == [][KeyLive =>
                    (keyOrigin' = keyOrigin /\ keyAt' = keyAt)]_vars
CancelPreservesDescendants == [][(\E origin \in Ids : Cancel(origin)) =>
  {t \in tasks : ~t.root} \subseteq tasks']_vars
\* Refusing a nonempty selection would satisfy oldest-first safety vacuously.
OwnCanClaim == \A w \in Workers, selected \in Selections :
  (Ready(w) /\ Fresh # {} /\ Eligible(selected) # {}) =>
    ENABLED (\E i \in LiveIds, j \in Fresh, hold \in Holds :
               Own(w, selected, i, j, hold))
OwnSelectsOldest == [][(\A w \in Workers, selected \in Selections, i \in LiveIds,
                         j \in Fresh, hold \in Holds :
  Own(w, selected, i, j, hold) =>
    /\ Row(i) \in Eligible(selected)
    /\ \A u \in Eligible(selected) :
         Row(i).created < u.created \/ (Row(i).created = u.created /\ i <= u.id))]_vars
=============================================================================
