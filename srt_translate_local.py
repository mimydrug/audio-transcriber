import argparse
import glob
import pysrt
import subprocess
import time
import sys
import re
import random
from typing import List, Tuple, Optional

# ---------------------------
# SETTINGS
# ---------------------------
# 권장 모델 (한국어 품질 순):
#   gemma3:27b  ← 최고 품질 (VRAM 18GB+)
#   qwen2.5:14b ← 균형 (VRAM 10GB+)
#   aya:8b      ← 기본값 (VRAM 6GB+)
MODEL = "aya:8b"

SRC_LANG = "auto"               # "ja" / "en" / "auto" — main()에서 갱신

BATCH_SIZE = 15                 # 20~40 권장
RETRY_PER_BATCH = 2             # 배치 전체 재시도 횟수
RETRY_PARTIAL = 1               # 누락 태그만 재요청 횟수

OLLAMA_TIMEOUT_SEC = 300        # 한 번의 ollama 호출 최대 대기(초) - 길면 늘리기(180~240)
KEEPALIVE = "10m"               # 모델 메모리 유지(속도/안정성 향상), 원치 않으면 None

SLEEP_BETWEEN_BATCH = 0.0

TARGET_SUFFIX = "_ko"

# 반복 축약 설정
ENABLE_COLLAPSE = True
MAX_REPEAT = 3


# ---------------------------
# PROGRESS BAR
# ---------------------------
def render_bar(done: int, total: int, width: int = 34) -> str:
    if total <= 0:
        return "[??????????????????????????????????]"
    ratio = min(max(done / total, 0.0), 1.0)
    filled = int(ratio * width)
    return "[" + ("█" * filled) + ("░" * (width - filled)) + f"] {done}/{total}"

def print_progress(done: int, total: int):
    sys.stdout.write("\r" + render_bar(done, total))
    sys.stdout.flush()


# ---------------------------
# REPEAT COLLAPSE
# ---------------------------
def collapse_repetitions(text: str, max_repeat: int = 3) -> str:
    """
    과도 반복을 max_repeat까지만 남기고 '...'로 축약.
    - "私は、私は、私は、..." (구절 반복)
    - "うっうっうっ..." (짧은 토큰 반복)
    - "んんんん..." (단일 문자 과다 반복)
    """
    if not text:
        return text
    s = text

    # (1) 구절(+구두점/공백) 반복: "私は、" 같은 단위
    # 동일 구절이 4회 이상 반복될 때만 축약
    def repl_phrase(m):
        phrase = m.group(1)
        return (phrase * max_repeat) + "..."

    s = re.sub(
        r'((?:[^ \n\r\t、,。.!?]{1,30}[、,。.!?\s]){1})\1{3,}',
        repl_phrase,
        s
    )

    # (2) 1~4자 토큰 반복(일본어/한글 포함)
    def repl_token(m):
        tok = m.group(1)
        return (tok * max_repeat) + "..."

    s = re.sub(
        r'([^\W\d_]{1,4})\1{3,}',
        repl_token,
        s
    )

    # (3) 단일 문자 8회 이상 반복
    def repl_char(m):
        ch = m.group(1)
        return (ch * max_repeat) + "..."

    s = re.sub(
        r'(.)(?:\1){7,}',
        repl_char,
        s
    )

    return s


# ---------------------------
# OLLAMA CALL (timeout + retry)
# ---------------------------
def ollama_run(prompt: str, timeout_sec: int) -> str:
    cmd = ["ollama", "run", MODEL]
    if KEEPALIVE:
        cmd += ["--keepalive", KEEPALIVE]

    # stderr를 함께 받아서 디버깅 가능하게
    result = subprocess.run(
        cmd,
        input=prompt.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_sec
    )
    out = result.stdout.decode("utf-8", errors="ignore").strip()
    err = result.stderr.decode("utf-8", errors="ignore").strip()

    # 가끔 stderr에 경고가 찍히지만 정상 출력도 있을 수 있음 -> out 우선
    if not out and err:
        # 완전히 비었으면 err도 같이 반환(디버깅용)
        return err
    return out


def detect_source_lang(path: str) -> str:
    """SRT 파일 내용으로 소스 언어 자동 감지 (ja / en)"""
    try:
        subs = pysrt.open(path, encoding="utf-8")
        sample = " ".join(s.text for s in subs[:40])
        # 히라가나·가타카나·한자 비율로 판별
        cjk = sum(1 for c in sample if "\u3040" <= c <= "\u9fff" or "\uff00" <= c <= "\uffef")
        alpha = sum(1 for c in sample if c.isalpha())
        if alpha == 0:
            return "ja"
        return "ja" if (cjk / alpha) > 0.25 else "en"
    except Exception:
        return "ja"


