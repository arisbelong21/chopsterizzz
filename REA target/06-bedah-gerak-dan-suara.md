# 06 — Bedah gerak & suara `igloo.inc` (v2)

Sumber: bundle publik `assets/App3D-d68cb154.js` (1.486.303 byte; nama build hash-nya **sama**
dengan yang tercatat di `01-teardown.md` → situs belum berubah), `assets/index-2e130bc1.js`
(16.5 KB), dan `index.html`. Hash SHA-256 ketiga file ada di `evidence/sumber-bundle-v2.md`.
Metode: unduh → 14 pemindaian terarah (konstanta, shader, audio, event) → semua angka di bawah
**dikutip dari kode**, bukan perkiraan. Tidak ada eksploitasi; hanya aset publik.

---

## 0. Dua koreksi untuk `01-teardown.md`

1. **Tidak ada native scroll.** `html, body { overflow: hidden; touch-action: none }` (di-inject
   dari `entry.js`), wheel ditangkap `addEventListener("wheel", …, { passive: false })` lalu
   `preventDefault()`. Halaman **tidak pernah** bergeser; `window.scrollY` selalu 0.
   Jadi §4.1 "native scroll + GSAP ScrollTrigger" **salah**.
2. **ScrollTrigger dan GSAP praktis tidak dipakai untuk kamera.** Di bundle hanya ada **2**
   kemunculan `ScrollTrigger` (keduanya internal library), **0** `scrub`, **0** `pin`,
   **0** `gsap.to(`. GSAP dipakai untuk satu hal: tween `auto-center` setelah scroll berhenti.
   Kamera digerakkan oleh **loop rAF + lerp berbasis FPS**, bukan timeline yang di-scrub.

Ini penting karena mengubah cara menirunya: yang perlu ditiru bukan "timeline panjang", tapi
**rantai peredaman nilai scroll**, lalu tiap bab membaca `progress` sendiri.

---

## 1. Rantai gerak: dari jari ke piksel

Semua di `class` pengendali scroll (`this.scroll = { total, targetY1, targetY2, y, velocity }`).

| # | Tahap | Kode (disederhanakan) | Angka | Efeknya |
|---|---|---|---|---|
| 1 | Input | `targetY2 += deltaY * scrollMultiplier` | `0.00075` | satu notch roda (±100 px) ≈ 0,075 bab; 1 bab = 1 unit |
| 2 | Redam 1 | `targetY1 = lerpFPSLimited(targetY1, targetY2, .075, 100 * mult)` | `0.075`, batas `0.075` unit/frame | nilai pertama yang "mengejar" input, dengan **batas kecepatan** |
| 3 | Redam 2 | `y = lerpFPS(y, targetY1, .15)` | `0.15` | posisi visual; dua lapis = akselerasi + ekor |
| 4 | Rem | `targetY2 = clamp(targetY2, y - t, y + t)` | `t = 750 * mult = 0.5625` | input **dijinakkan**: target tidak boleh lebih dari 0,56 bab di depan yang terlihat |
| 5 | Deadzone | `if |y - targetY2| < 0.1 * mult` → samakan | `0.000075` | berhenti tepat, tanpa sisa getar |
| 6 | Velocity | `v += |y - yPrev|; v *= frictionFPS(.98); clamp(0,1); v<.001 → 0` | `0.98` | satu angka 0..1 untuk semua efek "kencang" |

Fungsi peredamnya bukan `lerp(a,b,t)` biasa, tapi **lerp yang dinormalisasi waktu**:

```js
damp(k, fpsRatio)   = 1 - Math.exp(Math.log(1 - k) * fpsRatio)
lerpFPS(a, b, k)    = a + (b - a) * damp(k, fpsRatio)
frictionFPS(k)      = Math.exp(Math.log(k) * fpsRatio)
```

