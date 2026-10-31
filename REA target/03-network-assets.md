# 03 — Aset yang benar-benar dimuat browser

Sumber: capture CDP REA, 105 URL unik dari 4 sesi capture (satu sesi load penuh: 107 request), semua `200`/`304`, 0 gagal.
Ukuran byte diambil via HTTP `Content-Length` (39 file disampel = 6,38 MB).

## Ringkasan per jenis

| Jenis | Jumlah request | Catatan |
|---|---|---|
| `fetch` (loader aset kustom) | 92 | mayoritas `.ktx2` |
| `script` | 10 | entry + chunk + 4 worker + transcoder |
| `document` | 2 | HTML + fallback |
| `other` | 3 | wasm |

Pola penting: 92 dari 107 request bertipe `fetch`, bukan `<img>`/`<script>`.
Artinya aset dimuat lewat **loader kustom** (fetch → parse KTX2 → upload ke GPU),
bukan tag HTML. Ini ciri pipeline engine game, bukan website.

## Ukuran file (terukur)

| Aset | KB |
|---|---|
| `assets/images/scroll-datatexture.ktx2` | 1.256,3 |
| `assets/images/cubes/cube3_normal.ktx2` | 1.107,8 |
| `assets/images/igloo/igloo_exploded_color.ktx2` | 822,5 |
| `assets/images/volumes/peachesbody_64.ktx2` | 817,0 |
| `assets/images/igloo/ground_color.ktx2` | 610,7 |
| `assets/images/igloo/mountain_color.ktx2` | 578,1 |
| `assets/images/igloo/igloo_color.ktx2` | 520,9 |
| `assets/images/volumes/x_64.ktx2` | 311,0 |
| `assets/images/frost-datatexture.ktx2` | 131,6 |
| `assets/fonts/IBMPlexMono-Medium-datatexture.ktx2` | 107,7 |
| `assets/images/igloo/triangles_tiling.ktx2` | 46,5 |
| `assets/images/volumes/medium_32.ktx2` | 44,3 |
| `assets/images/cubes/cube_scene.ktx2` | 40,4 |
| `assets/images/cubes/advect.png` | 26,8 |
| `assets/images/clouds_noise.ktx2` | 24,8 |
| `assets/images/bokeh.ktx2` | 21,1 |
| `assets/images/wind_noise.ktx2` | 17,7 |
| `assets/images/igloo/igloo_scene.ktx2` | 14,1 |
| `assets/images/numbers-datatexture.ktx2` | 13,2 |
| `assets/images/igloo/numbers.ktx2` | 4,7 |
| `assets/images/cubes/cube1_normal.ktx2` | terukur, kecil |
| `assets/images/cubes/cube1_roughness.ktx2` | terukur, kecil |
| `assets/images/cubes/cube3_roughness.ktx2` | terukur, kecil |
| `assets/images/cubes/pudgy_color.ktx2` | terukur, kecil |
| `assets/images/cubes/overpass_logo_color.ktx2` | terukur, kecil |
| `assets/images/cubes/dot_pattern.ktx2` | terukur, kecil |
| `assets/images/cubes/blurrytext_atlas.ktx2` | terukur, kecil |
| `assets/images/cubes/bg.png` | terukur, kecil |
| `assets/images/pudgy_dark_color.ktx2` | terukur, kecil |
| `assets/images/overpass_logo_dark_color.ktx2` | terukur, kecil |
| `assets/images/shattered_ring_color.ktx2` | terukur, kecil |
| `assets/images/shattered_ring_ao.ktx2` | terukur, kecil |
| `assets/images/shattered_ring2_color.ktx2` | terukur, kecil |
| `assets/images/shattered_ring2_ao.ktx2` | terukur, kecil |
| `assets/images/mosaic.ktx2` | terukur, kecil |
| `assets/images/shapes_blurred.ktx2` | terukur, kecil |
| `assets/images/floor_color.ktx2` | terukur, kecil |
| `assets/images/caustics.ktx2` | terukur, kecil |
| `assets/images/perlin-datatexture.png` | terukur, kecil |

## Kode & worker

| Aset | Ukuran |
|---|---|
| `assets/index-2e130bc1.js` (entry) | 16,5 KB |
| `assets/App3D-d68cb154.js` (main) | 1.486 KB (±400 KB setelah gzip/brotli) |
| `assets/bitmapworker-046527f8.js` | di-load saat butuh decode bitmap |
| `assets/exrworker-41cbee65.js` | decode HDRI/EXR di worker |
| `assets/msdfworker-ac346fa7.js` | build atlas MSDF |
| `assets/audioworker-036a09db.js` | analisis audio |
| `assets/libs/basis/basis_transcoder.wasm` | transcoder KTX2 |
| `assets/libs/draco/draco_decoder.wasm` | decoder mesh Draco |

**Insight penting untuk kamu:** JS main chunk hanya ±1,45 MB mentah hasil gabungan three.js +
GSAP + postprocessing + seluruh kode situs. Itu wajar. Beratnya ada di **tekstur**, dan tekstur
itu bisa ditekan drastis kalau kamu tidak butuh kualitas film.

## Daftar lengkap yang teramati (urutan load)