def build_prompt(tagged_lines: List[str]) -> str:
    text_block = "\n".join(tagged_lines)

    if SRC_LANG == "en":
        return f"""당신은 영어 자막을 한국어로 번역하는 전문 번역가입니다.
자연스럽고 현실적인 한국어 구어체로 번역하세요. 직역을 피하고 의역을 허용합니다.
영어 관용구·수동태는 한국어답게 풀어 쓰세요.

규칙(매우 중요):
1) 각 줄은 반드시 1:1로 대응해야 합니다.
2) 각 줄 맨 앞의 태그 [L###]를 절대 삭제/변경하지 마세요.
3) 출력은 번역된 줄만 그대로 출력하세요. (설명/머리말/번호 추가 금지)
4) 줄 수와 줄바꿈 개수는 입력과 동일해야 합니다.

입력:
{text_block}
"""
    else:  # ja (기본)
        return f"""당신은 일본어 자막을 한국어로 번역하는 전문 번역가입니다.
자연스럽고 현실적인 한국어 구어체로 번역하세요. 의역을 허용합니다.
일본어 특유의 어말 표현(ね·よ·かな 등)은 자연스러운 한국어 어미로 변환하세요.
일본어 원문을 절대 출력에 남기지 마세요.

규칙(매우 중요):
1) 각 줄은 반드시 1:1로 대응해야 합니다.
2) 각 줄 맨 앞의 태그 [L###]를 절대 삭제/변경하지 마세요.
3) 출력은 번역된 줄만 그대로 출력하세요. (설명/머리말/번호 추가 금지)
4) 줄 수와 줄바꿈 개수는 입력과 동일해야 합니다.

입력:
{text_block}
"""


def parse_tagged_output(out_text: str, expected_tags: List[str]) -> Tuple[List[str], List[str]]:
    got = {}
    for line in out_text.splitlines():
        line = line.strip()
        if line.startswith("[L") and "]" in line:
            tag = line.split("]", 1)[0] + "]"
            body = line.split("]", 1)[1].strip()
            got[tag] = body

    translated = []
    missing = []
    for tag in expected_tags:
        if tag in got and got[tag].strip():
            translated.append(got[tag])
        else:
            translated.append("")
            missing.append(tag)
    return translated, missing


def translate_tagged_lines(tagged_lines: List[str], expected_tags: List[str]) -> Tuple[List[str], List[str]]:
    prompt = build_prompt(tagged_lines)
    out = ollama_run(prompt, timeout_sec=OLLAMA_TIMEOUT_SEC)
    return parse_tagged_output(out, expected_tags)


def translate_batch(lines: List[str]) -> List[str]:
    # 입력 축약(선택)
    if ENABLE_COLLAPSE:
        lines = [collapse_repetitions(t, MAX_REPEAT) for t in lines]

    expected_tags = [f"[L{i+1:03d}]" for i in range(len(lines))]
    tagged_lines = [f"{expected_tags[i]} {lines[i]}" for i in range(len(lines))]

    # 배치 전체 재시도 루프
    last_translated = None

    for attempt in range(RETRY_PER_BATCH + 1):
        # 디버그: 첫 배치가 “멈춘 것처럼 보이는 문제”를 위해 로그 출력
        if attempt == 0:
            # 첫 시도만 배치 시작 로그
            pass

        try:
            translated, missing = translate_tagged_lines(tagged_lines, expected_tags)
        except subprocess.TimeoutExpired:
            translated, missing = ([""] * len(lines)), expected_tags[:]  # 전부 누락으로 처리
        except Exception as e:
            print(f"\n[오류 발생] {e}") # 이 줄을 추가하여 구체적인 에러 확인
            translated, missing = ([""] * len(lines)), expected_tags[:]

        last_translated = translated

        # 누락이 있으면 누락만 재요청(부분 재시도)
        if missing:
            miss_indices = [int(tag[2:5]) - 1 for tag in missing]

            for _ in range(RETRY_PARTIAL):
                miss_tagged = [tagged_lines[i] for i in miss_indices]
                miss_tags = [expected_tags[i] for i in miss_indices]
                try:
                    miss_trans, miss_missing = translate_tagged_lines(miss_tagged, miss_tags)
                except subprocess.TimeoutExpired:
                    continue
                except Exception:
                    continue

                # 채우기
                new_miss_indices = []
                for k, idx in enumerate(miss_indices):
                    if miss_trans[k].strip():
                        translated[idx] = miss_trans[k]
                    else:
                        new_miss_indices.append(idx)
                miss_indices = new_miss_indices
                if not miss_indices:
                    break

        # 평가: 빈 줄이 너무 많으면 재시도
        empties = sum(1 for t in translated if not t.strip())
        if empties <= max(1, len(lines) // 10):
            # 출력 축약(선택)
            if ENABLE_COLLAPSE:
                translated = [collapse_repetitions(t, MAX_REPEAT) for t in translated]
            # 최종: 그래도 빈 건 원문 유지
            final = [translated[i] if translated[i].strip() else lines[i] for i in range(len(lines))]
            return final

        # 마지막 시도면 원문 유지로 방어
        if attempt == RETRY_PER_BATCH:
            final = []
            for i, t in enumerate(translated):
                final.append(t if t.strip() else lines[i])
            if ENABLE_COLLAPSE:
                final = [collapse_repetitions(t, MAX_REPEAT) for t in final]
            return final

        # 다음 시도 전에 약간 쉬기(너무 빠르게 재요청하면 불안정할 때)
        time.sleep(0.2 + random.uniform(0.0, 0.2))

    # 이론상 여기 도달 X
    if last_translated:
        return [last_translated[i] if last_translated[i].strip() else lines[i] for i in range(len(lines))]
    return lines


# ---------------------------
# TIME FORMAT
# ---------------------------
def format_seconds(sec: float) -> str:
    sec = int(round(sec))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m}m{s:02d}s"


