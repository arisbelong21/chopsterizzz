# 04 — Blueprint: membangun portofolio bergaya igloo.inc

Tujuan: website portofolio dengan **satu canvas WebGL + narasi scroll**, tapi dengan  
keputusan teknis yang sadar (tidak sekadar menyalin 6,4 MB aset).

---

## Langkah 0 — Tentukan dulu: kamu butuh yang mana?

Ada 4 level. Jangan lompat ke level 4 kalau level 2 sudah cukup.

| Level                 | Hasil                                                              | Bobot    | Waktu                       | Kapan dipakai                                               |
| --------------------- | ------------------------------------------------------------------ | -------- | --------------------------- | ----------------------------------------------------------- |
| **L1 — Editorial**    | HTML/CSS biasa + animasi scroll ringan (GSAP/IntersectionObserver) | <300 KB  | 2–4 hari                    | Portofolio kerja yang harus dibaca rekruter/di-crawl Google |
| **L2 — Hybrid**       | Halaman normal + *satu* hero 3D (model atau shader)                | 1–1,5 MB | 1–2 minggu                  | **Rekomendasi utama.** 80% efek, 20% biaya                  |
| **L3 — Scene-driven** | Full-screen canvas, teks sebagai HTML overlay, 3–4 scene           | 2–3 MB   | 3–5 minggu                  | Portofolio kreatif/studi desain                             |
| **L4 — Igloo-class**  | Full WebGL, UI in-canvas, volumetrik, custom shader                | 6–10 MB  | 2–4 bulan (1 senior 3D web) | Showreel studio, bukan portofolio pribadi                   |

Kalau tujuanmu "portofolio pribadi biar dilirik studio", **L2**. Kalau "biar orang ingat", **L3**.

---

## Langkah 1 — Stack

### Opsi A — Mirip igloo.inc (faithful)

```
Vite + Svelte (atau React) + three.js + GSAP (tween/snap saja)
+ @pmndrs/postprocessing + three-msdf-text-utils + three-mesh-bvh
+ KTX2 (Basis/Zstd) + Draco
```

> Scroll bawaan igloo **bukan** ScrollTrigger — lihat `06-bedah-gerak-dan-suara.md` §1.
> Mereka memakai scroll virtual + `lerpFPS` sendiri; GSAP hanya untuk snap `inOut3`.

- Kelebihan: kontrol penuh, hasil maksimal, bundle kecil per fitur.
- Kekurangan: kamu menulis sendiri semua lifecycle (dispose, resize, load, fallback).
- Cocok kalau kamu nyaman dengan three.js murni.

### Opsi B — React Three Fiber (paling produktif)

```
Next.js (atau Vite + React) + @react-three/fiber + @react-three/drei
+ @react-three/postprocessing + drei <Text>/<ScrollControls> + GSAP
```

- Kelebihan: `useFrame`, `useGLTF`, `useTexture` menghapus 70% boilerplate;  
  `ScrollControls` + `useScroll` sudah menyelesaikan masalah scroll→3D.
- Kekurangan: React runtime + sedikit overhead; harus disiplin `dispose`.
- **Ini yang saya sarankan untuk L2/L3.**

### Opsi C — Tool visual

```
Spline (scene + export web) → embed di Next.js/Astro
```

- Kelebihan: cepat, tanpa shader manual, animasi visual.
- Kekurangan: bundle Spline runtime besar (±1–2 MB), kontrol performa terbatas,  
  sulit dioptimasi untuk mobile low-end.

### Opsi D — Tanpa 3D engine

```
GSAP + CSS 3D transform + <canvas> 2D untuk grain/particles
```

- Kelebihan: 50–150 KB, jalan di semua device, SEO aman.
- Kekurangan: tidak bisa volumetrik/lighting realistis. Tapi **banyak situs "mahal" cuma pakai ini.**

---

## Langkah 2 — Struktur project (Opsi B, L2/L3)

