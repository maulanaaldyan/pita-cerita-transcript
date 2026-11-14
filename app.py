from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from youtube_transcript_api import YouTubeTranscriptApi
import os
import re
import json
import html
import xml.etree.ElementTree as ET
import requests

app = Flask(__name__, static_folder='.')
CORS(app)

ANGKA_MAP = {
    '1': 'satu', '2': 'dua', '3': 'tiga', '4': 'empat', '5': 'lima',
    '6': 'enam', '7': 'tujuh', '8': 'delapan', '9': 'sembilan', '10': 'sepuluh',
    '11': 'sebelas', '12': 'dua belas', '15': 'lima belas', '20': 'dua puluh',
    '30': 'tiga puluh', '50': 'lima puluh', '100': 'seratus', '1000': 'seribu'
}

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7"
}


def extract_video_id(url: str) -> str:
    """Ekstrak 11 karakter Video ID dari berbagai format URL YouTube (Shorts, Watch, Youtu.be)."""
    if not url:
        return ""
    url = url.strip()
    patterns = [
        r'(?:youtube\.com\/shorts\/)([a-zA-Z0-9_-]{11})',
        r'(?:youtube\.com\/watch\?v=)([a-zA-Z0-9_-]{11})',
        r'(?:youtu\.be\/)([a-zA-Z0-9_-]{11})',
        r'(?:youtube\.com\/embed\/)([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return ""


def parse_caption_xml(xml_text: str):
    """Mengubah XML subtitle YouTube menjadi teks bersih dan daftar timestamp."""
    segments = []
    timestamped = []
    root = ET.fromstring(xml_text)
    for elem in root.findall('.//text'):
        raw_txt = "".join(elem.itertext())
        txt = html.unescape(re.sub(r'\s+', ' ', raw_txt)).strip()
        if not txt:
            continue
        start_val = float(elem.attrib.get('start', 0.0) or 0.0)
        sec = int(start_val)
        mm, ss = divmod(sec, 60)
        segments.append(txt)
        timestamped.append(f"[{mm:02d}:{ss:02d}] {txt}")
    return " ".join(segments), "\n".join(timestamped)


def fetch_via_innertube_android(video_id: str):
    """Strategi cadangan menggunakan API Innertube Android YouTube (sering lolos blokir IP cloud Vercel)."""
    payload = {
        "context": {
            "client": {
                "clientName": "ANDROID",
                "clientVersion": "19.09.37",
                "androidSdkVersion": 30,
                "hl": "id",
                "gl": "ID"
            }
        },
        "videoId": video_id
    }
    resp = requests.post(
        "https://www.youtube.com/youtubei/v1/player",
        json=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "com.google.android.youtube/19.09.37 (Linux; U; Android 11) gzip"
        },
        timeout=12
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    tracks = data.get("captions", {}).get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])
    if not tracks:
        return None

    chosen = None
    for lang_prefix in ['id', 'en']:
        for tr in tracks:
            if str(tr.get("languageCode", "")).startswith(lang_prefix):
                chosen = tr
                break
        if chosen:
            break
    if not chosen:
        chosen = tracks[0]

    base_url = chosen.get("baseUrl", "")
    if not base_url:
        return None

    sub_resp = requests.get(base_url, headers=BROWSER_HEADERS, timeout=12)
    if sub_resp.status_code == 200 and "<text" in sub_resp.text:
        full_txt, ts_txt = parse_caption_xml(sub_resp.text)
        if full_txt:
            return full_txt, ts_txt, chosen.get("languageCode", "auto")
    return None


def fetch_via_watch_page(video_id: str):
    """Strategi cadangan membaca captionTracks langsung dari halaman watch YouTube."""
    resp = requests.get(f"https://www.youtube.com/watch?v={video_id}", headers=BROWSER_HEADERS, timeout=12)
    if resp.status_code != 200:
        return None
    match = re.search(r'"captionTracks":(\[.*?\])', resp.text)
    if not match:
        return None
    tracks = json.loads(match.group(1))
    if not tracks:
        return None

    chosen = None
    for lang_prefix in ['id', 'en']:
        for tr in tracks:
            if str(tr.get("languageCode", "")).startswith(lang_prefix):
                chosen = tr
                break
        if chosen:
            break
    if not chosen:
        chosen = tracks[0]

    base_url = chosen.get("baseUrl", "").replace("\\u0026", "&")
    if not base_url:
        return None

    sub_resp = requests.get(base_url, headers=BROWSER_HEADERS, timeout=12)
    if sub_resp.status_code == 200 and "<text" in sub_resp.text:
        full_txt, ts_txt = parse_caption_xml(sub_resp.text)
        if full_txt:
            return full_txt, ts_txt, chosen.get("languageCode", "auto")
    return None


