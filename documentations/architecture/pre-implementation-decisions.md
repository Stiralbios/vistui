# Pre-Implementation Decisions

This document lists the remaining open decisions that need concrete values or policies before implementation can begin without ambiguity. Each section ends with a decision field for the project owner to fill in.

---

## 1. System Default ChatGroup Config

The following values are used when a ChatGroup is created without explicit config.

| Setting | Description | Proposed default | Decision |
|---------|-------------|------------------|----------|
| `token_budget` | Max tokens returned in memory retrieval. | 2000 | |
| `memory_ratio.message` | Default fraction for raw messages. | 0.40 | |
| `memory_ratio.event` | Default fraction for events. | 0.10 | |
| `memory_ratio.topic` | Default fraction for topics. | 0.30 | |
| `memory_ratio.fact` | Default fraction for facts. | 0.20 | |
| `use_llm_memory_ratio` | Whether to call the LLM for memory ratio per message. | true | |
| `protected_message_count` | Most recent messages excluded from consolidation. | 10 | |
| `topic_update_threshold` | Min score for a topic to be considered for update. | 0.75 | |
| `fact_update_threshold` | Min score for a fact to be considered for update. | 0.80 | |
| `fact_history_count` | Messages linked to a fact sent during fact update. | 5 | |
| `max_facts_per_message` | Max existing facts to update + new facts to create per message. | 5 | |
| `max_topics_per_message` | Max existing topics to update + new topics to create per message. | 5 | |
| `batch_inactivity_seconds` | Seconds of no activity before worker starts. | 3600 | |
| `processing_timeout_seconds` | Max seconds a sub-task may run before marked failed. | 3600 | |
| `max_retries` | Max retry attempts per failed sub-task. | 5 | |
| `retry_base_delay_seconds` | Initial retry delay, doubled each attempt. | 60 | |
| `retry_max_delay_seconds` | Cap on retry delay. | 300 | |

---

## 2. System Default Ranking Formula

The default ranking formula used when a ChatGroup is created.

**Proposed default:**

```python
lambda vector_score, bm25_score, salience, linked_count: max(
    vector_score * salience * linked_count,
    bm25_score * salience * linked_count
)
```

**Decision:**
 
 Note: vector_score, bm25_score and linked_count are normalized. Salience is between 0 and 1

```python
lambda vector_score, bm25_score, salience, linked_count: max(
    vector_score * salience * linked_count,
    bm25_score * salience * linked_count
)
```

---

## 3. Default Prompt Templates

Each prompt is a Jinja2 template stored in `ChatGroup.prompts`. We need default versions for all of them.

### 3.1 Salience prompt

Inputs: `full_text`, `role`, `chatgroup_description`

Expected output: JSON object with `salience` in `[0, 1]`.

**Proposed default:**

```jinja2
You are scoring how important a message is for long-term memory.
Rate the following {{ role }} message from 0.0 (trivial) to 1.0 (highly important).
Only output a JSON object with a single key "salience".

Message: {{ full_text }}
```

**Decision:**

```jinja2
You are scoring how important a message is for long-term memory.
Rate the following {{ role }} message from 0.0 (trivial) to 1.0 (highly important).
Only output a JSON object with a single key "salience".

Message: {{ full_text }}
```

### 3.2 Keywords prompt

Inputs: `full_text`, `role`, `chatgroup_description`

Expected output: JSON array of lowercase keyword strings.

**Proposed default:**

```jinja2
Extract 3 to 10 concise search keywords from the following {{ role }} message.
Output only a JSON array of lowercase strings.

Message: {{ full_text }}
```

**Decision:**

```jinja2
Extract 3 to 10 concise search keywords from the following {{ role }} message.
Output only a JSON array of lowercase strings.

Message: {{ full_text }}
```

### 3.3 Memory ratio prompt

Inputs: `full_text`, `role`, `chatgroup_description`

Expected output: JSON object with `message`, `event`, `topic`, `fact` summing to 1.0.

**Proposed default:**

```jinja2
For the following {{ role }} message, decide what kind of past memory would be most useful.
Return a JSON object with keys "message", "event", "topic", "fact" whose values sum to 1.0.

Message: {{ full_text }}
```

**Decision:**

```jinja2
For the following {{ role }} message, decide what kind of past memory would be most useful.
Return a JSON object with keys "message", "event", "topic", "fact" whose values sum to 1.0.

Message: {{ full_text }}
```

### 3.4 Event prompt

Inputs: `messages` (contiguous block), `existing_event_summary` (optional)

Expected output: JSON object with one or more events, each with `full_text` and `finished` boolean.

**Proposed default:**

