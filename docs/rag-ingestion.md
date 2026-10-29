# Panduan memasukkan dokumen dan proyek ke Aurora RAG

Panduan ini menjelaskan dua jalur ingestion Aurora. Pilih jalur berdasarkan cara Anda ingin mencari konten:

| Kebutuhan | Endpoint | Chroma collection | Cara mengambil hasil |
|---|---|---|---|
| Masukkan teks atau isi satu dokumen ke RAG chat | `POST /ingest` | `source_code` | `POST /query` atau `POST /api/v1/chat/completions` |
| Indeks satu direktori proyek secara otomatis | `POST /codebase/projects/{project_id}/index` | Collection terpisah per proyek | `POST /codebase/search` atau MCP `search_codebase` |

Kedua jalur memakai Chroma di `./data/chroma_data`, tetapi collection-nya berbeda. Mengindeks proyek lewat `/codebase` **tidak** menambahkan isinya ke konteks `/query` atau `/api/v1/chat/completions`. Sebaliknya, dokumen yang dikirim ke `/ingest` tersedia di collection `source_code`, bukan di collection proyek. Tidak ada endpoint upload PDF atau parser dokumen otomatis.

## Penyimpanan lokal

Chroma menggunakan direktori persisten `./data/chroma_data`, relatif terhadap working directory proses API. Dokumen `/ingest` disimpan di collection `source_code`; index proyek disimpan dalam collection terpisah di database Chroma yang sama. Metadata generation proyek berada di `./data/codebase_index` secara default; lokasi itu dapat diganti dengan `AURORA_CODEBASE_INDEX_ROOT`. Jalankan API dengan working directory yang konsisten dan pertahankan direktori data agar index tetap tersedia setelah restart.

## 1. Siapkan aplikasi dan model

Gunakan Python versi yang dipersyaratkan project (`>=3.12.13`), `uv`, `curl`, `jq`, OpenSSL CLI, dan Ollama. Jalankan perintah dari direktori repository:

```bash
uv sync --frozen
cp .env.example .env
```

