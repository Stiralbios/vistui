# Vistui Phase 7: Issue 007 — streaming event consolidation

Fixes issue 007 (events are grouped in fixed 10-message blocks, see `plan/issue.md`)
by turning the event pipeline into a **streaming event builder**: each round takes the
next waiting message and the chat's **last event**, and a decision model call decides
to **extend** the last event, open a **new** one, or **mix** (the message closes the
last event and opens the next one). Events become fully flexible — 1 to any number of
messages, decided message by message, with no window and no boundary truncation — and
all three consolidation pipelines (event/topic/fact) move to salience-style per-round
message limits.

## The flow

Per message (oldest `event`-waiting first, outside the protected window):

1. Load the chat's **last event** (`ORDER BY end_at DESC, id DESC LIMIT 1`).
2. No last event → forced **new** (no decision call).
3. Otherwise one `decide()` call (System One choice question, task `event_boundary`):
   state = chatgroup description, last event (summary + start/end), message (role,
   text, created_at); criteria = `{extend, new, mixed}` (chatgroup-rewordable
   descriptions). Resolution: argmax of the answer's probabilities; missing
   probabilities → failure, the message stays waiting and retries.
4. Apply the action:
   - **extend**: re-summarize the event from **all its member messages plus this one**
     (`event_summary` generation prompt), rewrite `full_text`, bump `end_at` /
     `edited_at`, null `embedding` / `salience_score`, link the message.
   - **new**: `event_summary([message])` → new `EventDO` (`start_at = end_at` =
     message time), link the message.
   - **mix**: extend the last event **including** the boundary message, then create
     the new event from the same message — it is linked to both.
5. Mark the message `event: DONE`.

Every summary is computed from source messages, so there is no incremental drift by
construction (an extension never rewrites from a summary — only from the member
texts). Extending invalidates the row exactly like topic/fact updates today
(`pipelines.py` topic:238-240 / fact:359-361): `edited_at = now`,
`embedding = None`, `salience_score = None`, re-entering the memory embedding and
salience queues for free.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Event unit of work | One message per decision: next waiting message + chat's last event; extend / new / mix | Fully flexible spans; no block boundary can cut a discussion; symmetric with topic/fact ("one message, diffed against current state") |
| Boundary decision | `decide()` choice question, task `event_boundary` (decision model → local-ollama d3:4b); instructions = chatgroup `event_boundary` prompt; criteria keys fixed to `extend`/`new`/`mix` with descriptions from `event_boundary_criteria` (validator like `memory_ratio_criteria`); argmax resolution, no fallback | Same machinery as salience / memory ratio; the keys are load-bearing (the pipeline switches on them); a failed decision retries for free via the waiting flag |
| Event summaries | One generation prompt `event_summary` (chat task, task name `event_summary`): given **all the event's messages** (members + the new one when extending), produce the summary. Used on every new event and **every extension** | Grounded in sources on every write — no drift, no separate close-time refresh; long events cost a bigger call but a correct summary |
| Extend/mix mutation | Rewrite `full_text`, `end_at = max(...)`, `edited_at = now`, `embedding = None`, `salience_score = None` | Topic/fact update precedent; the NULL columns are the queue markers, so re-embedding / re-scoring come for free |
| Mix semantics | Boundary message belongs to both events: last event re-summarized including it, new event created from it, linked to both | Replaces the old "a message can span event boundaries" rule with a concrete action |
| Prompts schema | `ChatGroupPrompts`: remove `event`, add `event_boundary` (instructions), `event_boundary_criteria` (dict, keys exactly extend/new/mix), `event_summary` (generation template). Legacy stored `event` JSONB keys are ignored on read (extras not forbidden) — **no migration** | The new prompts are live per-chatgroup config, salience-style; old rows keep working and pick up defaults |
| Per-round limits | `VISTUI_BATCH_EVENT_MESSAGE_LIMIT` (default 5), `VISTUI_BATCH_TOPIC_MESSAGE_LIMIT` (5), `VISTUI_BATCH_FACT_MESSAGE_LIMIT` (5); `VISTUI_BATCH_EVENT_BLOCK_SIZE` removed; no back-compat | All consolidation stages become message-by-message with a chosen batch per round, like salience (5) and embedding (32) |
| Round transactions | Each message = its own transaction inside `run_round`; a failure marks **that** message `FAILED` (retry_count++, last_failure_at) and the round continues; the worker's outer catch stays as the safety net | Salience-style throughput with correct failure attribution (today the worker marks the peeked anchor even if a later message failed); crash isolation is per message |
| Task routing | `overrides_models`: remove `event`, add `event_boundary` (decision → local-ollama d3:4b) and `event_summary` (chat → gomodel) | Boundary is a decision task; summaries are generation tasks |
| Dead config (out of scope) | `topic_candidate` / `topic_update` / `fact_candidate` / `fact_update` prompts stay module constants | Making topic/fact prompts live per-chatgroup is a separate change; noted in the issue as a known inconsistency |
| Status peek | `_peek_message_id` keeps returning the round's anchor (first waiting message) | Status detail only; the pipeline loops over its limit internally |