```jinja2
Summarize the following contiguous chat messages into one or more events.
If continuing an existing event, use it as context.
Return a JSON array of objects with fields: "full_text" (string), "finished" (boolean), "message_ids" (list of IDs covered).

{% if existing_event_summary %}Current open event: {{ existing_event_summary }}{% endif %}

Messages:
{% for msg in messages %}
- [{{ msg.role }}] {{ msg.full_text }}
{% endfor %}
```

**Decision:**

```jinja2
Summarize the following contiguous chat messages into one or more events.
If continuing an existing event, use it as context.
Return a JSON array of objects with fields: "full_text" (string), "finished" (boolean), "message_ids" (list of IDs covered).

{% if existing_event_summary %}Current open event: {{ existing_event_summary }}{% endif %}

Messages:
{% for msg in messages %}
- [{{ msg.role }}] {{ msg.full_text }}
{% endfor %}
```

### 3.5 Topic candidate prompt

Inputs: `message`, `candidate_topics`, `max_topics_per_message`

Expected output: JSON object with `update_topic_ids` and `new_topics`.

**Proposed default:**

```jinja2
Given the message and the candidate topics, decide which topics should be updated or if new topics should be created.
Return a JSON object with:
- "update_topic_ids": list of up to {{ max_topics_per_message }} topic IDs
- "new_topics": list of new topic summaries, if any

Message: {{ message.full_text }}

Candidate topics:
{% for t in candidate_topics %}
- {{ t.id }}: {{ t.full_text }}
{% endfor %}
```

**Decision:**

```jinja2
Given the message and the candidate topics, decide which topics should be updated or if new topics should be created.
Return a JSON object with:
- "update_topic_ids": list of up to {{ max_topics_per_message }} topic IDs
- "new_topics": list of new topic summaries, if any

Message: {{ message.full_text }}

Candidate topics:
{% for t in candidate_topics %}
- {{ t.id }}: {{ t.full_text }}
{% endfor %}
```

### 3.6 Topic update prompt

Inputs: `topic`, `message`, `chatgroup_description`

Expected output: JSON object with `full_text` (or null to keep unchanged).

**Proposed default:**

```jinja2
Update the following topic based on the new message, or return null if unchanged.
Return a JSON object with key "full_text" containing the updated summary, or "full_text": null.

Topic: {{ topic.full_text }}

New message: {{ message.full_text }}
```

**Decision:**

```jinja2
Update the following topic based on the new message, or return null if unchanged.
Return a JSON object with key "full_text" containing the updated summary, or "full_text": null.

Topic: {{ topic.full_text }}

New message: {{ message.full_text }}
```

### 3.7 Fact candidate prompt

Inputs: `message`, `candidate_facts`, `max_facts_per_message`

Expected output: JSON object with `update_fact_ids` and `new_facts`.

**Proposed default:**

```jinja2
Given the message and the candidate facts, decide which facts should be updated or if new facts should be created.
Return a JSON object with:
- "update_fact_ids": list of up to {{ max_facts_per_message }} fact IDs
- "new_facts": list of new fact texts, if any

Message: {{ message.full_text }}

Candidate facts:
{% for f in candidate_facts %}
- {{ f.id }}: {{ f.full_text }}
{% endfor %}
```

**Decision:**

```jinja2
Given the message and the candidate facts, decide which facts should be updated or if new facts should be created.
Return a JSON object with:
- "update_fact_ids": list of up to {{ max_facts_per_message }} fact IDs
- "new_facts": list of new fact texts, if any

Message: {{ message.full_text }}

Candidate facts:
{% for f in candidate_facts %}
- {{ f.id }}: {{ f.full_text }}
{% endfor %}
```

### 3.8 Fact update prompt

Inputs: `fact`, `message`, `history_messages`

Expected output: JSON object with `full_text` (or null to keep unchanged).

**Proposed default:**

```jinja2
Update the following fact based on the new message and recent context, or return null if unchanged.
Return a JSON object with key "full_text" containing the updated fact, or "full_text": null.

Fact: {{ fact.full_text }}

Recent context:
{% for msg in history_messages %}
- [{{ msg.role }}] {{ msg.full_text }}
{% endfor %}

New message: {{ message.full_text }}
```

**Decision:**

```jinja2
Update the following fact based on the new message and recent context, or return null if unchanged.
Return a JSON object with key "full_text" containing the updated fact, or "full_text": null.

Fact: {{ fact.full_text }}

Recent context:
{% for msg in history_messages %}
- [{{ msg.role }}] {{ msg.full_text }}
{% endfor %}

New message: {{ message.full_text }}
```

---

## 4. Event Lifecycle Semantics

How events are created, extended, and closed.

