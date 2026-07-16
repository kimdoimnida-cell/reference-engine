#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
레퍼런스 생성기 — 서버 (링크 → 전자동). 로컬/클라우드 공용.

파이프라인:
  인스타/유튜브/틱톡 링크
    → ① yt-dlp로 캡션(설명) + 오디오 추출  (인스타 실패 시 작성자 유튜브에서 동일 콘텐츠 대체)
    → ② Gemini 멀티모달 1회 호출: 음성 전사 + 한국어 번역 + 캡션/대사 분류 + 김대영(YLZ) 변환
  (faster-whisper·ffmpeg 불필요 — Gemini가 m4a 오디오를 직접 전사)

환경:
  GEMINI_API_KEY (env) 우선, 없으면 server/gemini_key.txt
  PORT (env) 없으면 8765
"""
import os
import re
import json
import base64
import tempfile
import traceback
from pathlib import Path
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from flask import Flask, request, jsonify, Response

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
STYLE_BIBLE = (ROOT / "YLZ_Style_Bible.md").read_text(encoding="utf-8")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY") or (
    (BASE / "gemini_key.txt").read_text(encoding="utf-8").strip()
    if (BASE / "gemini_key.txt").exists() else ""
)
MODEL = "gemini-flash-latest"
PORT = int(os.environ.get("PORT", "8765"))

app = Flask(__name__)

# ─────────────────────────── ① 추출 ───────────────────────────

def _ydl_opts(workdir, prefix="audio"):
    return {
        "format": "bestaudio/best",
        "outtmpl": str(workdir / (prefix + ".%(ext)s")),
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
    }


def extract_from_url(url, workdir):
    import yt_dlp
    with yt_dlp.YoutubeDL(_ydl_opts(workdir)) as ydl:
        info = ydl.extract_info(url, download=True)
    files = list(workdir.glob("audio.*"))
    if not files:
        raise RuntimeError("오디오 다운로드 실패")
    return {
        "audio": files[0],
        "caption": (info.get("description") or "").strip(),
        "uploader": info.get("uploader") or info.get("uploader_id") or "",
        "title": info.get("title") or "",
        "platform": (info.get("extractor_key") or "").lower(),
        "twin_url": "",
    }


def find_youtube_twin(uploader, hint, workdir):
    """인스타 추출 실패 시: 작성자명+힌트로 유튜브에서 동일 콘텐츠를 찾아 대체."""
    import yt_dlp
    query = f"ytsearch3:{uploader} {hint}".strip()
    opts = _ydl_opts(workdir, prefix="yt")
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=True)
    entries = info.get("entries") or []
    files = list(workdir.glob("yt.*"))
    if not entries or not files:
        raise RuntimeError("유튜브 대체 콘텐츠도 못 찾음")
    e = entries[0]
    return {
        "audio": files[0],
        "caption": (e.get("description") or "").strip(),
        "uploader": e.get("uploader") or uploader,
        "title": e.get("title") or "",
        "platform": "youtube(대체)",
        "twin_url": e.get("webpage_url") or "",
    }

# ─────────────────── ② Gemini 멀티모달 (전사+번역+분류+변환) ───────────────────

SYSTEM_PROMPT = f"""당신은 김대영(YLZ MEDIA)의 콘텐츠 재창작 엔진이다.
입력으로 레퍼런스 영상의 '음성 오디오'와 '원본 캡션(글)'을 받는다. 다음을 순서대로 수행해 JSON으로 출력한다.

1) 전사: 오디오 속 말을 원어 그대로 정확히 받아 적는다. → raw_transcript
2) 번역: 캡션과 전사문이 한국어가 아니면 각각 자연스러운 한국어로 맥락 번역(직역 금지, 뉘앙스 유지). 한국어면 정리. → translated_caption, translated_script
3) 분류: 캡션(글)과 대사(말)를 분리해 각 핵심 요약. → caption_summary, script_summary
4) 재창작: 레퍼런스의 소재·구조만 차용하고 표현·훅·문장은 전부 아래 Style Bible의 김대영/김디오 톤으로 새로 쓴다. 원문 번역 복붙 금지.
   → mode, source_gist, hooks(정확히 3개, 각 type은 훅 5유형 중 택1 한국어 표기), script(구간별 4~6개), caption(캡션 구조·줄바꿈 포함), hashtags(2~5개)

