"""Tests for the default prompt texts in llm/prompts.py."""

from backend.chatgroup.schemas import ChatGroupPrompts
from backend.llm import prompts


def test_chatgroup_prompts_defaults_are_the_prompt_constants():
    """ChatGroupPrompts uses the llm/prompts.py constants as its field defaults —
    one source of truth per prompt text, shared with the pipelines."""
    defaults = ChatGroupPrompts()
    assert defaults.salience == prompts.SALIENCE
    assert defaults.salience_rubric == prompts.DEFAULT_SALIENCE_RUBRIC
    assert defaults.memory_ratio == prompts.MEMORY_RATIO
    assert defaults.memory_ratio_criteria == prompts.DEFAULT_MEMORY_RATIO_CRITERIA
    assert defaults.event_boundary == prompts.EVENT_BOUNDARY
    assert defaults.event_boundary_criteria == prompts.DEFAULT_EVENT_BOUNDARY_CRITERIA
    assert defaults.event_summary == prompts.EVENT_SUMMARY
    assert defaults.topic_candidate == prompts.TOPIC_CANDIDATE
    assert defaults.fact_candidate == prompts.FACT_CANDIDATE


def test_decision_prompts_are_usable_question_material():
    """The decision-model constants are the material of System One questions: full
    instruction templates plus keyed criteria, and the salience template carries the
    {kind} placeholder the batch stages substitute."""
    assert prompts.SALIENCE.format(kind="message")
    assert set(prompts.DEFAULT_EVENT_BOUNDARY_CRITERIA) == {"extend", "new", "mixed"}
    assert set(prompts.DEFAULT_MEMORY_RATIO_CRITERIA) == {"message", "event", "topic", "fact"}
    assert len(prompts.DEFAULT_SALIENCE_RUBRIC) >= 2
    assert "{messages}" in prompts.EVENT_SUMMARY
    prompts.EVENT_SUMMARY.format(messages="sample")  # the template must survive formatting
