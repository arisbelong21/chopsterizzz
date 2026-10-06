# Instalasi Chopster

Panduan ini menjelaskan cara menjalankan Chopster dari source, memasang extension browser, dan membuat paket Windows. Paket yang diperiksa berisi **source code**, bukan file EXE siap pakai.

## 1. Pilih cara instalasi

- **Pengguna biasa — EXE:** unduh ZIP rilis Windows dari halaman **Releases** GitHub jika tersedia. Ekstrak seluruh ZIP; jangan hanya menyalin `Chopster.exe`.
- **Pengembang — source:** clone repository atau unduh **Code → Download ZIP**, lalu ekstrak ke folder lokal. Lanjutkan ke bagian 2.
- Jika halaman Releases belum menyediakan paket Windows, gunakan instalasi source di bawah atau buat sendiri paket EXE mengikuti bagian 6.

## 2. Persyaratan

Target proyek ini adalah Windows 64-bit. Gunakan Windows x64 dan Python 3.12 64-bit untuk instalasi source maupun build.

| Komponen | Wajib? | Keterangan |
|---|---:|---|
| Python 3.12 64-bit | Ya untuk source/build | Installer mencoba memasangnya otomatis jika Python Launcher atau WinGet tersedia. |
| FFmpeg dan FFprobe | Ya untuk fitur media | Dipakai untuk membaca, memotong, dan merender video/audio. Harus berada di `PATH` atau, untuk paket portable, di samping EXE. |
| Deno 2.3+ **atau** Node.js 22+ | Disarankan untuk unduhan YouTube | Runtime JavaScript yang dapat dibutuhkan `yt-dlp` untuk ekstraksi YouTube modern. |
| Microsoft Visual C++ 2015–2022 Redistributable x64 | Ya pada Windows yang belum memilikinya | Dibutuhkan oleh beberapa pustaka native. Installer mencoba memasangnya lewat WinGet. |
| Koneksi internet | Ya saat setup/penggunaan layanan online | Diperlukan untuk memasang paket, mengambil media, dan menggunakan provider AI eksternal. |
| Git | Opsional | Hanya diperlukan bila memilih `git clone`; pengguna ZIP tidak memerlukannya. |
| Node.js/npm | Opsional untuk pengembangan UI web | Tidak diperlukan untuk menjalankan aplikasi biasa; bundle `web_dist` sudah disertakan. |

> Chopster tidak menyertakan API key. Fitur lokal yang tersedia dapat digunakan tanpa provider AI, tetapi fitur yang memang memerlukan layanan AI eksternal membutuhkan key milik pengguna.

## 3. Instalasi source (cara termudah)

### A. Ambil source

Dengan Git:

```bash
git clone https://github.com/arisbelong21/chopsterizzz.git
cd chopsterizzz
```

Atau unduh ZIP source dari repository GitHub ini, ekstrak, kemudian buka folder yang berisi `install_dependencies.bat`.

### B. Pasang dependensi

Klik dua kali `install_dependencies.bat`, atau jalankan dari Command Prompt/PowerShell yang dibuka pada folder proyek:

```bat
install_dependencies.bat
```

Script tersebut akan:

1. Memeriksa/memasang FFmpeg dan FFprobe, Deno bila Node.js 22+ belum tersedia, serta Visual C++ Runtime melalui WinGet bila diperlukan.
2. Memastikan Python 3.12 tersedia (mencoba Python Launcher/WinGet jika belum terpasang).
3. Membuat virtual environment `.venv-source`.
4. Memasang paket dari `requirements.txt` ke virtual environment tersebut.

Izinkan prompt WinGet/Windows bila muncul. Proses membutuhkan koneksi internet. Jika baru dipasang, tutup dan buka kembali terminal agar perubahan `PATH` terbaca, lalu jalankan kembali script bila diminta.

### C. Verifikasi lalu jalankan

Periksa Python dan tool media:

```bat
py -3.12 --version
ffmpeg -version
ffprobe -version
```

Periksa salah satu runtime JavaScript jika memakai untuk YouTube:

```bat
deno --version
```

atau:

```bat
node --version
```

Kemudian jalankan:

```bat
run_chopster.bat
```

`run_chopster.bat` akan memakai `.venv-source` bila tersedia. Jangan hapus folder itu sebelum aplikasi ditutup.

## 4. Instalasi manual (bila WinGet tidak tersedia)