`fpsRatio = deltaTime * 60`. Artinya rasa gerak **sama** di 60 Hz, 120 Hz, dan 144 Hz.
Kalau pakai `lerp(a, b, 0.1)` mentah, di 144 Hz gerakan jadi 2,4× lebih cepat — ini kesalahan
paling umum di situs scroll-driven, dan igloo tidak melakukannya.

**Kenapa terasa "berat tapi responsif":** ada tiga gerbang berbeda — batas kecepatan redam 1,
clamp target, dan deadzone. Semua mencegah "whip" saat user menggebrak trackpad, tapi tidak
menahan gerakan kecil. Ditambah `y` selalu mengejar `targetY1` (bukan `targetY2`), jadi masih ada
ekor halus setelah input berhenti.

---

## 2. Kamera: tiga lapis, semuanya lerp

`basePosition` / `baseTarget` diisi per-scene dari `progress` (koreografi). Di atasnya:

1. **Parallax mouse di bola, bukan translasi.**
   `position = baseTarget + rotate(basePosition − baseTarget, phi, theta)`.
   `theta target = fit(mouseX, −1…1, −90°…90°) × 0.07` → **±6,3°**;
   `phi target = ±90° × 0.025` → **±2,25°**.
   Diperhalus `lerpFPS(…, 0.035)` → butuh ±1,5–2 detik sampai benar-benar tenang. Kursor tidak
   "menempel", kamera **berayun** ke arah kursor lalu berhenti.
2. **Dua sphere terpisah**: satu untuk posisi, satu untuk target (`displacement.target`), dengan
   `lerpTarget = 0.035`. Kalau keduanya digeser sama besar, tidak ada parallax; karena target
   bergeser lebih lambat, latar dan teks punya kedalaman berbeda. (Di build ini `displacement.target`
   dibiarkan 0 — mekanismenya ada, tapi tidak diaktifkan.)
3. **Noise shake — ini kunci "tidak pernah diam".**
   `shake.setScalar(0.01…0.02)` rad, `shakeSpeed 0.1…0.5`, lalu:

   ```js
   offsetPhi   = sineNoise1(12.23, 3.44, -3.234 + time * shakeSpeed.x) * shake.x
   offsetTheta = sineNoise1(-2.45, 4.789, 7.343 + time * shakeSpeed.y) * shake.y
   roll        = sineNoise1(23.434, -1.565, 8.454 + time * shakeSpeed.z) * shake.z
   ```

   `sineNoise1` = noise halus dari beberapa sinus bertingkat (bukan satu `sin(t)`), jadi tidak
   pernah mengulang pola yang sama dan tidak terlihat seperti getaran. Amplitudo 0,01 rad ≈
   **0,57°** — nyaris tak terasa sebagai gerakan, tapi cukup membuat frame tidak pernah identik.
   Inilah bedanya "render mati" vs "hidup".
4. **Rate override per scene**: `0.035 / 0.035 / 0.075` (default), `0.02 / 0.02 / 0.015` untuk
   scene lambat, `0` untuk scene UI (kamera dikunci). FOV 30 dan 25 — tele, bukan wide.
5. `touchAmount` turun ke 0,5 saat layar disentuh lalu diam → mobile lebih tenang.

---

## 3. Snap 1,4 detik (auto-center)

```js
if (targetY2 !== lastTarget) { needed = true; lastTarget = targetY2; lastTime = now }
// di render loop:
if (needed && !animating && now - lastTime > 1.4) {
  needed = false
  if (progress % 1 !== 0) centerScroll(nearestBoundary, duration)
  else scene.autoCenter(progress)
}
centerScroll(y, d) → gsap.to(scroll, { y: round(y), duration: d, ease: "inOut3" })
StopAutoCenter() dipanggil di setiap wheel/keydown → animasi langsung dibunuh.
```

Yang membuat snap ini tidak mengganggu: (a) menunggu **1,4 detik** setelah input terakhir,
(b) hanya kalau posisi memang **tidak** di batas bab, (c) durasi `2` detik (ditambah offset khusus
scene), (d) setiap sentuhan baru langsung mematikan tween. Rasanya "dibantu", bukan "dibajak".

