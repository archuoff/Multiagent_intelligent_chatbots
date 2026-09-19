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

    intent: str  # Domain_qn/normal_qn/clarification_qn
    needs_clarification: bool
    ask_user: str  # The question to ask the user for clarification.

    retrieved_chunks: list[dict]        # Raw retrieval results
    reranked_chunks: list[dict]         # After cross-encoder reranking
    image_chunks: list[dict]            # Image-specific results
    merged_context: str

    answer: str                         # Generated answer
    sources: list[dict]                 # Source citations
    model_used: str                     # Which LLM model was used

   

    