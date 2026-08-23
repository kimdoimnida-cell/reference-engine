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
from urllib.error import HTTPError, URLError

from flask import Flask, request, jsonify, Response

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
STYLE_BIBLE = (ROOT / "YLZ_Style_Bible.md").read_text(encoding="utf-8")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY") or (
    (BASE / "gemini_key.txt").read_text(encoding="utf-8").strip()
    if (BASE / "gemini_key.txt").exists() else ""
)
# flash-latest(=최신 프리뷰)는 무료 티어 오디오 쿼터가 자주 막힘(429) → 검증된 모델 우선, 막히면 다음으로 폴백
MODELS = ["gemini-3-flash-preview", "gemini-flash-latest", "gemini-2.5-flash"]
PORT = int(os.environ.get("PORT", "8765"))


def _gemini_call(payload):
    """모델 폴백 체인 × 재시도. 429/503이면 같은 모델 재시도 후 다음 모델로."""
    data = json.dumps(payload).encode("utf-8")
    last = None
    for model in MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
        for attempt in range(2):
            try:
                req = Request(url, data=data, headers={"content-type": "application/json"})
                with urlopen(req, timeout=300) as r:
                    return json.loads(r.read().decode("utf-8"))
            except HTTPError as e:
                last = e
                if e.code in (404, 429, 500, 503):
                    time.sleep(2 * (attempt + 1))
                    continue
                raise
            except (TimeoutError, URLError, OSError) as e:
                # 읽기 타임아웃·네트워크 순단 → 재시도 후 다음 모델로
                last = e
                time.sleep(2)
                continue
    raise RuntimeError(f"AI 호출 실패(모든 모델 시도): {last}")


# ─────────────────── 노션 「YLZ 콘텐츠 발행 캘린더」 자동 적재 ───────────────────
NOTION_TOKEN_FILE = Path.home() / ".notion" / "token.txt"
NOTION_TOKEN = NOTION_TOKEN_FILE.read_text(encoding="utf-8").strip() if NOTION_TOKEN_FILE.exists() else ""
NOTION_DB = "164755620a4740459ffc1b806c46c981"  # YLZ 콘텐츠 발행 캘린더


NOTION_HEADERS = {"Authorization": f"Bearer {NOTION_TOKEN}",
                  "Content-Type": "application/json",
                  "Notion-Version": "2022-06-28"}


def _nrt(text):
    return [{"text": {"content": str(text)[:1800]}}] if text else []


def _nblock(kind, text):
    return {"object": "block", "type": kind, kind: {"rich_text": _nrt(text)}}


