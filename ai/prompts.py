"""Prompt templates for highlight analyzer and content-pack generation."""
from __future__ import annotations

HIGHLIGHT_SYSTEM = (
    "Kamu adalah editor video berpengalaman. Analisis transkrip video panjang dan identifikasi "
    "segmen-segmen yang paling menarik sebagai kandidat clip pendek. Jangan menjanjikan virality. "
    "Beri skor editorial 0-100 sebagai heuristik, bukan probabilitas. Jawab HANYA JSON valid."
)


def highlight_prompt(transcript_excerpt: str, duration: float, language: str = "id", instruction: str = "", scope_start: float = 0.0, scope_end: float | None = None, chunk_index: int = 1, chunk_count: int = 1) -> str:
    return f"""
Transkrip berasal dari video berdurasi {duration:.0f} detik (bahasa: {language}).

Instruksi pengguna: {instruction or "Pilih bagian paling menarik secara editorial."}

Tugas:
- Identifikasi 3-8 kandidat clip terbaik dari BAGIAN TIMELINE INI, ideal 20-60 detik.
- Cari hook, konflik, kejutan, payoff, insight, humor, emosi, atau pertanyaan yang memancing rasa ingin tahu.
- Jangan mengambil segmen hanya karena berada di awal.
- Untuk setiap kandidat beri: title, start, end, duration, excerpt, reason, hook, context_required, weaknesses, score (0-100).
- Jangan membuat kutipan palsu. Excerpt harus benar-benar berasal dari transkrip.
- Kandidat harus berada di rentang {scope_start:.1f}–{(scope_end if scope_end is not None else duration):.1f} detik.

Format JSON:
{{"candidates": [{{"title": "...", "start": 12.3, "end": 45.6, "duration": 33.3, "excerpt": "...", "reason": "...", "hook": "...", "context_required": "...", "weaknesses": "...", "score": 82}}]}}

Bagian timeline {chunk_index}/{chunk_count}: {scope_start:.1f}s → {(scope_end if scope_end is not None else duration):.1f}s

Transkrip bertimestamp:
{transcript_excerpt[:15000]}

Jawab HANYA JSON, tanpa markdown.
""".strip()


CAPTION_SYSTEM = (
    "Kamu adalah senior social-media copywriter dan YouTube copy strategist. Buat content pack yang "
    "spesifik terhadap isi transkrip, bukan template generik. Jangan membuat fakta yang tidak didukung. "
    "Buat beberapa opsi judul, hook/caption, deskripsi pendek, deskripsi panjang bergaya YouTube, hashtag, "
    "keyword SEO, dan pinned comment. Jawab HANYA JSON valid."
)


def caption_prompt(transcript: str, platform: str = "TikTok", tone: str = "casual", language: str = "id", count: int = 5, style: str = "hooks") -> str:
    return f"""
Buat content pack untuk potongan video/podcast berikut.

Platform utama: {platform}
Tone: {tone}
Bahasa: {language}
Jumlah opsi: {count}
Style: {style}

Kebutuhan OUTPUT:
1. titles: {count} judul yang benar-benar berbeda. Hindari judul generik seperti "rangkuman singkat". Gunakan curiosity gap, konflik, insight, pertanyaan, atau payoff yang memang didukung isi.
2. captions: {count} caption siap posting, natural, tidak terlalu pendek, dan punya hook.
3. description_short: deskripsi 2-4 kalimat.
4. description_long: deskripsi YouTube-style sekitar 250-450 kata, berisi pembuka yang menarik, konteks/topik, poin yang dibahas, dan ajakan berdiskusi. Jangan mengarang detail.
5. hashtags: 8-15 hashtag relevan.
6. keywords: 8-15 keyword SEO relevan.
7. pinned_comment: satu komentar tersemat yang memancing diskusi tanpa clickbait palsu.
8. thumbnail_text: 4-6 opsi teks thumbnail pendek (2-6 kata).

Aturan:
- Jangan membuat fakta, nama, angka, kutipan, atau kejadian yang tidak ada di transkrip/konteks.
- Jangan menjanjikan virality.
- Untuk judul, boleh provokatif secara editorial tetapi tetap jujur terhadap isi.
- Deskripsi panjang harus terasa seperti deskripsi YouTube asli, bukan paragraf filler.

Transkrip/konteks:
{transcript[:12000]}

Format JSON:
{{
  "titles": ["..."],
  "captions": ["..."],
  "description_short": "...",
  "description_long": "...",
  "hashtags": ["#..."],
  "keywords": ["..."],
  "pinned_comment": "...",
  "thumbnail_text": ["..."]
}}

Jawab HANYA JSON, tanpa markdown.
""".strip()