1. Pasang Python 3.12 64-bit, lalu pastikan `py -3.12 --version` berhasil.
2. Pasang FFmpeg/FFprobe dan masukkan folder `bin`-nya ke `PATH`, atau letakkan `ffmpeg.exe` dan `ffprobe.exe` pada folder proyek. Pastikan kedua perintah `-version` di atas berhasil.
3. Pasang salah satu: Deno 2.3+ atau Node.js 22+. Pastikan perintah `deno --version` atau `node --version` berjalan dari terminal baru.
4. Pasang Microsoft Visual C++ 2015–2022 Redistributable x64 jika belum tersedia.
5. Dari folder proyek, buat virtual environment dan pasang dependensi Python:

```bat
py -3.12 -m venv .venv-source
.venv-source\Scripts\python.exe -m pip install --upgrade pip
.venv-source\Scripts\python.exe -m pip install -r requirements.txt
```

6. Jalankan aplikasi dengan:

```bat
.venv-source\Scripts\python.exe main.py
```

Gunakan Python dari `.venv-source` untuk perintah `pip` berikutnya agar dependensi tidak terpasang ke Python global yang berbeda.

## 5. Fitur opsional

### AI provider

Buka **Settings** di Chopster dan isi konfigurasi provider dengan API key milik Anda sendiri. Provider global untuk Content Clipper dan key khusus Auto Clip Studio merupakan konfigurasi terpisah. Uji koneksi/kemampuan model sebelum memakai fitur vision. Jangan memasukkan API key ke source code, file `.env` yang di-commit, tangkapan layar, atau issue publik GitHub.

### Extension Chrome/Chromium

Extension mengirim URL ke aplikasi lokal; extension **tidak** mengunduh media sendiri. Chopster harus sedang berjalan.

1. Buka `chrome://extensions` (atau `edge://extensions` pada Microsoft Edge).
2. Aktifkan **Developer mode**.
3. Pilih **Load unpacked**.
4. Pilih folder `browser_extension` di source, atau `browser_extension` di dalam folder hasil build `dist\Chopster`.
5. Buka Chopster, lalu gunakan tombol extension atau klik kanan tautan/halaman/video dan pilih **Kirim ke Chopster**.

Extension memerlukan izin untuk berkomunikasi dengan `127.0.0.1`/`localhost`. Jika status menyatakan Chopster belum berjalan, pastikan aplikasi dibuka dan extension berasal dari rilis yang kompatibel.

### Diarization/pemisahan pembicara (opsional)

Fitur ini memerlukan `pyannote.audio`, token Hugging Face, serta akses yang disetujui ke model yang digunakan. Paketnya besar dan tidak diperlukan untuk instalasi dasar. Untuk memasangnya ke virtual environment source:

```bat
.venv-source\Scripts\python.exe -m pip install -r requirements_speaker_diarization.txt
```

Sediakan `HF_TOKEN` atau `HUGGINGFACE_TOKEN` di environment pengguna sebelum menjalankan aplikasi. Jangan menaruh token di repository. `install_speaker_diarization.bat` memakai `python` default sistem; bila memakai `.venv-source`, gunakan perintah di atas agar paket terpasang ke environment yang benar.

### Mengubah frontend Auto Clip Studio (opsional)

Frontend React/TypeScript siap pakai sudah berada di `auto_clip_studio/web_dist`; pengguna aplikasi tidak perlu memasang Node.js/npm untuk membukanya. Jika mengubah source frontend, pasang Node.js/npm, lalu:

```bat
cd auto_clip_studio\web_source
npm ci
npm run lint
npm run build
```

Jalankan ulang Chopster setelah build selesai. Build frontend menulis bundle produksi ke `web_dist`.

## 6. Membuat paket EXE Windows

Build dilakukan di Windows x64 dengan Python 3.12 64-bit dan koneksi internet. Pastikan runtime eksternal pada bagian 2 tersedia, kemudian dari folder source jalankan:

```bat
build_exe.bat
```

Script membuat `.venv-build`, memasang dependensi build, membersihkan folder `build`/`dist` lama, lalu membuat paket PyInstaller **onedir**. Log tersimpan di `build_exe.log`. Hasil utamanya:

```text
dist\Chopster\Chopster.exe
```

Ini bukan EXE mandiri. Untuk membagikan build, kompres dan bagikan **seluruh folder `dist\Chopster`**, termasuk `_internal`, `browser_extension`, dan `SETUP_DEPENDENCIES.bat`. Jangan mengirim atau memindahkan EXE saja.

Di PC Windows penerima:

