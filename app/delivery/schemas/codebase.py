from pydantic import BaseModel, ConfigDict, Field


class CodebaseIndexRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)


class CodebaseSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1, max_length=16_384)
    limit: int = Field(ge=1, le=50)


class CodebaseSearchResult(BaseModel):
    content: str
    path: str
    start_line: int | None
    end_line: int | None
    score: float


class CodebaseSearchResponse(BaseModel):
    results: list[CodebaseSearchResult]


class CodebaseProject(BaseModel):
    project_id: str


class CodebaseProjectListResponse(BaseModel):
    projects: list[CodebaseProject]
