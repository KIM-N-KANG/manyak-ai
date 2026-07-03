---
version: 2
updated: 2026-07-03
---

# 스토리 컴파일 시스템 명세

---

## 1. 목적

사용자가 [스토리라인 생성](1-STORYLINES.md) 단계에서 고른 한 편의 스토리라인과 추가정보·태그를, 한 편의 인터랙티브 채팅 플레이를 구동할 풍성한 **스토리 명세**로 확장하는 시스템입니다.

스토리라인은 4단계 흐름을 압축한 짧은 줄거리일 뿐이라, 그대로는 채팅 플레이의 무대·등장인물·주인공 설정으로 쓸 수 없습니다. 컴파일은 이 희소한 입력을 받아 세계관·인물 카드·주인공 프로필·연출 규칙·시작 장면으로 구체화합니다. 이 산출물(스토리 명세)은 이후 채팅에서 STORY·CHARACTER·USER 설정으로 쓰입니다.

와이어프레임상 "추가 정보 입력 → (컴파일) → 플레이 시작"의 전환 지점에 대응하며, 스토리라인 생성 다음·채팅 플레이 이전 단계입니다.

---

## 2. 시스템 구성 요소

스토리라인 생성과 동일하게 클라이언트(백엔드) ↔ AI 서버(FastAPI) ↔ LLM API(DeepSeek)로 구성됩니다. 차이는 입력이 태그가 아니라 **선택한 스토리라인 1편 + 추가정보 + 원본 태그**이고, 출력이 이야기 3편이 아니라 **스토리 명세 1건**이라는 점입니다.

컴파일은 스토리라인보다 출력이 길고 구조가 복잡해(인물 카드 최대 5명 등) 더 큰 모델을 쓰고, 빈 칸을 채우기 위한 부분 재호출이 추가됩니다.

| 항목 | 스토리라인 생성 | 컴파일 |
|---|---|---|
| 입력 | 태그 3종 | 선택 스토리라인 + 추가정보 + 태그 3종 |
| 출력 | 이야기 3편 + 추천정보 | 스토리 명세 1건(4테이블) |
| 모델 | `deepseek-v4-flash` | `deepseek-v4-pro` |
| 호출 | 단일 호출 | 본호출 + 빈 블록 부분 재호출(최대 2회) |

프롬프트 템플릿은 `prompt/story/COMPILE-TEMPLATE.md`이며, 스토리라인과 마찬가지로 `[SYSTEM]`·`[USER]` 두 블록으로 구성됩니다.

---

## 3. 시스템 흐름과 각 단계의 이유

```
[1] 백엔드   →  선택 스토리라인+추가정보+태그 (POST /api/v1/story/compile)  →  AI 서버
[2] AI 서버  →  자리표시자 치환으로 완성된 프롬프트 생성
[3] AI 서버  →  LLM 호출  →  세분 JSON(StorySpec 구조) 수신
[4] AI 서버  →  meta.genre를 입력 태그로 덮어쓰기
[5] AI 서버  →  빈 필수 필드 탐지
[6] AI 서버  →  (빈 필드가 있으면) 그 블록만 부분 재호출 — 최대 2회
[7] AI 서버  →  StorySpec(Pydantic) 파싱
[8] AI 서버  →  세분 명세를 ERD 4테이블 통글 마크다운으로 변환
[9] AI 서버  →  스토리 명세 + 로깅 메타 반환  →  백엔드
```

**[3] 세분 JSON으로 받는 이유**: LLM에게 최종 형태(통글 마크다운)로 바로 답하게 하면, 어느 부분이 비었는지 탐지하거나 그 부분만 다시 채우기가 어렵습니다. 그래서 LLM은 필드가 잘게 나뉜 세분 JSON으로 답하고, 서버가 그 결과를 검증·보정한 뒤 최종 형태로 재구성합니다.