모드 판정: 비즈·브랜딩·SNS수익화·AI면 '김대영'(기본), 상황극·글로벌엔터·공주님·목소리·제품광고면 '김디오'. 사용자가 지정하면 따른다.
모든 출력 텍스트는 한국어(김디오 글로벌 광고 모드만 예외적으로 영어 허용).

아래 Style Bible을 반드시 지킨다.

{STYLE_BIBLE}"""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "raw_transcript": {"type": "STRING"},
        "translated_caption": {"type": "STRING"},
        "translated_script": {"type": "STRING"},
        "caption_summary": {"type": "STRING"},
        "script_summary": {"type": "STRING"},
        "mode": {"type": "STRING", "enum": ["김대영", "김디오"]},
        "source_gist": {"type": "STRING"},
        "hooks": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "type": {"type": "STRING"}, "text": {"type": "STRING"}}, "required": ["type", "text"]}},
        "script": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "time": {"type": "STRING"}, "role": {"type": "STRING"}, "text": {"type": "STRING"}},
            "required": ["time", "role", "text"]}},
        "caption": {"type": "STRING"},
        "hashtags": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["raw_transcript", "translated_caption", "translated_script", "caption_summary",
                 "script_summary", "mode", "source_gist", "hooks", "script", "caption", "hashtags"],
}


def gemini_pipeline(audio_path, caption_raw, note, mode):
    mode_line = (f"[모드 지정] '{mode}' 모드로 작성하라." if mode and mode != "auto" else "[모드] 자동 판정.")
    user_text = f"""[레퍼런스]
## 원본 캡션(글)
{caption_raw or '(없음)'}

## 추가 지시
{note or '(없음)'}
{mode_line}

첨부된 오디오를 전사한 뒤, 위 캡션과 함께 번역·분류하고 Style Bible에 따라 김대영(YLZ) 콘텐츠로 변환해 JSON으로 출력하라."""

    audio_b64 = base64.b64encode(Path(audio_path).read_bytes()).decode()
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [
            {"inline_data": {"mime_type": "audio/mp4", "data": audio_b64}},
            {"text": user_text},
        ]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
            "maxOutputTokens": 16000,
            "temperature": 0.9,
            "thinkingConfig": {"thinkingBudget": 2048},
        },
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_KEY}"
    data = json.dumps(payload).encode("utf-8")
    # Gemini가 가끔 503/429(과부하)를 뱉음 → 지수 백오프로 자동 재시도
    last_err = None
    for attempt in range(4):
        try:
            req = Request(url, data=data, headers={"content-type": "application/json"})
            with urlopen(req, timeout=180) as r:
                out = json.loads(r.read().decode("utf-8"))
            break
        except HTTPError as e:
            last_err = e
            if e.code in (429, 500, 503) and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    else:
        raise last_err
    parts = out["candidates"][0]["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts)
    return json.loads(text)

# ─────────────────── 전략 분석 (퍼널 해부) ───────────────────

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
_TAG = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_HREF = re.compile(r'href=["\'](https?://[^"\'>\s]+)["\']', re.I)
_STRIP = re.compile(r"<[^>]+>")
_SALES_KW = re.compile(r"course|checkout|buy|pay|price|pricing|cart|shop|store|product|coaching|consult|program|enroll|join|book|calendar|cal\.com|calendly|stan\.store|gumroad|kajabi|teachable|udemy|smartstore|payhip|lemonsqueeze|class101|krw|won|\$|원", re.I)


def _fetch_text(url, limit=4000):
    try:
        req = Request(url, headers={"User-Agent": _UA})
        with urlopen(req, timeout=12) as r:
            html = r.read(600000).decode("utf-8", "ignore")
    except Exception as e:
        return "", [], f"(가져오기 실패: {str(e)[:40]})"
    links = list(dict.fromkeys(_HREF.findall(html)))
    body = _STRIP.sub(" ", _TAG.sub(" ", html))
    body = re.sub(r"\s+", " ", body).strip()
    return body[:limit], links, ""


def fetch_funnel(profile_url):
    """프로필/링크인바이오 URL → 본문 + 유망한 아웃바운드 링크(판매/가격) 몇 개 크롤해 텍스트로."""
    main_text, links, err = _fetch_text(profile_url, 4000)
    parts = [f"[메인 페이지: {profile_url}]\n{main_text or err}"]
    # 판매·가격 신호가 있는 아웃바운드 링크 우선 최대 3개
    picks, seen_dom = [], set()
    for L in links:
        dom = re.sub(r"^https?://([^/]+).*", r"\1", L)
        if any(x in dom for x in ("instagram.com", "facebook.", "tiktok.", "youtube.", "cdn", "fonts.", "gstatic", "google")):
            continue
        if _SALES_KW.search(L) or dom not in seen_dom:
            picks.append(L); seen_dom.add(dom)
        if len(picks) >= 3:
            break
    for L in picks:
        t, _, e = _fetch_text(L, 2500)
        parts.append(f"[연결 페이지: {L}]\n{t or e}")
    return "\n\n".join(parts)[:8000]


STRATEGY_SYSTEM = """당신은 SNS 퍼널·수익화 전략 분석가다. 레퍼런스 크리에이터의 콘텐츠(캡션+대사)와, 있다면 프로필/링크인바이오/랜딩 페이지 크롤 텍스트를 받아
'이 사람이 이 콘텐츠로 무엇을 어떻게 파는가'를 해부해 JSON으로 출력한다.

