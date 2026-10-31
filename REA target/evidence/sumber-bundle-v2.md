# Bukti sumber — bedah v2 (06)

Diunduh 28 Oktober 2026, 3 file publik. Hash sama dengan capture REA 27 Oktober 2026
(`App3D-d68cb154.js` disebut di `01-teardown.md` §2) → situs belum berubah.

```
0911f8ad8fa1a4ac603abf7bcc2fc7bb277ef1417860c046412f033daa8d9f03  1410     index.html
65da799bca47bbc9b308199e344b981e07bd3e648fa2ad39697bf0400cf4a8c2  16546    assets/index-2e130bc1.js
e25f1c14079fb4e5e492c68e487b296ff06f5e385af90af4278f64bfe004a0ac  1486303  assets/App3D-d68cb154.js
```

URL:

- https://www.igloo.inc/
- https://www.igloo.inc/assets/index-2e130bc1.js
- https://www.igloo.inc/assets/App3D-d68cb154.js

Cara mengulang:

```powershell
$dir = "$env:TEMP\opencode\igloo"; New-Item -ItemType Directory -Force $dir | Out-Null
curl.exe -sL --compressed https://www.igloo.inc/assets/App3D-d68cb154.js -o "$dir\App3D.js"
Get-FileHash "$dir\App3D.js" -Algorithm SHA256
```

Semua angka di `06-bedah-gerak-dan-suara.md` berasal dari file ketiga (minified 1,49 MB).
Nama variabel di dokumen adalah humanisasi; nilainya tidak diubah.
