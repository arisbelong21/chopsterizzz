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

## Automation — publish & jadwalkan (baru di Chopster 8.5.5)

Automation mengubah clip yang sudah dirender menjadi kiriman nyata ke platform:

- **Alur**: pilih clip → pilih platform/akun → Publish Now atau jadwal → review → Automation Engine (upload → verifikasi → history).
- **AI Content Pack**: Automation memakai ulang pack yang dihasilkan Content Clipper AI (`Generate AI Content Pack`) — title options, description, caption, hashtags, SEO, pinned comment — dan menyesuaikannya per platform. Render Auto Clip Studio (`aut_acs_handoff`) dan export Content Clipper AI (`aut_cca_handoff`) yang sukses otomatis terdaftar di Automation → Studio sebagai media siap publish; keduanya bisa dimatikan terpisah di Settings → Automation. Auto-Pilot juga punya saklar global (`aut_autopilot_enabled`) yang hanya menahan penjadwalan otomatis — media tetap tercatat dan tombol **Run now** tetap bisa dipakai.
- **Account & OAuth**: buat developer app lalu isi Client ID/Secret di Automation → Accounts → Developer Configuration (Secret tersimpan DPAPI, tidak pernah tampil atau tercatat). Daftarkan **Redirect URI** (default `http://localhost:51234/`, bisa diubah di kolom Developer Configuration) dengan **persis sama** di console tiap platform — Meta dan TikTok menolak redirect URI yang tidak terdaftar. Jika kredensial belum diisi, tombol Connect langsung menampilkan panduan langkah demi langkah, bukan error umum.
  - YouTube → Google Cloud: aktifkan **YouTube Data API v3**, buat **OAuth client ID tipe Desktop app**, isi Client ID + Secret. Upload memakai resumable `videos.insert`; app harus lolos API audit (ATA) agar upload tampil publik.
  - Facebook → Meta for Developers: create app → produk **Facebook Login** → isi **Valid OAuth Redirect URIs** dengan redirect URI Chopster → isi App ID (Client ID) + App Secret; login memilih Page; publish video/réel via Graph API.
  - TikTok → TikTok for Developers: app **Content Posting API**, daftarkan **Redirect URL** persis, salin **Client Key** (isi sebagai Client ID) + Client Secret. Direct Post (`creator_info → init → FILE_UPLOAD → status`); client tanpa audit hanya bisa menayangkan posting private.
  - Instagram → Official API (butuh Facebook Page terhubung + Instagram Professional, termasuk Page Publishing Authorization/2FA; memakai kredensial Facebook) **atau** Browser Session (tanpa kredensial, butuh playwright).
- **Browser Session / AI Browser Agent** (fallback Instagram): install sekali `pip install playwright && python -m playwright install chromium`. Login dilakukan manual di jendela browser Chopster — Chopster tidak pernah meminta password, tidak membaca cookie browser utama, tidak mem-bypass CAPTCHA/MFA, dan tidak memakai koordinat klik. Agent memakai aksi semantik dengan validator domain/file, retry terbatas, dan pause (ACTION_REQUIRED) saat platform meminta konfirmasi.
- **Komunikasi jujur dengan platform**: bila app belum diaudit/direview, Chopster menampilkan keadaan sebenarnya (ACTION_REQUIRED + panduan) dan tidak pernah memalsukan keberhasilan.
- **Keamanan**: token OAuth disimpan di `automation_secrets.dpapi` (DPAPI), terpisah dari settings.json, job row, maupun log. Akun yang diputus tetap menyimpan riwayat publish.
- **Scheduler lokal**: job terjadwal bertahan setelah restart (reconciliation otomatis tanpa duplikat), duplikasi publish dicegah via media fingerprint + idempotency key, dan verifikasi memeriksa bukti sukses tiap platform sebelum mencatat PUBLISHED.

## Catatan rilis

Paket source ini sengaja tidak memuat `tests`, cache, virtual environment, hasil build, file EXE, maupun dokumen audit lama. Uji installer/dependency di Windows target sebelum membagikan hasil build. Gunakan hanya media yang Anda punya hak untuk unduh dan proses.