원칙:
- 근거 우선. 입력에서 확인되는 건 단정, 추정은 '추정'이라 명시. 없는 수치·가격 창작 금지.
- 실용적으로. 두루뭉술한 말 금지. 김대영(YLZ)이 바로 벤치마킹할 수 있게.
- 모든 텍스트 한국어.

분석 항목:
1. funnel_stage: 이 콘텐츠의 목적 (인지 / 리드수집 / 세일즈 중 택1 + 한 줄 근거)
2. cta_mechanism: 무엇을 시키는가 (댓글 리드마그넷 / DM / 프로필 링크 / 팔로우·저장 등) + cta_detail로 정확히 뭐라고 했는지
3. lead_magnet: 미끼 자료 분석 — promised(뭘 준다 했나), likely_contents(그 안에 뭐가 들었을지 추정), why(왜 이걸 미끼로)
4. ticket: 로우티켓 vs 하이티켓 신호 — low_or_high(저가/고가/복합/불명), evidence(근거)
5. profile_funnel: 프로필/링크인바이오 크롤 텍스트가 있으면 채운다. 없으면 available=false. linkinbio_structure(링크 구성·순서), products(이름·가격대tier·메모 배열), email_capture(이메일 수집 여부·방식), upsell_path(업셀 경로)
6. full_funnel_map: 미끼→저가→고가로 어떻게 태우는지 한 문단(추정 포함, 표시)
7. benchmark_takeaway: 김대영/YLZ가 이걸 훔친다면 구체 액션 3개(배열)
8. limits: 자동으로 확인 못 한 부분(예: 댓글-DM 자동응답 미끼 실물, JS로 가려진 가격 등) 한 줄"""

STRATEGY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "funnel_stage": {"type": "STRING"},
        "cta_mechanism": {"type": "STRING"},
        "cta_detail": {"type": "STRING"},
        "lead_magnet": {"type": "OBJECT", "properties": {
            "promised": {"type": "STRING"}, "likely_contents": {"type": "STRING"}, "why": {"type": "STRING"}},
            "required": ["promised", "likely_contents", "why"]},
        "ticket": {"type": "OBJECT", "properties": {
            "low_or_high": {"type": "STRING"}, "evidence": {"type": "STRING"}},
            "required": ["low_or_high", "evidence"]},
        "profile_funnel": {"type": "OBJECT", "properties": {
            "available": {"type": "BOOLEAN"},
            "linkinbio_structure": {"type": "STRING"},
            "products": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "name": {"type": "STRING"}, "price_tier": {"type": "STRING"}, "note": {"type": "STRING"}},
                "required": ["name", "price_tier", "note"]}},
            "email_capture": {"type": "STRING"}, "upsell_path": {"type": "STRING"}},
            "required": ["available", "linkinbio_structure", "products", "email_capture", "upsell_path"]},
        "full_funnel_map": {"type": "STRING"},
        "benchmark_takeaway": {"type": "ARRAY", "items": {"type": "STRING"}},
        "limits": {"type": "STRING"},
    },
    "required": ["funnel_stage", "cta_mechanism", "cta_detail", "lead_magnet", "ticket",
                 "profile_funnel", "full_funnel_map", "benchmark_takeaway", "limits"],
}


def gemini_json(system, user, schema, max_tokens=8000):
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema,
                             "maxOutputTokens": max_tokens, "temperature": 0.7, "thinkingConfig": {"thinkingBudget": 2048}},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_KEY}"
    data = json.dumps(payload).encode("utf-8")
    last = None
    for attempt in range(4):
        try:
            with urlopen(Request(url, data=data, headers={"content-type": "application/json"}), timeout=180) as r:
                out = json.loads(r.read().decode("utf-8"))
            break
        except HTTPError as e:
            last = e
            if e.code in (429, 500, 503) and attempt < 3:
                time.sleep(2 * (attempt + 1)); continue
            raise
    else:
        raise last
    parts = out["candidates"][0]["content"]["parts"]
    return json.loads("".join(p.get("text", "") for p in parts))


@app.post("/api/strategy")
def strategy():
    if not GEMINI_KEY:
        return jsonify({"error": "서버에 AI 키가 설정되지 않았어요."}), 503
    body = request.get_json(force=True, silent=True) or {}
    caption = (body.get("caption") or "").strip()
    transcript = (body.get("transcript") or "").strip()
    profile_url = (body.get("profile_url") or "").strip()
    if not (caption or transcript):
        return jsonify({"error": "분석할 콘텐츠(캡션·대사)가 없어요."}), 400
    funnel_text = ""
    if re.match(r"^https?://", profile_url):
        funnel_text = fetch_funnel(profile_url)
    user = f"""[레퍼런스 콘텐츠]