def _split(text, size=1800):
    """노션 rich_text 2000자 제한 → 문단(빈 줄) → 줄 → 강제 순으로 잘라 조각 리스트.

    size는 `_nrt`의 1800자 컷과 반드시 같거나 작아야 한다. 크면 조각마다 _nrt가 다시 잘라
    원문이 소리 없이 사라진다(1900으로 뒀다가 조각당 100자씩 손실난 적 있음).
    """
    text = (str(text) if text is not None else "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    out, buf = [], ""
    for para in re.split(r"\n\s*\n", text):
        for line in (para.split("\n") if len(para) > size else [para]):
            while len(line) > size:                     # 줄 자체가 초과 → 강제 분할
                if buf:
                    out.append(buf); buf = ""
                out.append(line[:size]); line = line[size:]
            piece = (buf + "\n\n" + line) if buf else line
            if len(piece) > size:
                out.append(buf); buf = line
            else:
                buf = piece
    if buf:
        out.append(buf)
    return out


_SENT = re.compile(r"(?<=[.!?…])\s+")


def _readable(text, per=3, thresh=300):
    """줄바꿈이 하나도 없는 긴 덩어리를 문장 3개씩 묶어 문단으로 나눈다.

    공백만 넣을 뿐 내용은 건드리지 않는다. 이미 줄바꿈이 있으면(캡션 등) 원저자 구조를 존중해 그대로 둔다.
    """
    t = (text or "").strip()
    if not t or len(t) < thresh or "\n" in t:
        return t
    sents = [s for s in _SENT.split(t) if s.strip()]
    if len(sents) <= per:
        return t
    return "\n\n".join(" ".join(sents[i:i + per]) for i in range(0, len(sents), per))


def _paras(text):
    """긴 텍스트 → 문단별 paragraph 블록(잘림 없음). 비면 '(없음)'."""
    t = _readable(text)
    blocks = []
    for para in re.split(r"\n\s*\n", t) if t else []:
        para = para.strip()
        if para:
            blocks += [_nblock("paragraph", p) for p in _split(para)]
    return blocks or [_nblock("paragraph", "(없음)")]


def _archive_children(result, source_url):
    """페이지 하단 '원문 아카이브' 토글에 들어갈 자식 블록들."""
    src = result.get("_source", {}) or {}
    out = [_nblock("heading_3", "소스 정보")]
    info = [f"플랫폼: {src.get('platform') or '-'}",
            f"작성자: {src.get('uploader') or '-'}",
            f"원본 URL: {source_url or '-'}"]
    if src.get("twin_url"):
        info.append(f"유튜브 대체: {src['twin_url']}")
    if src.get("saved_dir"):
        info.append(f"다운로드 폴더: {src['saved_dir']}")
    if src.get("steps"):
        info.append("처리 스텝: " + " / ".join(src["steps"]))
    out += [_nblock("bulleted_list_item", x) for x in info]

    if result.get("_type") == "cards":
        out.append(_nblock("heading_3", "슬라이드 원문 (OCR)"))
        slides = result.get("slides_ocr") or []
        if slides:
            for s in slides:
                body = f"[{s.get('n','')}] {s.get('headline','')}"
                if s.get("body"):
                    body += "\n" + s["body"]
                out += _paras(body)
        else:
            out += _paras(src.get("caption_raw"))
        out.append(_nblock("heading_3", "한국어 번역·요약"))
        out += _paras(result.get("translated"))
    else:
        out.append(_nblock("heading_3", "원본 캡션 (원어)"))
        out += _paras(src.get("caption_raw"))
        out.append(_nblock("heading_3", "원문 전사 (원어)"))
        out += _paras(result.get("raw_transcript") or src.get("script_raw"))
        out.append(_nblock("heading_3", "한국어 번역 — 캡션"))
        out += _paras(result.get("translated_caption"))
        out.append(_nblock("heading_3", "한국어 번역 — 대사"))
        out += _paras(result.get("translated_script"))
        out.append(_nblock("heading_3", "핵심 요약"))
        out += [_nblock("bulleted_list_item", f"캡션: {result.get('caption_summary') or '-'}"),
                _nblock("bulleted_list_item", f"대사: {result.get('script_summary') or '-'}")]
    return out


def _archive_toggles(result, source_url, size=90):
    """원문 블록들을 접힌 토글로 감싼다. 자식 100개 제한 → 넘치면 토글을 나눔."""
    kids = _archive_children(result, source_url)
    groups = []
    for i in range(0, len(kids), size):
        n = i // size + 1
        title = "원문 · 레퍼런스 아카이브" + (f" ({n})" if i else "")
        groups.append({"object": "block", "type": "toggle",
                       "toggle": {"rich_text": _nrt(title), "children": kids[i:i + size]}})
    return groups


def notion_append(page_id, blocks, size=90):
    """children 100개 제한 → 나눠서 이어붙이기."""
    for i in range(0, len(blocks), size):
        req = Request(f"https://api.notion.com/v1/blocks/{page_id}/children",
                      data=json.dumps({"children": blocks[i:i + size]}).encode("utf-8"),
                      headers=NOTION_HEADERS, method="PATCH")
        with urlopen(req, timeout=60) as r:
            r.read()


def local_save(result, source_url):
    """노션과 별개로 원문·번역·재창작을 로컬 output/ 에도 보관(노션 장애 대비)."""
    try:
        src = result.get("_source", {}) or {}
        d = ROOT / "output" / f"{time.strftime('%Y%m%d_%H%M')}_{_shortcode(source_url)}"
        d.mkdir(parents=True, exist_ok=True)
        head = (f"# 원문\n\n- 플랫폼: {src.get('platform','')}\n- 작성자: {src.get('uploader','')}\n"
                f"- 원본 URL: {source_url}\n- 유튜브 대체: {src.get('twin_url','')}\n\n")
        if result.get("_type") == "cards":
            slides = "\n\n".join(f"[{s.get('n','')}] {s.get('headline','')}\n{s.get('body','')}"
                                 for s in (result.get("slides_ocr") or []))
            (d / "01_source.md").write_text(head + "## 슬라이드 OCR\n\n" + slides, encoding="utf-8")
            (d / "02_translated.md").write_text(f"# 번역·요약\n\n{result.get('translated','')}", encoding="utf-8")
        else:
            (d / "01_source.md").write_text(
                head + f"## 원본 캡션\n\n{src.get('caption_raw','')}\n\n"
                       f"## 원문 전사\n\n{result.get('raw_transcript') or src.get('script_raw','')}", encoding="utf-8")
            (d / "02_translated.md").write_text(
                f"# 번역\n\n## 캡션\n\n{result.get('translated_caption','')}\n\n"
                f"## 대사\n\n{result.get('translated_script','')}\n\n"
                f"# 요약\n\n- 캡션: {result.get('caption_summary','')}\n- 대사: {result.get('script_summary','')}",
                encoding="utf-8")
        (d / "04_recreated.md").write_text(
            "# 재창작\n\n```json\n" + json.dumps(
                {k: v for k, v in result.items() if not k.startswith("_")}, ensure_ascii=False, indent=2) + "\n```",
            encoding="utf-8")
        return str(d)
    except Exception:
        traceback.print_exc()
        return ""


SALES_LINES = ["클로드코드·AI자동화", "콘텐츠 대행·컨설팅", "고단가 판매법"]
CATEGORIES = ["Marketing", "Automation", "Guide", "Insight", "Industry", "AI", "Ideas", "Case Study"]
FUNNELS = ["유입", "신뢰", "전환"]

TAG_SYSTEM = """너는 YLZ MEDIA 콘텐츠 운영자다. 레퍼런스에서 재창작한 콘텐츠 한 편을 받아 발행 캘린더에 넣을 메타데이터를 정한다.

title: 보드 카드에서 한눈에 읽히는 짧은 제목. 25자 내외, 요약문을 그대로 옮기지 말고 제목으로 다듬어라. 마침표로 끝내지 마라.
sales_line: 이 콘텐츠가 결국 어떤 상품으로 연결되는가.
  - 클로드코드·AI자동화: AI 도구·자동화·개발·시스템 구축
  - 콘텐츠 대행·컨설팅: SNS 운영·콘텐츠 제작·브랜딩·채널 성장
  - 고단가 판매법: 세일즈·설득·오퍼 설계·전환 구조·수익화
category: 콘텐츠 성격.
funnel: 유입(모르는 사람을 끌어옴) / 신뢰(아는 사람이 믿게 함) / 전환(믿는 사람이 사게 함)."""

TAG_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "sales_line": {"type": "string", "enum": SALES_LINES},
        "category": {"type": "string", "enum": CATEGORIES},
        "funnel": {"type": "string", "enum": FUNNELS},
    },
    "required": ["title", "sales_line", "category", "funnel"],
}


