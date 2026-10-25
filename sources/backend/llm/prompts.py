# Default prompts used when a ChatGroup has no custom prompt stored.
# All prompts expect JSON output.

EVENT_GROUPING = """\
You are grouping a contiguous block of chat messages into one or more events.
For each event, write a short summary and list the message IDs that belong to it.
A single message can belong to multiple events if it spans boundaries.

Return strictly a JSON object with the shape:
{{
  "events": [
    {{"summary": "...", "message_ids": ["msg-id-1", "msg-id-2"]}},
    ...
  ]
}}

Messages:
{messages}
"""


TOPIC_CANDIDATE = """\
You are updating topic summaries for a chat group.
Given a new message and a list of existing topics, decide for each topic whether to update it,
and decide whether any new topics should be created.

Return strictly a JSON array of actions with the shape:
[
  {{"action": "create", "summary": "..."}},
  {{"action": "update", "topic_id": "existing-topic-id", "summary": "..."}},
  {{"action": "skip", "topic_id": "existing-topic-id"}}
]

New message:
{message}

Existing topics (id - summary):
{topics}

Maximum topics to touch: {max_topics}
"""


FACT_CANDIDATE = """\
You are updating factual memories for a chat group.
Given a new message and a list of existing facts, decide for each fact whether to update it,
and decide whether any new facts should be created.

Return strictly a JSON array of actions with the shape:
[
  {{"action": "create", "summary": "..."}},
  {{"action": "update", "fact_id": "existing-fact-id", "summary": "..."}},
  {{"action": "skip", "fact_id": "existing-fact-id"}}
]

New message:
{message}

Existing facts (id - summary):
{facts}

Maximum facts to touch: {max_facts}
"""