## 캡션
{caption or '(없음)'}

## 대사(전사)
{transcript or '(없음)'}

## 프로필/링크인바이오/랜딩 크롤 텍스트
{funnel_text or '(프로필 URL 미제공 — profile_funnel.available=false 로)'}

위를 근거로 이 크리에이터의 퍼널·수익화 전략을 해부해 JSON으로 출력하라."""
    try:
        result = gemini_json(STRATEGY_SYSTEM, user, STRATEGY_SCHEMA)
        result["_funnel_crawled"] = bool(funnel_text)
        return jsonify({"result": result})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"전략 분석 오류: {e}"}), 500


# ─────────────────────────── 라우트 ───────────────────────────

@app.post("/api/run")
def run():
    body = request.get_json(force=True, silent=True) or {}
    url = (body.get("url") or "").strip()
    mode = body.get("mode") or "auto"
    note = body.get("note") or ""
    if not re.match(r"^https?://", url):
        return jsonify({"error": "링크(URL)를 넣어주세요."}), 400
    if not GEMINI_KEY:
        return jsonify({"error": "서버에 AI 키가 설정되지 않았어요."}), 503
    steps = []
    try:
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            try:
                ex = extract_from_url(url, workdir)
                steps.append(f"추출: {ex['platform'] or '링크'} 직접 성공")
            except Exception as e1:
                steps.append(f"직접 추출 실패({str(e1)[:50]}) → 유튜브 대체 검색")
                handle = re.search(r"instagram\.com/([^/?]+)", url)
                uploader = handle.group(1) if handle else ""
                ex = find_youtube_twin(uploader or "reels", note or "shorts", workdir)
                steps.append(f"유튜브 대체: {ex.get('twin_url','')}")
            result = gemini_pipeline(ex["audio"], ex["caption"], note, mode)
            steps.append("전사·번역·분류·김대영 변환 완료")
            result["_source"] = {
                "platform": ex.get("platform", ""),
                "uploader": ex.get("uploader", ""),
                "twin_url": ex.get("twin_url", ""),
                "caption_raw": ex.get("caption", ""),
                "script_raw": result.get("raw_transcript", ""),
                "steps": steps,
            }
            return jsonify({"result": result})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"처리 중 오류: {e}", "steps": steps}), 500


@app.get("/health")
def health():
    return jsonify({"ok": True, "key": bool(GEMINI_KEY)})


@app.get("/")
def index():
    return Response((BASE / "index.html").read_text(encoding="utf-8"), mimetype="text/html")


if __name__ == "__main__":
    print(f"레퍼런스 생성기 서버 → http://127.0.0.1:{PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