## Phase A — prompts schema, settings, task routing

### File plan

| File | Change |
|---|---|
| `sources/backend/chatgroup/schemas.py` | `ChatGroupPrompts`: drop `event`; add `event_boundary` (default instructions), `event_boundary_criteria` (default `DEFAULT_EVENT_BOUNDARY_CRITERIA`, keys exactly `extend`/`new`/`mix`), `event_summary` (default template with a `{messages}` placeholder); validator for the criteria keys |
| `sources/backend/llm/prompts.py` | Remove `EVENT_GROUPING` (its role moves to the chatgroup prompt default); `TOPIC_CANDIDATE` etc. untouched |
| `sources/backend/settings.py` | `AppSettings`: remove `VISTUI_BATCH_EVENT_BLOCK_SIZE`; add the three per-round message limits |
| `settings.yaml` | `overrides_models`: `event` entry → `event_boundary` (local-ollama, d3:4b) + `event_summary` (gomodel, chat model) |
| `demo_local/chatgroup_config.json` | Regenerate from the new schema defaults (local file, untracked) |

### Steps (TDD)

1. RED: schema tests — `ChatGroupPrompts` defaults for the three new keys; the criteria
   validator rejects unknown/missing keys (pydantic 422 via `ChatGroupCreate`); a
   legacy `{"event": ...}` prompts dict parses and is ignored.
2. RED: settings tests — the three limits exist with their defaults; the old block-size
   name is gone.
3. Implement, GREEN; update `settings.yaml` + regenerate the demo config; `just fmt /
   lint / typecheck / test`.

## Phase B — the streaming EventPipeline

### File plan

| File | Change |
|---|---|
| `sources/backend/batch/queries.py` | `get_waiting_messages_with_state(session, state_key, limit)` (plural variant of the single-message query, same protected-window predicate); `get_last_event(session, chat_id)` (`end_at DESC, id DESC LIMIT 1`); `get_event_member_texts(session, event_id)` (via links, ordered by `created_at`) |
| `sources/backend/batch/pipelines.py` | `EventPipeline` rewrite: per-message loop up to the limit; per message — own transaction, chatgroup prompts load (salience-style `.get(key) or ChatGroupPrompts().key`), `decide()` boundary question, apply extend/new/mix with `event_summary` generation, invalidation on rewrite, per-message `FAILED` marking with continue; no `General conversation block` fallback (a garbage answer retries) |
| `sources/backend/batch/worker.py` | Event partial passes `limit=settings.VISTUI_BATCH_EVENT_MESSAGE_LIMIT`; `_peek_message_id` unchanged |

### Steps (TDD)

1. RED: pipeline unit tests with the fake provider (decide → extend/new/mix; chat_json
   summaries):
   - no last event → new without a decision call;
   - extend: `event_summary` receives all member texts + the new message; `full_text`
     rewritten, `end_at`/`edited_at` bumped, `embedding`/`salience_score` NULLed,
     link created, message `DONE`;
   - new: one-message event created and linked;
   - mix: both events touched, message linked to both, last-event summary includes the
     boundary message;
   - the chatgroup's `event_boundary` instructions and `event_boundary_criteria`
     reach `decide()` verbatim;
   - limit: `run_round` handles N messages per call; a failing message (decide raises)
     is marked `FAILED` and the round continues with the rest.
2. Implement, GREEN; `just fmt / lint / typecheck / test`.

## Phase C — topic/fact per-round limits

### File plan

| File | Change |
|---|---|
| `sources/backend/batch/pipelines.py` | `TopicPipeline` / `FactPipeline`: loop over up to `limit` messages, one transaction per message, same per-message `FAILED`+continue handling (single-message round logic becomes the loop body) |
| `sources/backend/batch/worker.py` | Partials pass the topic/fact limits |

### Steps (TDD)

1. RED: limit tests — a round processes up to N messages; a mid-round failure marks only
   its message `FAILED` and the rest still process.
2. Implement, GREEN; `just fmt / lint / typecheck / test`.

## Phase D — integration, docs, issue flip

### File plan