| Question | Proposed answer | Decision |
|----------|-----------------|----------|
| What defines an event as "finished"? | The LLM returns `finished: true` for that event. | LLM return multiple events instead of one |
| Can an unfinished event be updated later with new messages? | Yes, until the LLM marks it finished. | Yes, as long as it's the last event |
| How is the currently open event tracked? | Per Chat, the event with the latest `end_at` that is not finished. | Per Chat, the event with the latest `end_at` that is not finished. |
| Can events overlap? | No. A message belongs to one or more contiguous events; event boundaries must partition or extend contiguous ranges without overlap. | A message can be the end of an event and the begging of one. So it's a conditionnal overlap |
| What happens if the LLM returns two events covering the same messages? | The system creates both and links messages to both. Overlap is allowed if the LLM explicitly states it. | The system create both and the previous event is finished |

---

## 5. Batch Worker Selection Policy

How the worker chooses the next message to process.

| Question | Proposed answer | Decision |
|----------|-----------------|----------|
| Order by timestamp or linked-list position? | By linked-list position within a Chat; by oldest message timestamp across Chats. | by oldest message timestamp across Chats => keep it simple|
| If a ChatGroup only has protected messages left for consolidation but embedding/salience are pending, does the worker still run? | Yes, embedding/salience run for all messages regardless of protected window. | Yes, embedding/salience run for all messages regardless of protected window. Note: embedding should be computed at the message creation |
| Does the worker skip consolidation for Chats with no unprotected messages? | Yes, it only picks messages that are eligible for at least one pending pipeline. | Yes, it only picks messages that are eligible for at least one pending pipeline. |

---

## 6. Ranking and Scoring Edge Cases

| Question | Proposed answer | Decision |
|----------|-----------------|----------|
| What if all vector scores are zero in a category? | `vector_score` is 0 for all candidates in that category. Formula still evaluates. | `vector_score` is 0 for all candidates in that category. Formula still evaluates. |
| What if only one candidate exists in a category? | Its normalized score is 1.0 for that category. | Its normalized score is 1.0 for that category. |
| Should `linked_count` include historical links or only current active links? | Include all lifetime links for events/topics/facts. | There is no "historical links" only active onces|
| Should messages have `linked_count = 1` or some other value? | `linked_count = 1` for raw messages. | messages have links to topics/facts/events so it's the total count of links for a message |
| Is there a system-wide max `token_budget` a caller can request? | No hard cap in v1; caller may request any positive integer. | No hard cap; caller may request any positive integer. |
| Can a caller request `token_budget = 0` to disable memory? | Yes; return an empty memory block. | |

---

## 7. Retry and Failure Policy

| Question | Proposed answer | Decision |
|----------|-----------------|----------|
| Max retries per pipeline before giving up? | 5 | 5  |
| Retry delay strategy? | Exponential backoff: 60s, 120s, 240s, capped at 300s. | Exponential backoff: 60s, 120s, 240s, capped at 300s. |
| Does retry count reset after success? | Yes, when a pipeline reaches `done`. | Yes, when a pipeline reaches `done`. |
| What happens when max retries is reached? | Pipeline is marked `done` with a failure flag; logged in admin status. | pipeline marked with a special failed (failed_cancelled) |

---

## 8. Message Deletion and Dangling Links

| Question | Proposed answer | Decision |
|----------|-----------------|----------|
| Physical or soft delete for messages? | Physical delete; repair linked list. | Physical delete; repair linked list. |
| What happens to event/topic/fact links when a message is deleted? | Links become dangling and are ignored. `linked_count` in ranking excludes deleted message links. | Links are deleted. Event topic and fact aren't updated |
| Should there be a reaper job to clean dangling links? | No in v1. | Deleted on deletion, no repair |
| Should deleting a message reset processing state? | No. | No |

---

## 9. Search Vector Generation

How `tsvector` columns are populated.

| Question | Proposed answer | Decision |
|----------|-----------------|----------|
| For Messages, what goes into `search_vector`? | Concatenation of `full_text` and `keywords`, converted to tsvector. | keyword only for bm25 search |
| For Events, Topics, Facts, what goes into `search_vector`? | `full_text` and `keywords`, converted to tsvector. | keyword only for bm25 search |
| Which text search config is used? | `english` (default) or ChatGroup-configurable. | `english` (default) or ChatGroup-configurable.|
| Is stemming applied? | Yes, via PostgreSQL default text search configuration. | Yes, via PostgreSQL default text search configuration. |

---

## 10. Migrations and Startup