**[4] genre를 덮어쓰는 이유**: 장르는 백엔드가 보낸 입력 태그가 정본입니다. LLM이 임의로 바꾸거나 누락할 수 있으므로, LLM 출력의 `meta.genre`는 무시하고 입력 태그로 덮어씁니다.

**[5]~[6] 빈 필드 검증·부분 재호출이 필요한 이유**: 스토리 명세는 채팅 플레이에 그대로 쓰이므로 필수 슬롯이 비면 안 됩니다. 그런데 Pydantic 검증은 빈 문자열(`""`)을 통과시키고, 파싱이 먼저 실패하면 다시 채울 기회가 사라집니다. 따라서 파싱 전 dict 단계에서 빈 필수 필드를 직접 찾아, 비어 있는 블록만 다시 채우는 부분 재호출로 메웁니다.

**[8] 통글 마크다운으로 변환하는 이유**: 백엔드의 ERD 4테이블 중 `story_settings`는 사람이 읽기 좋고 채팅 AI가 바로 슬롯에 끼울 수 있는 통글 마크다운으로 저장합니다. 검증에 유리한 세분 구조와 저장·활용에 유리한 통글 구조가 다르므로, 서버가 마지막에 세분 명세를 통글로 재조립합니다.

---

## 4. AI 서버 요구사항

### 4-1. 프롬프트 완성

`[USER]` 블록의 자리표시자를 입력값으로 치환합니다. 두 블록은 OpenAI 호환 `messages` 배열에 `system`·`user` 역할로 전달합니다(스토리라인과 동일).

| 자리표시자 | 치환 대상 |
|---|---|
| `{{선택_스토리라인}}` | selected_storyline 문자열 |
| `{{추가정보}}` | additional_info 문자열 (비어 있으면 `(없음)`) |
| `{{장르_태그}}` | genre_tags 배열 → 쉼표 구분 문자열 |
| `{{주인공_특징_태그}}` | protagonist_tags 배열 → 쉼표 구분 문자열 |
| `{{주변_인물_태그}}` | supporting_tags 배열 → 쉼표 구분 문자열 |

호출 파라미터는 모델만 다르고 나머지는 스토리라인과 같습니다: `response_format={"type": "json_object"}`, temperature 0.75, max_tokens 6144, 추론 비활성(`thinking: disabled`), 90초 타임아웃.

### 4-2. 세분 JSON 응답과 파싱

LLM 응답은 텍스트이므로 JSON으로 파싱합니다. 코드 펜스가 있으면 제거하고, 빈 응답이거나 객체(dict)가 아니면 유효하지 않은 응답으로 간주합니다(스토리라인 파싱과 동일).

LLM이 답하는 JSON은 최종 출력 형태가 아니라, 검증·재호출에 유리하도록 필드가 잘게 나뉜 **세분 스키마(`StorySpec`)**입니다(→ 5-2).

### 4-3. genre 주입

파싱한 dict의 `meta.genre`를, LLM 출력 대신 입력 `genre_tags`를 쉼표로 이은 문자열로 덮어씁니다. 본호출과 각 재호출 직후에 적용합니다.

### 4-4. 빈 필드 검증과 부분 재호출

파싱 전 dict 단계에서 빈 필수 필드를 직접 탐지합니다(빈 문자열·빈 배열·null·공백만을 빈 값으로 간주).

- 비어 있는 필드가 있으면, 그 필드가 속한 **블록만** 다시 채우도록 부분 재호출합니다. 재호출 프롬프트에는 직전 생성 결과를 맥락으로 주고, 빈 블록만 채워 그 블록만 최상위 키로 갖는 JSON을 돌려받습니다. 잘 나온 다른 블록은 보존하기 위해 응답에 포함하지 않게 합니다.
- 재호출은 **최대 2회**까지 반복합니다. 2회 후에도 빈 필수 필드가 남으면 502로 막습니다(→ 4-6).
- 검증에서 제외하는 예외 필드: `meta.genre`(서버가 입력 태그로 덮어씀), `user_role_setting.preference`(선택 입력이라 비어 있어도 됨).

