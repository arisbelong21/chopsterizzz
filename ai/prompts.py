"""Prompt templates for highlight analyzer and content-pack generation."""
from __future__ import annotations

from typing import Any


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


#: Mobile-first title discipline (adapted from the grounded long-form pipeline).
#: A phone cuts a YouTube title at roughly 50 characters, and beyond ~65 the
#: title is no longer readable in one glance. The reason to click therefore has
#: to sit *before* the cut, which is why the subject keyword belongs in the
#: first three words instead of at the end.
TITLE_MOBILE_CHARS = 50
TITLE_MAX_CHARS = 65

#: Stage sizes of the title pipeline: brainstorm wide, then let the critic pick.
#: A wide first stage is what allows the second stage to keep two candidates with
#: genuinely *different* angles instead of two paraphrases of the first idea.
TITLE_BRAINSTORM_COUNT = 20
TITLE_FINAL_COUNT = 4

#: The angles stage 1 must cover, so the candidate pool is not one idea seven
#: times.
TITLE_ANGLES = (
    "curiosity gap",
    "hasil spesifik",
    "how-to",
    "angka/data",
    "pertanyaan",
    "kontrarian",
    "cerita/pengalaman",
)

#: The four axes stage 2 scores every candidate on (1-10) and the exact key
#: names the critic must answer with. ``honesty`` doubles as the clickbait
#: guard: a candidate that promises what the transcript does not contain is
#: dropped no matter how curious it reads.
TITLE_CRITIC_DIMENSIONS = ("specificity", "curiosity", "clarity", "honesty")


def _output_language_names(language: Any) -> tuple[str, str]:
    """(Indonesian name, English name) of the requested OUTPUT language.

    The prompts themselves are written in Indonesian with English fragments;
    without an explicit rule the model copies that mix into the output
    ("bahasa kecampur"). Naming the language twice covers both reading paths.
    """
    code = str(language or "").strip().lower()
    if code.startswith("en"):
        return "Inggris", "English"
    if not code or code.startswith("id"):
        return "Indonesia", "Indonesian"
    return code, code


def _output_language_rule(language: Any) -> str:
    """The bullet that pins every output field to ONE language."""
    id_name, en_name = _output_language_names(language)
    rule = (
        f"- BAHASA OUTPUT: tulis SEMUA field 100% dalam bahasa {id_name} ({en_name}). "
        "Dilarang mencampur bahasa dalam satu kalimat atau satu pack; bahasa yang "
        "dipakai di prompt ini BUKAN bahasa output. Hashtag, keyword, pinned "
        "comment, dan thumbnail_text juga mengikuti bahasa output."
    )
    if str(language or "").strip().lower().startswith("en"):
        rule += (
            "\n- OUTPUT LANGUAGE: every field must be written entirely in English. "
            "Do not use Indonesian words anywhere in the output."
        )
    return rule


CAPTION_SYSTEM = (
    "Kamu adalah senior social-media copywriter dan YouTube copy strategist. Buat content pack yang "
    "spesifik terhadap isi transkrip, bukan template generik. Jangan membuat fakta yang tidak didukung. "
    "Setiap video punya transkripnya sendiri: judul, deskripsi, caption, dan hashtag harus lahir dari "
    "kata-kata di transkrip itu, dan tidak boleh memakai pola kalimat yang sama dengan video lain. "
    "Judul wajib terasa ditulis manusia, bukan mesin: kapital natural seperti judul YouTube/Shorts "
    "sungguhan (kata penting berawal kapital, kata sambung kecil tetap kecil) atau sentence case, 3-8 "
    "kata, layak diklik, BUKAN HURUF KAPITAL SEMUA, tanpa tanda kutip, tanpa emoji, tanpa titik di "
    "akhir, dan tetap jujur terhadap isi. "
    "Judul video asli / judul kerja dari renderer bukan judul final: jangan menyalin atau memparafrase "
    "judul itu, tulis judul baru dari isi transkrip. "
    "Setiap caption dan deskripsi wajib punya hook di kalimat pertama, isi spesifik di tengah, lalu CTA "
    "(ajakan komentar / follow / tonton sampai habis) di kalimat terakhir. "
    "Hashtag harus natural: campuran tag topik yang benar-benar muncul di transkrip + 1-2 tag niche, "
    "maksimal 2 tag umum seperti #shorts, dan bukan daftar tag generik yang bisa dipakai video apa pun. "
    "Judul teknis hasil renderer (mis. 'menit 1 sampai 2' atau '00:15 - 00:45') bukan judul final dan "
    "tidak boleh dipakai ulang. "
    "Buat beberapa opsi judul, hook/caption, deskripsi pendek, deskripsi panjang bergaya YouTube, hashtag, "
    "keyword SEO, dan pinned comment. Jawab HANYA JSON valid."
)


