# Chopster

Chopster adalah aplikasi desktop Windows untuk mengunduh media, menganalisis video, membuat klip pendek, menata subtitle, dan mengekspor hasil. Aplikasi menggabungkan Content Clipper AI dengan Auto Clip Studio yang berjalan lokal di dalam aplikasi desktop.

> Repository ini berisi source code. Untuk menjalankan dari source, ikuti langkah cepat di bawah. Untuk petunjuk lengkap, lihat [INSTALL.md](INSTALL.md).

## Fitur

- **Download:** antrean media berbasis `yt-dlp`, pratinjau metadata, pilihan format/kualitas, progres, retry, dan riwayat lokal.
- **Content Clipper AI:** transkripsi lokal dengan Faster-Whisper, perencanaan klip, analisis scene, subtitle, framing, branding, pengolahan audio, dan ekspor.
- **Auto Clip Studio:** workspace terintegrasi untuk menganalisis sumber video, memilih momen, mengedit subtitle/framing, dan merender beberapa klip.
- **Project dan History:** menyimpan proyek, preferensi, hasil, dan riwayat secara lokal.
- **Browser extension:** mengirim URL dari browser Chromium ke antrean Chopster melalui koneksi lokal; unduhan tetap dilakukan oleh aplikasi.
- **AI opsional:** dapat dikonfigurasi menggunakan provider dan API key milik pengguna. Whisper tetap menangani transkripsi.

## Persyaratan

Target instalasi dan build yang disediakan adalah Windows 64-bit.

- Python 3.12 64-bit untuk menjalankan source atau membuat build.
- FFmpeg dan FFprobe untuk fitur media.
- Deno 2.3+ atau Node.js 22+ dapat diperlukan `yt-dlp` untuk ekstraksi YouTube modern.
- Microsoft Visual C++ 2015–2022 Redistributable x64 pada komputer yang belum memilikinya.
- Koneksi internet untuk instalasi dependensi, unduhan media, dan provider AI eksternal.

Node.js/npm tidak diperlukan untuk penggunaan biasa; bundle Auto Clip Studio siap pakai sudah disertakan. Detail dan langkah manual tersedia di [INSTALL.md](INSTALL.md).

## Instalasi cepat dari source

Clone repository, lalu masuk ke foldernya:

```bash
git clone https://github.com/arisbelong21/chopsterizzz.git
cd chopsterizzz
```

Di Windows, buka folder proyek lalu jalankan:

```bat
.\install_dependencies.bat
.\run_chopster.bat
```

Installer menyiapkan runtime eksternal bila diperlukan, membuat virtual environment `.venv-source`, lalu memasang paket Python dari `requirements.txt`. Installer memerlukan koneksi internet dan dapat meminta persetujuan WinGet.

Jika WinGet tidak tersedia, pasang komponen yang diperlukan secara manual dan ikuti [panduan instalasi lengkap](INSTALL.md#instalasi-manual-bila-winget-tidak-tersedia).

## Membuat build Windows

Di Windows x64 dengan Python 3.12 64-bit, jalankan:

```bat
.\build_exe.bat
```

Hasil PyInstaller onedir berada di `dist\Chopster`. Bagikan seluruh folder hasil tersebut sebagai ZIP—jangan hanya membagikan `Chopster.exe`, karena folder `_internal` dan file pendamping diperlukan. Petunjuk untuk komputer penerima tersedia di [INSTALL.md](INSTALL.md#membuat-paket-exe-windows).

## Extension browser

Untuk memasang extension secara lokal, buka `chrome://extensions` atau `edge://extensions`, aktifkan **Developer mode**, pilih **Load unpacked**, lalu pilih folder `browser_extension`. Chopster harus sudah berjalan saat URL dikirim. Extension berkomunikasi dengan aplikasi melalui `127.0.0.1`/`localhost`; extension hanya mengirim URL, bukan file media.

## AI, privasi, dan penggunaan media

- API key tidak disertakan. Masukkan key milik Anda sendiri lewat Settings di dalam aplikasi; provider global dan Auto Clip Studio memiliki konfigurasi terpisah.
- Pada Windows, key AI disimpan menggunakan Windows DPAPI di profil pengguna. Jangan commit API key, token, file cookie, atau konfigurasi pribadi ke repository.
- Data proyek, preferensi, riwayat, cache, file sementara, dan ekspor disimpan lokal sesuai fitur yang digunakan.
- Jika memakai AI eksternal, teks atau sampel gambar yang dikirim ke provider mengikuti kebijakan provider tersebut.
- Keberhasilan unduhan bergantung pada dukungan `yt-dlp`, koneksi, autentikasi, dan kebijakan platform. Gunakan hanya media yang Anda berhak akses dan proses; aplikasi tidak menghapus batasan layanan platform.
- Fitur antrean publish/analytics bersifat lokal dan bukan integrasi untuk mengunggah otomatis ke platform sosial.

## Struktur utama

```text
ai/                  Provider AI dan alat konten
app/                 Aplikasi, konfigurasi, database, dan bridge lokal
auto_clip_studio/    Engine dan frontend Auto Clip Studio
browser_extension/   Extension Manifest V3
clipper/             Analisis, transkripsi, subtitle, dan render
requirements.txt     Dependensi Python utama
INSTALL.md           Panduan instalasi dan build lengkap
```

## Lisensi

Kode proyek Chopster dilisensikan di bawah MIT; lihat file [LICENSE](LICENSE). Lisensi ini berlaku hanya untuk bagian yang haknya dimiliki oleh pemegang hak cipta. Komponen, font, model, dan aset pihak ketiga dapat memiliki lisensi serta persyaratan atribusi tersendiri; periksa dan dokumentasikan persyaratannya sebelum mendistribusikan repository atau build.