필수 필드 점검 대상: `meta`(title·one_line_intro·description), `prompt_settings`(world_setting·rule_setting·tone_setting·length_ratio, plot_setting의 premise·conflict, character_setting 1개 이상과 각 카드의 5개 필드, user_role_setting의 preference 제외 4개 필드), `start`(name·prologue·start_situation), `suggested_inputs`(정확히 3개이며 각 항목이 비어 있지 않음).

### 4-5. 세분 → 통글 변환

검증을 통과한 세분 명세를 ERD 4테이블에 1:1로 대응하는 nested 형태로 재구성합니다. `story_settings`의 4개 필드는 사람이 읽기 좋은 통글 마크다운으로 조립합니다(→ 5-3). 별도 템플릿 파일 없이 서버 코드가 조립합니다.

### 4-6. 에러 처리

| 상황 | 요구 동작 |
|---|---|
| LLM API 호출 실패(타임아웃·rate limit·요청 거부·연동 오류) | 게이트웨이 오류(502) 반환 |
| 빈 응답이거나 유효하지 않은 JSON | 파싱 실패로 간주하고 502 반환 |
| 재호출 2회 후에도 필수 필드가 빔 | 502 반환 |
| 세분 명세가 `StorySpec` 스키마와 맞지 않음 | 스키마 검증 실패로 502 반환 |

502 응답 본문에는 사용자에게 보일 안내 메시지만 담고, 공급자 원문 오류는 Sentry로만 보냅니다(KNK-262).

### 4-7. 프롬프트 캐싱

`[SYSTEM]` 블록은 모든 호출에서 동일하므로, DeepSeek이 동일한 접두 컨텍스트를 자동으로 컨텍스트 캐시 처리합니다. 별도 캐시 설정은 필요하지 않으며, 적중 여부는 응답의 `usage.prompt_cache_hit_tokens`로 확인합니다.

---

## 5. 데이터 명세

### 5-1. API 계약

#### 요청 (Request)

```
POST /api/v1/story/compile
```

```json
{
  "selected_storyline": "선택한 스토리라인 본문",
  "additional_info": "주인공 호칭·선호 등 추가정보(선택)",
  "genre_tags":       ["무협", "회귀"],
  "protagonist_tags": ["냉혹한", "치밀한"],
  "supporting_tags":  ["배신자", "스승"]
}
```

| 필드 | 타입 | 제약 |
|---|---|---|
| selected_storyline | string | 필수 |
| additional_info | string | 선택(기본값 빈 문자열) |
| genre_tags | string[] | 필수 |
| protagonist_tags | string[] | 필수 |
| supporting_tags | string[] | 필수 |

#### 응답 (Response)

ERD 4테이블에 1:1 대응하는 nested 구조입니다.