def auto_tag(result):
    """재창작 결과를 읽고 제목·판매 라인·카테고리·퍼널 단계를 정한다.

    실패하면 빈 dict. 태깅이 없어도 적재 자체는 계속된다.
    """
    try:
        hooks = result.get("hooks") or []
        cards = result.get("cards") or []
        script = result.get("script") or []
        body = " / ".join([c.get("headline", "") for c in cards[:6]] or
                          [s.get("text", "") for s in script[:6]])
        user = (f"요약: {result.get('source_gist', '')}\n"
                f"훅: {' | '.join(h.get('text', '') for h in hooks[:3])}\n"
                f"본문: {body[:1200]}\n"
                f"캡션: {(result.get('caption') or '')[:600]}")
        return gemini_json(TAG_SYSTEM, user, TAG_SCHEMA, max_tokens=1000)
    except Exception:
        traceback.print_exc()
        return {}


def notion_save(result, source_url):
    """변환 결과 + 원문 아카이브를 발행 캘린더에 '작성중'으로 적재.

    반환 (페이지 URL, 경고문). 실패해도 예외를 밖으로 던지지 않는다.
    원문 적재만 실패해도 재창작 페이지는 남긴다.
    """
    if not NOTION_TOKEN:
        return "", "노션 토큰 없음"
    try:
        src = result.get("_source", {})
        hooks = result.get("hooks") or [{}]
        gist = (result.get("source_gist") or hooks[0].get("text") or "레퍼런스 재창작")
        tag = auto_tag(result)
        title = (tag.get("title") or gist)[:90]
        memo = f"레퍼런스 생성기 자동 저장 · 모드 {result.get('mode', '')} · {src.get('platform', '')}"
        if tag.get("title"):
            memo += f" | 원문 요약: {gist}"
        blocks = [_nblock("heading_2", "훅 후보")]
        blocks += [_nblock("bulleted_list_item", f"[{h.get('type','')}] {h.get('text','')}") for h in hooks[:5]]
        if result.get("_type") == "cards":
            blocks.append(_nblock("heading_2", "카드 구성"))
            blocks += [_nblock("bulleted_list_item",
                               f"{c.get('n','')}. [{c.get('role','')}] {c.get('headline','')} — {c.get('body','')}")
                       for c in (result.get("cards") or [])[:30]]
        else:
            blocks.append(_nblock("heading_2", "대본"))
            blocks += [_nblock("bulleted_list_item",
                               f"{s.get('time','')} [{s.get('role','')}] {s.get('text','')}")
                       for s in (result.get("script") or [])[:20]]
        blocks.append(_nblock("heading_2", "캡션"))
        blocks.append(_nblock("paragraph", result.get("caption") or ""))
        blocks.append(_nblock("heading_2", "해시태그"))
        blocks.append(_nblock("paragraph", " ".join(result.get("hashtags") or [])))
        props = {
            "주제": {"title": _nrt(title)},
            "상태": {"select": {"name": "작성중"}},
            "소스 메모": {"rich_text": _nrt(memo)},
        }
        if source_url:
            props["벤치마킹 소스"] = {"url": source_url}
        if tag.get("sales_line"):
            props["판매 라인"] = {"select": {"name": tag["sales_line"]}}
        if tag.get("category"):
            props["카테고리"] = {"select": {"name": tag["category"]}}
        if tag.get("funnel"):
            props["퍼널 단계"] = {"select": {"name": tag["funnel"]}}
        ch = {"cards": "카드뉴스"}.get(result.get("_type"), "릴스")
        props["채널"] = {"multi_select": [{"name": ch}]}
        # ① 재창작 결과로 페이지 먼저 생성 (children 100개 제한 → 앞 90개만)
        payload = {"parent": {"database_id": NOTION_DB}, "properties": props, "children": blocks[:90]}
        req = Request("https://api.notion.com/v1/pages", data=json.dumps(payload).encode("utf-8"),
                      headers=NOTION_HEADERS)
        with urlopen(req, timeout=30) as r:
            page = json.loads(r.read().decode("utf-8"))
        page_id, page_url = page.get("id", ""), page.get("url", "")
    except Exception:
        traceback.print_exc()
        return "", "페이지 생성 실패"
    # ② 나머지 + 원문 아카이브는 별도 append (여기서 깨져도 페이지는 유지)
    try:
        notion_append(page_id, blocks[90:] + _archive_toggles(result, source_url))
        return page_url, ""
    except Exception:
        traceback.print_exc()
        return page_url, "원문 아카이브 적재 실패"

