from pydantic import BaseModel, Field


class AgentSummary(BaseModel):
    agent_id: str
    display_name: str
    route: str
    domain_description: str = ""


class ChatRequest(BaseModel):
    agent_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    session_id: str | None = None
    conversation_history: list[dict[str, str]] = Field(default_factory=list)


class Citation(BaseModel):
    document_id: str | None = None
    source_type: str | None = None
    page_number: int | None = None
    slide_number: int | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    source_version: str | None = None


class RetrievedContext(BaseModel):
    chunk_id: str
    score: float | None = None
    content_text: str
    citation: Citation
    document_title: str | None = None
    chunk_type: str | None = None
    breadcrumbs: list[str] = Field(default_factory=list)
    image_path: str | None = None


class ChatResponse(BaseModel):
    agent_id: str
    session_id: str | None = None
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    retrieved_context: list[RetrievedContext] = Field(default_factory=list)
    status: str = "success"
