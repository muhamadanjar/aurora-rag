from langchain_chroma import Chroma
from langchain_community.embeddings import OllamaEmbeddings
from langchain_community.llms import Ollama

CHROMA_DIRECTORY = "./data/chroma_data"
CHROMA_COLLECTION = "source_code"


def create_vector_store(embedding_model: str) -> Chroma:
    return Chroma(
        collection_name=CHROMA_COLLECTION,
        embedding_function=OllamaEmbeddings(model=embedding_model),
        persist_directory=CHROMA_DIRECTORY,
    )


def create_chat_model(model: str) -> Ollama:
    return Ollama(model=model)


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