app = Flask(__name__)

# ─────────────────────────── ① 추출 ───────────────────────────

def _ydl_opts(workdir, prefix="audio", cookies=True):
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(workdir / (prefix + ".%(ext)s")),
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
    }
    # 인스타 익명 접속은 429/로그인 차단 — 카드뉴스(gallery-dl)와 동일하게 쿠키 사용
    if cookies:
        if COOKIES_FILE.exists():
            opts["cookiefile"] = str(COOKIES_FILE)
        elif COOKIES_BROWSER:
            opts["cookiesfrombrowser"] = (COOKIES_BROWSER,)
    return opts


def _ydl_extract(target, workdir, prefix="audio"):
    """쿠키 포함 시도 → 실패하면 쿠키 없이 1회 재시도."""
    import yt_dlp
    last = None
    for use_cookies in (True, False):
        try:
            with yt_dlp.YoutubeDL(_ydl_opts(workdir, prefix, cookies=use_cookies)) as ydl:
                return ydl.extract_info(target, download=True)
        except Exception as e:
            last = e
    raise last


def extract_from_url(url, workdir):
    info = _ydl_extract(url, workdir)
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
    query = f"ytsearch3:{uploader} {hint}".strip()
    info = _ydl_extract(query, workdir, prefix="yt")
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