1. Ekstrak seluruh folder paket.
2. Jalankan `SETUP_DEPENDENCIES.bat` bila FFmpeg/FFprobe atau runtime belum tersedia; WinGet mungkin meminta persetujuan.
3. Pastikan Visual C++ Redistributable x64 tersedia. Jika muncul error DLL seperti `VCRUNTIME`/`MSVCP`, pasang runtime tersebut dan buka ulang aplikasi.
4. Jalankan `Chopster.exe`.

Build hanya menyalin `ffmpeg.exe`, `ffprobe.exe`, atau `deno.exe` yang sudah tersedia di folder source; bila tidak ada, PC penerima harus memiliki runtime itu di `PATH` atau memasangnya melalui setup. YouTube dapat memerlukan cookie yang memang dimiliki pengguna; Chopster tidak mengatasi pembatasan akun/platform.

## 7. Pemecahan masalah

| Gejala | Yang perlu diperiksa |
|---|---|
| `Python 3.12 tidak ditemukan` | Pasang Python 3.12 64-bit, tutup/buka terminal, lalu cek `py -3.12 --version`. Jalankan kembali installer. |
| `WinGet tidak tersedia` | Pasang dependency secara manual seperti pada bagian 4. |
| `ffmpeg` atau `ffprobe` tidak dikenal | Tambahkan folder bin FFmpeg ke `PATH`, buka terminal baru, dan jalankan kedua perintah verifikasi. |
| Unduhan YouTube gagal pada ekstraksi JavaScript | Pastikan Deno 2.3+ atau Node.js 22+ dikenali di `PATH`; perbarui dependensi sesuai kebutuhan. Cookie hanya untuk akun/sumber yang berhak Anda akses. |
| Windows melaporkan DLL native hilang | Pasang Microsoft Visual C++ 2015–2022 Redistributable x64. |
| Extension tidak menemukan Chopster | Buka aplikasi lebih dulu, muat folder extension yang benar, dan pastikan extension serta bridge berasal dari versi yang kompatibel. |
| Analisis AI tidak berjalan | Periksa koneksi/provider, key, kuota, dan kemampuan model. AI eksternal opsional; tidak semua analisis memiliki fallback offline. |
| CUDA/GPU gagal | Pilih CPU/Auto di pengaturan transkripsi/perangkat; CUDA bukan syarat untuk menjalankan aplikasi. |

## 8. Checklist sebelum dipublikasikan di GitHub

- Tambahkan tautan ke panduan ini dari `README.md`.
- **Acuan rilis:** PRD terlampir dibuat sebelum revisi terbaru, jadi perlakukan sebagai catatan historis dan gunakan source serta keputusan rilis terbaru sebagai acuan. Tinjau ulang dokumentasi sebelum menautkannya dari README.
- **Tambahkan file `LICENSE` yang sesuai sebelum mengklaim repository sebagai open source.** Arsip source yang diperiksa tidak menyertakan `LICENSE`; pastikan Anda memiliki hak untuk menerbitkan kode dan seluruh aset.
- Tinjau syarat lisensi/atribusi aset dan dependency pihak ketiga, terutama font, model, dan berkas biner yang ikut terkemas. Sertakan `THIRD_PARTY_NOTICES` bila diperlukan.
- Jangan commit API key, token, cookies, `settings.json`, `secrets.dpapi`, `.env` berisi rahasia, virtual environment, cache, log, atau hasil build lokal. Tambahkan `.gitignore` untuk setidaknya `.venv*/`, `build/`, `dist/`, `*.log`, `__pycache__/`, `settings.json`, `secrets.dpapi`, `.env`, dan file cookies.
- Uji instalasi source dan build EXE pada Windows x64 yang bersih. Paket ZIP yang diperiksa belum berisi EXE hasil build atau hasil uji lintas perangkat.
- Untuk rilis pengguna, unggah ZIP berisi **seluruh** `dist\Chopster` sebagai asset GitHub Release; jelaskan bahwa build memerlukan dependency runtime dan tidak hanya terdiri dari satu EXE.

## 9. Privasi dan penggunaan yang bertanggung jawab

Pengaturan, proyek, riwayat, cache, file sementara, dan ekspor disimpan lokal sesuai fitur yang digunakan. Key AI sebaiknya dimasukkan lewat Settings; pada Windows, implementasi menyimpan key menggunakan DPAPI di profil pengguna. Jika memakai provider AI eksternal, teks transcript atau sampel gambar yang dikirim ke provider mengikuti kebijakan provider tersebut. Patuhi hak cipta, ketentuan layanan platform, dan hak akses media yang diunduh atau diproses.
