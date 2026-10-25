import re
from typing import List, Optional

def parse_time_str(time_str: str) -> float:
    """Parses time string in formats like HH:MM:SS,mmm or MM:SS,mmm or HH:MM:SS or MM:SS to seconds."""
    time_str = time_str.strip().replace(',', '.')
    # Extract millisecond if present
    ms = 0.0
    if '.' in time_str:
        parts = time_str.split('.')
        time_str = parts[0]
        try:
            ms = float('0.' + parts[1])
        except ValueError:
            pass
            
    time_parts = time_str.split(':')
    try:
        if len(time_parts) == 3:
            return int(time_parts[0]) * 3600 + int(time_parts[1]) * 60 + int(time_parts[2]) + ms
        elif len(time_parts) == 2:
            return int(time_parts[0]) * 60 + int(time_parts[1]) + ms
        elif len(time_parts) == 1:
            return float(time_parts[0]) + ms
    except ValueError:
        return 0.0

def parse_manual_subtitles(content: str, default_duration: float = 0.0) -> List[dict]:
    # Normalize line endings
    content = content.replace('\r\n', '\n').strip()
    
    # 1. Try standard SRT parsing first
    srt_regex = r'(?:\d+\n)?(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\n(.*?)(?=\n\n|\n\d+\n|\Z)'
    srt_matches = re.findall(srt_regex, content, re.DOTALL)
    
    if srt_matches:
        results = []
        for start_str, end_str, text in srt_matches:
            start = parse_time_str(start_str)
            end = parse_time_str(end_str)
            cleaned_text = text.replace('\n', ' ').strip()
            results.append({
                "text": cleaned_text,
                "start": start,
                "duration": max(0.1, end - start)
            })
        if results:
            return results

    # 2. Try parsing line-by-line for timestamped lines
    line_time_range_regex = r'^[\[\(]?(\d{1,2}:\d{2}(?::\d{2})?(?:[,.]\d{1,3})?)\s*(?:-|-->|\s)\s*(\d{1,2}:\d{2}(?::\d{2})?(?:[,.]\d{1,3})?)[\]\)]?\s*(.*)'
    line_single_time_regex = r'^[\[\(]?(\d{1,2}:\d{2}(?::\d{2})?(?:[,.]\d{1,3})?)[\]\)]?\s*(.*)'
    
    lines = content.split('\n')
    results = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # Match range first (e.g. 00:12 - 00:15 Text)
        m_range = re.match(line_time_range_regex, line)
        if m_range:
            start = parse_time_str(m_range.group(1))
            end = parse_time_str(m_range.group(2))
            text = m_range.group(3).strip()
            results.append({
                "text": text,
                "start": start,
                "duration": max(0.1, end - start)
            })
            continue
            
        # Match single timestamp (e.g. 00:12 Text)
        m_single = re.match(line_single_time_regex, line)
        if m_single:
            start = parse_time_str(m_single.group(1))
            text = m_single.group(2).strip()
            results.append({
                "text": text,
                "start": start,
                "duration": -1.0  # Will fill in later
            })
            continue

    if results:
        # Resolve duration for single timestamps
        for i in range(len(results)):
            if results[i]["duration"] == -1.0:
                if i + 1 < len(results):
                    diff = results[i+1]["start"] - results[i]["start"]
                    results[i]["duration"] = max(0.5, diff)
                else:
                    results[i]["duration"] = 3.0  # default for the last line
        return results

    # 3. Fallback: split text into paragraphs or sentences and distribute evenly across video duration
    duration_to_use = default_duration if default_duration > 0 else 60.0
    raw_sentences = [s.strip() for s in re.split(r'(?<=[.?!])\s+|\n+', content) if s.strip()]
    if raw_sentences:
        num_sentences = len(raw_sentences)
        sec_per_sentence = duration_to_use / num_sentences
        results = []
        for i, text in enumerate(raw_sentences):
            start = i * sec_per_sentence
            results.append({
                "text": text,
                "start": round(start, 2),
                "duration": round(sec_per_sentence, 2)
            })
        return results
        
    return []