[가독성 규칙 — raw_transcript·translated_caption·translated_script 공통]
말을 그대로 이어붙인 한 덩어리로 출력하지 마라. **주제가 바뀌는 지점마다 빈 줄(\\n\\n)로 문단을 나눈다**(문단당 2~4문장). 단어·표현은 절대 바꾸거나 요약하지 말고 줄바꿈만 넣는다.
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
    out = _gemini_call(payload)
    parts = out["candidates"][0]["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts)
    return json.loads(text)

# ─────────────────── ②-B 카드뉴스 (슬라이드 이미지 → OCR+번역+재창작) ───────────────────

CARD_SYSTEM_PROMPT = f"""당신은 김대영(YLZ MEDIA)의 콘텐츠 재창작 엔진이다.
입력으로 레퍼런스 '카드뉴스'의 슬라이드 이미지들을 순서대로(1번부터) 받는다. 다음을 순서대로 수행해 JSON으로 출력한다.

1) OCR: 각 슬라이드 이미지에서 보이는 텍스트를 순서대로 정확히 읽는다. 슬라이드마다 큰 제목(headline)과 본문(body)을 구분한다. 이미지 순서 = 카드 번호(n, 1부터). 워터마크·계정핸들·페이지표시(1/8 등)는 무시한다. → slides_ocr
2) 번역: 원문이 한국어가 아니면 자연스러운 한국어로 맥락 번역(직역 금지, 뉘앙스 유지). 한국어면 정리. **슬라이드 단위로 빈 줄(\\n\\n)을 넣어 문단을 나눠 출력**(한 덩어리 금지). 끝에 전체 흐름 요약 한 문단. → translated
3) 재창작: 레퍼런스의 소재·구조(카드 수·전개 순서·기승전결)만 차용하고 표현·훅·문장은 전부 아래 Style Bible의 김대영/김디오 톤으로 새로 쓴다. 원문 번역 복붙 금지.
   → mode, source_gist, hooks(정확히 3개, 표지 카드에 쓸 훅. 각 type은 훅 5유형 중 택1 한국어 표기), cards(카드별: n, role[표지/본문/전환/CTA 등], headline[한 줄], body[슬라이드에 들어갈 2~4줄]), caption(피드 캡션·줄바꿈 포함), hashtags(2~5개)

카드 개수는 원본과 비슷하게(±1) 유지한다. 1번은 표지(스크롤 멈추는 훅), 마지막은 보통 CTA·저장유도.
모드 판정: 비즈·브랜딩·SNS수익화·AI·정보성이면 '김대영'(기본), 상황극·글로벌엔터·감성·제품광고면 '김디오'. 사용자가 지정하면 따른다.
모든 출력 텍스트는 한국어.

아래 Style Bible을 반드시 지킨다.

{STYLE_BIBLE}"""

CARD_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "slides_ocr": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "n": {"type": "INTEGER"}, "headline": {"type": "STRING"}, "body": {"type": "STRING"}},
            "required": ["n", "headline", "body"]}},
        "translated": {"type": "STRING"},
        "mode": {"type": "STRING", "enum": ["김대영", "김디오"]},
        "source_gist": {"type": "STRING"},
        "hooks": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "type": {"type": "STRING"}, "text": {"type": "STRING"}}, "required": ["type", "text"]}},
        "cards": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "n": {"type": "INTEGER"}, "role": {"type": "STRING"},
            "headline": {"type": "STRING"}, "body": {"type": "STRING"}},
            "required": ["n", "role", "headline", "body"]}},
        "caption": {"type": "STRING"},
        "hashtags": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["slides_ocr", "translated", "mode", "source_gist", "hooks", "cards", "caption", "hashtags"],
}