```
src/
├── app/                        # routing / halaman
├── components/
│   ├── canvas/
│   │   ├── Scene.tsx           # <Canvas> root: dpr, gl props, fallback
│   │   ├── CameraRig.tsx       # kamera dikendalikan scroll
│   │   ├── HeroObject.tsx      # model utama (glTF)
│   │   ├── Effects.tsx         # Bloom / DOF / Noise postprocessing
│   │   └── Env.tsx             # HDRI (kecil! 512–1024 px, .hdr atau .exr)
│   └── ui/
│       ├── Overlay.tsx         # teks HTML asli (SEO + aksesibilitas)
│       └── Preloader.tsx       # curtain, tunggu <Suspense> selesai
├── lib/
│   ├── scroll.ts               # wheel → progress 0..1 (damping lerpFPS, clamp, snap)
│   ├── useAssetPreload.ts
│   └── device.ts               # deteksi GPU/tier → turunkan kualitas
└── assets/                     # sumber mentah (glb, hdr, font)
public/models/                  # hasil optimasi (draco/ktx2)
```

**Aturan penting:** teks tetap HTML di atas canvas (`position:absolute`, `pointer-events:none`  
untuk area canvas). Igloo merender teks di dalam 3D — hasilnya keren, tapi situsnya  
tidak punya satu kata pun yang bisa dibaca Google. Untuk portofolio pribadi itu bunuh diri.

---

## Langkah 3 — Alur kerja narasi scroll

```
scroll 0.00 ─── 0.25 ─── 0.50 ─── 0.75 ─── 1.00
   hero      objek berputar  kamera masuk  detail  CTA/kontak
```

Implementasi minimal (R3F):

```tsx
// CameraRig.tsx
const scroll = useScroll()                       // drei
useFrame((state) => {
  const p = scroll.offset                        // 0..1 halus
  state.camera.position.z = lerp(6, 1.6, easeOutCubic(p))
  state.camera.position.y = Math.sin(p * Math.PI) * 0.8
  state.camera.lookAt(0, 0, 0)
})
```

Di igloo.inc mereka bahkan meng-*bake* kurva scroll ke texture (`scroll-datatexture.ktx2`, 1,26 MB)  
supaya bentuk kurva bisa didesain di Figma/Houdini. **Kamu tidak perlu itu.** Uniform `float` cukup,  
kecuali ada 20+ parameter animasi yang harus disinkronkan per-frame.

---

## Langkah 4 — Pipeline aset (ini yang bikin beda)

### 4.1 Model

```bash
# glTF → glTF terkompresi (Draco + Meshopt), tekstur KTX2
npx gltf-transform optimize hero.glb hero.opt.glb \
  --compress draco --texture-compress ktx2 --texture-size 1024

# alternatif: gltfpack (meshopt, lebih ringan untuk mobile)
gltfpack -i hero.glb -o hero.glb -cc -tc
```

### 4.2 Tekstur → KTX2

```bash
# UASTC (kualitas tinggi, file lebih besar) — untuk normal map / UI tajam
basisu -ktx2 -uastc -uastc_level 2 -file normal.png

# ETC1S (paling kecil) — untuk warna diffuse / background
basisu -ktx2 -q 128 -file diffuse.png

# atau via toktx
toktx --t2 --encode etc1s --clevel 4 --qlevel 128 out.ktx2 in.png
```

### 4.3 Volume (kalau mau asap/awan/wajah volumetrik)

```
Houdini/EmberGen → .vdb → (tool internal / OpenVDB → 3D texture) → .ktx2 3D
→ shader raymarch dengan loop terbatas (32–64 step) + blue-noise dither
```

Ini level L4. Alternatif murah: **billboard sprite sequence / signed distance field 2D** yang  
dipalsukan dengan noise — 90% look, 10% biaya.

### 4.4 Font 3D (kalau teks harus di dalam scene)

```bash
# MSDF atlas
msdf-bmfont -f json -o font.png -m 512,512 -s 42 font.ttf
# lalu pakai three-msdf-text-utils / drei <Text> (drei pakai troika, lebih mudah)
```

### 4.5 HDRI

Turunkan ke 1K, `hdr`/`exr` dikompres → atau pakai `RoomEnvironment` bawaan three (0 byte).

---

## Langkah 5 — Budget performa (target realistis)

