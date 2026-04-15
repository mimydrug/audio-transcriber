#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run.py — 전사 + 번역 파이프라인 래퍼

실행 순서:
  1. 언어 입력 (엔터 = 자동감지)
  2. audio_to_srt.py   : 현재 폴더 음원 → SRT 전사
  3. srt_translate_local.py : SRT → 한국어 번역 (ko 전사 시 생략)
"""

import subprocess
import sys
from pathlib import Path


SEPARATOR = "─" * 52


def ask_language() -> str:
    """언어 코드 입력. 엔터 → 자동감지 반환."""
    print(SEPARATOR)
    lang = input("전사 언어 코드를 입력하세요 (예: ja / ko / en) [엔터=자동감지]: ").strip().lower()
    return lang


def run_step(label: str, cmd: list) -> bool:
    print(f"\n{SEPARATOR}")
    print(f"  {label}")
    print(SEPARATOR)
    result = subprocess.run(cmd)
    return result.returncode == 0


def main():
    print("\n" + "=" * 52)
    print("     음원 전사 + 한국어 번역 파이프라인")
    print("=" * 52)

    lang = ask_language()
    script_dir = Path(__file__).parent

    # ── STEP 1: 전사 ──────────────────────────────────
    transcribe_cmd = [sys.executable, str(script_dir / "audio_to_srt.py")]
    if lang:
        transcribe_cmd += ["--lang", lang]

    ok = run_step("STEP 1 / 2  |  음원 전사", transcribe_cmd)
    if not ok:
        print("\n[ERROR] 전사 실패. 파이프라인을 중단합니다.")
        sys.exit(1)

    # ── STEP 2: 번역 (ko 전사이면 생략) ──────────────
    if lang == "ko":
        print("\n[INFO] 한국어 전사이므로 번역 단계를 생략합니다.")
    else:
        translate_cmd = [sys.executable, str(script_dir / "srt_translate_local.py")]
        if lang in ("ja", "en"):
            translate_cmd += ["--lang-src", lang]
        # lang == "" (자동감지) 이면 --lang-src 없이 실행 → 번역 스크립트가 파일 내용으로 감지

        run_step("STEP 2 / 2  |  SRT 한국어 번역", translate_cmd)

    print("\n" + "=" * 52)
    print("  [DONE] 파이프라인 완료")
    print("=" * 52 + "\n")


if __name__ == "__main__":
    main()
