# audio-transcriber

음원 파일을 SRT 자막으로 전사하고, 한국어로 번역하는 파이썬 도구입니다.

---

## 파일 구성

| 파일 | 역할 |
|------|------|
| `run.py` | **파이프라인 래퍼** — 전사 → 번역 한 번에 실행 |
| `audio_to_srt.py` | 음원 → SRT 전사 (faster-whisper large-v3) |
| `srt_translate_local.py` | SRT → 한국어 번역 (Ollama 로컬 모델) |

---

## 빠른 시작

```bash
# 음원 파일들을 같은 폴더에 놓고 실행
python run.py
```

실행하면 언어를 묻습니다:
- `ja` → 일본어 전사 후 한국어 번역
- `en` → 영어 전사 후 한국어 번역
- `ko` → 한국어 전사 (번역 생략)
- 엔터 → 자동 감지

---

## 각 파일 단독 실행

### 전사만

```bash
python audio_to_srt.py               # 언어 입력 프롬프트
python audio_to_srt.py --lang ja     # 일본어 고정
python audio_to_srt.py --lang en     # 영어 고정
python audio_to_srt.py --fw-model large-v3  # 모델 지정 (기본값)
```

### 번역만 (SRT 파일이 이미 있을 때)

```bash
python srt_translate_local.py              # 언어 입력 프롬프트
python srt_translate_local.py --lang-src ja
python srt_translate_local.py --lang-src en
python srt_translate_local.py --model gemma3:27b  # 모델 변경
```

---

## 의존성

```bash
pip install faster-whisper pysrt
# + ffmpeg, ffprobe (PATH에 등록)
# + Ollama 설치 및 모델 pull
```

### 권장 번역 모델 (한국어 품질 순)

| 모델 | VRAM | 품질 |
|------|------|------|
| `gemma3:27b` | 18GB+ | 최고 |
| `qwen2.5:14b` | 10GB+ | 우수 |
| `aya:8b` | 6GB+ | 기본값 |

```bash
ollama pull aya:8b        # 기본
ollama pull qwen2.5:14b   # 권장
```

---

## 주요 특징

- **faster-whisper large-v3** 단일 엔진 (전사 정확도 최대화)
- beam_size=5 + 언어별 initial_prompt로 환각 억제
- VAD(음성 구간 감지)로 무음 구간 자동 제거
- 반복 자막 자동 축약 + dBFS 기반 노이즈 필터
- 일본어/영어 자동 감지 → 한국어 번역
- 번역 후 한국어 후처리 (가나 잔류 제거, 공백 정규화)
- 여러 파일 일괄 처리 지원
