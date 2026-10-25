# Naskah Video Demo — TikTok App Review

Untuk app: **NAMA DEVELOPER** (`<GITHUB_USERNAME>.github.io`)
Produk: **Login Kit** (`user.info.basic`) + **Content Posting API** (`video.upload`)

---

## 🔐 Status koneksi & masa berlaku token (15 Okt 2026)

Akun uji: **kei kei** — `auth_mode=oauth`, `status=connected`.

| Item | Nilai |
|---|---|
| Access token | **24 jam** (diperbarui otomatis oleh Chopster saat mau kedaluwarsa) |
| Refresh token | **365 hari** — berlaku s/d ± Okt 2027 |
| Scope diberikan | `video.upload,user.info.basic` (draft/inbox, **bukan** `video.publish`) |

Refresh **diuji langsung ke server TikTok** (bukan simulasi/mock) dan berhasil:
access token baru diterbitkan TikTok dan tersimpan ke DPAPI. Artinya menutup
Chopster lalu melanjutkan besok **tidak** menghilangkan sesi.

**Yang harus dijaga agar sesi tetap hidup:**
1. Jangan klik **Disconnect** di Accounts — itu menghapus token.
2. Jangan ganti **Client Key/Secret** aplikasi di portal — token lama jadi tidak cocok.
3. Jangan hapus **Target User** di Sandbox.
4. Jangan ubah daftar **scope** di portal — token yang sudah terbit tidak otomatis
   mendapat scope baru, sehingga harus connect ulang.

---

## ✅ Perbaikan Display API `user/info` (15 Okt 2026)

Setelah PKCE benar, otorisasi **berhasil** dan halaman consent muncul. Tapi
kembali ke Chopster muncul:

> "Gagal menyambungkan. Info akun TikTok gagal dibaca. Periksa scope user.info.basic."
> Detail: `TikTok user info HTTP 404: path(Janus)`

Penyebab: endpoint `/v2/user/info/` (Display API v2) adalah **GET**, sedangkan
kode memakai **POST**. TikTok menjawab `404 Unsupported path(Janus)`.

Dibuktikan langsung ke server TikTok (token sengaja tidak valid):

| Request | Hasil |
|---|---|
| `GET /v2/user/info/?fields=open_id,display_name` | `401 access_token_invalid` (rute ada) |
| `POST /v2/user/info/?fields=...` | `404 Unsupported path(Janus)` ← persis error di Chopster |

Perbaikan: `TikTokAuth._user_info()` memakai **GET** dan hanya meminta field
yang dicakup `user.info.basic` (`open_id`, `display_name`). `unique_id` **bukan**
field v2 yang sah dan sudah dihapus. Kode error di body (HTTP 200 tapi
`error.code != "ok"`) kini juga dilaporkan dengan jelas.

**Efek ke nama akun:** dengan scope `user.info.basic`, TikTok hanya memberi
`display_name` (nama tampilan), **bukan** `username`/`@handle`. Jadi akun tampil
sebagai nama tampilan. Untuk menampilkan `@handle`, perlu menambah scope
`user.info.profile` di portal **dan** di permintaan login — opsional, dan akan
menambah satu scope lagi di demo/app review.

---

## ✅ Perbaikan PKCE — penyebab connect "code challenge" (15 Okt 2026)

Gejala: klik **Connect** → browser Edge membuka halaman TikTok yang berbunyi
*"Kami tidak dapat masuk dengan TikTok … perbaiki hal berikut: **code challenge**"*,
lalu Chopster tidak pernah menerima kode. Halaman consent **tidak pernah muncul**
karena request otorisasinya ditolak TikTok sebelum halaman izin dirender.

Penyebab di kode (jalur desktop TikTok):

| Bagian | Sebelum | Sesudah |
|---|---|---|
| PKCE | tidak dikirim (`use_pkce` hanya dipakai YouTube) | `use_pkce=True` |
| `code_challenge` | — (tidak ada) | `SHA256(code_verifier)` |
| Encoding challenge | `base64url` (standar RFC 7636) | **hex** (yang diminta TikTok) |
| `code_verifier` di token exchange | tidak dikirim | dikirim |
| Nama kredensial di token exchange | `client_id` | **`client_key`** |
| Token refresh | `client_id` | **`client_key`** |

TikTok mewajibkan PKCE untuk app desktop, dan mendokumentasikan
`code_challenge = SHA256(code_verifier)` dalam **hex**, bukan base64url.
Token endpoint-nya juga memakai `client_key`, bukan `client_id`.

