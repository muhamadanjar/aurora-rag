from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings, OllamaLLM

CHROMA_DIRECTORY = "./data/chroma_data"
CHROMA_COLLECTION = "source_code"


def create_vector_store(embedding_model: str) -> Chroma:
    return Chroma(
        collection_name=CHROMA_COLLECTION,
        embedding_function=OllamaEmbeddings(model=embedding_model),
        persist_directory=CHROMA_DIRECTORY,
    )


def create_chat_model(
    model: str,
    *,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
) -> OllamaLLM:
    options = {
        key: value
        for key, value in {
            "temperature": temperature,
            "top_p": top_p,
            "num_predict": max_tokens,
        }.items()
        if value is not None
    }
    return OllamaLLM(model=model, **options)


def format_docs(documents) -> str:
    return "\n\n".join(
        f"File: {doc.metadata.get('source', 'Unknown')}\n{doc.page_content}"
        for doc in documents
    )


def indexed_vector_count() -> int:
    from chromadb import PersistentClient
    client = PersistentClient(path=CHROMA_DIRECTORY)
    collection = client.get_or_create_collection(name=CHROMA_COLLECTION)
    return collection.count()


def indexed_codebase_count() -> int:
    """Return the number of published codebase chunks without opening Chroma."""
    from app.infrastructure.codebase.index_store import published_chunk_count

    return published_chunk_count()