def fetch_youtube_transcript(video_id: str):
    """
    Penyedot transkrip 3 lapis (YouTubeTranscriptApi -> Innertube Android -> Watch Page)
    agar tetap sukses di server cloud Vercel maupun lokal.
    """
    preferred_langs = ['id', 'en', 'en-US', 'en-GB']
    segments = []
    timestamped = []
    detected_lang = 'unknown'

    def append_seg(item):
        txt = item.get('text', '') if isinstance(item, dict) else getattr(item, 'text', '')
        start = item.get('start', 0.0) if isinstance(item, dict) else getattr(item, 'start', 0.0)
        txt = re.sub(r'\s+', ' ', str(txt)).strip()
        if txt:
            segments.append(txt)
            sec = int(float(start or 0))
            mm, ss = divmod(sec, 60)
            timestamped.append(f"[{mm:02d}:{ss:02d}] {txt}")

    # Lapis 1A: YouTubeTranscriptApi versi baru (>= 1.0.0)
    try:
        ytt_api = YouTubeTranscriptApi()
        if hasattr(ytt_api, 'list'):
            transcript_list = ytt_api.list(video_id)
            transcript_obj = None
            try:
                transcript_obj = transcript_list.find_transcript(preferred_langs)
            except Exception:
                for t in transcript_list:
                    transcript_obj = t
                    break
            if transcript_obj is not None:
                detected_lang = getattr(transcript_obj, 'language_code', 'auto')
                for item in transcript_obj.fetch():
                    append_seg(item)
                if segments:
                    return " ".join(segments), "\n".join(timestamped), detected_lang
        elif hasattr(ytt_api, 'fetch'):
            for item in ytt_api.fetch(video_id, languages=preferred_langs):
                append_seg(item)
            if segments:
                return " ".join(segments), "\n".join(timestamped), 'auto'
    except Exception:
        pass

    # Lapis 1B: YouTubeTranscriptApi versi lama (<= 0.6.x)
    try:
        if hasattr(YouTubeTranscriptApi, 'list_transcripts'):
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            transcript_obj = None
            try:
                transcript_obj = transcript_list.find_transcript(preferred_langs)
            except Exception:
                for t in transcript_list:
                    transcript_obj = t
                    break
            if transcript_obj is not None:
                detected_lang = getattr(transcript_obj, 'language_code', 'auto')
                for item in transcript_obj.fetch():
                    append_seg(item)
                if segments:
                    return " ".join(segments), "\n".join(timestamped), detected_lang

        if hasattr(YouTubeTranscriptApi, 'get_transcript'):
            for item in YouTubeTranscriptApi.get_transcript(video_id, languages=preferred_langs):
                append_seg(item)
            if segments:
                return " ".join(segments), "\n".join(timestamped), 'auto'
    except Exception:
        pass

    # Lapis 2: Innertube Android API (sering tembus di server cloud Vercel)
    try:
        res_android = fetch_via_innertube_android(video_id)
        if res_android:
            return res_android
    except Exception:
        pass

    # Lapis 3: Direct Watch Page Scraper
    try:
        res_watch = fetch_via_watch_page(video_id)
        if res_watch:
            return res_watch
    except Exception:
        pass

    raise RuntimeError("Video ini tidak memiliki Subtitle/CC publik, atau YouTube membatasi akses otomatis dari server cloud.")


def translate_to_indonesian_free(text: str) -> str:
    """Menerjemahkan transkrip panjang tanpa batas ke Bahasa Indonesia secara gratis per bagian kalimat."""
    if not text:
        return ""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks = []
    current = ""
    for sent in sentences:
        if len(current) + len(sent) + 1 > 1800 and current:
            chunks.append(current.strip())
            current = sent
        else:
            current = f"{current} {sent}".strip() if current else sent
    if current:
        chunks.append(current.strip())

    translated_chunks = []
    for chunk in chunks:
        try:
            resp = requests.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": "auto", "tl": "id", "dt": "t", "q": chunk},
                timeout=15
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data and isinstance(data[0], list):
                    parts = [seg[0] for seg in data[0] if seg and seg[0]]
                    joined = " ".join(parts).strip()
                    if joined:
                        translated_chunks.append(joined)
                        continue
        except Exception:
            pass
        translated_chunks.append(chunk)

    return " ".join(translated_chunks).strip()


