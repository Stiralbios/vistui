# Vistui LLM Provider System

## Context

Vistui needs three kinds of model calls:

- **Embeddings** — dense vector representations of text.
- **LLM completions / chat** — memory ratio and all batch consolidation prompts.
- **Decision calls (System One)** — salience scoring for messages and memory objects.

Embeddings and chat are served by OpenAI-compatible APIs, so self-hosters can mix
providers (e.g., local Ollama for embeddings, remote OpenAI-compatible API for LLM
calls). Decision calls need an ollama System One endpoint (see below).

## Goals

1. Support multiple named OpenAI-compatible providers.
2. Allow different providers and models for embeddings versus LLM calls.
3. Keep secrets (API keys) out of the database and out of code.
4. Make provider configuration reloadable without rebuilding the application.

## Non-goals

- Supporting non-OpenAI SDKs directly.
- Built-in model fine-tuning.
- Fallback / retry across providers.

## Configuration

Providers and ranking formulas are declared in a YAML settings file loaded at startup.
Secrets are injected via environment variables.

### Example `settings.yaml`

```yaml
# Named ranking formulas for the hybrid search: chatgroups reference them by name,
# and the API accepts names only (unknown names are rejected with 422). The
# `default` entry is the same formula the code carries as its built-in; the section
# may override it or add further names.
ranking_formulas:
  default: >-
    lambda vector_score, bm25_score, salience, linked_count:
    max(vector_score, bm25_score) * salience * link_weight(linked_count)

providers:
  local-ollama:
    base_url: "http://localhost:11434/v1"
    api_key_env: "OLLAMA_API_KEY"  # optional; may be empty
    timeout_seconds: 60

  remote-llm:
    base_url: "https://api.example.com/v1"
    api_key_env: "REMOTE_API_KEY"
    timeout_seconds: 120

defaults_models:
  embedding:
    provider: "local-ollama"
    model: "nomic-embed-text"
    dimensions: 768
  llm:
    provider: "remote-llm"
    model: "gpt-4o-mini"
    temperature: 0.3
    max_tokens: 2048
  decision:
    provider: "local-ollama"
    model: "d3:4b"

overrides_models:
  # Decision tasks (salience, memory_ratio, event_boundary): their providers must be
  # a directly reachable ollama System One endpoint (not a chat model).
  salience:
    provider: "local-ollama"
    model: "d3:4b"
  memory_ratio:
    provider: "local-ollama"
    model: "d3:4b"
  event_boundary:
    provider: "local-ollama"
    model: "d3:4b"
  event_summary:
    provider: "remote-llm"
    model: "gpt-4o-mini"
    temperature: 0.2
  topic_candidate:
    provider: "remote-llm"
    model: "gpt-4o-mini"
    temperature: 0.2
  fact_candidate:
    provider: "remote-llm"
    model: "gpt-4o-mini"
    temperature: 0.2
```

### Environment variables

| Variable | Purpose |
|----------|---------|
| `OLLAMA_API_KEY` | API key for the `local-ollama` provider. |
| `REMOTE_API_KEY` | API key for the `remote-llm` provider. |
| `VISTUI_SETTINGS_FILE` | Path to `settings.yaml` (default `./settings.yaml`). |
| `VISTUI_DECISION_MODEL` | Decision model for the legacy env fallback (`d3:4b`). |

If `api_key_env` is not set, the provider is used without authentication (common for local Ollama).

### Current deployment

The dev deployment points both defaults at the **gomodel proxy** (`https://gomodel.eagal.org/v1`,
auth via `GOMODEL_TOKEN`), an OpenAI-compatible proxy in front of several ollama upstreams.
Model ids are prefixed by upstream: `ollama_yople/*` is our own ollama box (also reachable
directly via `local-ollama`), `strata_toaster/iq3_xxs` is a small quantized model hosted on
another PC that is *only* reachable through the proxy. The embedding goes through the same
proxy (`ollama_yople/embeddinggemma-2:740m`) — verified bit-identical to calling the box
directly (cosine 1.0), so switching the default between the two routes is safe.