Ease `inOut3` milik mereka: `M0,0 C0.6,0 0,1 1,1` — mulai cepat, ekor panjang.

---

## 4. Satuan gerak = **scene**, bukan halaman

Scroll bukan 1 dokumen panjang, tapi **tumpukan scene**:

```
scrollComposers = [entry, cubes, igloo]     // 3 scene, masing-masing punya `height`
scene.__top / __bottom dihitung ulang tiap frame
progress = (y − top) / (height + 1)
dua scene terdekat dirender SEKALIGUS saat transisi
```

Tiap scene punya kamera, komposisi, dan bloom sendiri; `isSceneVisible` mematikan scene di luar
rentang (hemat GPU + audio ikut mati). Scene intro memutar **timeline sekali jalan**
(`timeline.play(0)`) lalu diserahkan ke scroll — intro tidak bisa di-scroll mundur.

### Transisi antar scene: bukan crossfade

Shader komposit (`f3`) memotong dua tekstur scene dengan **garis diagonal ber-noise**:

```glsl
slope     = -0.2 * aspect;
slopeDisp = (scrollTex.b * 2.0 - 1.0) * 0.4;          // tepi potongan jadi bergerigi
inclination = mix(1.0 - vUv.x + slopeDisp, vUv.x + slopeDisp, step(slope, 0.0));
cutDiagonal = falloff(vUv.y + inclination * abs(slope), 0.0, 1.0, 0.2, incProgress);
cut         = falloff(scrollTex.r, 0.0, 1.0, 2.0, cutDiagonal);  // tepi di-threshold noise
// parallax + CA hanya di sekitar tepi
scene1 = chromatic_aberration(tScene1, vUv - vec2(0, 0.4*power2In(uProgress) + 0.025*cutDisp), …)
scene2 = chromatic_aberration(tScene2, vUv + vec2(0, 0.4*power2In(1-uProgress) + 0.025*(1-cutDisp)), …)
color  = mix(scene1, scene2, cut);
```

Tiga hal yang membuatnya terasa premium: **parallax vertikal 0,4** (scene lama turun, baru naik),
**CA 5 iterasi** yang hanya kuat di tepi dan di pinggir layar (`modulator = 12 × edge`), dan
**blue noise** untuk menyembunyikan pita CA. Semua digerakkan `uProgress` + `uProgressVel`.

---

## 5. Teks: dua kecepatan, per huruf

Teks di dalam WebGL (MSDF), tiap huruf punya bobot di vertex attribute. Reveal memakai **dua**
uniform: `uShow1` (cepat) dan `uShow2` (lambat), di-threshold dengan `falloff(weight, 0, 1, …)` →
huruf muncul **berurutan**, bukan sekaligus. Ada `uBlink` untuk kursor. Panel proyek punya scroll
sendiri (`lerpFPS(…, 0.1)`, margin 65 px), jadi teks panjang tidak mengganggu scroll utama.

`playBeep()` dipanggil saat teks mengetik: pilih acak dari 3 varian, dibatasi **400 ms**.

---

## 6. Suara: 3 lapis, amplopnya dari progress

Semua sampel `.ogg` (18 file), bukan sintesis: tidak ada `OscillatorNode`/`AudioBuffer` di bundle.

| Lapis | File | Volume | Cara naik-turun |
|---|---|---|---|
| Musik | `music-highq.ogg` | 0.20 | konstan, loop |
| Ruang | `room.ogg` | 0.45 | konstan, loop (ambience) |
| Angin | `wind.ogg` | 0 → **0.4** | `fit(p,.05,.2,0,1) × fit(p,.75,.95,1,0)` |
| Igloo | `igloo.ogg` | 0 → **0.5** | kedekatan objek |
| Serpih/partikel | `shard/circles/particles.ogg` | 0 → kecil | kedekatan |
| UI | `click/enter/leave-project`, `ui-long`, `ui-short`, `logo`, `manifesto`, `project-text` | 0.3–0.5 | sekali bunyi |
| Ketik | `beeps/beeps2/beeps3` | 0.5 | acak, throttle 400 ms |

