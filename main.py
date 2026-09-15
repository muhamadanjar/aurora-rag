from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional

# Komponen LangChain
from langchain_community.llms import Ollama
from langchain_community.embeddings import OllamaEmbeddings
from langchain_chroma import Chroma
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough

app = FastAPI(
    title="Aurota RAG API", 
    description="Microservice API untuk RAG repositori kode"
)

# ---------------------------------------------------------
# Konfigurasi AI & Vector DB
# ---------------------------------------------------------
# Gunakan model embedding khusus kode/teks dari Ollama (misal: nomic-embed-text)
embeddings = OllamaEmbeddings(model="nomic-embed-text")

# Inisialisasi LLM lokal (misal: llama3)
llm = Ollama(model="llama3")

# Inisialisasi ChromaDB (akan tersimpan secara lokal di folder ./chroma_data)
vector_store = Chroma(
    collection_name="source_code",
    embedding_function=embeddings,
    persist_directory="./data/chroma_data"
)

# Buat retriever untuk mengambil 4 potongan kode paling relevan
retriever = vector_store.as_retriever(search_kwargs={"k": 4})

# Template prompt agar LLM fokus menjawab berdasarkan konteks kode
template = """Anda adalah asisten AI khusus untuk developer.
Gunakan potongan kode berikut untuk menjawab pertanyaan. Jika jawabannya tidak ada di kode, katakan tidak tahu.

Konteks Kode (dan Path File):
{context}

Pertanyaan: {question}

Jawaban (Gunakan Markdown untuk format kode jika diperlukan):"""
prompt = PromptTemplate.from_template(template)

# RAG Chain menggunakan LCEL (LangChain Expression Language)
def format_docs(docs):
    # Menggabungkan konten kode beserta metadata path file
    return "\n\n".join(f"File: {doc.metadata.get('source', 'Unknown')}\n{doc.page_content}" for doc in docs)

rag_chain = (
    {"context": retriever | format_docs, "question": RunnablePassthrough()}
    | prompt
    | llm
    | StrOutputParser()
)

# ---------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------
class IngestRequest(BaseModel):
    code_content: str
    file_path: str
    language: str

class QueryRequest(BaseModel):
    question: str

class QueryResponse(BaseModel):
    answer: str

# ---------------------------------------------------------
# Endpoints
# ---------------------------------------------------------
@app.post("/ingest", summary="Simpan potongan kode ke Vector DB")
async def ingest_code(req: IngestRequest):
    try:
        # Menyimpan kode ke Chroma beserta metadata-nya
        vector_store.add_texts(
            texts=[req.code_content],
            metadatas=[{"source": req.file_path, "language": req.language}]
        )
        return {"status": "success", "message": f"Berhasil menyimpan {req.file_path}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/query", response_model=QueryResponse, summary="Tanya kode menggunakan RAG")
async def query_code(req: QueryRequest):
    try:
        # Menjalankan chain: Embed pertanyaan -> Cari di Chroma -> Kirim ke LLM -> Kembalikan teks
        response_text = rag_chain.invoke(req.question)
        return QueryResponse(answer=response_text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