def polish_pita_cerita_style(text: str) -> str:
    """Memformat teks menjadi 1 paragraf padat mengalir sesuai SOP Pita Cerita Sinema TANPA BATAS karakter/kata."""
    if not text:
        return ""
    s = re.sub(r'\[\d{1,2}:\d{2}(?::\d{2})?\]', ' ', text)
    s = re.sub(r'\[[^\]]*\]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()

    replacements = [
        (r'\bDia\b', 'Ia'),
        (r'\bdia\b', 'ia'),
        (r'\bTapi\b', 'Namun'),
        (r'\btapi\b', 'namun'),
        (r'\btetapi\b', 'namun'),
        (r'\bPada akhirnya\b', 'Hingga akhirnya'),
        (r'\bpada akhirnya\b', 'hingga akhirnya'),
        (r'\bSnout [Ll]ee\b', 'Snootley'),
        (r'\bCinema From The Sun\b', 'Pita Cerita Sinema'),
    ]
    for pat, rep in replacements:
        s = re.sub(pat, rep, s)

    def replace_num(match):
        val = match.group(0)
        return ANGKA_MAP.get(val, val)

    s = re.sub(r'\b(1|2|3|4|5|6|7|8|9|10|11|12|15|20|30|50|100|1000)\b', replace_num, s)

    if s:
        s = s[0].upper() + s.slice(1) if hasattr(s, 'slice') else s[0].upper() + s[1:]
        if s[-1] not in '.!?':
            s += '.'

    return s


def generate_pcs_draft_with_gemini(raw_text: str, api_key: str) -> str:
    """Menulis ulang transkrip dengan Gemini 2.5 Flash mengikuti gaya asli Pita Cerita Sinema tanpa batas kata."""
    if not api_key:
        return ""

    instruction = (
        "Kamu adalah penulis naskah senior untuk kanal YouTube 'Pita Cerita Sinema'. "
        "Terjemahkan (jika masih bahasa asing) dan tulis ulang transkrip berikut menjadi naskah AI Voice-Over Bahasa Indonesia "
        "dengan aturan wajib:\n"
        "1. JANGAN tambahkan kalimat hook basa-basi di luar cerita. Langsung buka kalimat pertama dengan AKSI KONKRET dari adegan pertama.\n"
        "2. Format WAJIB SATU PARAGRAF PENUH MENGALIR tanpa enter/baris baru sama sekali dan TANPA BATAS KATA/KARAKTER.\n"
        "3. Gunakan sudut pandang orang ketiga (POV 3) yang sinematik dan visual ('pria ini', 'gadis itu', 'sang ayah', 'ia', 'mereka').\n"
        "4. Hubungkan antar-adegan dengan konjungsi sebab-akibat yang rapat ('namun karena...', 'tepat saat...', 'sehingga ia...', 'membuatnya...', 'hingga akhirnya...').\n"
        "5. Tulis angka dalam bentuk kata terbilang bahasa Indonesia."
    )

    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    payload = {
        "contents": [{
            "parts": [{
                "text": f"{instruction}\n\nTranskrip Asli:\n{raw_text}\n\nLangsung tuliskan hasil naskahnya dalam 1 paragraf utuh dari awal hingga akhir tanpa dipotong:"
            }]
        }],
        "generationConfig": {
            "temperature": 0.55,
            "maxOutputTokens": 8192
        }
    }
    resp = requests.post(endpoint, json=payload, timeout=35)
    if resp.status_code == 200:
        data = resp.json()
        candidates = data.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            if parts:
                return polish_pita_cerita_style(parts[0].get("text", "").strip())
    return ""


@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "service": "Imaji Kreatif Transcript - Pita Cerita Sinema (Unlimited)"})


@app.route('/api/extract', methods=['POST'])
def extract_transcript():
    data = request.get_json(silent=True) or {}
    url = data.get('url', '')
    client_gemini_key = data.get('gemini_key', '') or os.environ.get('GEMINI_API_KEY', '')

    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({
            "status": "error",
            "message": "Format URL YouTube tidak dikenali. Pastikan link berupa YouTube Shorts, Watch, atau Youtu.be."
        }), 400

    try:
        raw_text, timestamped_text, lang = fetch_youtube_transcript(video_id)
        cleaned_raw = re.sub(r'\s+', ' ', raw_text).strip()

        pcs_draft = ""
        if client_gemini_key:
            try:
                pcs_draft = generate_pcs_draft_with_gemini(cleaned_raw, client_gemini_key)
            except Exception:
                pcs_draft = ""

        if not pcs_draft:
            translated = translate_to_indonesian_free(cleaned_raw)
            pcs_draft = polish_pita_cerita_style(translated)

        return jsonify({
            "status": "success",
            "video_id": video_id,
            "language": lang,
            "asli": cleaned_raw,
            "timestamped": timestamped_text,
            "pcs_draft": pcs_draft
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "video_id": video_id,
            "message": str(e)
        }), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