Perubahan: `_pkce_pair(hex_challenge=False)`, parameter baru
`run_desktop_oauth(pkce_hex=False, token_client_key="client_id", cancel_check=None)`,
dan `TikTokAuth.connect()` memakai `pkce_hex=True, token_client_key="client_key"`.
Google/Meta tidak berubah (default tetap base64url + `client_id`).

### Perbaikan hang "jendela tidak bisa ditutup"

Sebelumnya, saat connect gagal, Chopster menonaktifkan seluruh jendela
(terlihat **abu-abu**, tanpa tombol) dan menunggu worker OAuth selesai. Worker-nya
tidak menghormati pembatalan, jadi ia menunggu sampai batas waktu 5 menit —
jendela tidak bisa ditutup dan proses harus dihentikan lewat `cmd`.

Perbaikan: `run_desktop_oauth` kini menerima `cancel_check`, dan tombol Connect
meneruskan flag pembatalan. Menutup Chopster sekarang menghentikan penantian
dalam ~0,25 detik.

Sudah diuji: `211 passed, 3 subtests passed` (+2 test regresi baru).

---

## ✅ Scope sudah diperbaiki (15 Okt 2026)

Sebelumnya ada ketidakcocokan yang membuat jalur inbox mustahil berhasil:

| Bagian | Sebelum | Sesudah |
|---|---|---|
| `chopster/automation/auth.py` — `TikTokAuth` | `SCOPE = "user.info.basic,video.publish"` (selalu) | scope mengikuti `aut_tiktok_mode` |
| Mode `inbox` (dipakai sekarang) | minta `video.publish` ❌ | minta **`video.upload`** ✅ |
| Mode `direct` | minta `video.publish` | tetap `video.publish` |

Perubahan: ditambahkan method `TikTokAuth._scopes()` dan `connect()` memakai
`scope=self._scopes()`.

**Konsekuensi untuk demo:** app di portal harus punya scope `video.upload`.
Tidak perlu (dan jangan) minta `video.publish` kalau demo pakai mode inbox.

> Catatan: scope yang diminta saat login **menyatu** dengan yang sudah
> tersimpan. Kalau akun TikTok sudah pernah tersambung sebelum perbaikan ini,
> putuskan dulu lalu **Connect ulang** supaya token barunya memuat

> `video.upload`.

---

## 🔑 Cara merekam padahal app belum disetujui: SANDBOX

Ini pertanyaan paling sering bikin orang macet. Kuncinya: **kata "upload"
dipakai untuk dua hal yang berbeda.**

| | "Upload" yang mana | Ke mana |
|---|---|---|
| 1 | **Upload video demo** | Lampirkan file mp4 ke **form App review** di developers.tiktok.com — cuma lampiran file, **tidak lewat API** |
| 2 | **Upload video ke TikTok** | Chopster mengirim video ke **inbox TikTok-mu** lewat Content Posting API |

Jadi video demo **tidak** dikirim lewat API. Ia cuma file yang ditempelkan ke
form, seperti melampirkan berkas di email.

Lalu kenapa app-nya bisa "jalan" padahal belum di-approve? Karena **Sandbox**.

### Apa itu Sandbox

Dari dokumentasi resmi TikTok:

> "Sandbox mode is a restricted environment that allows you to try out
> integrations **without having to submit your app for review**."

- Bisa bikin sampai **5 sandbox** per app
- Bisa menambah sampai **10 akun TikTok** sebagai **target user**
- Sandbox punya **Client Key & Client Secret sendiri** (bukan yang production)
- Target user boleh memberi izin scope **tanpa app disetujui**

Artinya: Login Kit + Content Posting API **benar-benar berfungsi** di dalam
sandbox. Itulah yang direkam.

### Langkah membuat Sandbox

1. Buka **Manage apps** → pilih app-mu
2. Geser toggle di samping nama app → **Sandbox** *(kebalikan dari Production)*
3. Klik **Create Sandbox** → beri nama
   - Boleh **clone** konfigurasi dari Production, jadi tidak perlu isi ulang
4. Di dalam sandbox: cek **App details** + tambahkan produk
   - **Login Kit** → `user.info.basic`
   - **Content Posting API** → `video.upload`
5. Klik **Apply changes** *(wajib — kalau tidak, konfigurasi tidak aktif)*
6. Buka **Sandbox settings** → bagian **Target users** → **Add account**
   → login dengan akun TikTok-mu → setujui Developer ToS
   - Munculnya bisa sampai **1 jam** setelah refresh
7. Buka **Credentials** di mode Sandbox → salin **Client Key** dan
   **Client Secret khusus sandbox**
8. Daftarkan **Redirect URL**: `http://localhost:51234/` (kalau belum ada)

