# Vistui LLM Provider System

## Context

Vistui needs two kinds of model calls:

- **Embeddings** — dense vector representations of text.
- **LLM completions / chat** — keyword extraction, memory ratio, salience scoring, and all batch consolidation prompts.

Both are expected to be served by OpenAI-compatible APIs. This allows self-hosters to mix providers (e.g., local Ollama for embeddings, remote OpenAI-compatible API for LLM calls).

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

Providers are declared in a YAML file loaded at startup. Secrets are injected via environment variables.

### Example `providers.yaml`

```yaml
providers:
  local-ollama:
    base_url: "http://localhost:11434/v1"
    api_key_env: "OLLAMA_API_KEY"  # optional; may be empty
    timeout_seconds: 60

  remote-llm:
    base_url: "https://api.example.com/v1"
    api_key_env: "REMOTE_API_KEY"
    timeout_seconds: 120

defaults:
  embedding:
    provider: "local-ollama"
    model: "nomic-embed-text"
    dimensions: 768
  llm:
    provider: "remote-llm"
    model: "gpt-4o-mini"
    temperature: 0.3
    max_tokens: 2048

overrides:
  salience:
    provider: "remote-llm"
    model: "gpt-4o-mini"
    temperature: 0.0
  keywords:
    provider: "remote-llm"
    model: "gpt-4o-mini"
    temperature: 0.1
  memory_ratio:
    provider: "remote-llm"
    model: "gpt-4o-mini"
    temperature: 0.1
  event:
    provider: "remote-llm"
    model: "gpt-4o-mini"
    temperature: 0.2
  topic_candidate:
    provider: "remote-llm"
    model: "gpt-4o-mini"
    temperature: 0.2
  topic_update:
    provider: "remote-llm"
    model: "gpt-4o-mini"
    temperature: 0.2
  fact_candidate:
    provider: "remote-llm"
    model: "gpt-4o-mini"
    temperature: 0.2
  fact_update:
    provider: "remote-llm"
    model: "gpt-4o-mini"
    temperature: 0.2
```

### Environment variables

| Variable | Purpose |
|----------|---------|
| `OLLAMA_API_KEY` | API key for the `local-ollama` provider. |
| `REMOTE_API_KEY` | API key for the `remote-llm` provider. |
| `VISTUI_PROVIDERS_FILE` | Path to `providers.yaml` (default `./providers.yaml`). |

If `api_key_env` is not set, the provider is used without authentication (common for local Ollama).

## Provider abstraction

The application wraps the OpenAI SDK with a thin adapter:

```python
class Provider:
    def embed(self, texts: list[str]) -> list[list[float]]: ...
    def chat(self, messages: list[dict], task: str) -> str: ...
```

- `task` selects the override config (e.g., `salience`, `keywords`, `memory_ratio`, `event`, `topic_candidate`, `topic_update`, `fact_candidate`, `fact_update`, or `default`).
- If no override exists for a task, the `defaults.llm` or `defaults.embedding` config is used.
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
| `salience` | message text, role, ChatGroup description. |
| `keywords` | message text, role, ChatGroup description. |
| `memory_ratio` | message text, role, ChatGroup description. |
| `event` | contiguous message block, existing event summary (if continuing). |
| `topic_candidate` | existing topic summaries, new message, `max_topics_per_message`. |
| `topic_update` | existing topic, new message, ChatGroup context. |
| `fact_candidate` | existing fact summaries, new message, `max_facts_per_message`. |
| `fact_update` | existing fact, new message, recent fact-linked messages. |

## Separation of concerns

| Concern | Owned by |
|---------|----------|
| Which provider/model/temperature to use | `providers.yaml`. |
| How to format the request | Provider adapter. |
| What text to send and how to interpret the response | Prompt template and task code. |
| Where prompts are stored | ChatGroup `prompts` JSONB. |

## Error handling

- Provider connection errors are retried up to a configured number of times with exponential backoff.
- Invalid model responses are surfaced as task-level failures, not provider failures.
- Missing provider or task config causes a clear startup error.

## Open questions

- Should provider config be reloadable at runtime without restart?
- Should the system support streaming LLM responses for any task? (Probably not initially.)
- Should embeddings use a fallback dimension normalization if the provider returns a different size?

## Changelog

- 2026-07-11: Initial LLM provider design derived from `startingpoint.md` and discussion.
- 2026-07-11: Applied feedback: split `fact` task into `fact_candidate` and `fact_update`, split `topic` task into `topic_candidate` and `topic_update`.