def caption_prompt(transcript: str, platform: str = "TikTok", tone: str = "casual", language: str = "id",
                   count: int = 5, style: str = "hooks", *, working_title: str = "",
                   avoid_titles: list[str] | None = None,
                   avoid_descriptions: list[str] | None = None,
                   force_distinct: bool = False,
                   ai_title_suggestion: str = "",
                   ai_caption_suggestion: str = "") -> str:
    """Prompt for ONE clip's content pack.

    ``working_title`` is the renderer/bot label the clip already carries (Auto
    Clip Studio title, Content Clipper AI export name, or the file name). It is
    handed over as context to be *replaced*, never as a title to reuse — feeding
    it as the "hook" was what made the AI echo ``menit 1 sampai 2`` back.

    ``ai_title_suggestion``/``ai_caption_suggestion`` are different: they are the
    analysis AI's OWN grounded copy for this clip, already written from the
    transcript. They are offered as adoptable starting material (never as a
    forbidden label), so a good suggestion is refined instead of discarded.

    ``avoid_titles``/``avoid_descriptions`` carry what earlier videos in the same
    batch already received, so a multi-video run cannot produce one description
    stamped on every video.

    Titles also carry the mobile length discipline (``TITLE_MOBILE_CHARS`` /
    ``TITLE_MAX_CHARS``) so the reason to click survives the phone title cut.
    """
    extras: list[str] = []
    working = str(working_title or "").strip()
    if working:
        extras.append(
            "Judul kerja dari renderer/import (BUKAN judul final — dilarang dipakai "
            f"ulang apa adanya, tulis judul baru yang lebih kuat): {working[:160]}")
    ai_title = str(ai_title_suggestion or "").strip()
    ai_caption = str(ai_caption_suggestion or "").strip()
    if ai_title or ai_caption:
        lines = ["Saran dari AI analisis video ini (sudah ditulis dari transkrip — "
                 "BOLEH dipakai atau diperbaiki, bukan wajib, tetap harus jujur "
                 "terhadap isi dan ikut semua aturan di bawah):"]
        if ai_title:
            lines.append(f"- Saran judul: {ai_title[:160]}")
        if ai_caption:
            lines.append(f"- Saran caption: {ai_caption[:240]}")
        extras.append("\n".join(lines))
    avoid_t = [str(t).strip() for t in (avoid_titles or []) if str(t).strip()][:8]
    if avoid_t:
        extras.append(
            "Judul yang SUDAH dipakai video lain di batch ini — wajib berbeda kata "
            "dan sudut pandang:\n" + "\n".join(f"- {t[:160]}" for t in avoid_t))
    avoid_d = [str(d).strip() for d in (avoid_descriptions or []) if str(d).strip()][:6]
    if avoid_d:
        extras.append(
            "Deskripsi video lain di batch ini — dilarang menyalin kalimat pembuka, "
            "susunan, atau pola apa pun darinya:\n"
            + "\n".join(f"- {d[:240]}" for d in avoid_d))
    if force_distinct:
        extras.append(
            "PERINGATAN KEUNIKAN: hasil sebelumnya terlalu mirip video lain di batch "
            "ini. Tulis ulang dengan struktur, kalimat pembuka, fokus, dan hashtag "
            "yang benar-benar berbeda.")
    extra_block = ("\n\n" + "\n\n".join(extras)) if extras else ""
    id_name, en_name = _output_language_names(language)
    return f"""
Buat content pack untuk potongan video/podcast berikut.

Platform utama: {platform}
Tone: {tone}
Bahasa output: {id_name} ({en_name})
Jumlah opsi: {count}
Style: {style}

Kebutuhan OUTPUT:
1. titles: {count} judul yang benar-benar berbeda dengan gaya kreator manusia (kapital natural seperti judul YouTube sungguhan, BUKAN KAPITAL SEMUA), 3-8 kata, tiap judul memakai sudut/angle yang BERBEDA (curiosity gap, hasil spesifik, how-to, angka, pertanyaan, kontrarian, cerita). Hindari judul generik seperti "rangkuman singkat". Ideal keep under {TITLE_MOBILE_CHARS} characters. Max {TITLE_MAX_CHARS}. Put the main subject/keyword in the first 3 words. The hook must be understandable at a glance on mobile.
2. captions: {count} caption siap posting. Setiap caption: 1 kalimat hook, 1-2 kalimat isi, lalu 1 CTA (ajakan komentar/follow/tonton sampai habis).
3. description_short: 2-4 kalimat, kalimat pertama hook dan kalimat terakhir CTA.
4. description_long: deskripsi YouTube-style sekitar 250-450 kata, berisi pembuka hook yang menarik, konteks/topik, poin yang dibahas, dan CTA penutup (ajakan berdiskusi/follow). Jangan mengarang detail.
5. hashtags: 8-15 hashtag natural: tag topik yang benar-benar ada di transkrip (boleh digabung, mis. #golpenalti) + 1-2 tag niche, maksimal 2 tag umum seperti #shorts.
6. keywords: 8-15 keyword SEO relevan.
7. pinned_comment: satu komentar tersemat yang memancing diskusi tanpa clickbait palsu.
8. thumbnail_text: 4-6 opsi teks thumbnail pendek (2-6 kata, HURUF KAPITAL).

Aturan:
{_output_language_rule(language)}
- Jangan membuat fakta, nama, angka, kutipan, atau kejadian yang tidak ada di transkrip/konteks.
- Jangan menjanjikan virality.
- Judul ditulis seperti manusia asli, bukan bot: kapital natural ala judul YouTube (contoh: "Penalti Detik Akhir Pecahkan Stadion"), JANGAN huruf kapital semua dan jangan pola template kaku; tanpa tanda kutip, tanpa emoji, tanpa titik di akhir.
- Panjang judul: ideal <= {TITLE_MOBILE_CHARS} karakter, keras maksimal {TITLE_MAX_CHARS} karakter. Assume phones cut after ~{TITLE_MOBILE_CHARS} characters. The reason to click must be visible in the first {TITLE_MOBILE_CHARS}. Kalau harus memotong, buang kata sambung — jangan buang informasi.
- Kalau ada beberapa judul, jangan buat variasi kata dari satu ide yang sama: pakai sudut yang benar-benar berbeda (pertanyaan, angka, how-to, kontrarian, hasil spesifik, cerita).
- Dilarang memakai judul kerja / judul video asli di atas sebagai judul final: jangan menyalin dan jangan memparafrase. Setiap judul harus jelas lahir dari isi transkrip ini saja.
- Setiap caption, description_short, dan description_long WAJIB punya hook di kalimat pertama dan CTA di kalimat terakhir.
- Deskripsi panjang harus terasa seperti deskripsi YouTube asli, bukan paragraf filler.
- Setiap field wajib lahir dari transkrip DI ATAS: sebutkan minimal 3 detail konkret
  (nama, angka, peristiwa, atau argumen) yang hanya ada di transkrip ini.
- Dilarang memakai kalimat pembuka, struktur paragraf, atau pola hashtag yang sama
  untuk video berbeda; dilarang menempelkan pola umum seperti "Potongan obrolan tentang ...".
- Dilarang memakai judul teknis/rentang waktu (mis. "menit 1 sampai 2", "00:15 - 00:45", "clip 3").
- Jangan menulis timestamp atau daftar chapter sendiri di description_long; sistem menambahkan blok chapter dari timestamp transkrip yang asli.
- Hashtag harus natural dan mengikuti topik transkrip ini, bukan deretan hashtag generik yang sama untuk semua video.{extra_block}

Transkrip/konteks:
{transcript[:12000]}

Format JSON:
{{
  "titles": ["Judul Natural Seperti Kreator Asli"],
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


TITLE_BRAINSTORM_SYSTEM = (
    "Kamu adalah editor YouTube Shorts yang menulis judul untuk dibaca di layar HP. "
    "Tulis BANYAK kandidat lebih dulu, jangan langsung memilih satu. Setiap kandidat harus lahir "
    "dari isi transkrip yang diberikan, bukan dari judul video asli. Jangan membuat fakta, angka, "
    "nama, atau kutipan yang tidak ada di transkrip. Judul wajib bergaya kreator manusia: kapital "
    "natural seperti judul YouTube sungguhan (BUKAN HURUF KAPITAL SEMUA), 3-8 kata, tanpa tanda "
    "kutip, tanpa emoji, tanpa titik di akhir. Jawab HANYA JSON valid."
)


def _candidate_line(index: int, candidate: Any) -> str:
    """One numbered candidate for the critic prompt (dict or plain string)."""
    if isinstance(candidate, dict):
        angle = str(candidate.get("angle") or "-").strip()
        return f'{index}. [{angle}] {str(candidate.get("title") or "").strip()}'
    return f"{index}. {str(candidate or '').strip()}"


def title_brainstorm_prompt(text: str, *, language: str = "id",
                            count: int = TITLE_BRAINSTORM_COUNT,
                            working_title: str = "",
                            avoid_titles: list[str] | None = None) -> str:
    """Stage 1: ask for a wide pool of candidates across fixed angles.

    The pool is deliberately larger than the final list: stage 2 can only keep
    two *different* angles if the first stage ever proposed them.
    """
    angles = "\n".join(f"{i + 1}. {angle}" for i, angle in enumerate(TITLE_ANGLES))
    extras: list[str] = []
    working = str(working_title or "").strip()
    if working:
        extras.append(
            "Judul kerja dari renderer/import (BUKAN judul final — dilarang dipakai "
            f"ulang apa adanya maupun diparafrase): {working[:160]}")
    avoid = [str(t).strip() for t in (avoid_titles or []) if str(t).strip()][:8]
    if avoid:
        extras.append(
            "Judul yang SUDAH dipakai video lain di batch ini — wajib berbeda kata "
            "dan sudut pandang:\n" + "\n".join(f"  - {t[:160]}" for t in avoid))
    extra_block = ("\n" + "\n".join(f"- {line}" for line in extras)) if extras else ""
    id_name, en_name = _output_language_names(language)
    lang_bullet = (
        f"- Tulis semua kandidat judul dalam bahasa {id_name} ({en_name}) saja — "
        "dilarang mencampur bahasa di dalam satu judul."
    )
    if str(language or "").strip().lower().startswith("en"):
        lang_bullet += " Every title must be entirely in English."
    return f"""