### Isi "Basic information" (sama untuk Sandbox & Production)

| Kolom | Isi |
|---|---|
| **App icon** | file 1024×1024 → `docs\assets\chopster-icon-play-1024.png` atau `chopster-icon-upload-1024.png` |
| **Category** | `Productivity` |
| **Description** *(batas ±120)* | lihat teks di bawah |
| **Terms of Service URL** | `https://<GITHUB_USERNAME>.github.io/terms.html` |
| **Privacy Policy URL** | `https://<GITHUB_USERNAME>.github.io/privacy.html` |
| **Platforms** | centang ✅ **Desktop** |
| **Web/Desktop URL** | `https://<GITHUB_USERNAME>.github.io/` |

Teks Description (105 karakter):

```
A desktop tool for creators to prepare and publish their own original videos to their own TikTok account.
```

> Perhatikan: kolom **Description di Basic information** batasnya **±120**,
> sedangkan **kolom penjelasan di App review** batasnya **1000**. Dua kolom
> berbeda — jangan tertukar.

### Lalu di Chopster

1. Tab **Accounts** → **DEVELOPER CONFIGURATION**
2. Ganti Client ID/Secret TikTok dengan **milik sandbox** (yang production
   `<CLIENT_ID_PRODUCTION>` **jangan** dipakai untuk merekam)
3. **Simpan Developer Config** → klik **Connect** TikTok
4. Upload video uji → cek masuk ke **inbox TikTok**
5. **Baru rekam** semuanya sesuai naskah di atas

### ⚠️ Batasan sandbox yang perlu kamu tahu

> Dokumentasi TikTok: *"Sandbox mode does not offer access to Content Posting
> API for **public** videos or Data Portability API."*

Artinya: di sandbox kamu **tidak bisa** memposting video **publik**.
Tapi jalur **draft/inbox** (`video.upload`) tetap jalan — dan itu justru
jalur yang kita pakai. Jadi tidak masalah.

Kalau ternyata sandbox menolak jalur inbox, lapor ke saya — jangan langsung
ganti ke Direct Post.

### Setelah selesai merekam

1. Geser toggle kembali ke **Production**
2. Isi **App review**: kolom penjelasan + **lampirkan video** hasil rekaman
3. Pastikan **products & scopes** di Production juga sudah benar
4. Klik submit

> Catatan: yang **disubmit untuk review** adalah konfigurasi **Production**,
> sedangkan yang **direkam** adalah **Sandbox**. Dua mode berbeda.

---

## 1. Persiapan sebelum merekam

### A. Di TikTok Developer Portal

- [x] **Platform** = Desktop, **Redirect URL** = `http://localhost:51234/`
- [x] Produk: **Login Kit** + **Content Posting API** (keduanya terpasang)
- [x] Scope: `user.info.basic` + `video.upload` — **Direct Post OFF**
- [x] **Sandbox**: akun TikTok-mu sudah terdaftar sebagai **target user**
      (wajib — app yang belum di-approve hanya bisa didemokan lewat Sandbox)
- [ ] Saat merekam: **tetap di mode Sandbox**, jangan ubah apa pun di portal

### B. Di Chopster — status saat ini (sudah beres)

- [x] Tab **Automation** → sub-tab **Accounts**
- [x] Kartu **DEVELOPER CONFIGURATION (DISIMPAN TERENKRIPTASI)**, baris **TikTok**:
      - `Client ID / Key` → `<CLIENT_ID_SANDBOX>` — ini **kredensial Sandbox**
      - `Client Secret` → tersimpan di **DPAPI** (tampil "(tersimpan DPAPI)")
- [x] Kolom **Redirect URI** → `http://localhost:51234/`
- [x] Klik **Simpan Developer Config**
- [x] Baris **TikTok** menunjukkan **● Connected** dengan nama akun
- [ ] Sebelum merekam, pastikan statusnya masih **● Connected**

> Nilai **Production** (`<CLIENT_ID_PRODUCTION>`) **tidak dipakai** untuk merekam.

### C. Bahan rekaman

- [ ] 1 video **milikmu sendiri** — mp4, ~10–20 detik, < 50 MB
- [ ] Taruh di folder netral: `C:\TikTokDemo\video-uji.mp4`
      *(supaya dialog file tidak menampilkan nama user / folder pribadimu di rekaman)*