The `d3:4b` decision model is the one thing that must be called **directly** (`local-ollama`):
it is a System One model (`POST /v1/systemone`, typed `choice`/`noul`/`score` questions, no
chat completions — ollama rejects chat for it), and gomodel does not forward `/v1/systemone`
to ollama upstreams yet (it answers that "provider type ollama has no System One API",
outdated for ollama ≥ 0.35).

## Provider abstraction

The application wraps the OpenAI SDK with a thin adapter:

```python
class Provider:
    def embed(self, texts: list[str]) -> list[list[float]]: ...
    def chat(self, messages: list[dict], task: str) -> str: ...
    async def decide(
        self, state, questions: dict[str, SystemOneQuestion], keep_alive=None, task: str | None = None
    ) -> dict[str, DecisionAnswer]: ...
```

- `task` selects the override config (e.g., `salience`, `memory_ratio`, `event_boundary`, `event_summary`, `topic_candidate`, `fact_candidate`, or `default`). The same overrides map serves chat tasks and decision tasks: the batch salience stage calls `decide(..., task="salience")`, the event boundary call uses `task="event_boundary"` and the search ratio call `task="memory_ratio"`; both resolve their override with `defaults.decision` as fallback.
- If no override exists for a task, the `defaults.llm`, `defaults.embedding` or `defaults.decision` config is used.
- The adapter parses the YAML and resolves `api_key_env` once at startup. The file can be reloaded on SIGHUP or via an admin endpoint.

## Embedding call semantics

- Batchable when possible, but a single message only needs one embedding.
- The configured `dimensions` is used to validate returned vectors before storing them.
- All embeddings are stored as `pgvector` vectors of the configured dimension.

## LLM call semantics

- All prompts are rendered as Jinja2 templates from the ChatGroup's `prompts` JSONB.
- The rendered prompt is sent as a chat completion.
- The response is expected to be either plain text or a JSON object; the caller is responsible for parsing.
- Each prompt template receives a well-defined input context documented in the prompt files.

### Prompt template inputs

| Template | Inputs |
|----------|--------|
| `salience` | message text, role, ChatGroup description. The template's `{kind}` is substituted with the element type; the question's criteria come from the chatgroup's `salience_rubric` prompts key (2-10 ordered levels, 5-level default). |
| `memory_ratio` | ChatGroup description, search query (the decide state). A decision task: the template becomes the instructions of one choice question whose criteria come from the chatgroup's `memory_ratio_criteria` prompts key (labels fixed to the memory types, at least two); the answer's probabilities are the distribution. |
| `event_boundary` | ChatGroup description, the chat's last event (summary, time range), the new message (role, text, created_at). A decision task: the template becomes the instructions of one choice question whose criteria come from the chatgroup's `event_boundary_criteria` prompts key (extend / new / mixed); the highest-probability criterion wins. |
| `event_summary` | the event's full member list, oldest first (`{messages}`), each message attributed to its speaker using the chatgroup's participant names. Always rebuilt from the sources, never from the previous summary. |
| `topic_candidate` | existing topic summaries, the new message attributed to its speaker, `max_topics_per_message`, the participant names (`config.user_name`/`config.assistant_name`). |
| `fact_candidate` | existing fact summaries, the new message attributed to its speaker, `max_facts_per_message`, the participant names. |

## Decision call semantics

Decision models (System One, e.g. `d3:4b`) answer **typed questions** on ollama's
`POST /v1/systemone` endpoint; they have no chat-completions API and never produce
conversational text.

- `decide(state, questions, keep_alive=None, task=None)` sends one JSON-serializable
  `state` shared by all questions (1-64, keyed by name), using the task's override
  provider/model or `defaults.decision`.
- Question types: `ChoiceQuestion` (labeled options -> best label + probabilities),
  `NoulQuestion` (yes/no -> probability of true) and `ScoreQuestion` (2-10 ordered rubric
  levels -> probability-weighted index plus per-level probabilities and confidence).
- Answers come back as `DecisionAnswer` objects keyed by the question names; answers never
  feed later questions (each is scored independently).
