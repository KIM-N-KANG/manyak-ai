# 스토리 출력 검증 명세서

---

## 1. 목적 및 범위

`POST /api/v1/generate-story` 엔드포인트가 반환하는 LLM 출력물이 `1-BACKGROUND.md`에 정의된 요구사항을 만족하는지 검증하는 절차를 정의합니다.

검증 대상은 세 가지입니다.

1. **출력 구조**: 응답이 지정된 JSON 스키마를 따르는지
2. **출력 내용**: 이야기·질문이 기본 품질 기준을 만족하는지
3. **출력 다양성**: 3편 이야기가 서로 충분히 다른 방향인지

---

## 2. 검증 원칙

위키 `Verification-전략`의 두 원칙을 따릅니다.

**Sprint Contract**: 검증 기준을 테스트 실행 전에 수치로 확정합니다. "잘 나왔는지"가 아니라 "통과/실패 여부를 판단할 수 있는 기준"으로 작성합니다.

**Generator-Evaluator 분리**: 품질 판단(Layer 4)은 이야기를 생성한 모델과 다른 별도 모델이 수행합니다. 같은 모델이 자기 출력을 평가하면 편향이 발생합니다.

---

## 3. 검증 레이어

레이어는 자동화 가능 여부와 비용 기준으로 4단계로 나뉩니다.

| 레이어 | 이름 | 자동화 | CI 포함 | API 호출 비용 |
|---|---|---|---|---|
| Layer 1 | 구조 검증 | 완전 자동 | O | 없음 (모의 응답) |
| Layer 2 | 내용 기본 검증 | 완전 자동 | O (API 키 환경) | LLM 호출 1회 |
| Layer 3 | 다양성 검증 | 완전 자동 | O (API 키 환경) | Layer 2와 동일 응답 재사용 |
| Layer 4 | 품질 검증 | 반자동 | X (수동 실행) | Evaluator LLM 호출 1회 추가 |

---

## 4. Layer 1 — 구조 검증

### 4-1. 목적

LLM API 없이 파서 로직만 테스트합니다. 모의(fixture) 응답을 입력으로 사용합니다.

### 4-2. 검증 항목

| 번호 | 항목 | 판단 기준 | 판단 방법 |
|---|---|---|---|
| S-01 | JSON 파싱 성공 | `json.loads()` 예외 없음 | assert |
| S-02 | 코드펜스 제거 후 파싱 성공 | ` ```json ``` ` 감싼 응답도 파싱 성공 | assert |
| S-03 | `stories` 배열 존재 | 키 `stories` 가 존재 | assert |
| S-04 | `stories` 개수 | `len(stories) == 3` | assert |
| S-05 | `id` 순서 | `stories[i].id == i+1` (1, 2, 3 순) | assert |
| S-06 | `story` 필드 타입 | `isinstance(story, str)` and `len > 0` | assert |
| S-07 | `questions` 필드 존재 및 타입 | `isinstance(questions, list)` | assert |
| S-08 | `questions` 개수 | 각 이야기마다 `len(questions) == 3` | assert |
| S-09 | 각 질문 타입 | `isinstance(q, str)` and `len > 0` | assert |
| S-10 | Pydantic 스키마 통과 | `StoryResponse(**data)` 예외 없음 | assert |

### 4-3. 픽스처 정의

테스트에 사용할 모의 응답 픽스처를 아래 두 종류로 준비합니다.

**정상 픽스처** (`fixture_valid.json`): 규격에 맞는 응답
```json
{
  "stories": [
    { "id": 1, "story": "...", "questions": ["...", "...", "..."] },
    { "id": 2, "story": "...", "questions": ["...", "...", "..."] },
    { "id": 3, "story": "...", "questions": ["...", "...", "..."] }
  ]
}
```

**코드펜스 픽스처** (`fixture_code_fence.json`): 코드펜스로 감싼 응답
```
```json
{ "stories": [ ... ] }
```
```

### 4-4. 실패 케이스 픽스처

파서가 올바르게 에러를 반환하는지도 확인합니다.