- [ ] HP dengan aplikasi TikTok, login akun **target user yang sama** (`kei kei`)
- [ ] Perekam layar: **OBS Studio** (disarankan) atau Xbox Game Bar `Win+G`
- [ ] Resolusi **1920×1080**, 30 fps, **kursor terlihat**
- [ ] Mikrofon aktif (Game Bar: Settings → Capturing → *Capture microphone*)
- [ ] **Focus Assist / Jangan Ganggu: ON** — matikan semua notifikasi
- [ ] Tutup aplikasi lain (chat, email, browser tab pribadi)
- [ ] Sudah login TikTok di browser → halaman izin langsung muncul
- [ ] **Bahasa situs TikTok = English** (ganti di tiktok.com → language selector).
      Reviewer-nya internasional; halaman izin berbahasa Inggris jauh lebih jelas.
- [ ] Chopster terbuka, tab **Automation → Accounts** menunjukkan TikTok **● Connected**

---

## 2. Naskah rekaman — shot by shot (target ≈ 3½ menit)

### Aturan main

1. Narasi **bahasa Inggris** (reviewer TikTok internasional). Terjemahan Indonesia
   ada di tiap shot supaya kamu paham maksudnya.
2. Rekam **satu take per shot**. Salah sedikit? Ulangi shot itu saja, jangan
   ulangi semuanya.
3. Jangan mempercepat video. Gerakan normal, jeda 1–2 detik saat menunjukkan bukti.
4. **Jangan** membuka tab **Content Clipper** / **Auto Clip Studio**.
5. **Jangan** menyentuh platform selain TikTok.
6. **Jangan** membuka kartu **DEVELOPER CONFIGURATION**.
7. Semua yang tampil = aplikasi asli, tanpa efek atau klaim berlebihan.

### Peta waktu

| # | Waktu | Shot | Bukti untuk reviewer |
|---|-------|------|----------------------|
| 1 | 0:00–0:07 | Kartu judul | Apa ini & apa yang didemokan |
| 2 | 0:07–0:18 | Buka Chopster | Aplikasi desktop, jalan lokal, tanpa server |
| 3 | 0:18–0:30 | Automation → Accounts | Akun TikTok terhubung |
| 4 | 0:30–0:40 | Disconnect | Titik awal alur login |
| 5 | 0:40–1:05 | Connect → halaman izin TikTok | **Login Kit `user.info.basic`** + **Content Posting API `video.upload`** |
| 6 | 1:05–1:15 | Kembali ke Chopster | Akun tampil dari `user.info.basic` |
| 7 | 1:15–1:28 | Settings → TikTok publish path | **Inbox draft (`video.upload`)**, Direct Post OFF |
| 8 | 1:28–1:50 | LANGKAH 1 — KONTEN | Video **milikku sendiri** dari komputernya |
| 9 | 1:50–2:08 | Isi judul + caption | Metadata ditulis pengguna |
| 10 | 2:08–2:22 | LANGKAH 2–4 | Hanya TikTok; keputusan akhir tetap di pengguna |
| 11 | 2:22–2:32 | Mulai Automation ▶ | Pengguna yang memicu proses |
| 12 | 2:32–2:50 | Queue | Upload lewat API resmi; hasilnya **draft**, bukan publish |
| 13 | 2:50–3:15 | HP: TikTok → Inbox | Draft benar-benar sampai ke akun sendiri |
| 14 | 3:15–3:25 | *(opsional)* tekan Post | Kreator sendiri yang memutuskan |
| 15 | 3:25–3:35 | Kartu penutup | Ringkasan |

---

### Shot 1 · 0:00–0:07 · Kartu judul

**Teks overlay**
```
Chopster — a desktop app for creators
TikTok Login Kit (user.info.basic) + Content Posting API (video.upload)
Demo recorded in the TikTok Sandbox environment
```
**Narasi (EN):** "This is Chopster, a desktop application that runs on a creator's
own computer. In this demo I will show the TikTok Login Kit and the Content
Posting API. Everything you see is recorded in the TikTok Sandbox environment."

**ID:** Ini Chopster, aplikasi desktop yang jalan di komputer kreator sendiri.
Saya akan menunjukkan TikTok Login Kit dan Content Posting API, direkam di Sandbox.

---

### Shot 2 · 0:07–0:18 · Ini aplikasi desktop

**Aksi:** tampilkan desktop → klik jendela **Chopster**. Sorot judul jendela
(*Chopster*) dan sidebar kiri (Dashboard, Download, … Automation, History,
Settings). **Jangan** klik *Content Clipper* / *Auto Clip Studio*.

**Teks overlay**
```
Runs locally on my computer · no server · no website
```
**Narasi (EN):** "Chopster runs entirely on my own computer. It has no server and
no website — the creator installs it. It has several tools, but for this demo I
only use the Automation tab."

**ID:** Chopster jalan sepenuhnya di komputer saya sendiri; tidak ada server dan
tidak ada versi web.

