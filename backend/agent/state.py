from typing import Annotated, TypedDict
from operator import add

class AgentState(TypedDict):
    """
    Represents the state of an agent.
"""
    agent_id: str
    user_query:str
    enhanced_query:str
    conversation_history:Annotated[list[dict], add]

    principal_group_codes: list[str]  # Requesting user's active group codes, for the ACL check in retrieval.
    principal_user_id: str  # Requesting user's id, for the allowed_users ACL check in retrieval.

    intent: str  # Domain_qn/normal_qn/clarification_qn
    resolved_query: str  # Self-contained, history-resolved version of user_query.
    needs_clarification: bool
    ask_user: str  # The question to ask the user for clarification.

    retrieved_chunks: list[dict]        # Raw retrieval results
    reranked_chunks: list[dict]         # After cross-encoder reranking
    image_chunks: list[dict]            # Image-specific results
    merged_context: str

    answer: str                         # Generated answer
    sources: list[dict]                 # Source citations
    model_used: str                     # Which LLM model was used

   

    