def gemini_cards(images, note, mode):
    """images: [{'mime': 'image/jpeg', 'b64': '...'}] — 슬라이드 순서대로."""
    mode_line = (f"[모드 지정] '{mode}' 모드로 작성하라." if mode and mode != "auto" else "[모드] 자동 판정.")
    user_text = f"""[레퍼런스 카드뉴스 — 슬라이드 {len(images)}장]
첨부된 이미지를 올린 순서대로(1번부터) 읽어라.

## 추가 지시
{note or '(없음)'}
{mode_line}

각 슬라이드 텍스트를 OCR·번역한 뒤, 위 지시와 Style Bible에 따라 김대영(YLZ) 카드뉴스로 재창작해 JSON으로 출력하라."""

    parts = [{"inline_data": {"mime_type": im["mime"], "data": im["b64"]}} for im in images]
    parts.append({"text": user_text})
    payload = {
        "systemInstruction": {"parts": [{"text": CARD_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": CARD_SCHEMA,
            "maxOutputTokens": 16000,
            "temperature": 0.9,
            "thinkingConfig": {"thinkingBudget": 2048},
        },
    }
    out = _gemini_call(payload)
    parts_out = out["candidates"][0]["content"]["parts"]
    return json.loads("".join(p.get("text", "") for p in parts_out))


# 카드뉴스 URL 자동 다운로드 (gallery-dl) — 다운로드본은 downloads/<shortcode>/ 에 보관
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")
COOKIES_FILE = BASE / "ig_cookies.txt"          # 있으면 파일 쿠키 사용
COOKIES_BROWSER = os.environ.get("IG_COOKIES_BROWSER", "chrome")  # 없으면 브라우저 쿠키


def _shortcode(url):
    m = re.search(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else re.sub(r"[^A-Za-z0-9]+", "", url)[-12:] or "post"


def _mime_of(path):
    e = path.suffix.lower()
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(e.lstrip("."), "image/jpeg")


def download_carousel(url):
    """인스타 캐러셀 URL → 슬라이드 이미지들을 downloads/<shortcode>/ 로 받아 경로 리스트(순서대로) 반환."""
    import subprocess, sys
    outdir = ROOT / "downloads" / _shortcode(url)
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "gallery_dl", "-d", str(outdir), "--no-mtime", "-o", "directory=[]"]
    if COOKIES_FILE.exists():
        cmd += ["--cookies", str(COOKIES_FILE)]
    elif COOKIES_BROWSER:
        cmd += ["--cookies-from-browser", COOKIES_BROWSER]
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=150)
    imgs = sorted([p for p in outdir.glob("*") if p.suffix.lower() in IMG_EXT], key=lambda p: p.name)
    if not imgs:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        hint = detail[-1] if detail else "이미지 없음"
        raise RuntimeError(f"다운로드 실패({hint[:120]}) — 로그인 쿠키(ig_cookies.txt)가 필요하거나 비공개 게시물일 수 있어요.")
    return imgs, str(outdir)

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
    out = _gemini_call(payload)
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
            saved_dir = local_save(result, url)
            steps.append(f"로컬 원문 보관: {saved_dir}" if saved_dir else "로컬 원문 보관 실패")
            notion_url, warn = notion_save(result, url)
            steps.append(f"노션 적재 완료{' — ' + warn if warn else ' (원문 아카이브 포함)'}"
                         if notion_url else f"노션 적재 실패({warn})")
            result["_notion_url"] = notion_url
            result["_local_dir"] = saved_dir
            return jsonify({"result": result})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"처리 중 오류: {e}", "steps": steps}), 500


