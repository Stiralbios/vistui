"""Default prompt texts for every LLM and decision-model task.

ChatGroupPrompts (backend.chatgroup.schemas) references these constants as its field
defaults: a chatgroup row stores them at creation and can override any of them per
prompts key. All prompts expect JSON output.

The decision-model prompts (salience, memory ratio, event boundary) become the
instructions and criteria of System One choice/score questions; the chat-model
prompts are sent as chat completions.
"""

# --- Decision-model prompts ---------------------------------------------------

# Full instruction template for the salience score question: `{kind}` is substituted
# with "message", "event", "topic" or "fact"; a template without the placeholder is
# sent verbatim.
SALIENCE = "Judge how much the {kind} in the state is worth remembering for future conversations."

# Ordered rubric for the salience question, lowest level first: the decision model's
# answer is a probability-weighted zero-based level index, normalized by
# len(rubric) - 1 into the 0..1 salience.
DEFAULT_SALIENCE_RUBRIC = [
    "Trivial: greetings, filler, or noise; not worth remembering.",
    "Low: minor context that will rarely matter later.",
    "Moderate: useful context or conversational continuity.",
    "High: meaningful personal facts, plans, or preferences worth keeping.",
    "Critical: core information - decisions, key people, dates, commitments.",
]

# Instructions of the memory-ratio choice question (the share of the retrieval budget
# each memory type deserves for a query).
MEMORY_RATIO = "Determine the desired memory category distribution."

# Criteria of the memory-ratio question, keyed by memory type: the decision model
# reports its probability distribution over these keys, and the search packing looks
# its shares up by them — so the keys are fixed to the four memory types while
# chatgroups reword the descriptions. A type left out of the criteria is offered no
# share of the retrieval budget.
DEFAULT_MEMORY_RATIO_CRITERIA = {
    "message": "A verbatim chat message.",
    "event": "A summary of a contiguous sequence of messages.",
    "topic": "The additive running summary of a recurring theme across the chatgroup.",
    "fact": "A small snippet of the current state of one thing about the user or the world.",
}

# Instructions of the event-boundary choice question: how the next message relates to
# the chat's last event (the state carries both, the criteria carry the actions).
EVENT_BOUNDARY = (
    "Decide how the message in the state relates to the last event: it continues it, "
    "starts a distinct new event, or both concludes the last event and starts the next."
)

# Criteria of the event-boundary question, keyed by action: the event pipeline
# switches on the keys, so they are fixed to the three actions while chatgroups
# reword the descriptions (a subset forbids an action).
DEFAULT_EVENT_BOUNDARY_CRITERIA = {
    "extend": "The message continues the last event: same activity, subject or exchange.",
    "new": "The message starts a distinct new event: new subject or activity, or a clear break.",
    "mixed": "The message concludes the last event and also starts the next one.",
}

# --- Chat-model prompts -------------------------------------------------------

# Generation template for event summaries: `{messages}` is substituted with the
# event's full message list — the summary is always rebuilt from the sources, so
# extending an event never rewrites from its previous summary.
EVENT_SUMMARY = (
    "Summarize the following chat messages into one concise event summary: what "
    "happened or was discussed, in order.\n\n"
    'Return strictly a JSON object with the shape:\n{{"summary": "..."}}\n\n'
    "Messages:\n{messages}"
)

# Topic maintenance: short title + additive body. The body only grows (earlier lines kept,
# new ones appended); the title can be reworded freely.
TOPIC_CANDIDATE = """\
You are maintaining the topics of a chat group.

The conversation is between two participants: {user_name} (the human) and {assistant_name}
(the AI). Messages come from either of them — attribute each statement to whoever made it,
and refer to the participants by name rather than as "the user" or "the AI".

A topic is the running summary of one recurring theme of the conversation: a short title
followed by an additive list of statements, shaped like:
Horses:
- {user_name} used to ride horses.
- {user_name} stopped riding two years ago.

When updating a topic, return its complete new text. The body only ever grows: everything
it already says plus the new information from the message, as additional lines — never drop
or rewrite earlier body lines, and if the message contradicts them, add the newer statement
as a new line so the evolution stays visible. The title may be reworded freely to stay
accurate.

Given a new message and the existing topics, decide for each topic whether to extend it, and
whether any new topics should be created.

Return strictly a JSON array of actions with the shape:
[
  {{"action": "create", "summary": "..."}},
  {{"action": "update", "topic_id": "existing-topic-id", "summary": "..."}},
  {{"action": "skip", "topic_id": "existing-topic-id"}}
]

New message from {speaker}:
{message}

Existing topics (id - summary):
{topics}

Maximum topics to touch: {max_topics}
"""

# Fact maintenance: short title + current-state body. The body is fully replaced when the
# truth changes; conflicting facts never coexist.
FACT_CANDIDATE = """\
You are maintaining the facts of a chat group.

The conversation is between two participants: {user_name} (the human) and {assistant_name}
(the AI). Messages come from either of them — attribute each statement to whoever made it,
and refer to the participants by name rather than as "the user" or "the AI".

A fact is a small snippet describing the current state of one specific thing about a
participant or the world: a short title followed by the current state, shaped like:
{user_name}'s mom health:
{user_name}'s mom has cancer.

When updating a fact, return its complete new text. The body holds no history and no lists:
when the truth changes, it is fully replaced by the new state, and two conflicting facts
must never coexist. Earlier states stay visible in the events and topics, not in the facts —
when the message invalidates an existing fact, update that fact to the new truth instead of
creating a second one. The title may be reworded freely; a fact the message does not change
is skipped.

Given a new message and the existing facts, decide for each fact whether to replace it, and
whether any new facts should be created.

Return strictly a JSON array of actions with the shape:
[
  {{"action": "create", "summary": "..."}},
  {{"action": "update", "fact_id": "existing-fact-id", "summary": "..."}},
  {{"action": "skip", "fact_id": "existing-fact-id"}}
]

New message from {speaker}:
{message}

Existing facts (id - summary):
{facts}

Maximum facts to touch: {max_facts}
"""