Untuk konfigurasi penuh, isi nilai wajib di `.env` (`DATABASE_URL`, `AUTH_USERNAME`, `AUTH_PASSWORD_HASH`, `JWT_SECRET`, dan `OPERATOR_UUID`) seperti dijelaskan pada bagian [Configuration di README](../README.md#configuration). Jalankan migrasi database:

```bash
uv run alembic upgrade head
```

Pastikan Ollama berjalan dan model yang digunakan tersedia. Nilai default Aurora adalah `nomic-embed-text` untuk embedding dan `llama3` untuk chat:

```bash
ollama pull nomic-embed-text
ollama pull llama3
```

Mulai Ollama terlebih dahulu jika belum berjalan, misalnya dengan `ollama serve` pada terminal tersendiri. Jika konfigurasi runtime mengganti `embedding.model` atau `chat.model`, unduh model yang dipilih, bukan hanya model default.

## 2. Konfigurasikan token dan jalankan API

`/ingest` dan `/query` memakai bearer token statis `AURORA_RAG_TOKEN`. `/codebase/*` memakai token terpisah, `AURORA_CODEBASE_TOKEN`. Atur ketiga secret dengan nilai berbeda: token RAG, token codebase, dan `JWT_SECRET` untuk menandatangani JWT management. Aplikasi tidak menukar token-token tersebut.

Buat token acak dan izinkan pemindaian direktori proyek yang memang akan diindeks:

```bash
export AURORA_RAG_TOKEN="$(openssl rand -hex 32)"
export AURORA_CODEBASE_TOKEN="$(openssl rand -hex 32)"
export AURORA_CODEBASE_ROOTS="$HOME/projects"
export API_URL="http://127.0.0.1:8000"
```

Simpan token di secret manager atau tempat aman agar bisa digunakan lagi oleh client. Codebase settings (`AURORA_CODEBASE_TOKEN`, `AURORA_CODEBASE_ROOTS`, dan `AURORA_CODEBASE_INDEX_ROOT`) dimuat dari `.env` pada working directory API atau environment proses; nilai environment mengalahkan `.env`. Cara export di atas adalah alternatif. Pastikan API dimulai dari direktori yang memiliki `.env` jika memakai file tersebut.

Jalankan API:

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Biarkan proses ini berjalan. Perintah `curl` di bawah harus dijalankan di shell yang memiliki nilai token yang sama. Untuk mengakses API dari jaringan lain, gunakan TLS dan konfigurasi jaringan yang sesuai; jangan mengekspos server development begitu saja.

## 3. Jalur A — masukkan dokumen atau teks ke RAG chat

`POST /ingest` menerima satu string isi konten dan metadata `file_path` serta `language`. Setiap request menyimpan satu teks sebagai satu dokumen di collection `source_code`; endpoint ini tidak membaca file dari disk, mengekstrak PDF, atau memecah teks panjang menjadi chunk. Jika Anda memiliki banyak file, kirim isi masing-masing file sebagai request tersendiri atau gunakan jalur indeks proyek pada bagian berikutnya.

### Kirim isi dokumen

Contoh berikut mengirim file Markdown secara utuh. `jq` menjaga newline dan karakter JSON tetap valid:

```bash
DOC="./README.md"
jq -n \
  --rawfile content "$DOC" \
  --arg file_path "README.md" \
  --arg language "markdown" \
  '{code_content: $content, file_path: $file_path, language: $language}' \
| curl --fail-with-body --silent --show-error \
  -X POST "$API_URL/ingest" \
  -H "Authorization: Bearer $AURORA_RAG_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @-
```

Bentuk request JSON-nya:

```json
{
  "code_content": "Isi dokumen yang akan diindeks...",
  "file_path": "README.md",
  "language": "markdown"
}
```

Respons sukses berupa status dan path sumber, misalnya:

```json
{
  "status": "success",
  "message": "Berhasil menyimpan README.md"
}
```

`file_path` disimpan sebagai metadata sumber untuk konteks RAG. Nilainya tidak menunjuk file yang dibaca server; 
isi yang benar-benar disimpan berasal dari `code_content`.

### Tanyakan isi yang sudah dimasukkan

Gunakan endpoint RAG lama `/query` dengan token RAG yang sama:

```bash
curl --fail-with-body --silent --show-error \
  -X POST "$API_URL/query" \
  -H "Authorization: Bearer $AURORA_RAG_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"question":"Apa tanggung jawab komponen RAG?"}'
```

Respons berbentuk `{"answer":"..."}`. Endpoint mencari konteks di `source_code` menggunakan `embedding.model` dan `retrieval.k` runtime (default retrieval: empat hasil), lalu meminta model chat menjawab. Response `/query` tidak mengembalikan daftar potongan sumber secara terpisah.

Aplikasi chat bergaya OpenAI `POST /api/v1/chat/completions` juga memakai collection `source_code`, tetapi endpoint tersebut memakai JWT management dari `POST /auth/token`, bukan `AURORA_RAG_TOKEN`. Format request chat dan autentikasinya dijelaskan di [README](../README.md#runtime-chat-and-configuration).

## 4. Jalur B — indeks seluruh direktori proyek

Gunakan jalur ini untuk mengindeks repository kode dari path lokal yang dapat dibaca proses API. 
API memindai direktori dan membuat collection proyek tersendiri. Client mengirim **path**, bukan isi file.

### 4.1 Siapkan root yang diizinkan

`AURORA_CODEBASE_ROOTS` berisi satu atau beberapa direktori root absolut yang diizinkan; pisahkan menggunakan path separator OS (`:` di Linux/macOS). Root harus sudah ada dan dapat dibaca proses API. Path request adalah path pada mesin server—bukan path pada komputer client—dan harus berada di bawah salah satu root tersebut. Ganti path contoh di bawah dengan direktori proyek yang sebenarnya.

```bash
export AURORA_CODEBASE_ROOTS="$HOME/projects"
export PROJECT_PATH="$HOME/projects/sample-service"
export PROJECT_ID="sample-service"
```

`PROJECT_ID` harus 1–64 karakter: huruf atau angka di awal, lalu huruf, angka, titik, `_`, atau `-`. Nilai `legacy` dicadangkan untuk collection `source_code` dan tidak bisa diindeks atau dihapus sebagai proyek.

### 4.2 Indeks atau sinkronkan proyek

```bash
jq -n --arg path "$PROJECT_PATH" '{path: $path}' \
| curl --fail-with-body --silent --show-error \
  -X POST "$API_URL/codebase/projects/$PROJECT_ID/index" \
  -H "Authorization: Bearer $AURORA_CODEBASE_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @-
```

Path request harus absolut dan berada di root yang diizinkan:

```json
{"path":"/home/user/projects/sample-service"}
```

Respons merangkum jumlah chunk dan file yang dilewati, misalnya:

```json
{
  "project_id": "sample-service",
  "indexed_chunks": 42,
  "skipped_files": 1,
  "skipped_paths": ["large-data.txt"]
}
```

Pemindaian mengikuti `.gitignore`, tidak mengikuti symbolic link, dan melewati file biner serta nama file yang diketahui mengandung secret. File teks dipecah menjadi chunk 80 baris dengan overlap 10 baris. File di atas 1 MiB dilewati. Batas total satu scan: 128 MiB, 10.000 file, 50.000 chunk, 100.000 entry direktori, kedalaman 64 direktori, dan 60 detik. Pemfilteran nama file tidak dapat menemukan secret yang tertanam di file sumber biasa; tinjau proyek sebelum mengindeksnya.
Hanya ekstensi teks yang didukung scanner (lihat `TEXT_SUFFIXES` di `app/infrastructure/codebase/index_store.py`) dan file tanpa ekstensi yang diindeks; ekstensi lain tidak otomatis dikonversi. Format seperti PDF tidak di-parse. Pastikan konten penting memakai format teks yang didukung atau kirim teksnya melalui `/ingest`.

Re-index menggunakan `project_id` yang sama untuk menyinkronkan isi terbaru dan menghapus hasil lama yang sudah tidak ada. Jika re-index gagal, generation yang sebelumnya terbit tetap dapat dicari. Index atau delete bersamaan untuk proyek yang sama dapat menghasilkan `409`.

### 4.3 Cari hasil proyek

```bash
curl --fail-with-body --silent --show-error \
  -X POST "$API_URL/codebase/search" \
  -H "Authorization: Bearer $AURORA_CODEBASE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"project_id":"sample-service","query":"di mana konfigurasi autentikasi?","limit":5}'
```

`limit` harus 1–50. Respons berisi snippet, path relatif, rentang baris, dan score:

```json
{
  "results": [
    {
      "content": "...",
      "path": "app/core/config.py",
      "start_line": 10,
      "end_line": 29,
      "score": 0.82
    }
  ]
}
```

Untuk melihat proyek yang tersedia:

```bash
curl --fail-with-body "$API_URL/codebase/projects" \
  -H "Authorization: Bearer $AURORA_CODEBASE_TOKEN"
```

Daftar selalu menyertakan `legacy` untuk collection `source_code`; proyek yang diindeks muncul dengan `project_id` masing-masing. Untuk membuang index proyek, gunakan `DELETE` dengan token yang sama:

```bash
curl --fail-with-body -X DELETE \
  "$API_URL/codebase/projects/$PROJECT_ID" \
  -H "Authorization: Bearer $AURORA_CODEBASE_TOKEN"
```

Penghapusan ini tidak menghapus isi collection legacy. Tidak ada endpoint untuk menghapus satu dokumen individual dari `source_code`.

### 4.4 Gunakan melalui MCP

MCP menyediakan `list_projects` dan `search_codebase`; ia mengirim request ke endpoint codebase di atas dan tidak mengindeks atau menghapus proyek sendiri. Konfigurasikan MCP client dengan URL API dan token codebase yang sama seperti server:

```json
{
  "mcpServers": {
    "aurora-rag": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/aurora-rag", "run", "python", "-m", "codebase_mcp.server"],
      "env": {
        "AURORA_RAG_URL": "http://127.0.0.1:8000",
        "AURORA_CODEBASE_TOKEN": "<token-yang-sama-dengan-server>"
      }
    }
  }
}
```

Jangan commit token asli ke file konfigurasi MCP. Ikuti panduan konfigurasi coding agent yang dipakai untuk menentukan cara menyediakan secret dengan aman.

## 5. Error umum

| Status | Penyebab umum | Tindakan |
|---|---|---|
| `401` | Bearer token hilang atau tidak cocok | Pastikan memakai token yang benar untuk endpoint: `AURORA_RAG_TOKEN` untuk `/ingest` dan `/query`, `AURORA_CODEBASE_TOKEN` untuk `/codebase/*`. |
| `503` | Token endpoint tersebut belum tersedia di environment proses API | Export token sebelum memulai ulang API. |
| `413` pada index/search proyek | Body request codebase melebihi 1 MiB | Kirim path dan parameter ringkas; jangan mengirim isi seluruh proyek di body. |
| `422` saat mengindeks | ID/path tidak valid, path di luar root, scan melampaui batas, atau file/aturan ignore bermasalah | Periksa `AURORA_CODEBASE_ROOTS`, path absolut, `.gitignore`, ukuran, dan daftar `skipped_paths`. |
| `409` saat index/delete | Operasi proyek yang sama masih berjalan | Tunggu operasi saat ini selesai, lalu coba lagi. |
| `502` pada operasi codebase | Embedding/model atau operasi index/search gagal | Pastikan Ollama berjalan dan `embedding.model` tersedia; periksa log API. |
| Jawaban RAG tidak menggunakan konten baru | Konten masuk ke jalur/collection yang berbeda, token/model salah, atau retrieval tidak mengambil dokumen tersebut | Pastikan `/ingest` dipakai untuk `source_code`; gunakan `/codebase/search` untuk proyek yang diindeks dengan `project_id` biasa. Periksa nilai `embedding.model` serta Ollama. |

`/ingest` dan `/query` menggunakan collection `source_code` dan token RAG. Hanya API codebase yang memiliki operasi delete proyek; `/ingest` tidak menyediakan daftar maupun penghapusan dokumen individual. Jangan mengedit direktori Chroma secara manual.
