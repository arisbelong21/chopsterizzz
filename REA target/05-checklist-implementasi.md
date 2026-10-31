# 05 — Checklist implementasi

Centang dari atas ke bawah. Jangan lanjut fase berikutnya sebelum fase ini hijau.

## Fase 1 — Fondasi (½ hari)

- [ ] `npm create vite@latest porto -- --template react-ts` (atau `next`)
- [ ] `npm i three @react-three/fiber @react-three/drei @react-three/postprocessing gsap`
- [ ] `<Canvas>` fullscreen, background solid, `dpr={[1, 2]}`
- [ ] 1 mesh kotak + `useFrame` rotasi → pastikan render loop jalan
- [ ] `resize` benar (tidak stretched), `gl={{ antialias: true }}`
- [ ] Cleanup: `dispose` geometry/material saat unmount

## Fase 2 — Aset (1–2 hari)

- [ ] Model glTF dioptimasi: `gltf-transform optimize` (Draco + KTX2, ≤1024²)
- [ ] Cek hasil di https://gltf-viewer.donmccurdy.com (tidak ada artefak)
- [ ] Tekstur diekspor KTX2 (ETC1S untuk diffuse, UASTC untuk normal/UI)
- [ ] Transcoders di-host sendiri di `public/` (`basis_transcoder.wasm`, `draco_decoder.wasm`)
- [ ] `useGLTF(url, true)` + `setKTX2Loader`, `setDRACOLoader` sudah terpasang
- [ ] HDRI ≤1024 (atau `RoomEnvironment` kalau bisa)
- [ ] Total `public/models` + `public/tex` ≤ 3 MB

## Fase 3 — Narasi scroll (2 hari)

- [ ] Pilih satu: drei `<ScrollControls>` **atau** scroll virtual sendiri (`lerpFPS`)
- [ ] Progress 0..1 terhubung ke kamera (bukan ke `document.scrollTop` mentah)
- [ ] Semua gerak pakai `lerpFPS`/`frictionFPS` (FPS-normal), bukan `lerp` mentah
- [ ] Velocity 0..1 dari |Δprogress| → uniform CA/bloom
- [ ] Snap `inOut3` hanya setelah 1,4 s idle, dan dibatalkan oleh input baru
- [ ] 3–5 beat narasi yang jelas (masuk → objek → detail → CTA)
- [ ] Scroll balik (reverse) tetap benar — uji naik-turun 10×
- [ ] Tidak ada layout shift saat canvas muncul

## Fase 4 — Teks & UI (1 hari)

- [ ] Teks konten tetap HTML (bukan MSDF) → bisa di-crawl & di-select
- [ ] Overlay `pointer-events:none`, hanya elemen interaktif yang `auto`
- [ ] Satu font, maksimal 2 weight, `font-display: swap`
- [ ] Minimal satu `<h1>` dan struktur heading berurutan
- [ ] Kontras teks ≥ 4.5:1 di atas canvas (uji dengan screenshot nyata)
- [ ] `og:image` 1200×630 statis (jangan andalkan screenshot canvas)

## Fase 5 — Look (1–2 hari)

- [ ] Postprocessing: `Bloom` selektif + `Noise`/grain halus + `Vignette`
- [ ] Hindari `Bloom` global penuh layar (bikin buram & mahal)
- [ ] Tone mapping `ACESFilmic`, `outputColorSpace = SRGBColorSpace`
- [ ] Palet dibatasi ≤5 warna (tulis hex-nya di satu file `theme.ts`)
- [ ] Bandingkan dengan referensi: gelap/terang, kontras, saturasi — bukan "keren", tapi "konsisten"

## Fase 6 — Performa (1–2 hari)

- [ ] `dpr` di-clamp (≤2 desktop, ≤1.5 mobile)
- [ ] Pause render saat `document.hidden`
- [ ] Pause render saat canvas di luar viewport (`IntersectionObserver`)
- [ ] Budget: transfer awal ≤1,5 MB — ukur di DevTools Network (throttle Fast 4G)
- [ ] LCP ≤2,5 s; CLS <0,1; INP <200 ms
- [ ] Lighthouse mobile ≥70 (situs 3D yang jujur sulit >85)
- [ ] Jumlah draw call <150 (buka `renderer.info.render.calls`)
- [ ] Tidak ada `console.log` tersisa (igloo.inc punya satu — jangan ditiru)

## Fase 7 — Aksesibilitas & fallback (1 hari)

- [ ] `prefers-reduced-motion: reduce` → tampilkan versi statis tanpa animasi
- [ ] WebGL2 tidak tersedia → fallback gambar/HTML penuh
- [ ] Semua tombol bisa diakses keyboard, `focus-visible` terlihat
- [ ] `aria-label` untuk ikon (sound, close, menu)
- [ ] Audio tidak autoplay; tombol mute jelas statusnya
- [ ] `prefers-color-scheme` (opsional tapi murah)

## Fase 8 — Rilis (½ hari)

- [ ] Deploy Vercel/Netlify, custom domain, HTTPS
- [ ] Header cache: `assets/*` → `immutable, max-age=31536000`
- [ ] Test di: Chrome desktop, Safari iOS nyata, Android mid-range nyata
- [ ] Cek di jaringan lambat (throttle) — apakah preloader jujur soal progres?
- [ ] Analytics ringan (Plausible/Umami), bukan Google Analytics berat
- [ ] Simpan snapshot OG + preview link (LinkedIn/X card)

## Red flags — berhenti kalau salah satu terjadi

- Transfer awal >3 MB untuk portofolio pribadi
- FPS <30 di HP mid-range dan tidak ada fallback
- Teks tidak bisa di-select / tidak ada di DOM
- Animasi tidak bisa dimatikan
- Kamu menghabiskan 2 minggu untuk shader sebelum ada satu kata konten tertulis
- Ada request ke CDN pihak ketiga yang memblokir render
