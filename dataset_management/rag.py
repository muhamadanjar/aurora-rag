from langchain_chroma import Chroma
from langchain_community.embeddings import OllamaEmbeddings
from langchain_community.llms import Ollama

from .runtime_config import CHROMA_COLLECTION, CHROMA_DIRECTORY


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
        f"File: {document.metadata.get('source', 'Unknown')}\n{document.page_content}"
        for document in documents
    )


def build_rag_prompt(configuration: dict[str, str], context: str, question: str) -> str:
    return configuration["rag.prompt"].format(context=context, question=question)