- `keep_alive` keeps the model loaded between batch requests (duration string or seconds).
- Decision calls are slow: `d3:4b` takes ~5-10s per scored question, and every question
  is scored against the whole state. The salience stages score element per element —
  one `decide()` call per message or memory object, state = chatgroup description plus
  the single element — and give the decision provider a generous `timeout_seconds`
  (300 in the dev config, which also covers the cold model load; only decision calls
  use `local-ollama`).
- Routing caveat: gomodel does not forward `/v1/systemone` to ollama upstreams, so the
  decision provider must be a directly reachable ollama (`local-ollama` in the dev config).

## Separation of concerns

| Concern | Owned by |
|---------|----------|
| Which provider/model/temperature to use | `settings.yaml`. |
| How to format the request | Provider adapter. |
| What text to send and how to interpret the response | Prompt template and task code. |
| Where prompts are stored | ChatGroup `prompts` JSONB; default texts in `sources/backend/llm/prompts.py`. |

## Error handling

- Provider connection errors are retried up to a configured number of times with exponential backoff.
- Invalid model responses are surfaced as task-level failures, not provider failures.
- Missing provider or task config causes a clear startup error.

## Open questions

- Should provider config be reloadable at runtime without restart?
- Should the system support streaming LLM responses for any task? (Probably not initially.)
- Should embeddings use a fallback dimension normalization if the provider returns a different size?

## Changelog

- 2026-11-22: All default prompt texts — decision-model (salience, memory ratio,
  event boundary) and chat-model (event summary, topic/fact) — live in
  `sources/backend/llm/prompts.py`; ChatGroupPrompts references the constants as its
  field defaults.
- 2026-11-22 (issue 007): The event task split into `event_boundary` (decision model:
  per-message extend/new/mixed choice against the chat's last event) and
  `event_summary` (chat model: rebuild an event's summary from its full member list).
- 2026-11-21: `memory_ratio` is a decision task (System One choice question over the
  memory types, one `decide()` call per opted-in search; probabilities are the
  distribution): its override must point at a directly reachable ollama endpoint, so
  the example no longer routes it to a chat model. The question's criteria are
  configurable per chatgroup via the `memory_ratio_criteria` prompts key.
- 2026-07-11: Initial LLM provider design derived from `startingpoint.md` and discussion.
- 2026-07-11: Applied feedback: split `fact` task into `fact_candidate` and `fact_update`, split `topic` task into `topic_candidate` and `topic_update`.
- 2026-11-22: Removed the separate `topic_update` and `fact_update` tasks; candidate prompts now directly emit the new/updated summary, so a single LLM call creates or updates topics and facts.
- 2026-11-22: Statements are attributed to their author: the candidate prompts name both participants (chatgroup config `user_name`/`assistant_name`, generic fallbacks) and attribute each message to its speaker; event summary member lists name each message's speaker too.
- 2026-11-15: Added `defaults.decision` and `Provider.decide()` for System One decision models (`/v1/systemone`): typed `choice`/`noul`/`score` questions, probability answers, direct-ollama routing only.
- 2026-11-15: Salience stages switched from batched calls (chunked at 5 questions) to
  element-per-element scoring — one `decide()` call per message / memory object.
- 2026-11-21: The salience decision question is fully configurable per chatgroup: the
  `salience` prompts key holds the complete instruction template (`{kind}` substituted
  with the element type) and the new `salience_rubric` key holds the ordered rubric
  levels (2-10); score normalization adapts to the rubric length.
- 2026-11-15: `decide()` gained the `task` parameter (overrides map now serves decision tasks too); the salience stages score messages and memory objects with `d3:4b` against a 5-level rubric, `keep_alive` keeps the model warm between rounds.

- 2026-11-21: `providers.yaml` renamed to `settings.yaml` — the operator's single
  settings file. The model sections became `defaults_models` and `overrides_models`
  (same content), and the file gained the `ranking_formulas` section: named ranking
  formulas referenced by chatgroups (the API accepts names only, closing the
  arbitrary-code-execution issue of formula authorship via the API). The env var
  `VISTUI_PROVIDERS_FILE` became `VISTUI_SETTINGS_FILE`.
