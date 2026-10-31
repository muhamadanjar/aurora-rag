# Spesifikasi Background Index Codebase

| | |
|---|---|
| **Status** | In-Review — belum diimplementasikan |
| **Tanggal** | 2026-10-31 |
| **Service** | `aurora-rag` |

## Ringkasan

Indexing codebase diusulkan menjadi job persisten yang berjalan di background. API segera mengembalikan ID job; operator dapat memantau tahap, progres, hasil, dan kegagalannya tanpa mempertahankan request HTTP. Ini tidak mengatasi koneksi Ollama atau menjamin proses embedding lebih cepat.

## Asumsi dan keputusan untuk ditinjau

1. PostgreSQL yang sudah digunakan Aurora tersedia untuk antrean dan status job; jika tidak tersedia, API tidak menerima job baru, bukan menyimpannya hanya di memori.
2. Endpoint index yang ada berubah dari hasil sinkron `200` menjadi `202` dan job ID. Ini perubahan breaking yang membutuhkan migrasi seluruh client; tidak ada endpoint sinkron paralel yang dipertahankan.
3. Worker dijalankan di dalam deployment Aurora yang memiliki akses ke PostgreSQL, filesystem sumber, volume Chroma dan index metadata, serta Ollama. Tidak ada Redis atau broker baru.
4. Setelah worker mati, job yang terputus ditandai gagal secara aman; operator mengirim request baru untuk mencoba lagi. Tidak ada retry otomatis, cancellation, atau resume per batch.
5. Hanya satu job index aktif per proyek; satu worker mengeksekusi satu job pada satu waktu. Ketentuan lintas proses tetap berlaku jika beberapa instance dijalankan.

## Latar belakang dan tujuan

Saat ini `POST /codebase/projects/{project_id}/index` menunggu `asyncio.to_thread` hingga indexing selesai. Thread menghindari pemblokiran event loop, tetapi koneksi HTTP tetap terbuka dan client hanya menerima hasil akhir atau error. Hasil re-index yang gagal tidak menggantikan generation sebelumnya. Pemindaian yang diamati untuk `korlap` menghasilkan 681 chunk, lalu gagal pada koneksi embedding; masalah akses Ollama tetap menjadi prasyarat operasional terpisah.

**Tujuan:** request index tidak menunggu scan/embedding; status dan hasil tetap bisa dibaca setelah restart; kesalahan dapat diidentifikasi tanpa membocorkan isi dokumen atau secret; index lama tetap tersedia sampai publication berhasil.

**Di luar cakupan:** background `/ingest`, perubahan strategi chunking, tuning embedding, automatic retry/cancel, API notifikasi, index incremental, perubahan perilaku pencarian, dan perbaikan koneksi Ollama.

## Kontrak API yang diusulkan

Semua endpoint di bawah memakai `Authorization: Bearer <AURORA_CODEBASE_TOKEN>` seperti endpoint codebase saat ini. Jangan mengembalikan token, isi file, absolute path, traceback, atau pesan exception mentah dalam status/error.

### Membuat job

`POST /codebase/projects/{project_id}/index`

```json
{"path": "/workspace/projects/contoh"}
```

Validasi sinkron mencakup autentikasi, bentuk request, project ID, path absolut dan kebijakan root yang diizinkan; filesystem dan symlink wajib divalidasi ulang dengan mekanisme confined scan saat job benar-benar berjalan. API mengembalikan `202` hanya setelah record `queued` tersimpan secara durable:

```json
{"job_id":"01900000-0000-7000-8000-000000000001","project_id":"contoh","status":"queued","status_url":"/codebase/index-jobs/01900000-0000-7000-8000-000000000001"}
```

`Location` berisi `status_url`. `401`/`503` untuk autentikasi sesuai kontrak sekarang; `422` untuk request atau ID/path yang tidak valid; `409` bila sudah ada job aktif atau delete sedang berlangsung untuk proyek yang sama; `503` bila penyimpanan job tidak tersedia. Error scan atau Ollama yang baru diketahui worker dicatat pada job, bukan dikembalikan pada respons `202`.

### Membaca status

`GET /codebase/index-jobs/{job_id}` mengembalikan `200` untuk job yang diketahui dan `404` jika tidak ada. Status dan hasil tetap tersedia setelah proses API direstart.

```json
{
  "job_id":"01900000-0000-7000-8000-000000000001",
  "project_id":"contoh",
  "status":"running",
  "stage":"embedding",
  "progress":{"processed_chunks":128,"total_chunks":681,"skipped_files":0},
  "result":null,
  "error":null,
  "created_at":"2026-10-31T12:00:00Z",
  "started_at":"2026-10-31T12:00:01Z",
  "finished_at":null
}
```

