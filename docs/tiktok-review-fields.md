# TikTok Production — teks siap tempel untuk App review

Semua teks di bawah **sudah dihitung panjangnya** supaya tidak kena batas karakter.
Salin **apa adanya** (jangan diubah), karena isinya harus cocok dengan aplikasi
yang sebenarnya dan dengan video demo.

> Catatan: Chopster dijalankan **hanya di komputer pengguna sendiri** dan tidak
> punya server. Semua teks di bawah konsisten dengan itu.

---

## 0. App details / Basic information

| Kolom | Isi |
|---|---|
| **App name** | `Chopster` |
| **Category** | `Productivity` ← **bukan** Entertainment |
| **Platforms** | centang \(\checkmark\) **Desktop** |
| **Terms of Service URL** | `https://<GITHUB_USERNAME>.github.io/terms.html` |
| **Privacy Policy URL** | `https://<GITHUB_USERNAME>.github.io/privacy.html` |
| **Web or Desktop URL** | `https://<GITHUB_USERNAME>.github.io/` |
| **Redirect URI** | `http://localhost:51234/` |

### Description (batas ±120 karakter)

<!-- TEKS:desc -->
A desktop tool for creators to prepare and publish their own original videos to their own TikTok account.
<!-- /TEKS -->

---

## 1. Kolom utama

**Label di portal:** *"Explain how each product and scope works within your app or website."*
**Batas:** 1000 karakter.

<!-- TEKS:utama -->
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
<!-- /TEKS -->

---

## 2. Login Kit — cara login dipakai

**Label yang mungkin muncul:** *"Describe how your app uses Login Kit"* /
*"How does the login flow work in your app?"*

<!-- TEKS:login -->
The creator connects their own TikTok account from the Chopster desktop app. Chopster opens TikTok's official authorisation page in the system browser using OAuth 2.0 with PKCE. The creator signs in on TikTok's own website and approves the request, so Chopster never sees or stores the TikTok password. TikTok then redirects back to http://localhost:51234/ with an authorisation code, which Chopster exchanges for an access token. The token is kept only on the creator's own computer.
<!-- /TEKS -->

---

## 3. Content Posting API — cara upload dipakai

**Label yang mungkin muncul:** *"Describe how your app uses the Content Posting API"* /
*"How does your app upload videos?"*

<!-- TEKS:upload -->
The creator picks one of their own video files stored on their own computer. Chopster sends that file to the Content Posting API upload endpoint, which places it in the creator's own TikTok inbox as a draft. In this mode no title, caption or privacy setting is transmitted; the creator opens the TikTok app, reviews the draft and finishes the post there by tapping Post. Chopster requests only video.upload (send to inbox) and never uses Direct Post (video.publish), so it cannot publish anything without the creator's own action inside the TikTok app.
<!-- /TEKS -->

---

## 4. Kalau ditanya soal data pengguna

### 4a. Data apa yang disimpan, dan di mana

<!-- TEKS:data -->
Chopster runs entirely on the creator's own computer and operates no server of its own. The only TikTok data it stores is the OAuth access token plus the connected account's display name, which is shown so the creator can confirm the right account is selected. Both are saved in the app's local configuration folder on that same computer. No TikTok data is sent to the developer or to any third party, and no analytics are collected.
<!-- /TEKS -->

### 4b. Cara pengguna menghapus data / mencabut izin

<!-- TEKS:hapus -->
The creator can disconnect the account at any time from the Accounts page inside Chopster, which deletes the stored token from the local configuration on their computer. Access can also be revoked from TikTok itself under Settings and privacy, Security and permissions, Manage app permissions. Revoking it there immediately invalidates the token that Chopster holds.
<!-- /TEKS -->

---

## 5. Kalau ditanya soal kepemilikan konten

**Label yang mungkin muncul:** *"Does your app allow users to post content they do not own?"*

<!-- TEKS:konten -->
No. Chopster only uploads video files that the creator already has on their own computer, and only to that same creator's own TikTok account. It does not download or repost content from TikTok or from any other user, it has no content library of its own, and it cannot post on behalf of anyone else.
<!-- /TEKS -->

---

## 6. Kalau ditanya metode posting

**Label yang mungkin muncul:** *"Which posting method does your app use?"*

```
Upload to Inbox (draft) — not Direct Post.
```

Alasan singkat (kalau ada kolom penjelasan):

<!-- TEKS:metode -->
Upload to Inbox only. The creator finishes the post inside the TikTok app, so Chopster never publishes on the creator's behalf. Direct Post is not enabled and the video.publish scope is not requested.
<!-- /TEKS -->

---

## 7. Lampiran video

| Item | Nilai |
|---|---|
| File | `C:\Users\<USER>\Videos\chopster login kit.mp4` |
| Durasi | 3 menit 16 detik |
| Ukuran | 40,7 MB (batas 50 MB) |
| Resolusi | 1920×1080, 30 fps |
| Format | MP4 / H.264 + AAC stereo |

Video harus **satu** file utuh yang menunjukkan alur dari login sampai draft
muncul di TikTok (lihat `docs\tiktok-app-review.md` bagian B).

---

## 8. Jangan lupa (sering bikin submit gagal)

1. **Produk di Production harus sama** dengan sandbox: Login Kit +
   Content Posting API, scope `user.info.basic` + `video.upload`.
   Scope yang tidak dipakai **harus dihapus**.
2. **Direct Post harus OFF** di Production juga.
3. **URL properties harus terverifikasi di mode Production**, bukan hanya
   sandbox. Kalau di Production masih tertulis belum terverifikasi, klik
   **Verify properties** → metode **URL prefix** → file signature sudah ada di
   repo, jadi cukup klik Verify.
4. **Klik Apply changes** setelah mengubah produk/scope, kalau tidak
   perubahannya tidak ikut dinilai.
5. **Jangan tukar Client ID/Secret Chopster** ke Production sebelum app-nya
   benar-benar disetujui.