---

### Shot 3 · 0:18–0:30 · Accounts

**Aksi:** sidebar **Automation** → sub-tab **Accounts** → zoom ke baris **TikTok**
yang menunjukkan **● Connected** beserta nama akun.

**Teks overlay**
```
TikTok account already connected
```
**Narasi (EN):** "In the Automation tab, Accounts shows the accounts I connected.
TikTok is connected. To show you the authorisation flow from the start, I will
disconnect it and connect it again."

**ID:** TikTok sudah tersambung. Saya putuskan dulu supaya alur login-nya terlihat.

---

### Shot 4 · 0:30–0:40 · Putuskan dulu

**Aksi:** klik **Disconnect** di baris TikTok → setujui konfirmasi.
(Setelah ini tombolnya berubah menjadi **Connect**.)

**Teks overlay**
```
Starting from a clean state
```
**Narasi (EN):** "I disconnect the account, so you can see the full login flow
from the beginning."

**ID:** Saya putuskan akunnya supaya alur login terlihat utuh.

---

### Shot 5 · 0:40–1:05 · Halaman izin TikTok — SHOT PALING PENTING

**Aksi:**
1. Klik **Connect** di baris TikTok.
2. Browser terbuka → halaman TikTok muncul. **Berhenti 2–3 detik** supaya
   reviewer sempat membaca. Pastikan yang terlihat:
   - judul **"chopster (Sandbox) ingin mengakses akun TikTok Anda"**
   - **"Mengakses info profil Anda (avatar dan nama tampilan)"** → `user.info.basic`
   - **"Mengunggah draf konten ke TikTok untuk diedit lebih lanjut"** → `video.upload`
3. Klik **Lanjutkan / Continue** (atau *Authorize*).
4. Setelah itu alamat browser berpindah ke `http://localhost:51234/…` —
   tunjukkan alamat itu **1–2 detik** (bukti alur desktop/loopback).

> Kalau Chopster dan browser terbuka di monitor berbeda, rekam **seluruh desktop**
> (OBS: *Display Capture*) atau pindahkan kedua jendela ke monitor yang sama.

**Teks overlay**
```
Login Kit → user.info.basic
Content Posting API → video.upload (draft only)
Login happens on TikTok's own website — Chopster never sees the password
```
**Narasi (EN):** "Chopster opens TikTok's own authorisation page. I sign in on
TikTok's website — Chopster never sees my password. TikTok shows exactly two
permissions: reading my basic profile information, such as my display name, and
uploading a draft to my own TikTok inbox. I approve."

**ID:** Chopster membuka situs TikTok sendiri; saya login di sana, Chopster tidak
pernah melihat password. TikTok menunjukkan dua izin: baca profil dasar, dan
unggah draf ke inbox saya.

---

### Shot 6 · 1:05–1:15 · Kembali ke Chopster

**Aksi:** kembali ke Chopster → tab **Accounts** → baris TikTok sekarang
**● Connected** dengan nama akun. Zoom sebentar.

**Teks overlay**
```
Account connected — display name read with user.info.basic
```
**Narasi (EN):** "Back in Chopster my TikTok account is connected, and the display
name you see was read with the user.info.basic scope."

**ID:** Akun tersambung; nama yang tampil dibaca lewat scope `user.info.basic`.

---

### Shot 7 · 1:15–1:28 · Publish path = draft

**Aksi:** sidebar **Settings** → cari baris **"TikTok publish path"** → tunjukkan
nilainya **"Inbox draft (aman, video.upload)"** → zoom 2 detik → kembali ke
**Automation**.

**Teks overlay**
```
video.upload = draft only · Direct Post is OFF
```
**Narasi (EN):** "In Settings, the TikTok publish path is set to Inbox draft,
which uses the video.upload scope. Direct Post is switched off. Chopster only
delivers drafts."

**ID:** Jalur publish TikTok = Inbox draft (`video.upload`). Direct Post dimatikan.

---

### Shot 8 · 1:28–1:50 · LANGKAH 1 — KONTEN

**Aksi:**
1. sub-tab **Create Automation**
2. dropdown sumber → **Manual Import**
3. klik **"Pilih file…"** → pilih `C:\TikTokDemo\video-uji.mp4` → Open
4. baris masuk ke daftar dengan label `[manual]`, centang aktif, thumbnail terlihat

**Teks overlay**
```
My own video file, chosen from my own computer
```
**Narasi (EN):** "This is my own video file, chosen from my own computer. I import
it manually — Chopster does not download or reuse anyone else's content."

**ID:** File video milik saya sendiri, dipilih manual dari komputer sendiri.