- **Amplop dari progress** (bukan acak): angin hidup di 5–20%, bertahan, mati di 75–95%. Jadi
  telinga ikut tahu "bab ini sudah mau habis" tanpa ada suara transisi.
- Master gain `setTargetAtTime(v, now, 0.35)` → perubahan volume punya **time constant 350 ms**
  (≈63% perubahan tercapai dalam 350 ms), jadi tidak pernah "klik". Volume per file 10 ms.
- **Mute default** (`muted: true`), audio opt-in lewat tombol sound; `AudioContext` di-suspend saat
  tab tidak terlihat.
- Tidak ada audio saat scroll itu sendiri — hanya bed yang volumenya berubah + beep saat teks.

---

## 7. Kecepatan menggerakkan gambar

- `scroll.velocity` (0..1) dikirim sebagai `uProgressVel` dan `uVelocity` (29 kemunculan di shader)
  → CA, motion blur, dan partikel ikut kencang saat scroll kencang, lalu meluruh dengan `0.98`.
- Bloom **satu kali** untuk seluruh situs: `levels 6, threshold 0.2, intensity 1, radius 0.85`.
- Grade warna lewat **LUT** `cubes/cube_scene.ktx2` (tetrahedral), bukan sekadar overlay CSS.
- Fluid sim (splat + advect) dipakai untuk tombol sound & "frost" kursor; `splatVelocity`
  di-ease `power4.out` dan meluruh 0.88/0.98.

---

## 8. Ringkasan angka (buat contekan)

```
scrollMultiplier      0.00075
redam 1 / 2           0.075 (limit 0.075/frame) / 0.15
clamp target          ±0.5625 unit dari y
deadzone              0.000075
velocity              0.98 friction, clamp 0..1, mati < 0.001
auto-center           tunggu 1.4 s, durasi 2 s, ease inOut3 (M0,0 C0.6,0 0,1 1,1)
kamera lerp           0.035 / 0.035 / 0.075  (lambat: 0.02/0.02/0.015)
parallax mouse        ±6.3° horizontal, ±2.25° vertikal
shake                 0.01–0.02 rad (0.6–1.1°), speed 0.1–0.5, sineNoise (non-periodik)
FOV                   30 / 25
bloom                 levels 6, threshold 0.2, intensity 1, radius 0.85
cut transition        slope −0.2×aspect, disp ±0.4, parallax 0.4, CA 5 iterasi
audio                 master fade 0.35 s, beep throttle 0.4 s, mute default
```

---

## 9. Yang bisa diadaptasi ke porto Aris (tema planet/nebula)

Prinsip: **tiru mekanikanya, bukan tampilannya.** Igloo itu gunung salju + igloo + kristal;
kita langit, planet, nebula. Yang bikin mereka terasa mahal bukan objeknya, tapi mekanika di atas.

### Tier A — murah, aman, terasa langsung (1 sesi)

Status jujur: porto kita **sudah** punya peredam eksponensial (`1 − e^(−dt·6.5)`, setara tahap 3
igloo), velocity 0..1, CA yang ikut kencang (×2.2), FOV punch, reveal per baris (stagger 0.08),
dan drift kamera. Yang belum ada di kolom "Target".

| Mekanik | Sekarang | Target | File |
|---|---|---|---|
| Tahap redam kedua + clamp | satu tahap (`raw → p`) | `raw → p1 (0.075) → p (0.15)` + clamp ±0.18 bab + deadzone | `App.tsx` |
| Noise shake | `sin(0.21t)` + `cos(0.17t)` (±0.2 unit, periodik) | 2 oktaf value-noise, ±0.006 rad, roll 0,3° | `World.tsx` |
| Orbit, bukan translasi | kamera digeser `px·0.95` / `py·0.62` | putar posisi di bola sekitar target, ±3° / ±1,2° | `World.tsx` |
| Teks dua kecepatan | baris sudah stagger; meta fade biasa | judul muncul cepat, meta 2× lebih lambat + caret | `Overlay.tsx` |
| CA/bloom reaktif | CA ×2.2 saat kencang, bloom statis | bloom +8% saat velocity tinggi | `World.tsx` |

