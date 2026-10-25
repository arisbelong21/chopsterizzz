# Chopster — halaman web untuk pendaftaran TikTok API

Folder ini berisi 3 halaman statis yang TikTok minta saat mendaftar **TikTok for Developers**:

| File | Dipakai untuk field di TikTok Developer Portal |
|---|---|
| `index.html` | **Web or Desktop URL** (URL website resmi) |
| `terms.html` | **Terms of Service URL** |
| `privacy.html` | **Privacy Policy URL** |

Tidak perlu server, tidak perlu hosting berbayar, tidak perlu domain. GitHub Pages gratis dan
sudah cukup — halaman-halaman ini hanya perlu bisa **dibuka publik** oleh tim review TikTok.

---

## Langkah 0 — Ganti placeholder (WAJIB)

Buka ketiga file dan ganti teks berikut (ada di semuanya):

- `GANTI_EMAIL@example.com` → alamat email kamu yang aktif
- `GANTI_NAMA_PENGEMBANG` → nama kamu / nama usaha kamu (di `terms.html` & `privacy.html`)
- Di `terms.html` bagian 10, ganti negara hukum kalau bukan Indonesia

> TikTok menolak data yang terlihat palsu/tidak lengkap. Isi yang wajar dan benar.

---

## Langkah 1 — Publish ke GitHub Pages (gratis)

1. Buat akun di <https://github.com> (kalau belum ada).
2. Klik **New repository**. Nama bebas, misalnya `chopster-site`.
   Pilih **Public** (wajib, kalau Private halaman tidak bisa diakses TikTok). Klik **Create**.
3. Di halaman repo, klik **Add file → Upload files**, lalu **drag 3 file** dari folder ini
   (`index.html`, `terms.html`, `privacy.html`). Klik **Commit changes**.
4. Masuk **Settings → Pages**.
   - *Source*: **Deploy from a branch**
   - *Branch*: **main** dan folder **/ (root)** → **Save**
5. Tunggu 1–2 menit, refresh halaman. Akan muncul tulisan
   *"Your site is live at ..."*

URL kamu jadi seperti ini (ganti `USERNAME` dan `chopster-site`):

```
https://USERNAME.github.io/chopster-site/            ← Website URL
https://USERNAME.github.io/chopster-site/terms.html     ← Terms of Service URL
https://USERNAME.github.io/chopster-site/privacy.html   ← Privacy Policy URL
```

Buka ketiganya di browser untuk memastikan tampil normal (bukan error 404).

---

## Langkah 2 — Isi di TikTok Developer Portal

Buka <https://developers.tiktok.com/apps> → app kamu → **App details**.

**Basic information**
- ToS URL → `https://USERNAME.github.io/chopster-site/terms.html`
- Privacy Policy URL → `https://USERNAME.github.io/chopster-site/privacy.html`

**Platforms**
- Pilih **Desktop** ← penting, lihat catatan di bawah
- Website URL → `https://USERNAME.github.io/chopster-site/`
- Redirect URI (Login Kit) → `http://localhost:51234/`

**Products**
- **Login Kit** (wajib, untuk OAuth)
- **Content Posting API** → aktifkan scope `video.upload` (draft/inbox)

**Scopes**
- `user.info.basic`
- `video.upload`

---

## Langkah 3 — Verifikasi kepemilikan URL

TikTok tidak menerima URL begitu saja; kamu harus membuktikan URL itu milikmu.

1. Di halaman app, klik tombol **URL properties** (atas halaman).
2. Pastikan kamu sedang di **Production mode**, klik **Verify properties**.
3. Pilih metode **URL prefix** (bukan Domain — karena kamu tidak punya DNS sendiri).
4. Masukkan URL lengkap: `https://USERNAME.github.io/chopster-site/`
5. TikTok memberi **file signature**. **Download** file itu.
6. Upload file signature itu ke repo GitHub kamu (root, satu folder dengan `index.html`).
   Tunggu ~1 menit sampai GitHub Pages selesai deploy.
7. Klik **Verify**.

Harusnya berhasil. Kalau muncul *"Your property could not be verified"*, coba:
- pastikan file signature benar-benar bisa dibuka di `https://USERNAME.github.io/chopster-site/<nama-file>`
- pastikan tidak ada redirect dan URL prefix diakhiri `/`
- tunggu beberapa menit lalu klik Verify lagi

**Kalau tetap gagal (metode URL prefix tidak jalan di github.io):**
satu-satunya cara lain adalah **verifikasi via Domain**, dan itu butuh **domain sendiri**
supaya kamu bisa menambah **DNS TXT record**. Pilihan:
- beli domain murah (`.my.id` / `.com`, sekitar Rp15–150rb/tahun), lalu arahkan ke
  **Cloudflare Pages** (gratis) atau **GitHub Pages** (custom domain)
- tambahkan TXT record yang diberikan TikTok di DNS domain tersebut → klik Verify

Dengan domain sendiri, kamu juga bisa memakai Netlify/Vercel/Cloudflare Pages dan DNS TXT,
yang lebih jarang gagal daripada metode file signature.

---

## Peringatan penting (baca sebelum berharap banyak)

1. **Pilih platform "Desktop", jangan "Web".**
   Untuk **Desktop**, TikTok mengizinkan redirect URI `http://localhost:PORT/`
   (host harus `localhost` atau `127.0.0.1` dan wajib ada nomor port).
   Untuk **Web**, redirect harus `https` dan tanpa port — tidak cocok dengan aplikasi
   desktop offline seperti Chopster.

2. **Direct Post belum tentu bisa dipakai.**
   Tanpa audit, semua video yang dikirim via Direct Post hanya bisa berstatus **private**
   (hanya kamu yang lihat). Jadi jangan targetkan auto-post langsung.

3. **Target yang realistis: `video.upload` (inbox/draft).**
   Ini yang default di Chopster (`aut_tiktok_mode = inbox`): video dikirim ke **inbox TikTok**,
   kamu tinggal konfirmasi/tekan Post dari aplikasi TikTok. Jalur ini jauh lebih mungkin lolos.

4. **Baca ini — bisa jadi masalah serius.**
   Content Sharing Guidelines TikTok menyatakan aplikasi yang dibuat harus ditujukan untuk
   **banyak pengguna**, dan secara eksplisit menyebut *tidak dapat diterima*:
   > "A utility tool to help upload contents to the account(s) you or your team manages."

   Artinya: tool pribadi untuk meng-upload ke akun sendiri **berisiko ditolak** saat audit
   produksi. Sandbox + `video.upload` kemungkinan besar masih bisa untuk pemakaian sendiri.

5. **Kalau pakai Sandbox**, tambahkan dulu akun TikTok kamu sebagai *target user* di
   pengaturan Sandbox, supaya akun itu boleh melakukan otorisasi.

---

## Alternatif yang tidak butuh persetujuan TikTok

Browser automation (AI agent) di Chopster **tidak butuh** pendaftaran API, tidak butuh
verifikasi URL, dan tidak butuh audit. Kelemahannya: ia bekerja dengan "melihat" halaman
TikTok, jadi lebih rapuh terhadap perubahan desain — dan itulah yang perlu diperbaiki
(lihat pembahasan "buka mata agent" / vision + jangan cepat menyerah).

Kesimpulan praktis: **jalankan keduanya**. API resmi untuk jalur draft/inbox yang resmi dan
stabil, browser agent untuk capacity auto-post.