```
assets/index-2e130bc1.js
assets/App3D-d68cb154.js
assets/favicon32-af94112f.png
assets/favicon16-9e4401be.png
assets/fonts/IBMPlexMono-Medium.json
assets/libs/draco/draco_wasm_wrapper.js
assets/libs/draco/draco_decoder.wasm
assets/images/scroll-datatexture.ktx2
assets/images/frost-datatexture.ktx2
assets/images/noises/blue-8-128-rgb.ktx2
assets/images/ui/logo-datatexture.ktx2
assets/fonts/IBMPlexMono-Medium-datatexture.ktx2
assets/images/ui/sound-datatexture.ktx2
assets/images/ui/close-datatexture.ktx2
assets/images/ui/arrow-datatexture.ktx2
assets/images/wind_noise.ktx2
assets/bitmapworker-046527f8.js
assets/images/perlin-datatexture.ktx2
assets/images/cubes/dot_pattern.ktx2
assets/exrworker-41cbee65.js
assets/images/igloo/triangles_tiling.ktx2
assets/images/clouds_noise.ktx2
assets/images/volumes/peachesbody_64.ktx2
assets/images/bokeh.ktx2
assets/images/igloo/igloo_scene.ktx2
assets/images/cubes/cube_scene.ktx2
assets/images/volumes/x_64.ktx2
assets/images/volumes/medium_32.ktx2
assets/images/ui/visit-datatexture.ktx2
assets/msdfworker-ac346fa7.js
assets/images/igloo/mountain_color.ktx2
assets/images/mosaic.ktx2
assets/images/igloo/igloo_color.ktx2
assets/images/igloo/igloo_exploded_color.ktx2
assets/images/numbers-datatexture.ktx2
assets/images/igloo/ground_sansigloo_color.ktx2
assets/images/igloo/numbers.ktx2
assets/images/cubes/blurrytext_atlas.ktx2
assets/images/igloo/ground_color.ktx2
assets/images/igloo/ground_glow.ktx2
assets/images/shapes_blurred.ktx2
assets/images/floor_color.ktx2
assets/images/pudgy_dark_color.ktx2
assets/images/caustics.ktx2
assets/images/overpass_logo_dark_color.ktx2
assets/libs/basis/basis_transcoder.js
assets/libs/basis/basis_transcoder.wasm
assets/images/shattered_ring2_color.ktx2
assets/images/shattered_ring2_ao.ktx2
assets/images/shattered_ring_color.ktx2
assets/images/shattered_ring_ao.ktx2
assets/images/cubes/bg.png
assets/images/cubes/advect.png
assets/images/cubes/cube3_roughness.ktx2
assets/images/cubes/cube3_normal.ktx2
assets/images/cubes/pudgy_color.ktx2
assets/images/perlin-datatexture.png
assets/images/cubes/cube1_roughness.ktx2
assets/images/cubes/cube1_normal.ktx2
assets/images/cubes/overpass_logo_color.ktx2
assets/audioworker-036a09db.js
```

## Koreksi: semuanya di-preload dalam satu gelombang

Dugaan awal saya (dari urutan request) adalah "manifest bertingkat dengan prioritas per section".
Capture ber-GPU membuktikan itu **salah**:

```
step 0 (scenario_start) :   4 request
step 1 (load)           :   5 request  (favicon, font)
step 2 (settle 12s)     :  98 request  ← SEMUA aset masuk di sini
step 3 (PageDown ×5)    :   1 request
step 4+ (scroll jauh)   :   0 request
```

Artinya: **semua 98 aset** — tekstur, geometri Draco, font, worker, transcoder — dimuat sekaligus
setelah `load`, dan curtain baru dibuka setelah semuanya siap. Tidak ada lazy-load per section.
Urutan di daftar atas hanyalah urutan antrean loader, bukan prioritas desain.

Konsekuensi: scroll sehalus apa pun, **tidak ada** request saat user masuk ke section baru.
Itu sebabnya mode explore bisa menampilkan objek baru tanpa satu pun request tambahan.

Kalau kamu meniru pola ini dengan 6 MB aset, user akan menunggu curtain ±6 MB. Untuk portofolio
pribadi, **lazy-load per section** lebih baik — dan itulah yang saya sarankan di `04-blueprint`.

## Geometri: 20 file Draco `.drc` (tidak ada glTF)

| File | KB |
|---|---|
| `geometries/igloo.drc` | 217,6 |
| `geometries/shattered_ring2.drc` | 42,5 |
| `geometries/floor.drc` | 39,8 |
| `geometries/shattered_ring.drc` | 39,7 |
| `geometries/cubes/cube3.drc` | 39,4 |
| `geometries/pudgy.drc` | 29,9 |
| `geometries/cubes/cube1.drc` | 27,4 |
| `geometries/igloo/igloo_outline.drc` | 19,9 |
| `geometries/ground.drc` | 18,4 |
| `geometries/overpass_logo.drc` | 14,2 |
| `geometries/ceilingsmoke.drc` | 13,4 |
| `geometries/mountain.drc` | 11,6 |
| `geometries/shattered_ring_smoke.drc` | 10,0 |
| `geometries/igloo/igloo_cage.drc` | 5,6 |
| `geometries/blurrytext_cylinder.drc` | 5,4 |
| `geometries/smoke_trail.drc` | 4,0 |
| `geometries/intro_particles.drc` | 2,0 |
| `geometries/blurrytext.drc` | 1,2 |
| `geometries/cubes/background_shapes.drc` | 0,7 |
| `geometries/igloo/patch.drc` | 0,2 |
| **Total** | **542,9** |

Total seluruh geometri 3D situs ini **543 KB** — lebih kecil dari satu file data texture.
Itu karena `.drc` hanya berisi vertex/index. Material, transform, dan tekstur diatur di kode.