Hasil: gerakan tidak pernah "berhenti lalu jalan" saat scroll, frame tidak pernah identik, dan
scroll kencang terasa punya tenaga — tanpa mengubah tata letak atau aksesibilitas.

Catatan: dua baris pertama di tabel ini yang paling terasa. Noise shake menggantikan sinus yang
periodenya ±30 detik (bagian yang paling "kelihatan buatan" di porto sekarang).

### Tier B — sedang (2–3 sesi)

| Mekanik | Versi kita | Catatan |
|---|---|---|
| Snap setelah idle | tunggu 1,4 s, snap `inOut3` **hanya** kalau sudah dekat batas bab (<25%) | teks kita panjang; snap agresif bikin mual |
| Aksen kamera per bab | tiap bab punya `progress` lokal 0..1 → planet maju/mundur, nebula membuka | `journey.ts` sudah punya station; tambah `localT` |
| Cut transition | teks keluar/masuk dengan **clip-path diagonal + parallax 8px + CA tipis** (CSS), bukan fade | meniru §4 tanpa render 2 scene |
| Audio 3 lapis | drone nebula (synth) + ambience + tick UI, amplop dari progress bab | opt-in, mute default, 350 ms fade |

### Tier C — berat / risiko tinggi

| Mekanik | Kenapa saya belum sarankan |
|---|---|
| **Virtual scroll penuh** | hilangkan scrollbar, Ctrl+F, anchor, `#track` SEO, dan scroll dinamis pembaca layar. Porto kita 9 bab teks; igloo hampir tanpa teks. Bisa, tapi harus ada pengganti aksesibilitas (tombol bab, keyboard mapping). |
| Render 2 scene + shader cut | butuh 2 render target + komposit; di mobile boros. Baru layak kalau Tier B sudah enak. |
| LUT `.cube` | perlu aset warna yang dinilai dengan mata; CSS grade kita sudah cukup. |
| Fluid sim | tidak nyambung ke tema langit. Skip. |

### Urutan eksekusi yang saya sarankan

1. **Tier A** semua (kecil, tidak mengubah konten).
2. Ukur: FPS di 1440×900 dan 390×844, tidak ada layout shift, teks tetap terbaca saat diam.
3. **B** satu per satu, mulai dari aksen kamera per bab (paling terasa untuk tema planet).
4. Audio terakhir — paling susah dinilai tanpa telinga Anda.

### Batas jujur

- Saya bisa menulis sintesis WebAudio yang rapi (drone, noise filter, tick), tapi **tidak bisa
  menilai enak/tidaknya**. Igloo memakai 18 sampel `.ogg` hasil sound designer.
- Noise shake dan parallax gampang; yang sulit adalah **menahan diri** — 0,6° sudah cukup,
  2° akan terlihat seperti kamera goyang.
- Semua angka di atas adalah titik awal, bukan resep mati. Rasa akhir tetap ditentukan dengan
  melihat frame-nya (luminance map/OCR seperti proses kita sebelumnya), bukan tebakan.

---

## 10. Batas bukti

- Angka diambil dari bundle **minified**; nama variabel saya humanisasi, nilainya apa adanya.
- `sineNoise1` diambil dari nama fungsi di bundle; implementasi persisnya tidak saya dekompilasi
  (tidak perlu — kita pakai noise sendiri).
- Bagian yang **tidak** terverifikasi: isi 18 file `.ogg` (tidak diunduh, hanya nama+volume),
  nilai `_iglooVolume` (dihitung dari jarak, rumus tidak diekstrak), dan perilaku snap saat
  keyboard-only (belum diuji runtime).