---

### Shot 9 · 1:50–2:08 · Judul & caption

**Aksi:** klik baris video → di kartu **PACK PER VIDEO** isi **Judul** dan
**Caption (TikTok / Instagram / Facebook)**. Ketik caption pendek, contoh:
`Test upload from the Chopster desktop app #demo`. Biarkan field lain default.

**Teks overlay**
```
I type the title and caption myself
```
**Narasi (EN):** "I type the title and caption myself. TikTok's draft API carries
only the video file, so I'll write the caption in the TikTok app at the end —
I'll show that."

**ID:** Judul dan caption saya tulis sendiri; caption versi TikTok-nya saya tulis
di app (endpoint draft hanya menerima video).

---

### Shot 10 · 2:08–2:22 · Platform, waktu, review

**Aksi:**
1. **Lanjut ▶** → **LANGKAH 2 — PLATFORM TUJUAN**: centang **hanya TikTok**
2. **Lanjut ▶** → **LANGKAH 3 — WAKTU**: pilih **Publish immediately**
3. **Lanjut ▶** → **LANGKAH 4 — REVIEW & JALANKAN**: tunjukkan ringkasan
   (media, platform, waktu, caption). Biarkan kotak
   *"Review sebelum publish"* **tidak** dicentang.

**Teks overlay**
```
Only TikTok is selected · the final publish decision stays with me
```
**Narasi (EN):** "I select only TikTok. The review step shows exactly what this run
will do — my own video, to TikTok only."

**ID:** Hanya TikTok yang dipilih; ringkasan menunjukkan persis apa yang dijalankan.

---

### Shot 11 · 2:22–2:32 · Mulai

**Aksi:** klik **Mulai Automation ▶** → dialog muncul
(*"Automation dibuat (QUEUED). 1 clip × 1 platform = 1 jobs."*) → klik **OK** →
Chopster otomatis pindah ke tab **Queue**.

**Teks overlay**
```
Automation created (QUEUED). 1 clip × 1 platform = 1 job.
The upload starts only because I clicked it
```
**Narasi (EN):** "Nothing happens automatically. I click Start, and Chopster
creates the job."

**ID:** Tidak ada yang jalan sendiri — saya yang menekan Start.

---

### Shot 12 · 2:32–2:50 · Queue: hasilnya draft

**Aksi:** di tab **Queue**, tunjukkan baris job: **Queued → Running →
Action required**, dengan chip **"⚠ Draft siap — publish di app"**.
Kalau perlu klik **↻ Muat Ulang**.

**Berapa lama menunggu (terukur 15 Okt):** setelah upload selesai, progress
berhenti di **88%** dengan label *"Menunggu TikTok menyiapkan draft…"*. TikTok
menjawab `PROCESSING_UPLOAD` sekitar 6× (≈30 detik) lalu **`SEND_TO_USER_INBOX`**.
Untuk video 17 MB totalnya **≈44 detik**; video demo (≤50 MB) perkirakan
**≤1 menit**. Jadi diam sejenak di layar itu memang benar — bukan macet, dan
jangan buru-buru menutup Chopster.

**Teks overlay**
```
Uploaded through the official Content Posting API
ACTION_REQUIRED = a draft was delivered. Chopster did NOT publish.
```
**Narasi (EN):** "I watch the queue. The job goes from Queued to Running and
finishes as Action required — Draft ready. That means the video was delivered as a
draft to my TikTok inbox through the official Content Posting API. Chopster did
not publish anything."

**ID:** Job berakhir "Action required — Draft siap": video terkirim sebagai draft
lewat Content Posting API resmi; Chopster tidak mem-publish apa pun.

---

### Shot 13 · 2:50–3:15 · HP: TikTok → Inbox

**Aksi:** buka aplikasi TikTok di HP → **Inbox** → draft muncul dengan thumbnail →
ketuk draft → halaman preview dengan tombol **Post**.

> **Di TikTok, kolom caption memang KOSONG — itu benar, bukan bug.** Endpoint
> draft (`/v2/post/publish/inbox/video/init/`) hanya menerima **file video**;
> tidak ada field judul/caption di dalamnya. Caption ditulis di aplikasi TikTok.
> Karena itu, sebelum menekan **Post**, ketik caption pendek di app (contoh:
> `My own clip — sent from Chopster as a draft`) supaya terlihat alurnya utuh.
> Justru inilah inti ceritanya: **pengguna yang menyelesaikan posting.**

> Rekam bagian HP ini terpisah (kamera HP / screen-record HP), lalu gabung saat
> editing. Tidak masalah, yang penting alurnya jelas.

