from app.infrastructure.codebase.index_store import CodebaseIndexStore


class CodebaseService:
    def __init__(self, store: CodebaseIndexStore | None = None):
        self.store = store or CodebaseIndexStore()

    def index(self, project_id: str, path: str, *, embedding_model: str | None = None):
        return self.store.index(project_id, path, embedding_model=embedding_model)

    def delete(self, project_id: str):
        return self.store.delete(project_id)

    def projects(self):
        return self.store.projects()

    def search(self, project_id: str, query: str, limit: int, *, embedding_model: str | None = None):
        return self.store.search(project_id, query, limit, embedding_model=embedding_model)
