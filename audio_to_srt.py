#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audio_to_srt v2.7.5 (Production build: SRT only by default)

핵심(운영용):
- 기본: SRT만 생성 (report/removed 파일 생성 차단)
- PowerShell 진행바가 [FILE DONE] 로그를 덮어쓰는 문제 수정 (print()로 줄바꿈 강제)
- 실행 파일 혼선 방지: [RUN] script path + sha256 일부 출력
- faster-whisper로 VAD 기반 keep-ranges 추출 -> openai-whisper로 2pass 전사
- repeat collapse + anti-halluc(증거 기반 저에너지 필터)
"""

from __future__ import annotations

import os
import re
import sys
import csv
import shutil
import subprocess
import tempfile
import time
import argparse
import wave
import audioop
import math
import unicodedata
import warnings
import hashlib
from pathlib import Path
from dataclasses import dataclass
from typing import List, Tuple, Optional, Set, Dict
from contextlib import contextmanager, redirect_stdout, redirect_stderr

# ---- 실행 식별자(혼선 방지) ----
try:
    _sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
    print(f"[RUN] script={__file__} | sha256={_sha}")
except Exception:
    pass

# DeprecationWarning 억제(로그 깔끔하게)
warnings.filterwarnings("ignore", category=DeprecationWarning, module=r".*audioop.*")

# tqdm 전역 무력화
os.environ["TQDM_DISABLE"] = "1"
try:
    import tqdm as _tqdm  # type: ignore
    _real_tqdm = _tqdm.tqdm

    def _quiet_tqdm(*args, **kwargs):
        kwargs["disable"] = True
        kwargs["leave"] = False
        kwargs["dynamic_ncols"] = False
        return _real_tqdm(*args, **kwargs)

    _tqdm.tqdm = _quiet_tqdm
except Exception:
    pass

import whisper  # openai-whisper

DEFAULT_OW_MODEL = "base"
DEFAULT_FW_MODEL = "large-v3"

TARGET_SR = 16000
TARGET_CH = 1

KEEP_PAD_SEC = 0.35
MIN_KEEP_RATIO = 0.08
KEEP_MERGE_GAP = 0.30
KEEP_MIN_LEN = 0.20

MAX_CHUNK_SEC = 25.0
CHUNK_OVERLAP_SEC = 0.40

TEMPERATURE = 0.0
CONDITION_ON_PREV = False
FP16 = True
WHISPER_VERBOSE = False

DROP_NO_SPEECH = 0.85
DROP_LOGPROB = -1.2
DROP_COMP_RATIO = 2.6
RESCUE_LOGPROB = -0.6

REPEAT_WINDOW_SEC = 12.0
REPEAT_MIN = 4
VOCAL_REPEAT_MIN = 8
REPEAT_SHORT_KEY_MAX = 24
REPEAT_PREFIX_MIN = 8
REPEAT_ALLOW_GAP_SEC = 1.2

DEFAULT_MEDIA_EXTS: Set[str] = {
    ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts",
}

# -----------------------------
# Anti-halluc (균형형)
# -----------------------------
HARD_DBFS = -62.0

ADAPTIVE_LOW_DBFS_QUANTILE = 0.15
ADAPTIVE_LOW_DBFS_MIN = -58.0
ADAPTIVE_LOW_DBFS_MAX = -45.0   # 균형점

LOW_ENERGY_LONG_TEXT_LEN = 28
MAX_REPEAT_RATIO = 0.38
MAX_DUP_SENTENCE = 2
LANG_MISMATCH_ALPHA_RATIO = 0.60

VOCAL_STRONG_DROP_NO_SPEECH = 0.90
VOCAL_STRONG_DROP_LOGPROB = -1.5

ANTI_DBFS_EVID_NO_SPEECH = 0.35
ANTI_DBFS_EVID_LOGPROB = -0.60
ANTI_DBFS_EVID_COMP = 3.00

SHORT_PROTECT_LEN = 12
SHORT_STRONG_NO_SPEECH = 0.55
SHORT_STRONG_LOGPROB = -1.00
SHORT_STRONG_COMP = 3.20

_REPEAT_TAG_RE = re.compile(r"\(x(\d+)\)\s*$", re.IGNORECASE)


@contextmanager
def suppress_console_output(enabled: bool = True):
    if not enabled:
        yield
        return
    with open(os.devnull, "w") as dn:
        with redirect_stdout(dn), redirect_stderr(dn):
            yield


def progress_bar(done: int, total: int, label: str = "") -> None:
    width = 30
    filled = int(width * done / max(1, total))
    bar = "█" * filled + "░" * (width - filled)
    msg = f"\r[{bar}] {done}/{total}"
    if label:
        msg += f"  {label}"
    sys.stdout.write(msg)
    sys.stdout.flush()
    if done >= total:
        sys.stdout.write("\n")


def fmt_dur(sec: float) -> str:
    sec = max(0.0, sec)
    m = int(sec // 60)
    s = int(sec % 60)
    return f"{m}m{s:02}s"


def srt_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000.0))
    s = (ms // 1000) % 60
    m = (ms // (1000 * 60)) % 60
    h = ms // (1000 * 60 * 60)
    return f"{h:02}:{m:02}:{s:02},{ms % 1000:03}"


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "output"


def which_or_die(bin_name: str) -> None:
    if shutil.which(bin_name) is None:
        raise RuntimeError(f"[ERROR] '{bin_name}' not found in PATH. Install it first.")


def auto_scan_inputs(cwd: Path) -> List[Path]:
    files = [p for p in cwd.iterdir() if p.is_file() and p.suffix.lower() in DEFAULT_MEDIA_EXTS]
    files.sort(key=lambda x: x.name.lower())
    return files


def run_cmd_capture(cmd: List[str]) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p.returncode, p.stdout, p.stderr


def run_cmd_silent(cmd: List[str]) -> None:
    rc, _, err = run_cmd_capture(cmd)
    if rc != 0:
        raise RuntimeError(f"Command failed:\n  {' '.join(cmd)}\n\nSTDERR:\n{err.strip()}")


def ffprobe_duration(path: Path) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=nw=1:nk=1", str(path)]
    rc, out, _ = run_cmd_capture(cmd)
    if rc != 0:
        return 0.0
    try:
        return float(out.strip())
    except Exception:
        return 0.0


def ffprobe_audio_stream_info(path: Path) -> Dict[str, str]:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a:0",
           "-show_entries", "stream=codec_name,codec_type,sample_rate,channels,channel_layout",
           "-of", "default=nw=1", str(path)]
    rc, out, _ = run_cmd_capture(cmd)
    if rc != 0:
        return {}
    info: Dict[str, str] = {}
    for ln in out.splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            info[k.strip()] = v.strip()
    return info


def is_already_wav_pcm16_16k_mono(path: Path) -> bool:
    if path.suffix.lower() != ".wav":
        return False
    info = ffprobe_audio_stream_info(path)
    if not info:
        return False
    codec = info.get("codec_name", "").lower()
    sr = info.get("sample_rate", "")
    ch = info.get("channels", "")
    return (codec == "pcm_s16le") and (sr == str(TARGET_SR)) and (ch == str(TARGET_CH))


def preprocess_to_wav16k_mono(input_path: Path, out_wav: Path) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats",
           "-i", str(input_path), "-vn", "-ac", str(TARGET_CH), "-ar", str(TARGET_SR),
           "-c:a", "pcm_s16le", str(out_wav)]
    run_cmd_silent(cmd)


def extract_clip(src_audio: Path, out_wav: Path, start: float, end: float) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats",
           "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
           "-i", str(src_audio), "-ac", str(TARGET_CH), "-ar", str(TARGET_SR),
           "-c:a", "pcm_s16le", str(out_wav)]
    run_cmd_silent(cmd)


def merge_ranges(ranges: List[Tuple[float, float]], gap: float) -> List[Tuple[float, float]]:
    if not ranges:
        return []
    ranges = sorted(ranges, key=lambda x: x[0])
    out = [ranges[0]]
    for s, e in ranges[1:]:
        ps, pe = out[-1]
        if s <= pe + gap:
            out[-1] = (ps, max(pe, e))
        else:
            out.append((s, e))
    return out


def pad_ranges(ranges: List[Tuple[float, float]], pad: float, total: float) -> List[Tuple[float, float]]:
    return [(max(0.0, s - pad), min(total, e + pad)) for s, e in ranges]


def drop_short_ranges(ranges: List[Tuple[float, float]], min_len: float) -> List[Tuple[float, float]]:
    return [(s, e) for s, e in ranges if (e - s) >= min_len]


def total_range_len(ranges: List[Tuple[float, float]]) -> float:
    return sum(max(0.0, e - s) for s, e in ranges)


def chunk_ranges(ranges: List[Tuple[float, float]], max_len: float, overlap: float) -> List[Tuple[float, float]]:
    chunks: List[Tuple[float, float]] = []
    for s, e in ranges:
        cur = s
        while cur < e:
            nxt = min(e, cur + max_len)
            chunks.append((cur, nxt))
            if nxt >= e:
                break
            cur = max(cur + 0.05, nxt - overlap)
    return chunks


def build_keep_ranges_from_fw(fw_model, wav16k_path: Path, total_dur: float, lang_hint: Optional[str]) -> Tuple[Optional[str], List[Tuple[float, float]]]:
    kwargs = {
        "task": "transcribe",
        "beam_size": 1,
        "temperature": 0.0,
        "vad_filter": True,
        "vad_parameters": {
            "min_silence_duration_ms": 900,
            "speech_pad_ms": int(KEEP_PAD_SEC * 1000),
            "max_speech_duration_s": 30.0,
        },
        "condition_on_previous_text": False,
        "word_timestamps": False,
        "compression_ratio_threshold": 2.6,
        "log_prob_threshold": -1.0,
        "no_speech_threshold": 0.6,
    }
    if lang_hint:
        kwargs["language"] = lang_hint

    seg_gen, info = fw_model.transcribe(str(wav16k_path), **kwargs)
    segs = list(seg_gen)

    detected_lang = getattr(info, "language", None)
    if detected_lang:
        detected_lang = str(detected_lang).strip().lower()

    keep: List[Tuple[float, float]] = []
    for s in segs:
        st = float(getattr(s, "start", 0.0) or 0.0)
        en = float(getattr(s, "end", st) or st)
        st = max(0.0, st)
        en = min(total_dur, en)
        if en - st >= 0.05:
            keep.append((st, en))

    keep = pad_ranges(keep, pad=KEEP_PAD_SEC, total=total_dur)
    keep = merge_ranges(keep, gap=KEEP_MERGE_GAP)
    keep = drop_short_ranges(keep, min_len=KEEP_MIN_LEN)

    if total_dur > 0:
        ratio = total_range_len(keep) / total_dur
        if ratio < MIN_KEEP_RATIO:
            keep = []

    return detected_lang, keep


@dataclass
class Seg:
    start: float
    end: float
    text: str
    no_speech_prob: Optional[float] = None
    avg_logprob: Optional[float] = None
    compression_ratio: Optional[float] = None


@dataclass
class RemovedItem:
    start: float
    end: float
    text: str
    reason: str
    stage: str
    evidence_flags: str = ""
    no_speech_prob: Optional[float] = None
    avg_logprob: Optional[float] = None
    compression_ratio: Optional[float] = None
    dbfs: Optional[float] = None


def is_vocalization_like(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if sum(1 for c in t if c in "~〜…") >= 2:
        return True
    core = re.sub(r"[\s~〜…\.\-–—\!\?\,]+", "", t)
    if not core:
        return True
    if len(core) <= 3:
        return True
    if len(core) >= 4 and len(set(core)) <= 2:
        return True
    return False


def should_drop_by_metrics(seg: Seg, preserve_vocals: bool) -> Tuple[bool, str]:
    if seg.no_speech_prob is None or seg.avg_logprob is None:
        return False, ""
    vocal = preserve_vocals and is_vocalization_like(seg.text)
    if vocal:
        if seg.no_speech_prob >= VOCAL_STRONG_DROP_NO_SPEECH and seg.avg_logprob <= -1.5:
            return True, "metrics:vocal_strong_no_speech"
        return False, ""
    if seg.no_speech_prob >= DROP_NO_SPEECH and seg.avg_logprob <= DROP_LOGPROB:
        return True, "metrics:no_speech+low_logprob"
    if seg.compression_ratio is not None and seg.compression_ratio >= DROP_COMP_RATIO and seg.avg_logprob <= DROP_LOGPROB:
        return True, "metrics:high_comp+low_logprob"
    if seg.avg_logprob >= RESCUE_LOGPROB:
        return False, ""
    return False, ""


_punct_re = re.compile(r"[\s\.\,\!\?\:\;\(\)\[\]\{\}\"\'\-\–\—\~〜…]+", re.UNICODE)


def normalize_key_cjk(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    t = unicodedata.normalize("NFKC", t)
    t = _punct_re.sub("", t)
    t = "".join((c.lower() if "A" <= c <= "Z" else c) for c in t)
    if len(t) > REPEAT_SHORT_KEY_MAX:
        t = t[:REPEAT_SHORT_KEY_MAX]
    return t


def common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def is_similar_repeat_key(k1: str, k2: str, min_prefix: int = REPEAT_PREFIX_MIN) -> bool:
    if not k1 or not k2:
        return False
    if k1 == k2:
        return True
    return common_prefix_len(k1, k2) >= min_prefix


def collapse_repeats_stronger(segs: List[Seg], preserve_vocals: bool) -> Tuple[List[Seg], int]:
    if not segs:
        return [], 0
    segs = sorted(segs, key=lambda x: (x.start, x.end))
    out: List[Seg] = []
    collapsed = 0
    cluster: List[Seg] = []
    cluster_key: str = ""
    cluster_first_start: float = 0.0
    cluster_last_end: float = 0.0

    def flush_cluster():
        nonlocal collapsed, cluster, cluster_key, cluster_first_start, cluster_last_end
        if not cluster:
            return
        repeats = len(cluster)
        first = cluster[0]
        last = cluster[-1]
        vocal = preserve_vocals and is_vocalization_like(first.text)
        local_min = VOCAL_REPEAT_MIN if vocal else REPEAT_MIN
        if repeats >= local_min:
            if vocal:
                out.append(Seg(first.start, last.end, first.text.strip(),
                               first.no_speech_prob, first.avg_logprob, first.compression_ratio))
            else:
                out.append(Seg(first.start, last.end, f"{first.text.strip()} (x{repeats})",
                               first.no_speech_prob, first.avg_logprob, first.compression_ratio))
            collapsed += (repeats - 1)
        else:
            out.extend(cluster)
        cluster = []
        cluster_key = ""
        cluster_first_start = 0.0
        cluster_last_end = 0.0

    for seg in segs:
        k = normalize_key_cjk(seg.text)
        if not k:
            flush_cluster()
            out.append(seg)
            continue
        if not cluster:
            cluster = [seg]
            cluster_key = k
            cluster_first_start = seg.start
            cluster_last_end = seg.end
            continue
        within_window = (seg.start - cluster_first_start) <= REPEAT_WINDOW_SEC
        small_gap = (seg.start - cluster_last_end) <= REPEAT_ALLOW_GAP_SEC
        similar = is_similar_repeat_key(cluster_key, k)
        if within_window and small_gap and similar:
            cluster.append(seg)
            cluster_last_end = max(cluster_last_end, seg.end)
        else:
            flush_cluster()
            cluster = [seg]
            cluster_key = k
            cluster_first_start = seg.start
            cluster_last_end = seg.end

    flush_cluster()
    out.sort(key=lambda x: (x.start, x.end))
    return out, collapsed


def read_wav_pcm16_mono(path: Path) -> Tuple[int, bytes]:
    with wave.open(str(path), "rb") as wf:
        ch = wf.getnchannels()
        sr = wf.getframerate()
        sw = wf.getsampwidth()
        n = wf.getnframes()
        if sw != 2:
            raise ValueError("WAV not PCM16")
        raw = wf.readframes(n)
    if ch != 1:
        raise ValueError("WAV not mono")
    return sr, raw


def seg_dbfs(sr: int, pcm16: bytes, start_s: float, end_s: float) -> float:
    start_i = max(0, int(start_s * sr))
    end_i = max(start_i + 1, int(end_s * sr))
    a = start_i * 2
    b = min(len(pcm16), end_i * 2)
    chunk = pcm16[a:b]
    if not chunk:
        return -120.0
    r = audioop.rms(chunk, 2)
    if r <= 0:
        return -120.0
    return 20.0 * math.log10(r / 32768.0)


def quantile(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    q = min(1.0, max(0.0, q))
    vs = sorted(values)
    idx = int(round((len(vs) - 1) * q))
    idx = min(len(vs) - 1, max(0, idx))
    return float(vs[idx])


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def alpha_ratio(text: str) -> float:
    if not text:
        return 0.0
    letters = sum(1 for c in text if c.isalpha())
    ascii_letters = sum(1 for c in text if ("A" <= c <= "Z") or ("a" <= c <= "z"))
    return ascii_letters / max(1, letters)


def repeat_ratio(text: str) -> float:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return 0.0
    toks = t.split(" ")
    if len(toks) < 8:
        return 0.0
    uniq = len(set(toks))
    return 1.0 - (uniq / max(1, len(toks)))


def too_many_duplicate_sentences(text: str) -> bool:
    parts = re.split(r"[\.!\?]+", text)
    parts = [p.strip().lower() for p in parts if p.strip()]
    if len(parts) < 3:
        return False
    counts: Dict[str, int] = {}
    for p in parts:
        counts[p] = counts.get(p, 0) + 1
        if counts[p] >= MAX_DUP_SENTENCE:
            return True
    return False


def has_repeat_tag(text: str) -> Optional[int]:
    m = _REPEAT_TAG_RE.search((text or "").strip())
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def has_dbfs_evidence(seg: Seg, repN: Optional[int]) -> bool:
    if repN is not None and repN >= REPEAT_MIN:
        return True
    if seg.no_speech_prob is not None and seg.no_speech_prob >= ANTI_DBFS_EVID_NO_SPEECH:
        return True
    if seg.avg_logprob is not None and seg.avg_logprob <= ANTI_DBFS_EVID_LOGPROB:
        return True
    if seg.compression_ratio is not None and seg.compression_ratio >= ANTI_DBFS_EVID_COMP:
        return True
    return False


def has_short_strong_evidence(seg: Seg, repN: Optional[int]) -> bool:
    if repN is not None and repN >= REPEAT_MIN:
        return True
    if seg.no_speech_prob is not None and seg.no_speech_prob >= SHORT_STRONG_NO_SPEECH:
        return True
    if seg.avg_logprob is not None and seg.avg_logprob <= SHORT_STRONG_LOGPROB:
        return True
    if seg.compression_ratio is not None and seg.compression_ratio >= SHORT_STRONG_COMP:
        return True
    return False


def anti_halluc_filter_segs_adaptive(
    segs: List[Seg],
    analysis_wav: Path,
    lang_hint: Optional[str],
    preserve_vocals: bool,
) -> Tuple[List[Seg], int, List[RemovedItem], float]:
    """returns: (kept, removed_count, removed_items, low_dbfs_th)"""
    if not segs:
        return [], 0, [], clamp(-45.0, ADAPTIVE_LOW_DBFS_MIN, ADAPTIVE_LOW_DBFS_MAX)

    try:
        sr, pcm = read_wav_pcm16_mono(analysis_wav)
    except Exception:
        return segs, 0, [], clamp(-45.0, ADAPTIVE_LOW_DBFS_MIN, ADAPTIVE_LOW_DBFS_MAX)

    db_list: List[float] = []
    seg_db_cache: List[float] = []
    for seg in segs:
        db = seg_dbfs(sr, pcm, seg.start, seg.end)
        seg_db_cache.append(db)
        if db > -110.0:
            db_list.append(db)

    qv = quantile(db_list, ADAPTIVE_LOW_DBFS_QUANTILE)
    low_dbfs_th = clamp(qv if qv is not None else -45.0, ADAPTIVE_LOW_DBFS_MIN, ADAPTIVE_LOW_DBFS_MAX)

    out: List[Seg] = []
    removed_items: List[RemovedItem] = []
    removed = 0

    def mark_removed(item: RemovedItem) -> None:
        nonlocal removed
        removed += 1
        removed_items.append(item)

    for seg, db in zip(segs, seg_db_cache):
        s, e, t = seg.start, seg.end, (seg.text or "").strip()
        if not t:
            mark_removed(RemovedItem(s, e, t, "anti:empty", "anti",
                                     no_speech_prob=seg.no_speech_prob, avg_logprob=seg.avg_logprob,
                                     compression_ratio=seg.compression_ratio, dbfs=db))
            continue

        vocal = preserve_vocals and is_vocalization_like(t)
        repN = has_repeat_tag(t)

        # HARD drop
        if db < HARD_DBFS:
            if vocal:
                if (seg.no_speech_prob is not None and seg.avg_logprob is not None
                    and seg.no_speech_prob >= VOCAL_STRONG_DROP_NO_SPEECH
                    and seg.avg_logprob <= VOCAL_STRONG_DROP_LOGPROB):
                    mark_removed(RemovedItem(s, e, t, "anti:vocal_hard_dbfs_strong_no_speech", "anti",
                                             no_speech_prob=seg.no_speech_prob, avg_logprob=seg.avg_logprob,
                                             compression_ratio=seg.compression_ratio, dbfs=db))
                    continue
                out.append(seg)
                continue

            mark_removed(RemovedItem(s, e, t, "anti:hard_dbfs", "anti",
                                     no_speech_prob=seg.no_speech_prob, avg_logprob=seg.avg_logprob,
                                     compression_ratio=seg.compression_ratio, dbfs=db))
            continue

        # 언어 불일치(ja/ko인데 로마자 비율 과도) - vocal 제외
        if (not vocal) and (lang_hint in ("ja", "ko")):
            if len(t) >= 20 and alpha_ratio(t) >= LANG_MISMATCH_ALPHA_RATIO:
                mark_removed(RemovedItem(s, e, t, "anti:lang_mismatch_ascii_heavy", "anti",
                                         no_speech_prob=seg.no_speech_prob, avg_logprob=seg.avg_logprob,
                                         compression_ratio=seg.compression_ratio, dbfs=db))
                continue

        # vocal: 기본 보존
        if vocal:
            if (seg.no_speech_prob is not None and seg.avg_logprob is not None
                and seg.no_speech_prob >= VOCAL_STRONG_DROP_NO_SPEECH
                and seg.avg_logprob <= VOCAL_STRONG_DROP_LOGPROB):
                mark_removed(RemovedItem(s, e, t, "anti:vocal_strong_no_speech", "anti",
                                         no_speech_prob=seg.no_speech_prob, avg_logprob=seg.avg_logprob,
                                         compression_ratio=seg.compression_ratio, dbfs=db))
                continue
            out.append(seg)
            continue

        # 반복성/중복 문장
        if repeat_ratio(t) >= MAX_REPEAT_RATIO:
            mark_removed(RemovedItem(s, e, t, "anti:token_repeat_ratio", "anti",
                                     no_speech_prob=seg.no_speech_prob, avg_logprob=seg.avg_logprob,
                                     compression_ratio=seg.compression_ratio, dbfs=db))
            continue
        if too_many_duplicate_sentences(t):
            mark_removed(RemovedItem(s, e, t, "anti:dup_sentences", "anti",
                                     no_speech_prob=seg.no_speech_prob, avg_logprob=seg.avg_logprob,
                                     compression_ratio=seg.compression_ratio, dbfs=db))
            continue

        # 저에너지 처리
        dbfs_lt_th = db < low_dbfs_th
        if dbfs_lt_th:
            # 짧은 대사 보호
            if len(t) <= SHORT_PROTECT_LEN and (not has_short_strong_evidence(seg, repN)):
                out.append(seg)
                continue

            # (xN) + 저에너지
            if repN is not None and repN >= REPEAT_MIN:
                mark_removed(RemovedItem(s, e, t, "anti:repeat_tag_low_dbfs", "anti",
                                         no_speech_prob=seg.no_speech_prob, avg_logprob=seg.avg_logprob,
                                         compression_ratio=seg.compression_ratio, dbfs=db))
                continue

            # 긴 문장 + 저에너지 -> evid 있을 때만
            if len(t) >= LOW_ENERGY_LONG_TEXT_LEN:
                if has_dbfs_evidence(seg, repN):
                    mark_removed(RemovedItem(s, e, t, "anti:adaptive_low_dbfs_long_text_evid", "anti",
                                             no_speech_prob=seg.no_speech_prob, avg_logprob=seg.avg_logprob,
                                             compression_ratio=seg.compression_ratio, dbfs=db))
                    continue
                out.append(seg)
                continue

            # 일반 저에너지 -> evid 있을 때만
            if has_dbfs_evidence(seg, repN):
                mark_removed(RemovedItem(s, e, t, "anti:adaptive_low_dbfs_evid", "anti",
                                         no_speech_prob=seg.no_speech_prob, avg_logprob=seg.avg_logprob,
                                         compression_ratio=seg.compression_ratio, dbfs=db))
                continue

            out.append(seg)
            continue

        out.append(seg)

    out.sort(key=lambda x: (x.start, x.end))
    return out, removed, removed_items, low_dbfs_th


def segs_to_srt_entries(segs: List[Seg]) -> List[Tuple[float, float, str]]:
    entries: List[Tuple[float, float, str]] = []
    for s in segs:
        t = (s.text or "").strip()
        if not t:
            continue
        st, en = float(s.start), float(s.end)
        if en <= st:
            en = st + 0.2
        entries.append((st, en, t))
    entries.sort(key=lambda x: (x[0], x[1]))
    return entries


def save_srt(entries: List[Tuple[float, float, str]], out_path: Path) -> None:
    lines: List[str] = []
    idx = 1
    for s, e, t in entries:
        t = (t or "").strip()
        if not t:
            continue
        lines.append(str(idx))
        lines.append(f"{srt_time(s)} --> {srt_time(e)}")
        lines.append(t)
        lines.append("")
        idx += 1
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def save_removed_reports(removed: List[RemovedItem], base_out_srt: Path) -> None:
    if not removed:
        return
    csv_path = Path(base_out_srt.with_suffix("").as_posix() + "_removed.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["start", "end", "reason", "stage", "no_speech_prob", "avg_logprob", "compression_ratio", "dbfs", "text"])
        for r in removed:
            w.writerow([
                f"{r.start:.3f}", f"{r.end:.3f}", r.reason, r.stage,
                "" if r.no_speech_prob is None else f"{r.no_speech_prob:.4f}",
                "" if r.avg_logprob is None else f"{r.avg_logprob:.4f}",
                "" if r.compression_ratio is None else f"{r.compression_ratio:.4f}",
                "" if r.dbfs is None else f"{r.dbfs:.2f}",
                (r.text or "").replace("\n", " ").strip()
            ])
    srt_path = Path(base_out_srt.with_suffix("").as_posix() + "_removed.srt")
    entries = [(r.start, r.end, f"[{r.stage}] {r.reason}\n{r.text}") for r in removed if (r.text or "").strip()]
    save_srt(entries, srt_path)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--ow-model", type=str, default=DEFAULT_OW_MODEL)
    ap.add_argument("--fw-model", type=str, default=DEFAULT_FW_MODEL)

    # 운영 기본값
    ap.add_argument("--anti-halluc", type=str, default="on", choices=["on", "off"])
    ap.add_argument("--preserve-vocals", type=str, default="on", choices=["on", "off"])

    # 운영 기본: 실험 파일 생성 차단
    ap.add_argument("--report", type=str, default="off", choices=["on", "off"])
    ap.add_argument("--save-removed", type=str, default="off", choices=["on", "off"])

    ap.add_argument("--lang", type=str, default="", help="en/ko/ja ... empty=auto")
    ap.add_argument("--low-dbfs-q", type=float, default=ADAPTIVE_LOW_DBFS_QUANTILE,
                    help="adaptive low-dbfs quantile (0~1), default 0.15")
    return ap.parse_args()


def ask_language_if_needed(lang_from_args: str) -> Optional[str]:
    # --lang가 주어지면 입력을 묻지 않는다.
    lang = (lang_from_args or "").strip()
    if lang:
        return lang

    # --lang가 비어있을 때만 입력(엔터=자동감지)
    try:
        u = input("언어 코드를 입력하세요 (예: ja, ko, en) [엔터=자동감지]: ").strip()
        return u if u else None
    except (EOFError, KeyboardInterrupt):
        return None


def main() -> None:
    which_or_die("ffmpeg")
    which_or_die("ffprobe")

    args = parse_args()
    user_lang = ask_language_if_needed(args.lang)

    anti = (args.anti_halluc.lower() == "on")
    preserve_vocals = (args.preserve_vocals.lower() == "on")
    write_report = (args.report.lower() == "on")
    save_removed = (args.save_removed.lower() == "on")

    global ADAPTIVE_LOW_DBFS_QUANTILE
    ADAPTIVE_LOW_DBFS_QUANTILE = float(args.low_dbfs_q)
    ADAPTIVE_LOW_DBFS_QUANTILE = min(0.5, max(0.01, ADAPTIVE_LOW_DBFS_QUANTILE))

    inputs = [p for p in auto_scan_inputs(Path.cwd())]
    if not inputs:
        print("[ERROR] No media files found in current folder.", file=sys.stderr)
        sys.exit(1)

    ow_model = whisper.load_model(args.ow_model.strip() or DEFAULT_OW_MODEL)

    from faster_whisper import WhisperModel  # type: ignore
    device = "cuda"
    try:
        import torch  # type: ignore
        if not torch.cuda.is_available():
            device = "cpu"
    except Exception:
        device = "cpu"

    if device == "cuda":
        fw_model = None
        for compute_type in ("float16", "int8_float16"):
            try:
                fw_model = WhisperModel(args.fw_model.strip() or DEFAULT_FW_MODEL, device="cuda", compute_type=compute_type)
                break
            except Exception:
                fw_model = None
        if fw_model is None:
            fw_model = WhisperModel(args.fw_model.strip() or DEFAULT_FW_MODEL, device="cpu", compute_type="int8")
    else:
        fw_model = WhisperModel(args.fw_model.strip() or DEFAULT_FW_MODEL, device="cpu", compute_type="int8")

    report_rows: List[Dict[str, str]] = []
    report_path = Path.cwd() / f"_transcribe_report_v2.7.5_{int(time.time())}.csv"

    total_files = len(inputs)
    done_files = 0
    start_all = time.perf_counter()

    print(
        f"\n[CONFIG] anti_halluc={anti} preserve_vocals={preserve_vocals} "
        f"save_removed={save_removed} report={write_report} low_dbfs_q={ADAPTIVE_LOW_DBFS_QUANTILE} "
        f"lang={user_lang or 'auto'} ow={args.ow_model} fw={args.fw_model} device={device}\n"
    )

    for p in inputs:
        file_start = time.perf_counter()
        progress_bar(done_files, total_files, p.name)

        stem = sanitize_filename(p.stem)
        out_srt = Path.cwd() / f"{stem}.srt"

        tmp_dir = Path(tempfile.mkdtemp(prefix="v2_7_5_prod_"))
        detected_lang: Optional[str] = None
        keep_ranges: List[Tuple[float, float]] = []

        chunks_count = 0
        seg_raw = 0
        gating_dropped = 0
        collapsed_count = 0
        anti_removed_cnt = 0
        low_dbfs_th_used = float("nan")
        removed_all: List[RemovedItem] = []

        try:
            if is_already_wav_pcm16_16k_mono(p):
                wav16k = p
            else:
                wav16k = tmp_dir / f"{stem}_16k.wav"
                preprocess_to_wav16k_mono(p, wav16k)

            total_dur = ffprobe_duration(wav16k)

            with suppress_console_output(True):
                detected_lang, keep_ranges = build_keep_ranges_from_fw(
                    fw_model=fw_model,
                    wav16k_path=wav16k,
                    total_dur=total_dur,
                    lang_hint=user_lang,
                )

            lang_for_pass2 = user_lang or detected_lang or None
            if not keep_ranges:
                keep_ranges = [(0.0, total_dur)]

            chunks = chunk_ranges(keep_ranges, max_len=MAX_CHUNK_SEC, overlap=CHUNK_OVERLAP_SEC)
            chunks_count = len(chunks)

            segs_all: List[Seg] = []

            for (s, e) in chunks:
                if e - s < 0.15:
                    continue
                clip = tmp_dir / f"clip_{s:.2f}_{e:.2f}.wav"
                extract_clip(wav16k, clip, start=s, end=e)

                with suppress_console_output(True):
                    result = ow_model.transcribe(
                        str(clip),
                        language=lang_for_pass2,
                        task="transcribe",
                        fp16=FP16,
                        temperature=TEMPERATURE,
                        condition_on_previous_text=CONDITION_ON_PREV,
                        verbose=WHISPER_VERBOSE,
                    )

                for seg in result.get("segments", []):
                    seg_raw += 1
                    ss = float(seg["start"]) + s
                    ee = float(seg["end"]) + s
                    txt = str(seg.get("text", "")).strip()
                    if not txt:
                        gating_dropped += 1
                        if save_removed:
                            removed_all.append(RemovedItem(ss, ee, txt, "metrics:empty", "metrics",
                                                          no_speech_prob=seg.get("no_speech_prob"), avg_logprob=seg.get("avg_logprob"),
                                                          compression_ratio=seg.get("compression_ratio"), dbfs=None))
                        continue

                    obj = Seg(
                        start=ss,
                        end=ee if ee > ss else ss + 0.2,
                        text=txt,
                        no_speech_prob=seg.get("no_speech_prob", None),
                        avg_logprob=seg.get("avg_logprob", None),
                        compression_ratio=seg.get("compression_ratio", None),
                    )

                    drop, reason = should_drop_by_metrics(obj, preserve_vocals=preserve_vocals)
                    if drop:
                        gating_dropped += 1
                        if save_removed:
                            removed_all.append(RemovedItem(obj.start, obj.end, obj.text, reason, "metrics",
                                                          no_speech_prob=obj.no_speech_prob, avg_logprob=obj.avg_logprob,
                                                          compression_ratio=obj.compression_ratio, dbfs=None))
                        continue

                    segs_all.append(obj)

            segs_all, collapsed_count = collapse_repeats_stronger(segs_all, preserve_vocals=preserve_vocals)

            if anti:
                before = len(segs_all)
                segs_all, anti_removed_cnt, removed_anti, low_dbfs_th_used = anti_halluc_filter_segs_adaptive(
                    segs_all,
                    analysis_wav=wav16k,
                    lang_hint=lang_for_pass2,
                    preserve_vocals=preserve_vocals,
                )
                after = len(segs_all)
                anti_removed_cnt = max(anti_removed_cnt, before - after)
                if save_removed:
                    removed_all.extend(removed_anti)
            else:
                low_dbfs_th_used = float("nan")

            entries = segs_to_srt_entries(segs_all)
            save_srt(entries, out_srt)

            if save_removed:
                save_removed_reports(removed_all, out_srt)

        except Exception as e:
            sys.stderr.write(f"\n[WARN] Failed: {p.name} | {e}\n")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        done_files += 1
        progress_bar(done_files, total_files, p.name)

        file_elapsed = time.perf_counter() - file_start
        th_msg = f"{low_dbfs_th_used:.2f}dBFS" if math.isfinite(low_dbfs_th_used) else "n/a"

        # ✅ PowerShell 진행바 덮어쓰기 방지: [FILE DONE] 전 줄바꿈 강제
        print()

        print(
            f"[FILE DONE] {p.name} | {fmt_dur(file_elapsed)} | lang={lang_for_pass2 or 'auto'} | "
            f"anti={anti} preserve_vocals={preserve_vocals} | low_dbfs_th={th_msg} | "
            f"chunks={chunks_count} seg_raw={seg_raw} gating_drop={gating_dropped} "
            f"repeat_collapsed={collapsed_count} anti_removed={anti_removed_cnt} | "
            f"final_lines={len(entries)} | out={out_srt.name}"
        )

        if write_report:
            report_rows.append({
                "file": p.name,
                "out_srt": out_srt.name,
                "lang": str(lang_for_pass2 or "auto"),
                "anti": str(anti),
                "preserve_vocals": str(preserve_vocals),
                "low_dbfs_q": f"{ADAPTIVE_LOW_DBFS_QUANTILE:.3f}",
                "low_dbfs_th": "" if not math.isfinite(low_dbfs_th_used) else f"{low_dbfs_th_used:.2f}",
                "chunks": str(chunks_count),
                "seg_raw": str(seg_raw),
                "gating_drop": str(gating_dropped),
                "repeat_collapsed": str(collapsed_count),
                "anti_removed": str(anti_removed_cnt),
                "final_lines": str(len(entries)),
                "elapsed_sec": f"{file_elapsed:.3f}",
            })

    if write_report and report_rows:
        with open(report_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(report_rows[0].keys()))
            w.writeheader()
            w.writerows(report_rows)
        print(f"\n[REPORT] saved: {report_path.name}")

    elapsed = time.perf_counter() - start_all
    print(
        f"\n[DONE] {total_files} files in {elapsed/60:.1f} min | anti={anti} preserve_vocals={preserve_vocals} | "
        f"low_dbfs_q={ADAPTIVE_LOW_DBFS_QUANTILE} ow={args.ow_model} fw={args.fw_model} | "
        f"lang={(user_lang or 'auto')}\n"
    )


if __name__ == "__main__":
    main()