| Metrik                      | Target portofolio      | Igloo.inc                     |
| --------------------------- | ---------------------- | ----------------------------- |
| Total transfer awal         | ≤1,5 MB                | ±6,4 MB+                      |
| Main JS (gzip)              | ≤250 KB                | ±400 KB                       |
| Waktu sampai scene terlihat | ≤2,5 s (4G)            | lebih lama (curtain menutupi) |
| FPS                         | 60 desktop / 30 mobile | 60 desktop                    |
| Jumlah draw call            | <150                   | banyak                        |
| Tekstur                     | ≤1024², KTX2           | 2048–4096, KTX2               |
| Device low-end              | fallback gambar statis | tidak ada fallback            |

**Wajib ada (igloo tidak punya):**

```tsx
// 1. Hormati preferensi user
const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches
if (reduce) return <StaticHeroImage />

// 2. Deteksi GPU lemah → turunkan DPR / matikan postprocessing
<Canvas dpr={[1, tier === "low" ? 1 : 2]} gl={{ antialias: false, powerPreference: "high-performance" }} />

// 3. Pause saat tab tidak aktif (igloo sudah: visibilitychange)
useEffect(() => {
  const onVis = () => setPaused(document.hidden)
  document.addEventListener("visibilitychange", onVis)
  return () => document.removeEventListener("visibilitychange", onVis)
}, [])

// 4. Fallback kalau WebGL2 tidak ada
if (!document.createElement("canvas").getContext("webgl2")) return <StaticHeroImage />
```

---

## Langkah 6 — Urutan implementasi (L2, 10 hari kerja)

| Hari | Kerjaan                                                     | Output                 |
| ---- | ----------------------------------------------------------- | ---------------------- |
| 1    | Setup Vite/Next + R3F, canvas kosong, 1 kotak berputar      | pipeline hidup         |
| 2    | Preloader + `Suspense` + reveal curtain                     | tidak ada flash kosong |
| 3    | Import model glTF hasil optimasi, lighting + HDRI 1K        | hero object tampil     |
| 4    | Scroll → progress (damping FPS-normal) → kamera              | narasi gerak           |
| 5    | Teks HTML overlay + tipografi (2 font, disiplin)            | konten terbaca & SEO   |
| 6    | Postprocessing: bloom selektif + grain + vignette halus     | look "film"            |
| 7    | Optimasi: KTX2, DPR clamp, dispose, pause on hidden         | <1,5 MB                |
| 8    | Mobile: turunkan kualitas, uji device nyata                 | 30 fps stabil          |
| 9    | Aksesibilitas: reduced-motion, alt text, keyboard, fallback | lolos audit dasar      |
| 10   | Deploy (Vercel) + analytics ringan + OG image               | live                   |

---

## Langkah 7 — Yang benar-benar membuat igloo.inc terasa mahal (dan murah ditiru)

1. **Disiplin tipografi** — satu font (IBM Plex Mono), dua weight saja.
2. **Palet terbatas** — biru-es, putih, hampir tanpa aksen.
3. **Preloader yang menutupi loading**, bukan spinner.
4. **Transisi yang tidak pernah "berhenti"** — selalu ada gerakan halus di background.
5. **Interaksi kecil yang menghargai user** — "Click to explore" memberi izin, bukan memaksa.
6. **Audio opt-in**, bukan autoplay.
7. **Konsistensi material** — semua objek punya bahasa permukaan yang sama (frosty/glass).
8. **Detail sisa developer pun ada** (`REMOVE AFTER PROJECTS ARE COMPLETED`) — bukti bahwa  
   yang bikin terasa "sempurna" itu bukan kesempurnaan kode, tapi keputusan visual.

---

## Yang tidak perlu kamu tiru

- UI 100% di dalam canvas (SEO & aksesibilitas mati).
- 6,4 MB tekstur untuk satu halaman.
- Tanpa `prefers-reduced-motion`.
- Tanpa fallback WebGL.
- Datatexture untuk hal yang bisa jadi uniform.

---

## Estimasi biaya kalau diserahkan ke studio

| Lingkup                                                                   | Range                   |
| ------------------------------------------------------------------------- | ----------------------- |
| L1 editorial premium                                                      | Rp 8–20 juta            |
| L2 hybrid 3D hero                                                         | Rp 30–70 juta           |
| L3 scene-driven 3–4 scene                                                 | Rp 90–200 juta          |
| L4 igloo-class (volumetrik + custom shader + sound design)                | Rp 300 juta – 1 miliar+ |
| Referensi: situs seperti ini biasanya diproduksi tim 3–5 orang, 2–4 bulan | —                       |