@app.post("/api/cards")
def cards():
    if not GEMINI_KEY:
        return jsonify({"error": "서버에 AI 키가 설정되지 않았어요."}), 503
    body = request.get_json(force=True, silent=True) or {}
    raw_images = body.get("images") or []
    url = (body.get("url") or "").strip()
    mode = body.get("mode") or "auto"
    note = body.get("note") or ""
    steps = []
    saved_dir = ""
    imgs = []

    if raw_images:
        # 직접 업로드: dataURL 또는 순수 base64 모두 허용, 순서 유지, 최대 20장
        for it in raw_images[:20]:
            d = (it.get("data") if isinstance(it, dict) else it) or ""
            m = (it.get("mime") if isinstance(it, dict) else "") or "image/jpeg"
            if d.startswith("data:"):
                head, _, b64 = d.partition(",")
                mm = re.match(r"data:([^;]+)", head)
                if mm:
                    m = mm.group(1)
                d = b64
            if d:
                imgs.append({"mime": m, "b64": d})
        steps.append(f"업로드 슬라이드 {len(imgs)}장 수신")
    elif re.match(r"^https?://", url):
        # URL 자동 다운로드 (gallery-dl → downloads/<shortcode>/)
        try:
            paths, saved_dir = download_carousel(url)
            for p in paths[:20]:
                imgs.append({"mime": _mime_of(p), "b64": base64.b64encode(p.read_bytes()).decode()})
            steps.append(f"URL에서 슬라이드 {len(imgs)}장 다운로드 → {saved_dir}")
        except Exception as e:
            return jsonify({"error": str(e)}), 502
    else:
        return jsonify({"error": "카드뉴스 URL을 넣거나 슬라이드 이미지를 올려주세요."}), 400

    if not imgs:
        return jsonify({"error": "이미지를 읽지 못했어요."}), 400
    try:
        result = gemini_cards(imgs, note, mode)
        ocr = result.get("slides_ocr", []) or []
        ocr_head = "\n".join(f"{s.get('n','')}. {s.get('headline','')}" for s in ocr)
        ocr_body = "\n\n".join(f"[{s.get('n','')}] {s.get('body','')}" for s in ocr)
        steps.append(f"슬라이드 {len(imgs)}장 OCR·번역·재창작 완료")
        result["_type"] = "cards"
        result["_source"] = {
            "platform": "카드뉴스",
            "uploader": (note[:24] if note else f"슬라이드 {len(imgs)}장"),
            "url": url,
            "saved_dir": saved_dir,
            "twin_url": "",
            "caption_raw": ocr_head,
            "script_raw": ocr_body,
            "steps": steps,
        }
        local_dir = local_save(result, url)
        steps.append(f"로컬 원문 보관: {local_dir}" if local_dir else "로컬 원문 보관 실패")
        notion_url, warn = notion_save(result, url)
        steps.append(f"노션 적재 완료{' — ' + warn if warn else ' (원문 아카이브 포함)'}"
                     if notion_url else f"노션 적재 실패({warn})")
        result["_notion_url"] = notion_url
        result["_local_dir"] = local_dir
        return jsonify({"result": result})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"카드뉴스 처리 오류: {e}"}), 500


@app.get("/health")
def health():
    return jsonify({"ok": True, "key": bool(GEMINI_KEY)})


@app.get("/")
def index():
    return Response((BASE / "index.html").read_text(encoding="utf-8"), mimetype="text/html")


if __name__ == "__main__":
    print(f"레퍼런스 생성기 서버 → http://127.0.0.1:{PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