**Teks overlay**
```
The draft is waiting in my own TikTok inbox
The upload API sends the video file only — I write the caption here
Nothing is public until I tap Post
```
**Narasi (EN):** "In the TikTok app on my phone, the draft is waiting in my
inbox — the video only, because TikTok's upload API carries the video file and
nothing else. I write the caption here myself, review it, and post it. Nothing
is public yet."

**ID:** Draft ada di Inbox TikTok saya (video saja — caption saya tulis di app),
belum publik, saya sendiri yang memutuskan.

---

### Shot 14 · 3:15–3:25 · *(opsional)* tekan Post

**Aksi:** ketuk **Post**. Lakukan **hanya** kalau akun ini memang akun uji dan
kamu tidak keberatan videonya tayang. Kalau tidak, cukup tunjukkan tombol **Post**
lalu kembali — tetap sah.

**Teks overlay**
```
The creator publishes — not the app
```
**Narasi (EN):** "I post it myself from the TikTok app. The app never published on
my behalf."

**ID:** Saya sendiri yang menekan Post; aplikasi tidak pernah posting sendiri.

---

### Shot 15 · 3:25–3:35 · Kartu penutup

**Teks overlay**
```
Chopster · desktop app for creators
Login Kit → user.info.basic     Content Posting API → video.upload
Draft only. The creator publishes.
No server · no auto-publish
```
**Narasi (EN):** "Chopster delivered my own video to my own TikTok account as a
draft, using TikTok's official APIs — and I published it myself."

**ID:** Chopster mengirim video milik saya ke akun saya sendiri sebagai draft
lewat API resmi TikTok, dan saya sendiri yang mem-publish.

---

### Kalau videonya kepanjangan

- Buang **Shot 14** (posting manual) → hemat 10 detik
- Percepat bagian menunggu di **Queue** (~45 detik berhenti di 88%) jadi timelapse
  8×, sisakan label *"Menunggu TikTok menyiapkan draft…"* tetap terbaca
- **Jangan** dipercepat: halaman izin (Shot 5) dan chip hasil (Shot 12)
- **Kartu judul** 7 detik → 4 detik
- Target akhir: **di bawah 3 menit**

---

## 3. Setelah rekaman — editing & kirim

### 1) Gabung & pasang teks overlay

Editor bebas: **CapCut** (paling gampang), Clipchamp, atau DaVinci Resolve.
Tempel **Teks overlay** dari tiap shot di atas (font default, putih + outline
hitam, posisi bawah-tengah). Pakai **bahasa Inggris**, sama seperti narasi.

### 2) Ekspor mp4 ≤ 50 MB

Target 1920×1080, H.264, 30 fps. ffmpeg sudah tersedia di mesin ini
(`C:\ffmpeg-8.0-full_build\bin\ffmpeg.exe`):

```powershell
cd C:\TikTokDemo
ffmpeg -i rekaman-awal.mp4 -c:v libx264 -crf 26 -preset medium -pix_fmt yuv420p `
  -c:a aac -b:a 128k -movflags +faststart demo-chopster-tiktok.mp4
```

Kalau masih > 50 MB, turunkan resolusi/bitrate:

```powershell
ffmpeg -i rekaman-awal.mp4 -vf scale=1280:-2 -c:v libx264 -crf 30 -preset slow `
  -c:a aac -b:a 96k -movflags +faststart demo-chopster-tiktok-720p.mp4
```

Cek hasilnya:

```powershell
Get-Item .\demo-chopster-tiktok.mp4 | Select-Object Name, Length
ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 .\demo-chopster-tiktok.mp4
```

### 3) Kirim ke App review

1. Portal → app → toggle kembali ke **Production**
2. **App review** → kolom penjelasan → tempel teks dari `docs\tiktok-app-review.md`
3. Lampirkan **demo-chopster-tiktok.mp4** (maks 5 file, masing-masing ≤ 50 MB)
4. Cek **Products**: Login Kit (`user.info.basic`) + Content Posting API
   (`video.upload`) — **tanpa** `video.publish`
5. Submit

---

## 4. Kalau bermasalah saat merekam