| Question | Proposed answer | Decision |
|----------|-----------------|----------|
| Are Alembic migrations run automatically on container startup? | Yes, in the entrypoint before starting the app. | Yes, in the entrypoint before starting the app. |
| Is there a separate init container option documented? | Yes, as an alternative for production-like deployments. | Yes, as an alternative for production-like deployments.  |
| What happens if a migration fails on startup? | Container exits with error; does not start serving requests. | Container exits with error; does not start serving requests. |

---

## 11. Provider YAML Schema

| Question | Proposed answer | Decision |
|----------|-----------------|----------|
| Which per-task parameters can be overridden besides `model`, `temperature`, `max_tokens`? | Any OpenAI chat completion parameter: `top_p`, `frequency_penalty`, `presence_penalty`, `stop`, `seed`, `response_format`. | Any OpenAI chat completion parameter: `top_p`, `frequency_penalty`, `presence_penalty`, `stop`, `seed`, `response_format`. |
| Is `response_format: { "type": "json_object" }` recommended for prompts that require JSON? | Yes. | Yes.|
| What happens if `api_key_env` points to an unset variable? | Startup error. | Startup error. |
| What happens if `api_key_env` is omitted? | Call provider without authentication key. | Call provider without authentication key. |

---

## 12. Authentication and Access Control (v1)

| Question | Proposed answer | Decision |
|----------|-----------------|----------|
| Is there any authentication in v1? | No. | Yes. All operation beside loging under authentification. Each user have the same rights in v1|
| Can any caller read/write any User, ChatGroup, Chat, Message? | Yes, in v1. | Yes if authentified|
| Is there at least a note in the docs warning that v1 is unauthenticated? | Yes. | A note that the right management is lacking and any user can do anything|

---

## 13. Message `full_text` Limits

| Question | Proposed answer | Decision |
|----------|-----------------|----------|
| Max length for `full_text`? | 32,000 characters. | 32,000 characters. |
| Behavior on exceed? | 400 Bad Request. | 400 Bad Request. |
| Min length? | 1 non-whitespace character. | no |

---

## 14. PUT /messages semantics

| Question | Proposed answer | Decision |
|----------|-----------------|----------|
| Is `PUT /chats/{id}/messages/{id}` a full overwrite or partial update? | Full overwrite of the message resource. | Full overwrite of the message resource. |
| Can `role` be changed on edit? | Yes. | Yes. |
| If `prev_message_id` changes, does the API update neighbors? | No; callers must manage neighbor links. The API only validates consistency. | No; callers must manage neighbor links. The API only validates consistency.|
| If a message is edited, do embedding/salience reset to `waiting`? | Yes, always. | Yes, always (embedding are computed on creation) |
| If a message outside the protected window is edited, do event/topic/fact states reset? | No. | No. |

---

## 15. Scale and Indexing

| Question | Proposed answer | Decision |
|----------|-----------------|----------|
| Expected max messages per Chat? | Tens of thousands. | Tens to hundreds of thousands. |
| Expected max total messages per ChatGroup? | Hundreds of thousands. | |
| Vector index type? | HNSW for faster ANN at scale. | HNSW for faster ANN at scale. |
| Full-text index type? | GIN on `search_vector`. | GIN on `search_vector`. |
| Should processing state fields be indexed? | Yes, B-tree on the JSONB processing state keys used for queue queries. | Yes, B-tree on the JSONB processing state keys used for queue queries. |
| Should we paginate linked-list traversal? | Yes, for list endpoints with cursor pagination. | Yes, for list endpoints with cursor pagination. |

---

## 16. Health and Readiness

| Question | Proposed answer | Decision |
|----------|-----------------|----------|
| `GET /health` checks what? | App is running and can connect to Postgres. | App is running and can connect to Postgres. |
| Is there a separate `/ready` endpoint? | No in v1. | No in v1. |

---

## 17. Logging

| Question | Proposed answer | Decision |
|----------|-----------------|----------|
| Log format? | Structured JSON to stdout. | Text log on stdout |
| Log level configurable via env var? | Yes, `VISTUI_LOG_LEVEL` (default INFO). | Yes, `VISTUI_LOG_LEVEL` (default INFO). |
| Are LLM request/response payloads logged? | No full payloads; log task name, provider, latency, and success/failure. | No full payloads; log task name, provider, latency, and success/failure. |

---

## 18. Justfile Tasks (recommended)

| Task | Command | Decision |
|------|---------|----------|
| Run tests | `just test` | `just test` |
| Run server locally | `just run` | `just run` |
| Run migrations | `just migrate` | `just migrate` |
| Start Docker Compose | `just up` | `just up` |
| Lint | `just lint` | `just lint` |
| Format | `just fmt` | `just fmt` |



## Changelog

- 2026-07-11: Created pre-implementation decisions form.