| 픽스처 이름 | 내용 | 기대 동작 |
|---|---|---|
| `fixture_stories_2.json` | `stories` 2개 | S-04 실패 → 서비스 레이어에서 처리 |
| `fixture_questions_2.json` | 질문 2개인 이야기 포함 | S-08 실패 → Pydantic validation error |
| `fixture_not_json.txt` | JSON 아닌 텍스트 | `json.JSONDecodeError` → HTTPException 500 |

---

## 5. Layer 2 — 내용 기본 검증

### 5-1. 목적

실제 LLM API를 호출하여 반환된 응답의 내용이 최소 품질 기준을 만족하는지 확인합니다.

### 5-2. 테스트 입력 픽스처

내용 검증은 아래 고정 입력으로만 실행합니다. 입력을 고정해야 결과를 비교·추적할 수 있습니다.

```json
{
  "genre": ["게임", "회귀"],
  "protagonist": ["소심한", "눈치 빠른"],
  "characters": ["압도적인", "거친"]
}
```

### 5-3. 검증 항목

| 번호 | 항목 | 판단 기준 | 판단 방법 |
|---|---|---|---|
| C-01 | 이야기 최소 길이 | 각 `story` 문자 수 ≥ 80자 | assert |
| C-02 | 이야기 문장 수 | 각 `story`의 문장이 정확히 4개 | 아래 문장 분리 기준 참고 |
| C-03 | 이모지 없음 | `story`와 `questions`에 이모지 문자 없음 | 유니코드 범위 검사 |
| C-04 | 질문 최소 길이 | 각 `question` 문자 수 ≥ 10자 | assert |
| C-05 | 질문 문장 수 | 각 `question`이 1문장 (마침표 1개 이하) | assert |
| C-06 | 태그 반영 여부 | 입력 태그 중 1개 이상이 이야기 3편 중 어느 곳에 등장 | 키워드 포함 검사 |
| C-07 | 응답 시간 | 전체 API 응답 ≤ 30초 | `time.time()` 측정 |

**문장 수 판단 기준 (C-02)**:

한국어 문장 종결을 마침표(`.`)와 `다`, `요`, `까`, `군`, `네`로 끝나는 패턴으로 감지합니다. 정규식 `[다요까군네]\.$`로 분리합니다. 4개가 아니면 실패 처리하되, ±1 오차는 경고(warning)로만 기록합니다.

---

## 6. Layer 3 — 다양성 검증

### 6-1. 목적

3편의 이야기가 서로 다른 방향임을 수치로 검증합니다. Layer 2와 동일한 API 호출 응답을 재사용합니다.

### 6-2. 검증 항목

| 번호 | 항목 | 판단 기준 | 판단 방법 |
|---|---|---|---|
| D-01 | 스토리 쌍 텍스트 유사도 | 3쌍 (0-1, 0-2, 1-2) 모두 유사도 < 0.5 | `difflib.SequenceMatcher` |
| D-02 | 첫 문장 중복 금지 | 3편의 첫 문장이 모두 다름 | 완전 일치 검사 |
| D-03 | 도입 주어 중복 금지 | 3편의 첫 15자가 모두 다름 | 부분 일치 검사 |

**D-01 유사도 계산 방법**:

```python
from difflib import SequenceMatcher

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()

pairs = [(s[0], s[1]), (s[0], s[2]), (s[1], s[2])]
for a, b in pairs:
    assert similarity(a.story, b.story) < 0.5
```

**임계값 0.5 근거**: 완전히 다른 문체·소재의 텍스트는 0.2 이하, 같은 구조 다른 단어는 0.5~0.7 범위에 위치합니다. 0.5 이상이면 동일 서술 구조를 공유하는 것으로 간주합니다.

---

## 7. Layer 4 — 품질 검증 (LLM-as-Judge)

### 7-1. 목적

규칙으로 판단할 수 없는 품질 요소를 별도 LLM(Evaluator)이 평가합니다. Generator와 Evaluator를 반드시 다른 컨텍스트(별도 클라이언트 인스턴스)로 실행합니다.

### 7-2. 실행 방식

수동 스크립트(`tests/eval/eval_story_quality.py`)로 실행합니다. CI에 포함하지 않습니다.

### 7-3. 검증 항목

