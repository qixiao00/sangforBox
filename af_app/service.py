from datetime import datetime, timezone
from pydantic import BaseModel, Field
from af_app.knowledge import KnowledgeRepository
from af_app.devices import DeviceService
from af_app.policy_optimizer import PolicyOptimizer


class ProbeRequest(BaseModel):
    message: str = Field(default="hello AF", min_length=1, max_length=200)


class ProbeResult(BaseModel):
    ok: bool
    message: str
    database: str
    schema_version: int
    timestamp: str


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=20)
    doc_type: str | None = None
    doc_id: str | None = None
    version: str | None = None


class KnowledgeSearchHit(BaseModel):
    chunk_id: str
    doc_id: str
    doc_type: str
    version: str | None
    title: str | None
    title_path: list[str]
    page_start: int
    page_end: int
    score: float
    snippet: str
    keywords: list[str]
    prev_chunk_id: str | None
    next_chunk_id: str | None
    match_type: str


class KnowledgeSearchResponse(BaseModel):
    query: str
    retrieval: str = "SQLite FTS5 + jieba BM25"
    hits: list[KnowledgeSearchHit]


class ProbeService:
    def __init__(self, database, output_directory=None):
        self.database = database
        self.knowledge = KnowledgeRepository(database)
        self.devices = DeviceService(database, output_directory=output_directory)
        self.policy_optimizer = PolicyOptimizer(self.devices, database.path)

    def run(self, message: str) -> ProbeResult:
        request = ProbeRequest(message=message)
        return ProbeResult(ok=True, message=request.message, database="connected",
                           schema_version=self.database.probe(),
                           timestamp=datetime.now(timezone.utc).isoformat())

    def search_knowledge(self, request: KnowledgeSearchRequest) -> KnowledgeSearchResponse:
        hits = self.knowledge.search(request.query, request.top_k, request.doc_type,
                                     request.doc_id, request.version)
        return KnowledgeSearchResponse(query=request.query, hits=hits)