```json
{
  "stories": {
    "title": "...",
    "one_line_intro": "...",
    "description": "..."
  },
  "story_settings": {
    "world_setting": "# 세계관\n...\n\n# 전제\n...\n\n# 갈등\n...",
    "character_setting": "# 등장인물\n\n## 이름\n### 성격\n...",
    "user_role_setting": "# 주인공\n## 호칭\n...",
    "rule_setting": "# 전개 규칙\n...\n\n# 문체 톤\n...\n\n# 분량 배분\n묘사 7 : 대사 3"
  },
  "story_start_settings": {
    "name": "...",
    "start_situation": "...",
    "prologue": "..."
  },
  "story_suggested_inputs": ["...", "...", "..."],
  "meta": {
    "model": "deepseek-v4-pro",
    "prompt_versions": { "COMPILE": 3 },
    "provider": "deepseek",
    "input_token_count": 3500,
    "output_token_count": 2200,
    "retry_count": 0
  }
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| stories | object | 노출 메타(`stories` 테이블). genre는 백엔드가 입력 태그로 채우므로 제외 |
| stories.title / one_line_intro / description | string | 제목·한 줄 소개·소개문 |
| story_settings | object | 채팅 AI 프롬프트 재료(`story_settings` 테이블). 4필드 모두 통글 마크다운 |
| story_start_settings | object | 시작 설정(`story_start_settings` 테이블). name·start_situation·prologue |
| story_suggested_inputs | string[] | 첫 입력 추천 문구. 정확히 3개 |
| meta | object | 응답 로깅 메타(`ai_call_logs` 적재용, KNK-243) |
| meta.retry_count | number | 부분 재호출 횟수(0~2) |

`meta`의 나머지 필드(model·prompt_versions·provider·input_token_count·output_token_count)는 스토리라인과 동일합니다. 토큰 수는 본호출과 재호출을 **합산**하며, model은 본호출 응답값을 씁니다.

### 5-2. 내부 세분 스키마 (StorySpec)

LLM이 답하고 서버가 검증·재호출에 쓰는 중간 JSON입니다. 백엔드로는 나가지 않고, 4-5에서 통글로 변환됩니다.

```json
{
  "meta": { "title": "...", "one_line_intro": "...", "description": "...", "genre": "..." },
  "prompt_settings": {
    "world_setting": "...",
    "plot_setting": { "premise": "...", "conflict": "..." },
    "rule_setting": "...",
    "tone_setting": "...",
    "length_ratio": "묘사 7 : 대사 3",
    "character_setting": [
      { "name": "...", "personality": "...", "tone": "...", "motivation": "...", "attitude_to_user": "..." }
    ],
    "user_role_setting": { "name": "...", "role": "...", "background": "...", "personality": "...", "preference": "" }
  },
  "start": { "name": "...", "prologue": "...", "start_situation": "..." },
  "suggested_inputs": ["...", "...", "..."]
}
```

| 영역 | 필드 | 설명 |
|---|---|---|
| meta | title·one_line_intro·description·genre | 노출 메타. genre만 서버가 입력 태그로 덮어씀 |
| prompt_settings | world_setting | 거시 세계관·설정 |
| | plot_setting.premise / conflict | 도입 상황 / 앞으로 일어날 수 있는 갈등(확정 결과 아님) |
| | rule_setting | 전개 속도·긴장 곡선 등 연출 규칙 |
| | tone_setting | 장면 전체의 서술 톤 |
| | length_ratio | 묘사와 대사의 비중(`묘사 N : 대사 M`) |
| | character_setting | 주변 인물 카드 1~5명. 각 카드는 name·personality·tone·motivation·attitude_to_user |
| | user_role_setting | 주인공 프로필. name·role·background·personality·preference(선택) |
| start | name·prologue·start_situation | 시작 설정 이름·도입 나레이션·첫 장면 |
| suggested_inputs | string[] | 첫 입력 추천 문구 3개 |

### 5-3. 세분 → 통글 변환 규칙

세분 명세의 필드를 ERD 4테이블로 재구성합니다.

| ERD 테이블 | 출력 필드 | 세분 명세 출처 |
|---|---|---|
| stories | stories | meta(genre 제외) |
| story_settings | story_settings(통글 4필드) | prompt_settings 7필드를 4통글로 재구성 |
| story_start_settings | story_start_settings | start |
| story_suggested_inputs | story_suggested_inputs | suggested_inputs |

`story_settings` 4개 통글 필드의 구성과 마크다운 구조는 다음과 같습니다.

| 통글 필드 | 구성(세분 출처) | 마크다운 구조 |
|---|---|---|
| world_setting | world_setting + plot_setting | `# 세계관` / `# 전제` / `# 갈등` |
| character_setting | character_setting[] | `# 등장인물` + 인물마다 `## 이름` / `### 성격`·`### 말투`·`### 동기`·`### 주인공을 대하는 태도` |
| user_role_setting | user_role_setting | `# 주인공` / `## 호칭`·`## 역할`·`## 배경`·`## 성격`·`## 입력 선호` |
| rule_setting | rule_setting + tone_setting + length_ratio | `# 전개 규칙` / `# 문체 톤` / `# 분량 배분` |

