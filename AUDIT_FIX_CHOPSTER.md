# Audit dan perbaikan Chopster

Tanggal: 7 Oktober 2026 (WIB)

## Dasar dan batas perubahan

- Target: `Chopster READY.zip` (177 berkas).
- Referensi: `Chopster_8.5.4_mix_ai_update_fixed.zip` (193 berkas).
- Paket hasil tetap menggunakan kode READY, bukan menggantinya dengan versi referensi. Dari 176 path yang sama pada kedua sumber, 147 identik dan 29 berbeda; READY memiliki satu berkas tambahan, sedangkan referensi memiliki 17 berkas tambahan.
- Semua 177 berkas READY dipertahankan. Isi 15 berkas diubah secara terarah; 162 berkas lainnya dipertahankan byte-for-byte. Laporan ini adalah satu-satunya berkas tambahan dalam ZIP.
- Folder teratas dinormalkan dari `Chopster/` menjadi `chopster/`, sesuai nama paket Python dan struktur referensi. Peluncur sumber juga dibuat tahan terhadap perubahan nama folder oleh pengguna.
- Tidak mengubah desain antarmuka, branding, nomor versi, pilihan model/routing AI, konfigurasi pengguna, atau menambahkan fitur. Skrip pengujian dan laporan lama yang sudah dibuang dari READY tidak dikembalikan.

## Temuan yang diperbaiki

Prioritas berikut adalah penilaian dampak pada aplikasi, bukan skor CVSS.

| Prioritas | Berkas | Temuan dan perbaikan |
|---|---|---|
| Kritis untuk startup | `main.py` | Paket berada di folder `Chopster`, tetapi kode mengimpor `chopster`. Error `ModuleNotFoundError` direproduksi pada READY; referensi dengan folder huruf kecil lolos. Bootstrap sumber kini memuat paket lokal secara eksplisit sebelum impor internal. Loader build frozen tetap dipertahankan. |
| Sedang | `Chopster.spec` | Helper pengumpulan submodul dijalankan sebelum path untuk Analysis berlaku. Parent paket dimasukkan ke `sys.path` sebelum helper. Ini pemeriksaan konfigurasi build, bukan klaim build EXE berhasil. |
| Sedang | `install_speaker_diarization.bat` | Installer opsional menggunakan `python` global meskipun peluncur memakai `.venv-source`. Kini memilih interpreter lingkungan aplikasi yang sama jika tersedia. |
| Sedang | `install_runtime_dependencies.bat` | Keberhasilan setup dapat dilaporkan tanpa verifikasi akhir runtime JavaScript/VC++. Kini memeriksa Deno 2.3+ atau Node 22+, serta status pemasangan VC++ sebelum pesan sukses. |
| Tinggi | `app/database.py` | Koneksi SQLite dibagi oleh worker tanpa serialisasi transaksi. Tambahan `RLock` melindungi operasi database, termasuk baca/tulis dan penutupan. Tidak mengubah skema database. |
| Sedang | `ui/pages/downloader_page.py` | Tabel dikosongkan, tetapi baris hanya dibuat jika engine mengirim status `Menunggu`; engine mengirim status lain. Baris kini dibuat di GUI sebelum worker dimulai, lalu sinyal memperbarui baris tersebut. |
| Sedang | `app/bridge.py` | URL dari query/form/JSON di-decode lagi dengan `unquote`, sehingga karakter encoded atau token URL dapat berubah. Decoding tambahan dihapus. |
| Sedang | `app/crash_guard.py` | Exception worker dapat membuat dialog Qt di luar thread GUI. Semua error tetap dicatat; dialog hanya dibuat jika reporter berjalan pada thread GUI. |
| Tinggi | `clipper/highlight_analyzer.py` | `validate_highlight` dipanggil tanpa diimpor, menyebabkan hasil AI valid terbuang ke fallback. Impor dipulihkan. Bentuk `candidates`, list, dan `highlights` dinormalisasi sebelum validator yang sama; validasi rentang/durasi tidak dilewati. |
| Sedang | `clipper/master_analysis.py` | Jalur second-pass kamera memanggil `stabilize_camera_path` tanpa impor, sehingga stabilisasi/penyimpanan cache tidak selesai. Impor ditambahkan pada jalur tersebut. |
| Tinggi | `auto_clip_studio/engine/routers/media.py` | Validasi sumber frame dan timestamp berada di dalam docstring sehingga tidak dieksekusi. Dipindahkan menjadi kode aktif sebelum pemrosesan frame. |
| Sedang | `auto_clip_studio/engine/routers/media.py` | Upload setelah Clear Temp gagal karena direktori uploads hilang. Direktori tujuan kini dibuat kembali sebelum penulisan. |
| Sedang | `auto_clip_studio/engine/routers/media.py` | ID upload dari nama bertitik/panjang ditolak oleh preview, padahal uploader menghasilkan nama tersebut. Aturan ID diselaraskan tanpa mengizinkan separator path; pemeriksaan keamanan sumber tetap dijalankan. |
| Sedang | `auto_clip_studio/engine/routers/render.py` | Retry kedua dapat menjadwalkan render ganda ketika batch sudah berjalan tetapi klip masih `pending`. Batch dengan status `running` kini langsung menolak retry. |
| Sedang | `auto_clip_studio/web_source/src/App.tsx`; `auto_clip_studio/web_dist/assets/app.js` | Isi subtitle manual tidak memengaruhi kunci cache; sanitasi prompt juga menyebabkan benturan. Kunci kini menyertakan SHA-256 subtitle dan encoding prompt yang mempertahankan perbedaan teks. Source dan bundle yang dipakai aplikasi diperbaiki bersama. |
| Sedang | `auto_clip_studio/engine/video_engine.py` | SFX pendek pada video tanpa audio dapat memotong durasi output. Filter audio SFX kini diberi padding sampai durasi klip. Uji FFmpeg nyata: video 6 detik + SFX 1 detik sebelumnya menghasilkan 1 detik, sesudah perbaikan menghasilkan 6 detik. |