| File | Change |
|---|---|
| `tests/backend/batch/test_batch_integration.py` | Adapt: `VISTUI_BATCH_EVENT_BLOCK_SIZE` → `VISTUI_BATCH_EVENT_MESSAGE_LIMIT`; event-done accounting per message; embedding assertion relaxes (events rewritten in the final round legitimately wait for the next round's `memory_embedding`) |
| `documentations/architecture/batch-consolidation.md` | Rewrite the event section (streaming builder, extend/new/mix, invalidation), stage limits table, per-message failure semantics |
| `documentations/architecture/llm-provider.md` | Task table: `event` → `event_boundary` (decision) + `event_summary` (chat); settings example |
| `documentations/architecture/api-contract.md` | Chatgroup create/patch prompts shape (three new keys, old `event` gone), validation rule for the criteria keys, changelog |
| `documentations/architecture/data-model.md` | Event semantics: unbounded spans, mutation on extend (invalidation columns), changelog |
| `documentations/architecture/retrieval-system.md` | Event description touch-up if needed |
| `plan/issue.md` | Flip issue 007 to DONE with the as-built note |

### Steps (TDD)

1. Adapt the integration test, run `just test-integration` against the real providers.
2. Docs updates with dated changelog entries (2026-11-22).
3. Issue flip; `just fmt / lint / typecheck / test / test-integration` all green.

## Verification (every phase)

`just fmt` · `just lint` · `just typecheck` · `just test` (hermetic) — plus
`just test-integration` in Phase D. Line length 120; comments describe current
behavior only (no plan/issue references in code).

## Results

### All phases (done 2026-11-22)

- **Phase A**: `ChatGroupPrompts` gained `event_boundary` (decision instructions),
  `event_boundary_criteria` (validator: keys ⊆ extend/new/mixed, ≥ 2 entries) and
  `event_summary` (validator: `{messages}` placeholder **and** the template must
  survive `str.format` — stray braces rejected, else every summary call would fail);
  the legacy `event` key is ignored by the model. `AppSettings` gained
  `VISTUI_BATCH_EVENT/TOPIC/FACT_MESSAGE_LIMIT` (10/5/5); `VISTUI_BATCH_EVENT_BLOCK_SIZE`
  and `EVENT_GROUPING` are gone. `settings.yaml` routes `event_boundary` to the
  decision model (`d3:4b`) and `event_summary` to the chat model;
  `demo_local/chatgroup_config.json` regenerated from the schema defaults.
- **Phase B**: `EventPipeline` rebuilt on a shared `_process_waiting_messages` runner
  (fetch up to `limit` oldest waiting outside the protected window, one transaction per
  message, per-message failure isolation via `record_stage_failure`: the flag stays
  waiting, `retry_count`/`last_failure_at` recorded, round continues). Per message: last
  event loaded (`get_last_event`), none → forced new; else one `decide()` choice question
  (`boundary`, task `event_boundary`, keep_alive 10m, argmax over probabilities — a
  missing distribution is a failure). `extend`/`mixed` re-summarize the touched event
  from its **full member list** (`get_event_messages`), re-derive the range, bump
  `edited_at`, clear embedding/salience; `mixed` additionally opens the next event from
  the boundary message and links it to both. Old search/admin test fakes upgraded to the
  new call shapes (boundary → extend keeps one event over all seeded messages).
- **Phase C**: topic/fact pipelines run on the same per-message runner with their own
  limits; the worker passes all three `VISTUI_BATCH_*_MESSAGE_LIMIT`s. Search tests
  adapted to 5-per-round topic/fact (all ten seeded messages end up triple-linked).
- **Phase D**: integration test adapted — exact accounting asserted when every model
  answer was well-formed (`recent_failures` unchanged), isolation invariants otherwise
  (queue only drains, no FAILED flags, stage bounds, salience prefix property). Two
  real-model findings drove extra changes: the small summary model intermittently
  echoed the JSON response schema (`{"type": "object"}`), so the default
  `event_summary` template now shows the expected JSON shape (escaped `{{...}}`,
  matching the topic/fact prompt style) — failure rate went from 3/10 calls to 0/20;
  and the tolerant assertions keep the test green through the remaining flakiness since
  retries are free by design. Docs updated with 2026-11-22 changelogs
  (batch-consolidation, llm-provider, api-contract, data-model, features); issue 007
  flipped DONE.
- **Verification**: 148 unit tests + 1 deselected green; `just fmt / lint / typecheck`
  clean; `just test-integration` green in 19:42 with zero model failures (strict
  assertion path: depths [25, 20], 20 events done, 5+5 salience/topic/fact per round,
  all 30 embedded, search sections passing).
