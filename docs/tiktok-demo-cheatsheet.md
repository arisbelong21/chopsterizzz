# Cheat Sheet — Rekam Demo TikTok (1 halaman)

Buka file ini di layar kedua / HP saat merekam. Naskah lengkap:
`docs\tiktok-demo-video-script.md`.

**Sebelum mulai:** video uji di `C:\TikTokDemo\video-uji.mp4` · bahasa TikTok = English ·
Do Not Disturb ON · Chopster terbuka · TikTok **● Connected** · OBS 1920×1080 30fps + mic.

> ⚠️ **Folder `C:\TikTokDemo` masih KOSONG.** Copy video aslimu ke sana dengan nama
> `video-uji.mp4` **sebelum merekam**. Kalau tidak, Shot 8 (Pilih file…) akan
> membuka folder `C:\Users\NAMA-USER-ASLI\Downloads\...` — nama user Windows-mu
> ikut terekam. Ini bukan untuk keperluan API, murni supaya rekamannya bersih.

| # | Waktu | Lakukan | Narasi (EN) |
|---|-------|---------|-------------|
| 1 | 0:00 | Kartu judul | "This is Chopster, a desktop app that runs on a creator's own computer. I'll show the TikTok Login Kit and Content Posting API, recorded in the TikTok Sandbox." |
| 2 | 0:07 | Tampilkan desktop → klik jendela **Chopster**; sorot judul + sidebar | "It runs entirely on my own computer. No server, no website. For this demo I only use the Automation tab." |
| 3 | 0:18 | **Automation** → tab **Accounts**; zoom baris TikTok **● Connected** | "TikTok is connected. To show the authorisation flow, I'll disconnect and reconnect it." |
| 4 | 0:30 | Klik **Disconnect** di baris TikTok → setujui | "I disconnect, so you can see the full login flow." |
| 5 | 0:40 | Klik **Connect** → **berhenti 2–3 detik** di halaman izin → sorot 2 izin → **Lanjutkan** → tunjukkan `localhost:51234` | "Chopster opens TikTok's own authorisation page. I sign in on TikTok's website — Chopster never sees my password. TikTok shows two permissions: my basic profile, and uploading a draft to my own inbox. I approve." |
| 6 | 1:05 | Kembali ke Chopster → baris TikTok **● Connected** + nama | "The display name was read with the user.info.basic scope." |
| 7 | 1:15 | **Settings** → baris **TikTok publish path** = **Inbox draft (aman, video.upload)** → zoom | "Publish path is Inbox draft, which uses video.upload. Direct Post is off. Chopster only delivers drafts." |
| 8 | 1:28 | **Automation** → **Create Automation** → sumber **Manual Import** → **Pilih file…** → `video-uji.mp4` | "My own video file from my own computer. Imported manually." |
| 9 | 1:50 | Klik baris video → isi **Judul** + **Caption** | "I type the title and caption myself. TikTok's draft API takes only the video — I write the TikTok caption in the app at the end." |
| 10 | 2:08 | **Lanjut ▶** → centang **hanya TikTok** → **Lanjut ▶** → **Publish immediately** → **Lanjut ▶** → lihat ringkasan | "Only TikTok. The review shows exactly what this run will do." |
| 11 | 2:22 | Klik **Mulai Automation ▶** → OK → pindah otomatis ke **Queue** | "Nothing happens automatically. I click Start." |
| 12 | 2:32 | Queue: **Queued → Running → Action required** + chip **"⚠ Draft siap — publish di app"**. Progress berhenti di **88%** ±45 detik — tunggu, jangan panik (klik **↻ Muat Ulang** bila perlu) | "It finishes as Action required — Draft ready. The video went to my TikTok inbox as a draft through the official Content Posting API. Chopster did not publish." |
| 13 | 2:50 | HP: app TikTok → **Inbox** → draft (thumbnail ada; kolom caption **kosong** — memang begitu) → **ketik caption** → lihat tombol **Post** | "The draft is in my inbox — video only, because the upload API carries just the file. I write the caption here myself. Nothing is public yet." |
| 14 | 3:15 | *(opsional)* ketuk **Post** | "I post it myself. The app never published for me." |
| 15 | 3:25 | Kartu penutup | "Chopster delivered my own video to my own account as a draft using TikTok's official APIs — and I published it myself." |

**Ekspor ≤ 50 MB**

```powershell
cd C:\TikTokDemo
ffmpeg -i rekaman-awal.mp4 -c:v libx264 -crf 26 -preset medium -pix_fmt yuv420p `
  -c:a aac -b:a 128k -movflags +faststart demo-chopster-tiktok.mp4
```

**Jangan terekam:** Content Clipper · Auto Clip Studio · platform selain TikTok ·
kartu DEVELOPER CONFIGURATION · folder pribadi · notifikasi.