Transkrip potongan ini (bahasa: {language}):
{str(text or '')[:8000]}

Tugas TAHAP 1 — BRAINSTORM JUDUL:
- Tulis {count} kandidat judul, bukan satu. Makin banyak sudut, makin bagus pilihan akhirnya.
- Sebar kandidat ke {len(TITLE_ANGLES)} sudut berikut (setiap sudut minimal 2 kandidat):
{angles}
- Panjang: ideal <= {TITLE_MOBILE_CHARS} karakter, keras maksimal {TITLE_MAX_CHARS} karakter.
- Letakkan subjek/kata kunci utama di 3 kata pertama.
- Assume phones cut after ~{TITLE_MOBILE_CHARS} characters. The reason to click must be visible in the first {TITLE_MOBILE_CHARS}.
- Gaya judul seperti kreator manusia asli: kapital natural ala judul YouTube, BUKAN huruf kapital semua, bukan template kaku.
- Judul harus bisa dipahami sekilas di layar HP (clarity), spesifik (bukan "rangkuman singkat"), dan jujur terhadap isi transkrip.
- Setiap kandidat memakai kata-kata yang benar-benar ada di transkrip di atas.
{lang_bullet}{extra_block}

Format JSON:
{{"candidates": [{{"title": "Judul natural 3-8 kata", "angle": "curiosity gap", "why": "alasan singkat"}}]}}