---

## 6. 프롬프트 요구사항

전문은 `prompt/story/COMPILE-TEMPLATE.md`이며, 핵심 요구사항은 다음과 같습니다.

**역할 매핑**: 스토리라인의 구성 요소를 올바른 대상에 귀속시킵니다.

| 구성 요소 | 귀속 대상 |
|---|---|
| 주인공(사용자가 1인칭으로 연기) | `user_role_setting` (절대 `character_setting`에 넣지 않음) |
| 주변 인물(주인공이 아닌 등장인물) | `character_setting` (AI가 연기할 NPC) |
| 세계관·전개·분위기 | `world_setting` / `plot_setting` / `rule_setting` / `tone_setting` / `length_ratio` |

**필드별 작성 규칙**(요지):

- `meta.title`: 기존 웹소설처럼 자극적으로, 가장 센 한 방을 앞세워 짓는다(설명조 금지). 한 문장·공백 포함 25자 이내 권장.
- `plot_setting.conflict`: 앞으로 일어날 수 있는 갈등·분기만 적고, 확정된 결과처럼 쓰지 않는다.
- `length_ratio`: 묘사와 대사의 비중을 `묘사 N : 대사 M` 형식으로 적는다.
- `character_setting`: 이야기에 실제로 등장하는 주요 인물 **최대 5명만** 카드화하고, 그 이상은 `world_setting` 배경으로 흡수한다. 인물마다 말투·성격이 서로 구분되게 한다.
- `suggested_inputs`: 첫 입력 추천 문구 최대 3개. 행동 묘사는 `*...*`로 감쌀 수 있다.

**가독성**: 모든 서술형 값은 채팅 플레이에 그대로 노출되므로, 어려운 한자어·번역체를 피하고 쉬운 말·자연스러운 어순으로 쓴다. 한 명사 앞에 관형어를 3개 이상 쌓지 않는다. 여러 문장으로 이루어진 값은 문장마다 이중 개행(`\n\n`)으로 한 문장씩 출력한다.

**태그 귀속**: 특징 태그(주인공·주변 인물)는 형용사를 그대로 옮기지 말고, 그 특징이 드러나는 구체적 행동·습관·선택·말버릇으로 풀어 쓴다. 입력에 없는 특징을 임의로 지어내지 않는다.

**출력 형식**: 코드 펜스·머리말 없이 JSON만 반환한다. 모든 값은 한국어로 쓰고 외국어를 섞지 않는다.

---

## 7. 테스트 기준

| 항목 | 기준 |
|---|---|
| 응답 형식 | 응답이 4테이블 nested 구조의 유효한 JSON인지 확인 |
| 필수 필드 | meta·story_settings·story_start_settings 슬롯이 비어 있지 않은지 확인 |
| 인물 카드 | character_setting이 1~5명이고 각 카드 5필드가 채워졌는지 확인 |
| 추천 입력 | story_suggested_inputs가 정확히 3개인지 확인 |
| genre 주입 | 노출 genre가 LLM 출력이 아니라 입력 태그로 채워졌는지 확인 |
| 통글 변환 | story_settings 4필드가 약속된 마크다운 헤더 구조로 조립됐는지 확인 |
| 부분 재호출 | 빈 블록이 있을 때 그 블록만 다시 채우고, 2회 후에도 누락이면 502인지 확인 |
| 응답 메타 | `meta`에 model·prompt_versions·provider·토큰 수·retry_count가 실리는지 확인 |
| 에러 처리 | 호출 실패·파싱 실패·스키마 검증 실패 시 502 반환 |

---

## 부록. 프롬프트 템플릿 전문

전체 내용은 `prompt/story/COMPILE-TEMPLATE.md`를 참조합니다.

서버는 `[SYSTEM]` 블록을 `system` 역할 메시지로, `[USER]` 블록의 `{{...}}` 자리표시자를 실제 입력값으로 치환한 뒤 `user` 역할 메시지로 전달합니다.