| Gejala | Tindakan |
|---|---|
| Halaman izin TikTok **tidak muncul** (langsung balik ke Chopster) | Log out dari tiktok.com di browser itu, lalu **Connect** lagi — login + halaman izin akan tampil penuh. Alur resminya tetap sama. |
| Error **"code challenge"** | Sudah diperbaiki (lihat bagian PKCE di atas). Kalau masih muncul, kirim screenshot. |
| Error **"Info akun TikTok gagal dibaca"** | Sudah diperbaiki (Display API harus **GET**). Kalau muncul lagi, kirim screenshot. |
| Queue berakhir **FAILED**, bukan "Action required" | **Sudah diperbaiki (15 Okt).** Penyebab lamanya: Chopster menunggu status `SEND_TO_USER`, padahal TikTok mengirim **`SEND_TO_USER_INBOX`** — draft sudah sampai, tapi job ditandai gagal. Kalau masih muncul, kirim teks error di baris Queue + potongan `chopster.log`. |
| Draft **tidak ada** di Inbox TikTok HP | Pastikan akun TikTok di HP = **target user** yang sama (`kei kei`). Tarik-refresh daftar Inbox. |
| Upload terasa lama (> 3 menit) | Wajar untuk video besar. Perkecil video uji (< 50 MB) dan pastikan internet stabil. Jangan potong rekaman di tengah proses. |

### Jangan sampai terekam

- Tab **Content Clipper** dan **Auto Clip Studio**
- Platform selain TikTok (jangan diklik sama sekali)
- Kartu **DEVELOPER CONFIGURATION** (Client ID / Client Secret)
- **Nama folder / user Windows-mu** → karena itu video uji ditaruh di `C:\TikTokDemo\`
- Notifikasi, chat, email, tab browser pribadi


> 💡 **Catatan narasi:** pakai **bahasa Inggris** atau tambahkan **subtitle
> Inggris**. Peninjau TikTok lintas negara; narasi Indonesia tanpa subtitle
> menyulitkan mereka menilai alur.

---

## 5. Hal yang membuat review tertunda / ditolak

| ❌ Jangan | Kenapa |
|---|---|
| Mempercepat adegan halaman izin | Peninjau harus bisa membaca scope yang diminta |
| Menyembunyikan/memotong antarmuka Chopster | "The video should clearly show the user interface and user interactions" |
| Memakai video milik orang lain | Harus konten original milikmu |
| Memperlihatkan Client Secret / token / URL OAuth di layar | Itu kredensial — blur sebelum upload |
| Mengklaim Chopster yang memposting | Review akan menilai itu Direct Post; padahal draft |
| Pakai akun berbeda antara demo dan akun uji | Harus akun yang sama, target user Sandbox |
| Durasi bertele-tele tanpa inti | Fokus ke alur, potong bagian menunggu |

---

## 6. Setelah upload video

Isi juga kolom lain di bagian **App review**:

- **Kolom penjelasan** (0/1000) — teksnya ada di `docs/tiktok-app-review.md`, bagian A
- **Demo video** — hasil rekaman di atas (mp4, ≤50 MB, maks 5 file)
- **Products & Scopes** — pastikan sudah: Login Kit `user.info.basic` +
  Content Posting API `video.upload`

---

## 7. Ringkasan checklist

```
KODE — SELESAI
[x] PKCE hex + client_key untuk TikTok desktop
[x] Scope mengikuti mode (video.upload untuk inbox)
[x] Display API user/info memakai GET
[x] Status draft dibaca dari SEND_TO_USER_INBOX (bukan SEND_TO_USER)
[x] Upload selesai + status belum final → "⚠ Hasil belum pasti", bukan FAILED
[x] Nama akun dibaca dari user.info.basic
[x] Refresh token teruji langsung ke TikTok (masa berlaku 365 hari)

PORTAL — SANDBOX (UNTUK MEREKAM) — SELESAI
[x] Produk: Login Kit + Content Posting API
[x] Scope: user.info.basic + video.upload (Direct Post OFF)
[x] Apply changes
[x] Target users: akun TikTok-mu terdaftar
[x] Redirect URL = http://localhost:51234/
[x] Client Key/Secret sandbox sudah dipasang di Chopster

CHOPSTER — SELESAI
[x] Developer Configuration terisi kredensial SANDBOX
[x] Connect TikTok → akun terhubung, scope terverifikasi
[ ] Uji sekali: upload video → muncul di inbox TikTok   ← lakukan sebagai dry run

REKAMAN — TUGAS BERIKUTNYA
[ ] 1 video original mp4, ~10–20 detik, < 50 MB, simpan di C:\TikTokDemo\
[ ] Bahasa TikTok = English, Do Not Disturb ON
[ ] Rekam 15 shot sesuai bagian 2 (±3½ menit, narasi Inggris)
[ ] Ekspor mp4 ≤ 50 MB (perintah ffmpeg di bagian 3)

SUBMIT
[ ] Toggle app → Production
[ ] Production: ToS/Privacy URL + Platforms=Desktop + scopes benar
[ ] App review: kolom penjelasan + lampirkan video
[ ] Submit for review
```