Jawab HANYA JSON, tanpa markdown.
""".strip()


TITLE_CRITIC_SYSTEM = (
    "Kamu adalah kritikus judul yang dingin dan teliti. Nilai setiap kandidat judul apa adanya "
    "dengan skor 1-10, tandai yang clickbait atau tidak didukung transkrip, dan jangan memuji. "
    "Kembalikan semua kandidat yang dinilai, lalu pilih yang sudutnya paling berbeda. "
    "Jawab HANYA JSON valid."
)


def title_critic_prompt(candidates: list[Any], text: str, *, language: str = "id",
                        count: int = TITLE_FINAL_COUNT) -> str:
    """Stage 2: score the pool and name the best *different* angles."""
    listing = "\n".join(_candidate_line(i + 1, c) for i, c in enumerate(candidates))
    dims = ", ".join(TITLE_CRITIC_DIMENSIONS)
    return f"""
Transkrip potongan ini (bahasa: {language}):
{str(text or '')[:4000]}

Kandidat judul (hasil brainstorm TAHAP 1):
{listing}

Tugas TAHAP 2 — NILAI KANDIDAT JUDUL:
- Beri skor 1-10 untuk SETIAP kandidat di atas pada 4 dimensi: {dims}.
- specificity: menyebut hal konkret dari transkrip, bukan kalimat umum.
- curiosity: membuat orang ingin tahu, tanpa membohongi isi.
- clarity: bisa dipahami sekilas di layar HP.
- honesty: tidak menjanjikan hal yang tidak ada di transkrip. Skor <= 3 berarti clickbait/tidak didukung.
- Tulis judulnya PERSIS seperti di daftar di atas supaya bisa dicocokkan.
- Sebutkan angle tiap kandidat (pakai nama angle dari daftar di atas).
- Lalu pilih maksimal {count} kandidat terbaik yang sudutnya BERBEDA satu sama lain (bukan variasi kata dari ide yang sama).

Format JSON:
{{"scores": [{{"title": "Judul persis seperti di daftar di atas", "angle": "curiosity gap", "specificity": 8, "curiosity": 7, "clarity": 9, "honesty": 10, "note": "catatan singkat"}}], "top": ["Judul terbaik 1", "Judul terbaik 2"]}}

Jawab HANYA JSON, tanpa markdown.
""".strip()
