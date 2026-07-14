#!/usr/bin/env python
"""
STT 폴백: 영상 링크 -> 오디오 다운로드 -> faster-whisper 전사 -> 텍스트 출력.
유튜브 쌍둥이를 못 찾은 인스타/틱톡 릴스의 '대사'를 확보할 때만 사용.

의존성:
  - yt-dlp  (없으면: pip install yt-dlp)  # 영상/오디오 다운로드
  - faster-whisper (이미 설치됨)          # 전사 (av로 오디오 디코딩, 시스템 ffmpeg 불필요)

사용:
  python stt.py "<영상URL>" [--lang ko] [--model small]
출력:
  전사 텍스트를 stdout으로 출력. --out 지정 시 파일로도 저장.
"""
import argparse
import sys
import tempfile
from pathlib import Path


def download_audio(url: str, workdir: Path) -> Path:
    try:
        import yt_dlp
    except ImportError:
        sys.exit("ERROR: yt-dlp 미설치. 실행: pip install yt-dlp")
    out_tmpl = str(workdir / "audio.%(ext)s")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": out_tmpl,
        "quiet": True,
        "noprogress": True,
        # ffmpeg 없이도 원본 오디오 컨테이너를 그대로 받는다(후처리 없음).
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    files = list(workdir.glob("audio.*"))
    if not files:
        sys.exit("ERROR: 오디오 다운로드 실패")
    return files[0]


def transcribe(audio: Path, lang: str, model_size: str) -> str:
    from faster_whisper import WhisperModel
    # CPU + int8 로 가볍게. GPU 있으면 device="cuda"로 바꿔도 됨.
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        str(audio),
        language=None if lang == "auto" else lang,
        vad_filter=True,
    )
    return "".join(seg.text for seg in segments).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--lang", default="auto", help="ko / en / auto (기본 auto)")
    ap.add_argument("--model", default="small", help="tiny/base/small/medium/large-v3")
    ap.add_argument("--out", default=None, help="전사 텍스트 저장 경로(선택)")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td)
        audio = download_audio(args.url, workdir)
        text = transcribe(audio, args.lang, args.model)

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