Sebagian masalah ditemukan juga pada referensi yang tampak berjalan; karena itu menyalin seluruh referensi saja tidak cukup. Perubahan dibatasi pada masalah yang dapat memengaruhi mekanisme, keamanan pemrosesan, atau hasil/performa alat.

## Cakupan audit

Seluruh 177 berkas diinventarisasi, dibandingkan, dan diperiksa integritasnya. Validasi otomatis dijalankan sesuai jenis berkas: kompilasi sintaks Python, sintaks JavaScript, parsing JSON/JSONC dan XML/SVG, verifikasi gambar/font, serta pemeriksaan target impor lokal. Audit semantik difokuskan pada alur startup, installer/build, database/antrean, bridge, AI/kamera, backend/frontend ACS, upload, cache, retry, dan render.

Ini bukan klaim penelaahan formal setiap baris atau bukti bahwa setiap kombinasi fitur bebas bug. Model ONNX diinventarisasi tetapi tidak diuji inferensinya. Audit tidak menghapus sampah tambahan atau melakukan refactor kosmetik.

## Hasil validasi

### Pengujian nyata di sandbox

- Bootstrap impor lokal: 6 skenario lulus (nama folder `chopster`, `Chopster`, dan nama dengan spasi; masing-masing dari direktori paket dan parent).
- SQLite: 3 thread × 500 insert menghasilkan 1.500 entri dan 1.500 ID unik; CRUD history/proyek juga lulus.
- Parsing bridge: GET, POST JSON, POST form, dan POST query mempertahankan URL encoded.
- FFmpeg/ffprobe: video tanpa audio 6 detik dengan SFX 1 detik tetap berdurasi 6 detik setelah perbaikan.
- Node: sintaks bundle dan perbedaan kunci cache diperiksa; 6 kasus berbeda tetap berbeda dan source/bundle konsisten.

### Pengujian terisolasi dengan mock/double

- Kode antrean dan callback engine diuji tanpa menjalankan GUI Qt atau mengunduh video.
- Crash reporter diuji dengan Qt doubles: worker tetap dicatat tanpa membuat dialog; jalur main-thread tetap membuat dialog.
- Parser AI menerima tiga bentuk envelope dan fenced JSON, serta tetap menolak data/rentang tidak valid.
- Cabang second-pass kamera menjalankan stabilisasi asli; AI eksternal, input frame, dan penulis cache diganti mock.
- Handler ACS dieksekusi dari kode sumber dengan dependency doubles: penolakan sumber/timestamp tidak valid, nama upload, upload setelah Clear Temp, batas ukuran upload, dan penolakan retry ganda.

### Regresi referensi dan pemeriksaan statis

- Dari 76 pengujian referensi, 74 dapat digunakan dan lulus dengan import doubles untuk dependensi opsional yang tidak tersedia. Ini bukan pengujian integrasi dependensi tersebut.
- Satu pengujian template subtitle memerlukan implementasi `yt_dlp` nyata dan tidak dijalankan.
- Satu pengujian packaging lama tidak digunakan karena mensyaratkan nama distribusi `Chopster by Aris` dan skrip audit lama yang memang tidak ada pada READY. Kegagalan awalnya bukan alasan untuk mengubah branding atau mengembalikan berkas yang telah dibersihkan. Aset/path build dan sintaks spec diperiksa secara terpisah.
- 104 berkas Python lolos kompilasi sintaks; 5 JavaScript lolos pemeriksaan sintaks Node.
- 4 JSON, 2 JSONC, 11 XML/SVG, 7 gambar, dan 7 header font valid pada pemeriksaan yang relevan.
- Seluruh 288 rujukan impor absolut internal, termasuk impor paket utama, mengarah ke modul/paket yang tersedia.
- Pemeriksaan installer BAT dan konfigurasi PyInstaller bersifat statis, bukan eksekusi Windows.
- ZIP diperiksa ulang untuk CRC, jumlah berkas, isi hasil patch, dan kesamaan byte berkas yang tidak ditargetkan.

## Batas kepastian

Pengujian dilakukan pada Linux, bukan Windows pengguna. PySide6/Qt WebEngine, FastAPI lengkap, `yt_dlp` nyata, PyInstaller, GPU/CUDA, Whisper/diarization, login/cookie platform, dan API AI pengguna tidak tersedia untuk pengujian end-to-end. Karena itu laporan ini **tidak menjamin nol error pada seluruh PC Windows** dan tidak menyatakan EXE atau seluruh fitur sudah diuji secara penuh.

Error impor yang dilaporkan dan regresi terisolasi di atas sudah diperbaiki serta diuji sesuai cakupan yang disebutkan. Paket ini tetap distribusi sumber, bukan installer mandiri yang membundel Python, semua dependensi, model, dan FFmpeg.

## Menjalankan hasil

1. Ekstrak ZIP ke folder baru; jangan menjalankan file BAT dari dalam arsip dan jangan menumpuknya di atas folder lama.
2. Buka folder `chopster` yang berisi `main.py` dan `run_chopster.bat`.
3. Jika dependensi di folder baru belum terpasang, jalankan `install_dependencies.bat` dan tunggu sampai selesai. Tetap diperlukan interpreter Python dan prasyarat sistem yang didukung aplikasi.
4. Jalankan `run_chopster.bat`.
5. `install_speaker_diarization.bat` tetap opsional, hanya untuk fitur diarization.

Tidak ada konfigurasi atau database pengguna di luar ZIP yang diubah selama pengerjaan.
