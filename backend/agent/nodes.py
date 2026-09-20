import logging
import json
import time
from typing import Any

import requests

from .state import AgentState
from .config import AgentConfig, get_agent_config

logger = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "llama3.1"  # swap to whatever you've pulled locally


def classify_intent(agent_state: AgentState) -> dict[str, Any]:
    """
    Classifies the intent of the user's query based on the agent's state.

    Args:
        agent_state (AgentState): The current state of the agent.

    Returns:
        dict[str, Any]: Partial state update.
    """
    query = agent_state["user_query"]
    history = agent_state["conversation_history"]
    agent_id = agent_state["agent_id"]

    logger.info(f"[classify_intent] Query: {query[:80]}...")

    start = time.monotonic()
    classification = call_intent_classifier(agent_id, query, history)
    latency_ms = (time.monotonic() - start) * 1000
    log_classification(agent_id, query, classification, latency_ms)

    return state_update(query, classification)


def state_update(query: str, classification: dict[str, Any]) -> dict[str, Any]:
    intent = classification["intent"]
    result: dict[str, Any] = {"intent": intent}

    if intent == "clarification_qn":
        result["needs_clarification"] = True
        result["ask_user"] = classification.get(
            "clarification_question", "Could you clarify your question?"
        )
    elif intent == "general_chat":
        result["needs_clarification"] = False
        result["answer"] = _canned_response(query)
    elif intent == "out_of_scope":
        result["needs_clarification"] = False
        result["answer"] = _out_of_scope_response(query)
    else:  # Domain_qn
        result["needs_clarification"] = False
        result["resolved_query"] = classification.get("resolved_query", query)

    return result


def _canned_response(query: str) -> str:
    return "Hi! Ask me anything within my knowledge base and I'll help."


def _out_of_scope_response(query: str) -> str:
    return "That's outside what I can help with here — I'm scoped to this agent's knowledge base."


INTENT_CLASSIFICATION_SYSTEM_PROMPT = """You are a query intent classifier for the {agent_name} agent.

CONTEXT: {agent_name} helps engineers with {domain_description}, drawing on
this agent's own knowledge base only. It does not have access to other JLR
agents' domains outside its scope.

Classify the user's CURRENT message into exactly ONE intent. Use conversation
history only to resolve pronouns/follow-ups — classify based on what the user
needs right now, not what was asked before.

INTENTS (check top to bottom, stop at first match):

1. general_chat — greetings, thanks, small talk, or meta questions about the
   bot itself.
   Examples: "hi", "thanks", "what can you help with?", "who are you?"

2. out_of_scope — a real question, but clearly NOT about {agent_name}'s domain.
   Examples: "how do I cook pasta?", "what's the weather today?"

3. clarification_qn — a query that's on-topic for {agent_name} but missing
   enough detail to search anything useful (no specific component, programme,
   document, or entity named — and none inferable from history).
   Examples: "what's the status?", "check the value", "is it approved?"

4. Domain_qn — a specific, answerable question within {agent_name}'s scope,
   with enough detail to search directly (a named component, programme,
   supplier, document, or comparable entity is present or inferable from history).

ADDITIONAL FIELDS:

- needs_clarification: true only when intent is "clarification_qn". False otherwise.
- clarification_question: required if needs_clarification is true — ask ONE
  specific question that would resolve the ambiguity, phrased naturally.
  Otherwise null.
- resolved_query: the self-contained version of the query used for retrieval.
    - If this is a follow-up-style reference (pronouns, "the other one", "and
      for X instead"), rewrite it using conversation history so it stands
      alone without needing prior context.
    - Otherwise, lightly clean the query (fix obvious typos, expand obvious
      abbreviations) but preserve the user's actual wording and intent.
    - For general_chat or out_of_scope, just pass the original query through
      unchanged.

OUTPUT — respond with ONLY this JSON object, no markdown fences, no other text:
{{
  "intent": "general_chat | out_of_scope | clarification_qn | Domain_qn",
  "needs_clarification": true | false,
  "clarification_question": "..." | null,
  "resolved_query": "..."
}}"""


def call_intent_classifier(
    agent_id: str, query: str, history: list[dict] | None = None
) -> dict[str, Any]:
    """Classify user intent to route the query appropriately."""
    history = history or []
    config = _load_config(agent_id)

    system_prompt = INTENT_CLASSIFICATION_SYSTEM_PROMPT.format(
        agent_name=config.display_name,
        domain_description=config.domain_description,
    )

    messages = [{"role": "system", "content": system_prompt}]
    for msg in history[-4:]:
        messages.append(msg)
    messages.append({"role": "user", "content": query})

    try:
        raw = _call_ollama(messages, temperature=0.1)
    except Exception as e:
        logger.error(f"[classify_intent] LLM call failed: {e}")
        return _fallback_classification(query)

    return _parse_classification(raw, query)


def _call_ollama(messages: list[dict], temperature: float = 0.1, timeout: float = 10.0) -> str:
    response = requests.post(
        OLLAMA_URL,
        json={
            "model": OLLAMA_MODEL,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()["message"]["content"]


def _parse_classification(raw: str, query: str) -> dict[str, Any]:
    """Strips markdown fences if present, parses JSON, falls back to a safe
    default if the LLM's output is malformed."""
    try:
        json_str = raw.strip()
        if json_str.startswith("```"):
            json_str = json_str.split("```")[1]
            if json_str.startswith("json"):
                json_str = json_str[4:]
        result = json.loads(json_str)

        result.setdefault("needs_clarification", False)
        result.setdefault("clarification_question", None)
        result.setdefault("resolved_query", query)
        return result

    except (json.JSONDecodeError, IndexError) as e:
        logger.warning(f"[classify_intent] Failed to parse intent JSON: {raw!r} ({e})")
        return _fallback_classification(query)


def _fallback_classification(query: str) -> dict[str, Any]:
    """Fail-open default: matches the CURRENT schema (Domain_qn, capital D)."""
    return {
        "intent": "Domain_qn",
        "needs_clarification": False,
        "clarification_question": None,
        "resolved_query": query,
    }


def _load_config(agent_id: str) -> AgentConfig:
    return get_agent_config(agent_id)


def log_classification(
    agent_id: str, query: str, classification: dict[str, Any], latency_ms: float
) -> None:
    logger.info(
        "intent_classified",
        extra={
            "agent_id": agent_id,
            "query": query[:200],
            "intent": classification.get("intent"),
            "needs_clarification": classification.get("needs_clarification"),
            "resolved_query": classification.get("resolved_query"),
            "latency_ms": round(latency_ms, 1),
        },
    )
    if latency_ms > 3000:
        logger.warning(
            f"[classify_intent] Slow classification: {latency_ms:.0f}ms for agent={agent_id}"
        )
