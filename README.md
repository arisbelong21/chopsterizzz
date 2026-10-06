# Chopster

Paket ini berisi **source Chopster untuk Windows**, bukan EXE hasil build. Nama produk yang ditampilkan adalah Chopster; nomor teknis yang diperlukan untuk kompatibilitas tetap berada di metadata internal.

## Fitur

- Download media melalui yt-dlp dengan riwayat dan antrean.
- Content Clipper AI untuk transkripsi Whisper lokal, analisis klip, subtitle, framing, branding, audio, dan ekspor.
- Auto Clip Studio untuk analisis sumber, pemilihan momen, penyuntingan, dan render klip.
- Settings, History, browser bridge, serta extension Chrome “Kirim ke Chopster”.

## Instalasi source dan build EXE

1. Ekstrak ZIP ke folder lokal di Windows.
2. Jalankan `install_dependencies.bat`. Script mencoba memasang FFmpeg/FFprobe, Deno, dan Visual C++ Runtime melalui WinGet, lalu memasang paket Python pada `.venv-source`. Perlu koneksi internet dan WinGet.
3. Jalankan `run_chopster.bat` untuk membuka source, atau `build_exe.bat` untuk membangun EXE onedir. Script build menyiapkan lingkungan Python build tersendiri dan menghasilkan `dist\Chopster`.
4. Untuk menjalankan distribusi onedir pada komputer lain, ekstrak seluruh folder `dist\Chopster`; jangan pindahkan EXE saja. Jalankan `SETUP_DEPENDENCIES.bat` bila komputer belum memiliki FFmpeg/FFprobe atau Visual C++ Runtime.

WinGet bisa meminta persetujuan pengguna dan koneksi internet. Jika perangkat tidak memiliki WinGet, pasang dependensi yang disebutkan secara manual. YouTube modern dapat memerlukan Deno 2.3+ atau Node.js 22+.

## AI dan privasi key

Instalasi baru tidak berisi API key bawaan. Pengguna memasukkan key sendiri. Pada Windows, key global dan key Auto Clip Studio disimpan terenkripsi di profil Windows pengguna dan tidak masuk ke source ZIP/build standar. Jangan menaruh `.env`, `settings.json`, `secrets.dpapi`, file cookies, atau key pribadi di folder source/distribusi.

CCA memakai provider global yang dipilih di Settings untuk tugas teks. Analisis visual jarak jauh hanya memakai model setelah kemampuan gambar terverifikasi; jika belum, fitur visual memakai Local Vision/Tracking. Auto Clip Studio memprioritaskan key khususnya dan hanya memakai AI global bila model global telah diverifikasi mendukung gambar. Whisper tetap mengerjakan transkripsi.

## Extension

Folder `browser_extension` berisi extension Chrome Manifest V3. Muat folder itu lewat `chrome://extensions` dalam Developer mode. Extension mengirim URL ke Chopster melalui loopback; pengunduhan dilakukan aplikasi. Versi internal bridge dipertahankan untuk kompatibilitas extension, sementara nama tampilan tetap Chopster.

## Catatan rilis

Paket source ini sengaja tidak memuat `tests`, cache, virtual environment, hasil build, file EXE, maupun dokumen audit lama. Uji installer/dependency di Windows target sebelum membagikan hasil build. Gunakan hanya media yang Anda punya hak untuk unduh dan proses.
