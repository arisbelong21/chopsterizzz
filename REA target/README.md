# REA target — Bedah `www.igloo.inc`

Analisis dengan **MCP REA**: passive CDP observation, kontrol browser (launch + connect), static bundle
analysis, dan export Evidence bundle resmi. Bukan "view-source".

Target: `https://www.igloo.inc/` · Analisis: 27 Oktober 2026

## Isi folder

| File | Isi |
|---|---|
| `01-teardown.md` | **Baca ini dulu.** Arsitektur, mekanisme per layer, tech stack, bukti, batasan |
| `02-tech-stack.json` | Stack + teknik + evidence dalam format mesin |
| `03-network-assets.md` | 105 request, ukuran per file, pipeline Draco/KTX2 |
| `04-blueprint-portofolio.md` | **Rencana bangun:** 4 level, 4 pilihan stack, pipeline aset, budget, 10 hari kerja |
| `05-checklist-implementasi.md` | Checklist siap-eksekusi per fase |
| `06-bedah-gerak-dan-suara.md` | **Bedah v2:** rantai scroll berinersia, kamera 3 lapis, snap 1,4 s, shader cut antar-scene, 18 file audio, + rencana adaptasi Tier A/B/C |
| `shots/` | **14 screenshot asli** (GPU nyata) + `index.html` sebagai galeri |
| `evidence/bundle.json` | Evidence bundle REA lengkap (21,7 MB): 4 capture, 324 event, semua screenshot base64 |

Buka `shots/index.html` di browser untuk melihat semua frame berurutan beserta keterangannya.

## Ringkasan 10 detik

Bukan website. Ini **satu canvas WebGL fullscreen**: HTML 1,4 KB, `<body>` hanya
`<div id="app"><div id="webgl"><div><canvas data-engine=…></div></div></div>` — total **37 node DOM**.
Semua teks, logo, dan tombol di dalam 3D (MSDF), bukan HTML.

Dibuat studio **[abeto.co](https://abeto.co)**, hosting **Vercel** di belakang **Cloudflare**.

**three.js r165** + **GSAP 3.12.5**/ScrollTrigger + **@pmndrs/postprocessing** +
**three-msdf-text-utils** + **three-mesh-bvh**. Geometri **Draco `.drc`** (20 file, 543 KB),
tekstur **KTX2/Basis + Zstd** (±6 MB), 4 Web Worker untuk decode.

Mekanisme intinya: **scroll → data texture → shader**, volumetrik dari **VDB → 3D texture KTX2** yang
di-ray-march, plus GPGPU advection. Semua aset di-preload dalam **satu gelombang** (98 request)
sebelum curtain dibuka — tidak ada lazy-load per section.

Geraknya sendiri bukan "scroll di-scrub", tapi **scroll virtual berinersia**: delta wheel → dua lapis
`lerpFPS` (0.075 → 0.15) → `progress` per scene, plus kamera yang punya parallax bola dan noise shake
supaya tidak pernah diam. Angka lengkap + rencana adaptasi: `06-bedah-gerak-dan-suara.md`.

## Dua koreksi dari analisis awal

1. **Bukan glTF.** Tidak ada file `.glb`/`.gltf` sama sekali. Geometri dikirim sebagai **Draco mentah
   (`.drc`)**, 20 file terpisah, di-decode via `draco_decoder.wasm` lalu dirakit jadi `BufferGeometry`.
   Capture pertama (headless) tidak menangkapnya karena render gagal lebih awal.
2. **Bukan preload bertingkat.** Saya sempat menduga ada manifest bertingkat dari urutan request.
   Capture ber-GPU membuktikan sebaliknya: **98 request terjadi dalam satu burst** di satu step,
   termasuk semua tekstur dan geometri. Urutan = urutan antrean loader, bukan prioritas per section.

`igloo-landing.png` (screenshot headless) sudah dihapus — hasilnya warna rata karena SwiftShader,
dan bisa menyesatkan. Penggantinya `shots/`.
