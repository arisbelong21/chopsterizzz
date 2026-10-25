# TikTok App Review — bahan yang harus diisi

## A. Kolom penjelasan (tempel apa adanya)

Kolom: **"Explain how each product and scope works within your app or website."**
Batas: **1000 karakter**. Teks di bawah sudah dihitung dan aman.

<!-- MULAI-TEKS -->
Chopster is a desktop app a creator installs on their own computer. It runs entirely on that computer and the developer operates no backend server.

PRODUCT: Login Kit
Lets the creator authorise Chopster to act on their own TikTok account. Authorisation happens on TikTok's website; the app never sees the password.

SCOPE: user.info.basic
Shows the connected account's display name in the app, so the creator can confirm the right account is selected. Only the account's open_id and display name are read; no other profile data is read or stored.

PRODUCT: Content Posting API
Delivers the creator's own video, chosen from their own device, to their own TikTok account.

SCOPE: video.upload
The video is uploaded into the creator's own TikTok inbox as a draft. The creator reviews it in the TikTok app and taps Post themselves. Chopster never publishes without the creator's confirmation.

Tokens are stored locally on the creator's device and can be revoked anytime.
<!-- SELESAI-TEKS -->

---

## B. Video demo (wajib, minimal 1)

Ketentuan dari TikTok:
- Format **mp4** atau **mov**
- Maksimal **5 file**, masing-masing **maks 50 MB**
- Harus menunjukkan **alur lengkap dari awal sampai akhir**
- Harus menunjukkan **antarmuka dan interaksi pengguna** yang jelas
- **Semua produk dan scope** yang dipilih harus terlihat di video
- Kalau app belum pernah disetujui, wajib mendemokan lewat **Sandbox environment**

### Urutan yang harus terekam

> Naskah lengkapnya (shot by shot + narasi Inggris + perintah ffmpeg):
> `docs\tiktok-demo-video-script.md` bagian **2**.
> Versi 1 halaman untuk dibaca saat merekam: `docs\tiktok-demo-cheatsheet.md`.

1. Buka aplikasi Chopster di komputer (tampilkan jendelanya)
2. Tunjuk ke tab **Accounts** → klik tombol connect TikTok
3. Halaman login TikTok terbuka → login → halaman izin muncul → klik **Authorize**
   *(poin penting: ini membuktikan Login Kit + user.info.basic)*
4. Kembali ke Chopster → akun TikTok terlihat terhubung
5. Pilih video dari komputer
6. Isi caption/title
   *(catatan penting: di mode **Inbox**, endpoint draft TikTok hanya menerima
   **file video** — judul/caption tidak ikut terkirim dan diisi pengguna di
   aplikasi TikTok. Judul otomatis hanya berlaku di mode Direct Post. Jadi di
   video demo, kolom caption TikTok wajar terlihat kosong.)*
7. Klik publish/upload
8. Tunjukkan proses upload berjalan
9. **Buka TikTok di HP** → tunjukkan video sudah masuk **Inbox / Draft**
   *(poin penting: ini membuktikan Content Posting API + video.upload)*
10. Tekan Post dari aplikasi TikTok

### Tips

- Rekam dengan resolusi jelas, jangan terlalu cepat
- Pastikan **nama akun TikTok** dan **antarmuka Chopster** terlihat
- Jangan pakai video yang menyesatkan — tunjukkan aplikasi yang sebenarnya
- Kalau video kepanjangan, potong bagian menunggu yang tidak penting

---

## C. Produk & Scope yang harus ditambahkan

Di bagian **Products**, tambahkan:

| Produk | Scope | Kegunaan |
|---|---|---|
| **Login Kit** | `user.info.basic` | supaya kreator bisa login & memberi izin |
| **Content Posting API** | `video.upload` | kirim video ke inbox TikTok sebagai draft |

> Jangan tambahkan scope yang tidak dipakai. TikTok menulis: *"If you don't need certain products or scopes, make sure to remove them, otherwise it will delay the review result."*

> Jangan pilih scope `video.publish` kecuali kamu memang mau auto-post langsung. Tanpa audit, video yang dikirim via Direct Post hanya bisa **private** — dan audit untuk tool pribadi **berisiko ditolak**.