`status` adalah `queued`, `running`, `succeeded`, atau `failed`. `stage` adalah `null` saat queued, lalu salah satu `validation`, `scan`, `create_collection`, `embedding`, `store_batch`, `publish`; saat terminal menunjukkan tahap terakhir. Jumlah total `null` sampai scan selesai; `processed_chunks` hanya bertambah setelah batch berhasil tersimpan. Hasil `succeeded`: `result={"indexed_chunks":681,"skipped_files":0}`. Saat gagal, `error={"code":"embedding_unavailable","message":"Layanan embedding tidak dapat dihubungi"}`; kategori lain sekurangnya `invalid_source`, `scan_failed`, `storage_failed`, `interrupted`, dan `internal_error`. Error tidak boleh menyertakan pesan mentah upstream. Waktu adalah UTC; field yang belum diketahui bernilai `null`.

### Konflik delete dan keamanan

`DELETE /codebase/projects/{project_id}` mengembalikan `409` bila proyek mempunyai job `queued` atau `running`; claim job, submit, dan delete harus diserialisasi secara atomik lintas proses agar hasilnya tidak bergantung urutan race. Search tetap memakai generation yang terbit. Jika proyek belum pernah berhasil di-index, perilaku search tetap mengikuti kontrak yang ada. Status job tidak memerlukan atau mengembalikan path sumber dan memakai token codebase yang sama, bukan JWT management.

## Penyimpanan dan eksekusi

- Migrasi PostgreSQL menambah record job: UUID, project ID, path internal, status, tahap, hitungan progres/hasil, kode error aman, waktu pembuatan/mulai/selesai, dan identitas eksekusi. Constraint/index parsial memastikan maksimal satu `queued`/`running` per proyek meskipun submit bersamaan dari banyak proses.
- Worker mengklaim job atomik di transaksi database; satu job tidak boleh dijalankan dua worker hidup sekaligus. Jangan memegang transaksi database terbuka selama scan atau embedding.
- Worker mengambil model embedding saat eksekusi di bawah model lock yang sudah ada dan mempertahankan lock sampai publication/gagal. Perubahan model dan job aktif tidak boleh menghasilkan campuran model vector. Path dan root yang berlaku diverifikasi saat eksekusi, bukan hanya saat submit.
- Pertahankan file lock proyek, confined scan, batas scan yang ada, dan mekanisme generation/publikasi Chroma. Worker harus mengakses volume dan path sumber yang sama dengan API; file lock lokal tidak menyediakan koordinasi lintas host tanpa filesystem dan locking bersama yang benar.
- Pengukuran progres dilakukan per batch setelah `collection.add` sukses; jangan menulis konten chunk ke database/log. Log terstruktur menyertakan job ID, tahap, status, jumlah, dan kategori error aman. Tidak log secret, isi file, path, atau exception mentah.
- Saat worker mati, recovery tidak boleh menandai job worker lain yang masih hidup sebagai interrupted. Gunakan mekanisme ownership/liveness yang dapat membedakan proses hidup dan mati serta mencegah pemilik lama melanjutkan publikasi setelah job dinyatakan interrupted. Job queued tetap tersedia. Mekanisme ini wajib diuji pada beberapa proses; timeout saja tanpa fencing tidak memenuhi persyaratan.
- Batas publikasi dan pencatatan sukses berada pada storage berbeda. Simpan identitas job pada metadata generation dan lakukan rekonsiliasi setelah restart: jika generation job sudah terbit, job diselesaikan sebagai `succeeded` dengan hasil yang dapat dipulihkan; jika belum, job menjadi `failed/interrupted` dan generation lama tetap terbit. Tidak boleh melaporkan gagal jika generation baru sebenarnya sudah terbit. Rekonsiliasi dan fencing harus tahan restart berulang.
- Simpan job terminal selama 30 hari, lalu hapus hanya record terminal; job aktif tidak boleh dihapus. Jadwal cleanup tidak boleh memengaruhi Chroma atau generation yang terbit.

## Migrasi dan operasional

Deploy migrasi database sebelum versi API/worker baru; worker wajib bisa mencapai source mounts, Chroma, metadata dan Ollama dengan izin yang sama seperti API saat ini. Perbarui Compose, `.env`/panduan yang relevan, OpenAPI, README, panduan ingestion dan seluruh client/test yang masih mengharapkan `200` serta `indexed_chunks` langsung. `legacy`, `/ingest`, `/query`, dan MCP search tetap tidak berubah. Log job dapat dipantau melalui log service yang menjalankan worker.