| 번호 | 항목 | 평가 질문 | 합격 기준 |
|---|---|---|---|
| Q-01 | 웹소설 문체 | "이 이야기가 웹소설 도입부로 자연스러운가?" | 3편 모두 5점 중 3점 이상 |
| Q-02 | 태그-이야기 정합성 | "이야기가 입력 태그를 자연스럽게 반영하는가?" | 3편 평균 3점 이상 |
| Q-03 | 질문 특화성 | "이 질문이 해당 이야기의 고유 설정을 언급하는가?" | 9개 질문 중 7개 이상 합격 |
| Q-04 | 다양성 주관 판단 | "3편의 분위기와 방향이 확연히 다른가?" | 5점 중 4점 이상 |

### 7-4. Evaluator 프롬프트

각 항목 평가 시 아래 형식의 프롬프트를 Evaluator LLM에 전달합니다.

```
다음 이야기를 읽고 [평가 질문]을 1~5점으로 평가하시오.
점수만 숫자로 반환하시오.

이야기:
{story}
```

### 7-5. 결과 저장

실행 결과를 `tests/eval/results/` 에 날짜 기준으로 저장합니다.

```
tests/eval/results/
  2026-06-10_eval.json
```

저장 형식:
```json
{
  "run_date": "2026-06-10",
  "input": { ... },
  "scores": {
    "Q-01": [4, 5, 3],
    "Q-02": [4, 4, 5],
    "Q-03": { "pass": 8, "fail": 1 },
    "Q-04": 4
  },
  "result": "PASS"
}
```

---

## 8. 판단 기준 총람 (Sprint Contract)

Layer 1~3은 자동 테스트 통과가 완료 기준입니다. Layer 4는 수동 실행 후 결과 파일 저장이 완료 기준입니다.

| 항목 | 기준값 | 레이어 | 자동화 |
|---|---|---|---|
| stories 개수 | 정확히 3 | L1 | O |
| questions 개수 | 이야기당 정확히 3 | L1 | O |
| story 문장 수 | 정확히 4 (±1 경고) | L2 | O |
| story 최소 길이 | 80자 이상 | L2 | O |
| 이모지 없음 | 0개 | L2 | O |
| 태그 반영 | 태그 1개 이상 텍스트에 포함 | L2 | O |
| 응답 시간 | 30초 이하 | L2 | O |
| 스토리 쌍 유사도 | 0.5 미만 | L3 | O |
| 첫 문장 중복 | 0건 | L3 | O |
| 웹소설 문체 점수 | 3편 모두 3점 이상 | L4 | X |
| 태그-이야기 정합성 | 평균 3점 이상 | L4 | X |
| 질문 특화성 통과율 | 9개 중 7개 이상 | L4 | X |
| 다양성 주관 점수 | 4점 이상 | L4 | X |

---

## 9. 테스트 디렉토리 구조

```
tests/
  unit/
    test_story_parser.py       # Layer 1: 픽스처 기반 파서 테스트
    fixtures/
      fixture_valid.json
      fixture_code_fence.txt
      fixture_stories_2.json
      fixture_questions_2.json
      fixture_not_json.txt
  integration/
    test_story_content.py      # Layer 2+3: 실제 API 호출 후 내용·다양성 검증
  eval/
    eval_story_quality.py      # Layer 4: 수동 실행 품질 평가 스크립트
    results/                   # 평가 결과 저장 (날짜별)
```

---

## 10. CI 실행 조건

| 레이어 | 실행 조건 | 명령 |
|---|---|---|
| Layer 1 | 항상 실행 | `pytest tests/unit/` |
| Layer 2~3 | `ANTHROPIC_API_KEY` 환경 변수 존재 시 | `pytest tests/integration/` |
| Layer 4 | 수동 실행 | `python tests/eval/eval_story_quality.py` |

Layer 2~3이 CI에서 실행되지 않을 경우 (API 키 없는 환경), 해당 테스트는 `pytest.skip`으로 건너뜁니다.

```python
import pytest, os

@pytest.fixture(autouse=True)
def require_api_key():
    if not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY 없음 — 통합 테스트 건너뜀")
```