# ---------------------------
# FILE PROCESS
# ---------------------------
def translate_srt_file(path: str) -> float:
    global SRC_LANG
    start = time.time()

    # 파일별 언어 감지 (auto 모드일 때)
    if SRC_LANG == "auto":
        detected = detect_source_lang(path)
        print(f"[INFO] 소스 언어 자동 감지: {detected} ({path})")
        SRC_LANG = detected

    subs = pysrt.open(path, encoding="utf-8")
    total = len(subs)

    texts = [(s.text or "").replace("\n", " ").strip() for s in subs]

    print(f"\n번역 중: {path}")
    print_progress(0, total)

    # 첫 배치 시작 로그(멈춤 착시 방지)
    first_batch_logged = False

    for i in range(0, total, BATCH_SIZE):
        batch_texts = texts[i:i + BATCH_SIZE]

        if not first_batch_logged:
            sys.stdout.write("\n[INFO] first batch: calling ollama...\n")
            sys.stdout.flush()
            first_batch_logged = True

        translated = translate_batch(batch_texts)

        for j, t in enumerate(translated):
            if t.strip():
                subs[i + j].text = t

        done = min(i + BATCH_SIZE, total)
        print_progress(done, total)

        if SLEEP_BETWEEN_BATCH > 0:
            time.sleep(SLEEP_BETWEEN_BATCH)

    sys.stdout.write("\n")
    sys.stdout.flush()

    out_path = path.replace(".srt", f"{TARGET_SUFFIX}.srt")
    subs.save(out_path, encoding="utf-8")

    elapsed = time.time() - start
    print(f"[DONE] {path} -> {out_path} | {format_seconds(elapsed)}")
    return elapsed


def main():
    global SRC_LANG, MODEL

    ap = argparse.ArgumentParser(description="SRT 한국어 번역")
    ap.add_argument("--lang-src", type=str, default="",
                    help="소스 언어 (ja / en). 미입력 시 자동 감지")
    ap.add_argument("--model", type=str, default="",
                    help=f"Ollama 모델명 (기본: {MODEL})")
    args = ap.parse_args()

    if args.model:
        MODEL = args.model.strip()

    if args.lang_src:
        SRC_LANG = args.lang_src.strip().lower()
    else:
        # CLI에서 직접 실행 시 언어 입력 요청
        u = input("소스 언어를 입력하세요 (ja / en) [엔터=자동감지]: ").strip().lower()
        SRC_LANG = u if u in ("ja", "en") else "auto"

    files = sorted(glob.glob("*.srt"))
    files = [f for f in files if not f.lower().endswith(f"{TARGET_SUFFIX}.srt")]

    print(f"{len(files)}개 파일 발견")
    if not files:
        return

    total_start = time.time()
    for f in files:
        translate_srt_file(f)

    total_elapsed = time.time() - total_start
    print(f"\n[ALL DONE] {len(files)} files | total {format_seconds(total_elapsed)}")


if __name__ == "__main__":
    main()