def extract_video_id(url: str) -> Optional[str]:
    """Extracts the 11-character YouTube video ID from various URL formats including live streams, shorts, embed, watch?v=, youtu.be, etc."""
    if not url:
        return None
    trimmed = url.strip()
    if re.match(r"^[a-zA-Z0-9_-]{11}$", trimmed):
        return trimmed

    patterns = [
        r"(?:[?&]v=)([a-zA-Z0-9_-]{11})",
        r"(?:youtu\.be\/|(?:www\.|m\.)?youtube(?:-nocookie)?\.com\/(?:embed|v|shorts|live)\/)([a-zA-Z0-9_-]{11})",
        r"(?:v=|\/v\/|embed\/|shorts\/|live\/|youtu\.be\/|\/embed\/|\/watch\?v=|\/watch\?.+&v=)([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, trimmed)
        if match:
            return match.group(1)

    return None

def lowercase_hashtags_in_string(text: str) -> str:
    """Finds all hashtags (#word) in a string and converts them to lowercase."""
    if not text:
        return text
    return re.sub(r'#\w+', lambda m: m.group(0).lower(), text)

LANGUAGE_NAMES = {
    'id': 'Indonesian (Bahasa Indonesia)',
    'en': 'English',
    'es': 'Spanish (Español)',
    'pt': 'Portuguese (Português)',
    'fr': 'French (Français)',
    'de': 'German (Deutsch)',
    'ja': 'Japanese (日本語)',
    'ko': 'Korean (한국어)',
    'zh': 'Chinese (中文)',
    'ar': 'Arabic (العربية)',
    'ru': 'Russian (Русский)',
}

ID_STOPWORDS = {
    'yang', 'dan', 'di', 'ini', 'itu', 'dengan', 'untuk', 'tidak', 'dari', 'dalam',
    'akan', 'pada', 'juga', 'ke', 'karena', 'bisa', 'ada', 'mereka', 'sudah', 'kita',
    'saya', 'kamu', 'orang', 'jadi', 'lagi', 'kalo', 'kalau', 'ya', 'banget', 'bukan',
    'tapi', 'sama', 'tau', 'tahu', 'gimana', 'kenapa', 'seperti', 'apa', 'nah', 'udah',
    'nih', 'dong', 'kan', 'lah', 'bang', 'mas', 'mbak', 'kak', 'nggak', 'gak', 'aja',
    'bener', 'gitu', 'adalah', 'oleh', 'secara', 'tersebut', 'pun', 'kok', 'deh', 'sih',
    'gue', 'lu', 'lo', 'luar', 'biasa', 'hanya', 'sangat', 'bagi', 'antara', 'tentang',
    'banyak', 'kurang', 'harus', 'mau', 'maupun', 'saat', 'ketika', 'terus', 'pasti',
    'masih', 'punya', 'makanya', 'ngomong', 'bikin'
}

EN_STOPWORDS = {
    'the', 'and', 'to', 'of', 'a', 'in', 'that', 'is', 'it', 'you', 'for', 'on',
    'are', 'as', 'with', 'they', 'at', 'be', 'this', 'have', 'from', 'or', 'one',
    'had', 'by', 'but', 'not', 'what', 'all', 'were', 'we', 'when', 'your', 'can',
    'there', 'an', 'which', 'she', 'do', 'how', 'their', 'if', 'will', 'up', 'about',
    'out', 'so', 'would', 'like', 'just', 'know', 'people', 'think', 'going', 'been',
    'them', 'some', 'could', 'him', 'into', 'other', 'than', 'then', 'now', 'look',
    'only', 'come', 'its', 'over', 'also', 'back', 'after', 'use', 'two', 'our',
    'work', 'first', 'well', 'way', 'even', 'new', 'want', 'because', 'any', 'these',
    'give', 'day', 'most', 'us', 'time', 'really', 'something', 'good', 'make'
}

ES_STOPWORDS = {
    'de', 'la', 'que', 'el', 'en', 'y', 'a', 'los', 'del', 'se', 'las', 'por',
    'un', 'para', 'con', 'no', 'una', 'su', 'al', 'lo', 'como', 'más', 'pero',
    'sus', 'le', 'ya', 'o', 'este', 'sí', 'porque', 'esta', 'son', 'entre',
    'está', 'cuando', 'muy', 'sin', 'sobre', 'ser', 'tiene', 'también', 'me',
    'hasta', 'hay', 'donde', 'quien', 'desde', 'todo', 'nos', 'durante', 'todos',
    'uno', 'les', 'ni', 'contra', 'otros', 'ese', 'eso', 'ante', 'ellos', 'esto'
}

PT_STOPWORDS = {
    'de', 'a', 'o', 'que', 'e', 'do', 'da', 'em', 'um', 'para', 'é', 'com',
    'não', 'uma', 'os', 'no', 'se', 'na', 'por', 'mais', 'as', 'dos', 'como',
    'mas', 'foi', 'ao', 'ele', 'das', 'tem', 'à', 'seu', 'sua', 'ou', 'ser',
    'quando', 'muito', 'há', 'nos', 'já', 'está', 'eu', 'também', 'só', 'pelo',
    'pela', 'você', 'isso', 'ela', 'entre', 'depois'
}

FR_STOPWORDS = {
    'de', 'la', 'le', 'et', 'les', 'des', 'en', 'un', 'du', 'une', 'que', 'est',
    'pour', 'qui', 'dans', 'a', 'par', 'plus', 'pas', 'au', 'sur', 'ne', 'se',
    'ce', 'il', 'sont', 'avec', 'son', 'cette', 'aux', 'ses', 'mais', 'ou',
    'ont', 'tout', 'comme', 'nous', 'sa', 'vous'
}

DE_STOPWORDS = {
    'der', 'die', 'und', 'in', 'den', 'von', 'zu', 'das', 'mit', 'sich', 'des',
    'auf', 'für', 'ist', 'im', 'dem', 'nicht', 'ein', 'eine', 'als', 'auch',
    'es', 'an', 'werden', 'aus', 'er', 'hat', 'dass', 'sie', 'nach', 'wird',
    'bei', 'einer', 'um', 'am', 'sind', 'noch', 'wie', 'einem', 'über'
}

# Distinctive colloquial Indonesian markers. These words are very rare in other
# languages, so they are weighted 2x while scoring Indonesian evidence. Without
# this weighting, short or jargon-heavy Indonesian transcripts fell below the
# evidence threshold and were reported as English.
ID_MARKERS = {
    'nggak', 'gak', 'ga', 'gitu', 'gini', 'udah', 'banget', 'bang', 'aja', 'saja',
    'kayak', 'kaya', 'emang', 'bener', 'bgt', 'gimana', 'kenapa', 'kalo', 'kalau',
    'sih', 'deh', 'dong', 'kok', 'nih', 'tuh', 'gw', 'gue', 'lu', 'lo', 'kalian',
    'bikin', 'ngomong', 'pengen', 'males', 'gampang', 'cuma', 'dapet', 'ngerti',
}

ID_WEIGHTS = {w: 1 for w in ID_STOPWORDS}
ID_WEIGHTS.update({w: 2 for w in ID_MARKERS})

def detect_transcript_language(transcript_lines: List[dict], title: str = "") -> dict:
    """
    Detects the primary spoken language of the video transcript using character script inspection
    and stopword analysis across Indonesian, English, Spanish, Portuguese, French, German, and others.
    Returns dict: {'code': 'id', 'name': 'Indonesian (Bahasa Indonesia)', 'confidence': float,
                   'uncertain': bool}

    `uncertain=True` means the transcript did not contain enough evidence for a confident call
    (short clip, jargon-only speech, partially fetched transcript). Callers must NOT hard-force
    English in that case - the AI is asked to decide from the dialogue itself. This function used
    to silently default to English, which is what turned Indonesian videos into English hooks,
    captions and subtitles. A code of 'auto' means "no usable evidence at all", so the caller
    lets the AI detect the spoken language on its own.
    """
    if not transcript_lines and not title:
        return {'code': 'auto', 'name': 'Auto-detect', 'confidence': 0.0, 'uncertain': True}

    sample_texts = [title] if title else []
    # Wider window than the old 150-line cap: long videos frequently open with an
    # English intro/promo while the actual dialogue is in another language.
    for line in (transcript_lines[:400] if transcript_lines else []):
        t = line.get("text", "")
        if t:
            sample_texts.append(t)
    
    full_sample = " ".join(sample_texts).strip()
    if not full_sample:
        return {'code': 'auto', 'name': 'Auto-detect', 'confidence': 0.0, 'uncertain': True}

    # 1. Non-Latin script checks
    if re.search(r'[\u3040-\u309F\u30A0-\u30FF]', full_sample):
        return {'code': 'ja', 'name': LANGUAGE_NAMES['ja'], 'confidence': 0.98, 'uncertain': False}
    if re.search(r'[\uAC00-\uD7AF\u1100-\u11FF]', full_sample):
        return {'code': 'ko', 'name': LANGUAGE_NAMES['ko'], 'confidence': 0.98, 'uncertain': False}
    if re.search(r'[\u4E00-\u9FFF]', full_sample):
        return {'code': 'zh', 'name': LANGUAGE_NAMES['zh'], 'confidence': 0.95, 'uncertain': False}
    if re.search(r'[\u0600-\u06FF]', full_sample):
        return {'code': 'ar', 'name': LANGUAGE_NAMES['ar'], 'confidence': 0.98, 'uncertain': False}
    if re.search(r'[\u0400-\u04FF]', full_sample):
        return {'code': 'ru', 'name': LANGUAGE_NAMES['ru'], 'confidence': 0.98, 'uncertain': False}

    # 2. Latin word tokenization
    tokens = re.findall(r'\b[a-zA-Z\u00C0-\u024F\u1E00-\u1EFF]+\b', full_sample.lower())
    if not tokens:
        return {'code': 'auto', 'name': 'Auto-detect', 'confidence': 0.0, 'uncertain': True}

    counts = {
        'id': sum(ID_WEIGHTS.get(w, 0) for w in tokens),
        'en': sum(1 for w in tokens if w in EN_STOPWORDS),
        'es': sum(1 for w in tokens if w in ES_STOPWORDS),
        'pt': sum(1 for w in tokens if w in PT_STOPWORDS),
        'fr': sum(1 for w in tokens if w in FR_STOPWORDS),
        'de': sum(1 for w in tokens if w in DE_STOPWORDS),
    }

    best_lang, best_score = max(counts.items(), key=lambda item: item[1])
    total_matches = sum(counts.values())
    confidence = round(best_score / total_matches, 2) if total_matches > 0 else 0.0

    # Weak evidence: cross-check with title words, then report an honest "uncertain"
    # verdict instead of silently claiming English.
    if best_score < 2:
        title_tokens = set(re.findall(r'\b[a-zA-Z]+\b', title.lower()))
        if title_tokens.intersection(ID_STOPWORDS) or title_tokens.intersection(ID_MARKERS):
            return {'code': 'id', 'name': LANGUAGE_NAMES['id'], 'confidence': 0.7, 'uncertain': False}
        if title_tokens.intersection(ES_STOPWORDS):
            return {'code': 'es', 'name': LANGUAGE_NAMES['es'], 'confidence': 0.7, 'uncertain': False}
        if total_matches == 0:
            return {'code': 'auto', 'name': 'Auto-detect', 'confidence': 0.0, 'uncertain': True}
        return {
            'code': best_lang,
            'name': LANGUAGE_NAMES.get(best_lang, best_lang.upper()),
            'confidence': confidence,
            'uncertain': True,
        }

    return {
        'code': best_lang,
        'name': LANGUAGE_NAMES.get(best_lang, best_lang.upper()),
        'confidence': confidence,
        'uncertain': False,
    }


# Human friendly aliases accepted from the UI / API when the user forces an output language.
LANGUAGE_ALIASES = {
    'indonesian': 'id', 'bahasa': 'id', 'bahasa indonesia': 'id', 'ind': 'id',
    'english': 'en', 'eng': 'en', 'american english': 'en',
    'spanish': 'es', 'espanol': 'es', 'espa\u00f1ol': 'es', 'spa': 'es',
    'portuguese': 'pt', 'portugues': 'pt', 'portugu\u00eas': 'pt', 'por': 'pt',
    'french': 'fr', 'francais': 'fr', 'fran\u00e7ais': 'fr', 'fra': 'fr', 'fre': 'fr',
    'german': 'de', 'deutsch': 'de', 'deu': 'de', 'ger': 'de',
    'japanese': 'ja', 'jpn': 'ja',
    'korean': 'ko', 'kor': 'ko',
    'chinese': 'zh', 'mandarin': 'zh', 'zho': 'zh', 'chi': 'zh',
    'arabic': 'ar', 'ara': 'ar',
    'russian': 'ru', 'rus': 'ru',
}


def resolve_language_override(value: Optional[str]) -> Optional[dict]:
    """
    Normalizes a user supplied output-language override into {'code', 'name'}.

    Accepts 'auto' (no override), ISO codes ('id', 'en', 'pt-BR', 'zh-Hans') and
    human readable names ('Indonesian', 'Bahasa Indonesia'). Returns None when the
    output language should be auto-detected from the video itself.
    """
    if value is None:
        return None
    raw = str(value).strip().lower().replace('_', '-')
    if raw in ('', 'auto', 'automatic', 'detect', 'autodetect', 'none', 'default'):
        return None
    code = raw.split('-')[0]
    candidate = raw if raw in LANGUAGE_NAMES else code
    if candidate not in LANGUAGE_NAMES:
        candidate = LANGUAGE_ALIASES.get(raw) or LANGUAGE_ALIASES.get(code) or ''
    if candidate not in LANGUAGE_NAMES:
        return None
    return {'code': candidate, 'name': LANGUAGE_NAMES[candidate]}


def sanitize_first_person_title(title: str, speaker_or_channel: str = "", lang: str = "en") -> str:
    """
    Sanitizes accidental first-person perspective ('I', 'Me', 'My', 'Saya', 'Aku', 'Gue')
    from generated clip titles and title suggestions, replacing them with speaker or channel attribution,
    or objective framing so titles never appear as the user's personal opinion.
    Preserves the target language (Indonesian, English, Spanish, etc.).
    """
    if not title:
        return title
    t = title.strip()
    speaker = speaker_or_channel.strip() if speaker_or_channel else ""
    if lang == "id":
        subject = speaker if speaker else "Host"
    elif lang == "es":
        subject = speaker if speaker else "El Presentador"
    else:
        subject = speaker if speaker else "The Speaker"

    # 1. English Why / How / What / When / Where
    t = re.sub(r"^why\s+i\s+think\b", f"{subject} Explains Why", t, flags=re.IGNORECASE)
    t = re.sub(r"^why\s+i\s+believe\b", f"{subject} Explains Why", t, flags=re.IGNORECASE)
    t = re.sub(r"^why\s+i\s+", f"Why {subject} ", t, flags=re.IGNORECASE)
    t = re.sub(r"^how\s+i\s+", f"How {subject} ", t, flags=re.IGNORECASE)
    t = re.sub(r"^what\s+i\s+think\b", f"{subject}'s Thoughts On", t, flags=re.IGNORECASE)
    t = re.sub(r"^what\s+i\s+learned\b", f"What {subject} Learned", t, flags=re.IGNORECASE)
    t = re.sub(r"^what\s+i\s+", f"What {subject} ", t, flags=re.IGNORECASE)
    t = re.sub(r"^when\s+i\s+", f"When {subject} ", t, flags=re.IGNORECASE)
    t = re.sub(r"^where\s+i\s+", f"Where {subject} ", t, flags=re.IGNORECASE)

    # 2. English My [Noun] (e.g. My Opinion, My Story, My Regret)
    t = re.sub(r"^my\s+([a-zA-Z]+)", lambda m: f"{subject}'s {m.group(1)}" if speaker else f"The {m.group(1)}", t, flags=re.IGNORECASE)

    # 3. English First-person action verbs (e.g. I Tried, I Discovered, I Built)
    t = re.sub(r"^i\s+(tried|found|made|discovered|bought|quit|lost|learned|realized|spent|built|saw|went|started|joined|left|hate|love)\b", rf"{subject} \1", t, flags=re.IGNORECASE)
    t = re.sub(r"^i\s+was\b", f"{subject} Was", t, flags=re.IGNORECASE)
    t = re.sub(r"^i\s+am\b", f"{subject} Is", t, flags=re.IGNORECASE)
    t = re.sub(r"^i\s+have\b", f"{subject} Has", t, flags=re.IGNORECASE)
    t = re.sub(r"^i\s+had\b", f"{subject} Had", t, flags=re.IGNORECASE)
    t = re.sub(r"^i\s+got\b", f"{subject} Got", t, flags=re.IGNORECASE)
    t = re.sub(r"^i\s+think\b", f"{subject} Thinks", t, flags=re.IGNORECASE)
    t = re.sub(r"^i\s+believe\b", f"{subject} Believes", t, flags=re.IGNORECASE)

    # 4. Indonesian / Malay first-person replacements (Saya, Aku, Gue, Gw)
    indo_subject = speaker if speaker else "Host"
    t = re.sub(r"^(kenapa|mengapa)\s+(saya|aku|gue|gw)\s+", rf"\1 {indo_subject} ", t, flags=re.IGNORECASE)
    t = re.sub(r"^(cara|bagaimana)\s+(saya|aku|gue|gw)\s+", rf"Cara {indo_subject} ", t, flags=re.IGNORECASE)
    t = re.sub(r"^(alasan)\s+(saya|aku|gue|gw)\s+", rf"Alasan {indo_subject} ", t, flags=re.IGNORECASE)
    t = re.sub(r"^(saya|aku|gue|gw)\s+(mencoba|menemukan|membuat|yakin|berpikir|menyesal|kehilangan|belajar|mulai|berhenti)\b", rf"{indo_subject} \2", t, flags=re.IGNORECASE)
    t = re.sub(r"^(pendapat|opini)\s+(saya|aku|gue|gw)\b", rf"Opini {indo_subject}", t, flags=re.IGNORECASE)

    return t.strip()


def is_safe_remote_url(url: str, allowed_domains: Optional[set] = None) -> bool:
    """
    Validates if a URL is safe to fetch remotely, preventing SSRF attacks to
    local loopback, private IP subnets, or cloud metadata services.
    """
    if not url:
        return False
    u = url.strip()
    try:
        import ipaddress
        import urllib.parse

        parsed = urllib.parse.urlsplit(u)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = (parsed.hostname or "").lower().strip()
        if not hostname:
            return False

        # Block localhost / link-local / loopback hostnames
        if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "metadata.google.internal"):
            return False

        # Check if hostname is an IP address and verify if private/reserved
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        except ValueError:
            # Not an IP literal, it's a domain name
            pass

        if allowed_domains:
            if not any(hostname == d or hostname.endswith("." + d) for d in allowed_domains):
                return False

        return True
    except Exception:
        return False