Rollback: hentikan penerimaan job dan worker, selesaikan atau tandai job aktif secara aman, lalu deploy versi sebelumnya. Backup database dan volume sebelum rollback/migrasi; data Chroma dan index yang sudah terbit tidak dihapus. Klien harus kembali ke kontrak sinkron sebelum rollback API. Migrasi downgrade/retensi job harus menyatakan hilangnya histori, bukan menghapus index Chroma.

## Kriteria penerimaan

1. Dengan embedding sengaja ditahan, POST mengembalikan `202` dan job `queued`/`running` tanpa menunggu embedding selesai; status dapat dipantau sampai sukses.
2. Progress bertambah hanya setelah batch tersimpan; hasil akhir cocok dengan jumlah chunk yang dipublikasikan. Scan kosong dapat sukses dengan nol chunk.
3. Ollama tidak dapat dihubungi: job `failed` pada tahap embedding dengan kategori aman; generasi lama tetap dapat dicari dan tidak ada secret dalam respons/log.
4. Dua submit serentak untuk satu proyek pada proses berbeda menghasilkan tepat satu job aktif; operasi index/delete bersamaan tidak dapat menghapus atau menerbitkan data secara salah.
5. Restart sebelum publication menghasilkan `failed/interrupted` dan mempertahankan index lama; restart setelah publication tetapi sebelum update status direkonsiliasi menjadi `succeeded`. Restart berulang tidak mengubah hasil yang benar.
6. Worker hidup yang lambat tidak dinyatakan mati dan worker lama tidak dapat publish setelah kehilangan ownership; job queued tidak hilang saat restart.
7. Path di luar root, symlink escape, file biner/secret, batas scan, dan perubahan source antara submit dan eksekusi tetap tunduk pada perlindungan scanner yang ada.
8. Status hanya dapat dibaca dengan token codebase, tidak membocorkan path/konten/token/traceback; ID yang tidak diketahui menghasilkan `404`.
9. Setelah migrasi, seluruh konsumen dan dokumentasi menggunakan `202` serta status job. `/ingest`, `/query`, dan MCP search tidak mengalami perubahan kontrak.
10. `embedding.model` tidak dapat berubah selama indexing yang sedang berjalan, dan generation hasil menggunakan model yang konsisten dengan metadata.

## Strategi pengujian dan perintah

Gunakan pytest dan pola `tests/test_codebase_api.py`; uji API, migrasi, race multi-proses, failure injection sebelum/sesudah publication, restart/recovery, dan isolasi token. Jalankan juga smoke HTTP dengan PostgreSQL, Ollama dan Docker Compose nyata, termasuk penolakan Ollama dan restart worker. Perintah tes terkait yang sudah ada:

```bash
.venv/bin/python -m pytest -q tests/test_codebase_api.py
make config
```

Perintah tambahan untuk migrasi, worker, dan pengujian integrasi ditentukan oleh implementasi; jangan menganggapnya sudah tersedia. Tidak ada tes yang dijalankan untuk draft dokumen ini.

## Alternatif, dampak, dan risiko

| Alternatif | Kelebihan | Kekurangan |
|---|---|---|
| FastAPI `BackgroundTasks` dalam memori | Paling sederhana | Tugas/status hilang saat proses mati; tidak cukup untuk recovery. |
| Job persisten PostgreSQL + worker (usulan) | Memakai database yang tersedia; status dan recovery dapat diaudit | Perlu migrasi, koordinasi antarproses, dan rekonsiliasi publication. |
| Broker/worker eksternal | Fleksibel untuk skala dan integrasi besar | Infrastruktur dan operasional tambahan yang belum terbukti diperlukan. |

**Kompatibilitas:** respons index berubah dan memerlukan cutover client. **Data:** migrasi menambah tabel; Chroma tidak dimigrasi, tetapi history job punya retensi dan harus dicadangkan jika diperlukan. **Kinerja:** request lebih singkat, bukan jaminan throughput meningkat; batas batch dan beban Ollama tetap berlaku. **Keamanan:** token codebase, pembatasan path, sanitasi error, dan isolasi sumber tetap wajib; worker memiliki akses sensitif yang sama seperti API. **Operasi/rollback:** database dan worker menjadi dependensi index; gangguan worker terlihat lewat status dan rekonsiliasi, tidak boleh diabaikan sebagai job yang selamanya running.

## Pertanyaan terbuka untuk persetujuan

- Apakah perubahan breaking `200` → `202` pada endpoint index yang ada dapat diterima untuk seluruh client eksternal?
- Apakah retensi hasil terminal 30 hari dan kebijakan interrupted tanpa retry otomatis sesuai kebutuhan operasional?
- Apakah deployment dibatasi pada satu host/shared filesystem yang mendukung file locking? Jika multi-host tanpa shared locking diperlukan, desain locking dan penyimpanan Chroma harus ditinjau ulang sebelum implementasi.
