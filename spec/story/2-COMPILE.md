---
version: 11
updated: 2026-08-26
---

# 스토리 컴파일 시스템 명세

---

## 1. 목적

사용자가 [스토리라인 생성](1-STORYLINES.md) 단계에서 고른 한 편의 스토리라인과 추가정보·장르 태그·인물 설정을, 한 편의 인터랙티브 채팅 플레이를 구동할 풍성한 **스토리 명세**로 확장하는 시스템입니다.

스토리라인은 4단계 흐름을 압축한 짧은 줄거리일 뿐이라, 그대로는 채팅 플레이의 무대·등장인물·주인공 설정으로 쓸 수 없습니다. 컴파일은 이 희소한 입력을 받아 세계관·인물 카드·주인공 프로필·연출 규칙·시작 장면으로 구체화합니다. 이 산출물(스토리 명세)은 이후 채팅에서 STORY·CHARACTER·USER 설정으로 쓰입니다.

와이어프레임상 "추가 정보 입력 → (컴파일) → 플레이 시작"의 전환 지점에 대응하며, 스토리라인 생성 다음·채팅 플레이 이전 단계입니다.

---

## 2. 시스템 구성 요소

스토리라인 생성과 동일하게 클라이언트(백엔드) ↔ AI 서버(FastAPI) ↔ LLM API로 구성됩니다. 차이는 입력이 장르·인물뿐이 아니라 **선택한 스토리라인 1편 + 추가정보 + 원본 장르 태그·인물 설정**이고, 출력이 이야기 3편이 아니라 **스토리 명세 1건**이라는 점입니다.

컴파일은 스토리라인보다 출력이 길고 구조가 복잡해(인물 카드 최대 5명 등) 더 큰 모델을 쓰고, 빈 칸을 채우기 위한 부분 재호출이 추가됩니다.

컴파일 모델과 추론 강도는 동일한 입력으로 Terra와 Luna의 여러 추론 강도를 실측해 결정했습니다. 응답 품질뿐 아니라 응답 시간과 토큰 비용을 함께 비교했고, 세 기준의 균형이 가장 나은 `gpt-5.6-terra`의 `medium`을 선택했습니다.

| 항목 | 스토리라인 생성 | 컴파일 |
|---|---|---|
| 입력 | 장르 태그 + 인물 설정 | 선택 스토리라인 + 추가정보 + 장르 태그 + 인물 설정 |
| 출력 | 이야기 3편 + 추천정보 | 스토리 명세 1건(4테이블 + 주요 사건·엔딩) |
| 모델 | `deepseek-v4-flash` | `gpt-5.6-terra` 또는 Gemini Flash(`STORY_COMPILE_MODEL` env) |
| 호출 | 단일 호출 | 본호출 + 문제 블록·인물 필드 부분 재호출(최대 2회) |

프롬프트 템플릿은 모델 공급자에 따라 나뉩니다(KNK-958).

| 공급자 | 템플릿 | `prompt_versions` 키 |
|--------|--------|---------------------|
| OpenAI(기본) | `prompt/story/COMPILE-TEMPLATE.md` | `COMPILE` |
| Google(Gemini) | `prompt/story/COMPILE-TEMPLATE-gemini.md` | `COMPILE_GEMINI` |

두 템플릿 모두 `[SYSTEM]`·`[USER]` 두 블록으로 구성되며, 같은 6개 슬롯을 씁니다. 서버는 `STORY_COMPILE_MODEL` 환경변수로 정해진 모델의 공급자를 보고 템플릿을 선택합니다. refill·에러 캡처·응답 meta도 같은 공급자 기준을 따릅니다.

---

## 3. 시스템 흐름과 각 단계의 이유

```
[1]  백엔드   →  선택 스토리라인+추가정보+장르·인물 (POST /api/v1/story/compile)  →  AI 서버
[2]  AI 서버  →  자리표시자 치환으로 완성된 프롬프트 생성
[3]  AI 서버  →  LLM 호출  →  세분 JSON(StorySpec 구조) 수신
[4]  AI 서버  →  meta.genre를 입력 태그로, 주인공 이름·성별을 입력값으로 덮어쓰기
[5]  AI 서버  →  빈 필수 필드·입력 인물 카드 누락·인물 이름 중복·외형 누락 탐지
[6]  AI 서버  →  문제 블록과 인물 이름·외형 필드를 한 번에 부분 재호출 — 최대 2회
[7]  AI 서버  →  StorySpec(Pydantic) 파싱
[8]  AI 서버  →  세분 명세를 ERD 4테이블 통글 마크다운으로 변환
[9]  AI 서버  →  인물 카드의 외형 필드로 인물별 이미지 병렬 생성(실패해도 계속)
[10] AI 서버  →  스토리 명세 + 인물별 이미지(base64) + 로깅 메타 반환  →  백엔드
```

**[3] 세분 JSON으로 받는 이유**: LLM에게 최종 형태(통글 마크다운)로 바로 답하게 하면, 어느 부분이 비었는지 탐지하거나 그 부분만 다시 채우기가 어렵습니다. 그래서 LLM은 필드가 잘게 나뉜 세분 JSON으로 답하고, 서버가 그 결과를 검증·보정한 뒤 최종 형태로 재구성합니다.

**[4] genre·주인공을 덮어쓰는 이유**: 사용자가 정한 값은 LLM 출력에 맡기지 않고 코드가 담보합니다. LLM이 임의로 바꾸거나 누락할 수 있기 때문입니다. 장르는 입력 태그가 정본이라 `meta.genre`를 덮어쓰고, 같은 원칙으로 주인공 이름·성별도 입력값이 있으면 `user_role_setting`에 덮어씁니다(KNK-838). 비운 항목은 LLM이 지은 값을 그대로 둡니다.

**[5]~[6] 빈 필드 검증·부분 재호출이 필요한 이유**: 스토리 명세는 채팅 플레이에 그대로 쓰이므로 필수 슬롯이 비면 안 됩니다. 그런데 Pydantic 검증은 빈 문자열(`""`)을 통과시키고, 파싱이 먼저 실패하면 다시 채울 기회가 사라집니다. 따라서 파싱 전 dict 단계에서 빈 필수 필드를 직접 찾습니다. 성격·사건처럼 서로 연결된 내용은 해당 블록을 통째로 다시 받고, 인물 이름과 이미지용 외형은 잘 나온 카드 내용을 보존하도록 문제 필드만 다시 받습니다. 같은 차수에 두 종류의 문제가 있으면 한 번의 재호출로 함께 고칩니다.

**[8] 통글 마크다운으로 변환하는 이유**: 백엔드의 ERD 4테이블 중 `story_settings`는 사람이 읽기 좋고 채팅 AI가 바로 슬롯에 끼울 수 있는 통글 마크다운으로 저장합니다. 검증에 유리한 세분 구조와 저장·활용에 유리한 통글 구조가 다르므로, 서버가 마지막에 세분 명세를 통글로 재조립합니다.

**[9] 인물별 이미지 생성(KNK-414)**: 컴파일이 성공하면 인물 카드의 외형 필드(age·body·face·hair·outfit·visual_identity)로 이미지 프롬프트를 조립하고, 인물별 이미지를 병렬 생성합니다. 이미지 생성은 컴파일의 부가물이라, 한 인물이 실패해도 나머지 인물과 스토리 명세에 영향을 주지 않습니다. 채팅 플레이에서 인물이 말할 때 해당 인물의 이미지를 보여주기 위해 컴파일 시점에 한 번 만들어 둡니다. 이미지를 S3에 직접 올리지 않고 base64로 응답에 실어 보내는 이유는, AI 서버가 저장소를 모르는 stateless 구조를 유지하기 위해서입니다.

**엔딩·주요 사건(KNK-417, KNK-465 이름 기반 재작업)**: 컴파일은 세계관·인물과 함께 주요 사건 3~5개와 엔딩 3개를 **한 번의 호출로** 생성합니다. 사건은 이야기의 갈림길이고, 엔딩은 그 사건들의 조합·해결 방식에 뿌리내려 성취 스펙트럼(온전한 성공 / 그 사이 전부 / 파멸)으로 결말 상태를 빈틈없이 덮습니다(상호배타+총망라). 성취 유형(해피·노말·배드)은 생성용 **내부 기준일 뿐 출력하지 않고**, 엔딩은 `name`으로 식별합니다. 엔딩은 정상 3개이되 재호출로도 못 채우면 빈 배열로 폴백합니다(→ 4-4). 이 둘은 `story_settings`처럼 통글로 뭉치지 않고 항목별 이산 필드 그대로 백엔드에 전달됩니다(→ 5-1).

---

## 4. AI 서버 요구사항

### 4-1. 프롬프트 완성

`[USER]` 블록의 자리표시자를 입력값으로 치환합니다. 두 블록은 OpenAI 호환 `messages` 배열에 `system`·`user` 역할로 전달합니다(스토리라인과 동일).

| 자리표시자 | 치환 대상 |
|---|---|
| `{{선택_스토리라인}}` | selected_storyline 문자열 |
| `{{추가정보}}` | additional_info 문자열 (비어 있으면 `(없음)`) |
| `{{장르_태그}}` | genre_tags 배열 → 쉼표 구분 문자열 |
| `{{주인공}}` | protagonist 세트 → 한 줄 인물 표기 |
| `{{주변_인물}}` | supporting_characters 배열 → 번호 붙인 인물 목록 |
| `{{로어북}}` | lorebooks 배열 → 항목별 `### name` + content 블록(`\n\n`으로 구분). 비어 있거나 미전달·null이면 `(없음)`. 세계관·용어 확장 재료로만 쓰고 원문을 출력에 노출하지 않음 |

인물 세트의 표기 규칙(비운 항목은 `(미정)`, 성별은 한국어, 0명이면 대체 문구)과 단일 패스 치환은 스토리라인과 같습니다(`1-STORYLINES.md §4-1`).

컴파일은 `response_format={"type": "json_object"}`, `max_completion_tokens=16384`, `reasoning_effort="medium"`, 90초 타임아웃으로 호출합니다. `gpt-5.6-terra`는 temperature를 받지 않으므로 temperature 인자를 보내지 않습니다. 출력 한도 16,384토큰은 추론 토큰과 실제 JSON 본문이 함께 사용합니다.

### 4-2. 세분 JSON 응답과 파싱

LLM 응답은 텍스트이므로 JSON으로 파싱합니다. 코드 펜스가 있으면 제거하고, 빈 응답이거나 객체(dict)가 아니면 유효하지 않은 응답으로 간주합니다(스토리라인 파싱과 동일).

LLM이 답하는 JSON은 최종 출력 형태가 아니라, 검증·재호출에 유리하도록 필드가 잘게 나뉜 **세분 스키마(`StorySpec`)**입니다(→ 5-2).

### 4-3. genre·주인공 주입

파싱한 dict의 `meta.genre`를, LLM 출력 대신 입력 `genre_tags`를 쉼표로 이은 문자열로 덮어씁니다.

같은 원칙으로 주인공 이름·성별도 덮어씁니다(KNK-838). 입력 `protagonist.name`이 있으면 `user_role_setting.name`에, `protagonist.gender`가 있으면 `user_role_setting.gender`에 한국어(`남성`·`여성`)로 씁니다. 비운 항목은 LLM이 지은 값을 그대로 둡니다. 주인공 프로필 블록이 통째로 없거나 객체가 아니면 주입을 건너뜁니다 — 그 블록은 부분 재호출이 채우고, 재호출 뒤 주입이 다시 실행됩니다.

주입은 본호출과 각 재호출 직후에 적용합니다. 재호출이 블록을 통째로 갈아끼우므로, 그때 다시 덮어쓰지 않으면 재호출이 데려온 LLM 값이 입력값을 되덮습니다.

### 4-4. 빈 필드 검증과 부분 재호출

파싱 전 dict 단계에서 빈 필수 필드를 직접 탐지합니다(빈 문자열·빈 배열·null·공백만을 빈 값으로 간주).

- 비어 있는 필드가 있으면, 그 필드가 속한 **블록만** 다시 채우도록 부분 재호출합니다. 재호출 프롬프트에는 직전 생성 결과를 맥락으로 주고, 빈 블록만 채워 그 블록만 최상위 키로 갖는 JSON을 돌려받습니다. 잘 나온 다른 블록은 보존하기 위해 응답에 포함하지 않게 합니다.
- 인물 카드 목록이 정상이라면 빈 이름·공백 이름·중복 이름과 빈 외형 6필드(age·body·face·hair·outfit·visual_identity)는 카드 전체가 아니라 해당 필드만 `character_updates`로 다시 받습니다. 서버는 요청한 인물 index와 필드만 병합하며, LLM이 함께 보낸 다른 필드는 무시합니다. 인물 카드 블록 자체를 다시 받는 차수에는 기존 index가 무효가 되므로 `character_updates`를 함께 요청하지 않습니다.
- 블록 문제와 인물 필드 문제가 동시에 있으면 한 번의 재호출 응답에 모두 담아 고칩니다. 두 문제를 합쳐 **최대 2회**까지 재호출합니다.
- `null`·빈 문자열·공백이나 개행만 있는 문자열을 모두 빈 필드로 판정합니다. 이름이 2회 후에도 비었거나 중복이면 502로 막습니다. 외형은 이미지 생성의 부가 입력이므로 2회 후에도 비어 있으면 컴파일은 성공시키고 해당 인물 이미지만 `appearance_missing`으로 처리합니다.
- 검증에서 제외하는 예외 필드: `meta.genre`(서버가 입력 태그로 덮어씀), `user_role_setting.preference`(선택 입력이라 비어 있어도 됨). 주인공 `name`·`gender`는 검증 대상이되, 입력값이 있으면 주입이 먼저 채우므로 재호출로 이어지지 않습니다.
- **입력 인물 카드 누락도 재호출 대상입니다**(KNK-833). 사용자가 이름을 지은 주변 인물이 인물 카드에 없으면 `character_setting` 블록을 부분 재호출로 다시 받습니다. 스토리라인처럼 전체를 다시 부르지 않는 이유는 컴파일이 가장 비싼 호출이라, 잘 나온 나머지 블록을 보존하는 쪽이 싸기 때문입니다. 카드 이름에 호칭이 붙을 수 있어(`서린 아씨`) 포함 여부로 판정하며, 이름을 비운 인물은 검증 대상이 아닙니다.
- 엔딩은 **soft 블록**입니다(KNK-465). 정상 3개를 목표로, 3개가 아니거나 항목 필드(name·achievement_condition·epilogue)가 비었거나 min_turns가 1 이상의 정수가 아니면(0·음수 포함) 다른 빈 블록과 동일하게 `endings` 블록을 부분 재호출로 채웁니다. 다만 재호출 2회 후에도 온전한 3개를 못 채우면 502가 아니라 **빈 배열(`[]`)로 폴백하고 200을 반환**합니다 — 스토리 본체·주요 사건은 살리고 부가물인 엔딩만 비웁니다(선택지 폴백과 같은 원칙). 엔딩은 성취 유형(해피·노말·배드)을 출력하지 않으며 `name`으로 식별합니다.

필수 필드 점검 대상: `meta`(title·one_line_intro·description), `prompt_settings`(world_setting·rule_setting·tone_setting·length_ratio, plot_setting의 premise·conflict, character_setting 1개 이상과 각 카드의 6개 필드(name·gender·personality·tone·motivation·attitude_to_user), user_role_setting의 preference 제외 5개 필드(name·gender·role·background·personality)), `start`(name·prologue·start_situation), `suggested_inputs`(정확히 3개이며 각 항목이 비어 있지 않음), `main_events`(3~5개이며 각 항목의 name·description·key_sentence가 비어 있지 않음), `endings`(정상 3개이며 각 항목의 name·achievement_condition·epilogue가 비어 있지 않고 min_turns가 1 이상의 정수 — 단 재호출로도 못 채우면 빈 배열로 폴백).

### 4-5. 세분 → 통글 변환

검증을 통과한 세분 명세를 ERD 4테이블에 1:1로 대응하는 nested 형태로 재구성합니다. `story_settings`의 4개 필드는 사람이 읽기 좋은 통글 마크다운으로 조립합니다(→ 5-3). 별도 템플릿 파일 없이 서버 코드가 조립합니다.

### 4-6. 에러 처리

| 상황 | 요구 동작 |
|---|---|
| LLM API 호출 실패(타임아웃·rate limit·요청 거부·연동 오류) | 게이트웨이 오류(502) 반환 |
| 빈 응답이거나 유효하지 않은 JSON | 파싱 실패로 간주하고 502 반환 |
| 재호출 2회 후에도 필수 필드가 비거나 입력 인물 카드가 누락되거나 인물 이름이 비었거나 중복됨(엔딩 제외) | 502 반환 |
| 재호출 2회 후에도 인물 외형 필드가 비어 있음 | 컴파일은 200 반환, 해당 인물 이미지는 `appearance_missing` |
| 재호출 2회 후에도 엔딩이 온전한 3개가 아님 | 빈 배열로 폴백하고 200 반환(502 아님) |
| 세분 명세가 `StorySpec` 스키마와 맞지 않음 | 스키마 검증 실패로 502 반환 |

502 응답 본문에는 사용자에게 보일 안내 메시지만 담고, 공급자 원문 오류는 Sentry로만 보냅니다(KNK-262).

### 4-7. 인물별 이미지 생성 (KNK-414)

컴파일 성공 후 인물 카드의 외형 6필드(age·body·face·hair·outfit·visual_identity)로 이미지 프롬프트를 조립하고 인물별 이미지를 병렬 생성합니다.

- **이미지 모델**: `gpt-image-2-2026-04-21`(스냅샷 고정). 이미지 모델은 텍스트 LLM과 같은 3층 구조(호출부 → 모델 특성 → SDK 어댑터)를 따릅니다.
- **출력 형식**: WebP(`output_format="webp"`). PNG 대비 파일 크기가 작아 base64 응답 전송에 유리합니다.
- **크기**: 1024×768(가로 4:3, `IMAGE_SIZE` env).
- **화질**: low(`IMAGE_QUALITY` env). 나중에 변경할 수 있도록 config 설정으로 분리했습니다.
- **동시 실행**: 최대 5명 동시(`asyncio.Semaphore(5)`). 주변 인물 최대 5명이 한 묶음에 돌아갑니다.
- **실패 격리**: 한 인물의 이미지 생성 실패가 다른 인물이나 컴파일 전체를 중단하지 않습니다. 실패한 인물은 응답에 에러 코드만 실립니다.
- **외형 필드 부족**: 컴파일 부분 재호출 2회 후에도 외형 6필드 중 하나라도 비어 있으면 해당 인물의 이미지 프롬프트를 조립할 수 없어 건너뜁니다.
- **에러 코드**: 응답에는 공급자 원문 대신 분류된 코드만 내려보냅니다(`timeout`, `rate_limited`, `rejected`, `appearance_missing`, `generation_failed`). 원문은 로그에만 남깁니다.
- **프롬프트 템플릿**: `prompt/image/CHARACTER-IMAGE-TEMPLATE.md`. 장르와 외형 필드를 XML 태그로 끼워 넣습니다. `visual_identity`를 `hair` 앞에 배치해 이미지 모델이 머리색을 먼저 인식하게 합니다.

### 4-8. 프롬프트 캐싱

Terra 호출에는 매번 동일한 `[SYSTEM]` 블록을 보내며 서버가 별도 캐시 옵션을 지정하지는 않습니다. OpenAI 응답의 `usage.prompt_tokens_details.cached_tokens`가 있으면 어댑터가 `cache_read_input_tokens`로 옮기고, 스토리 호출 진단 로그에 캐시 적중 토큰 수를 남깁니다.

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
  "genre_tags": ["무협", "회귀"],
  "protagonist": { "name": "서린", "gender": "FEMALE", "features": ["냉혹한", "치밀한"] },
  "supporting_characters": [
    { "name": "강우", "gender": "MALE", "features": ["배신자"] }
  ],
  "lorebooks": [{ "name": "내공", "content": "기를 단전에 쌓아 다스리는 힘." }]
}
```

| 필드 | 타입 | 제약 |
|---|---|---|
| selected_storyline | string | 필수 |
| additional_info | string | 선택(기본값 빈 문자열) |
| genre_tags | string[] | 필수 |
| protagonist | object | 필수. 인물 세트 하나(`{name, gender, features[]}`, 내용은 전부 선택) |
| supporting_characters | object[] | 선택. 미전달·빈 배열·null이면 0명. 제약은 스토리라인 요청과 동일(`1-STORYLINES.md §5-2`) |
| lorebooks | object[] | 선택(기본값 빈 배열, null 허용) — 각 항목 `{name, content}`. 세계관·용어 확장 재료로만 쓰고 원문을 출력에 노출하지 않음 |

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
    "character_setting": "# 등장인물\n\n## 이름\n### 성별\n...\n### 성격\n...",
    "user_role_setting": "# 주인공\n## 호칭\n...\n## 성별\n...",
    "rule_setting": "# 전개 규칙\n...\n\n# 문체 톤\n...\n\n# 분량 배분\n묘사 7 : 대사 3"
  },
  "story_start_settings": {
    "name": "...",
    "start_situation": "...",
    "prologue": "..."
  },
  "story_suggested_inputs": ["...", "...", "..."],
  "story_main_events": [
    { "name": "...", "description": "...", "key_sentence": "..." }
  ],
  "story_endings": [
    { "name": "...", "min_turns": 15, "achievement_condition": "...", "epilogue": "..." },
    { "name": "...", "min_turns": 15, "achievement_condition": "...", "epilogue": "..." },
    { "name": "...", "min_turns": 15, "achievement_condition": "...", "epilogue": "..." }
  ],
  "character_appearances": [
    {
      "name": "레이",
      "gender": "남성",
      "age": "20대 후반",
      "body": "건장한 체격",
      "face": "각진 턱선",
      "hair": "짧은 검은 머리",
      "outfit": "은색 판금 흉갑",
      "visual_identity": "왼쪽 관자놀이의 칼자국"
    }
  ],
  "character_images": [
    { "name": "레이", "image_base64": "UklGR...", "content_type": "image/webp", "error": null },
    { "name": "세린", "image_base64": null, "content_type": "image/webp", "error": "timeout" }
  ],
  "meta": {
    "model": "gpt-5.6-terra",
    "prompt_versions": { "COMPILE": 10, "CHARACTER_IMAGE": 1 },
    "provider": "openai",
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
| story_main_events | object[] | 주요 사건 3~5개(`story_main_events` 테이블). 각 항목 name·description·key_sentence. 배열 순서=명목 순서(비강제) |
| story_endings | object[] | 엔딩(`story_endings` 테이블). 정상 3개(폴백 시 0개). 각 항목 name·min_turns(1 이상 정수)·achievement_condition·epilogue. 성취 유형은 미출력, name으로 식별 |
| character_appearances | object[] | 인물별 외형 정보. 각 항목 name·gender·age·body·face·hair·outfit·visual_identity. 인물 전원이 포함되며, 백엔드가 저장해 이미지 재생성에 사용 |
| character_images | object[] | 인물별 이미지(KNK-414). 각 항목 name·image_base64(성공 시 WebP base64, 실패 시 null)·content_type(`"image/webp"`)·error(실패 시 사유 코드, 성공 시 null). 인물별로 성공/실패가 독립. 빈 배열은 인물 0명이거나 이미지 로직 자체가 실패한 경우 |
| meta | object | 응답 로깅 메타(`ai_call_logs` 적재용, KNK-243) |
| meta.retry_count | number | 부분 재호출 횟수(0~2) |

**백엔드 저장 안내(KNK-465)**: `story_endings`는 엔딩 4필드(name·min_turns·achievement_condition·epilogue)를 담을 칸으로, `story_main_events`는 name·description·key_sentence + 배열 순서를 담을 순서 칸으로 저장합니다(상위 정본 `5-ai-server.md §5-3-3`과 일치). 엔딩은 정상 3개이되 폴백 시 0개가 올 수 있습니다. 두 목록은 통글로 뭉치지 않고 항목별 이산 필드 그대로 내려가므로 칸별로 저장하면 됩니다. 사건의 배열 순서는 명목 순서일 뿐 전개를 강제하지 않습니다(건너뛰기 허용).

`meta`의 나머지 필드(model·prompt_versions·provider·input_token_count·output_token_count)는 스토리라인과 동일합니다. `prompt_versions`에는 컴파일 템플릿(`COMPILE` 또는 `COMPILE_GEMINI`)과 이미지 템플릿(`CHARACTER_IMAGE`) 버전이 함께 들어갑니다. 토큰 수는 본호출과 재호출을 **합산**하며, model은 본호출 응답값을 씁니다.

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
      { "name": "...", "gender": "...", "personality": "...", "tone": "...", "motivation": "...", "attitude_to_user": "...", "age": "...", "body": "...", "face": "...", "hair": "...", "outfit": "...", "visual_identity": "..." }
    ],
    "user_role_setting": { "name": "...", "gender": "...", "role": "...", "background": "...", "personality": "...", "preference": "" }
  },
  "start": { "name": "...", "prologue": "...", "start_situation": "..." },
  "suggested_inputs": ["...", "...", "..."],
  "main_events": [
    { "name": "...", "description": "...", "key_sentence": "..." }
  ],
  "endings": [
    { "name": "...", "min_turns": 15, "achievement_condition": "...", "epilogue": "..." },
    { "name": "...", "min_turns": 15, "achievement_condition": "...", "epilogue": "..." },
    { "name": "...", "min_turns": 15, "achievement_condition": "...", "epilogue": "..." }
  ]
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
| | character_setting | 주변 인물 카드 1~5명. 각 카드는 name·gender·personality·tone·motivation·attitude_to_user + 외형 6필드(age·body·face·hair·outfit·visual_identity). 입력한 주변 인물은 전원 카드가 되고, 남는 자리만 LLM이 채움. 외형 필드는 이미지 생성 전용이며 통글에는 싣지 않음(KNK-937). 선택 필드라 비어 있어도 컴파일은 성공하고 해당 인물의 이미지만 안 만들어짐 |
| | user_role_setting | 주인공 프로필. name·gender·role·background·personality·preference(선택). name·gender는 입력값이 있으면 서버가 덮어씀 |
| start | name·prologue·start_situation | 시작 설정 이름·도입 나레이션·첫 장면 |
| suggested_inputs | string[] | 첫 입력 추천 문구 3개 |
| main_events | object[] | 주요 사건 3~5개. 각 name·description·key_sentence. 이야기의 갈림길이자 엔딩을 가르는 축 |
| endings | object[] | 엔딩 정상 3개(폴백 시 0개). 각 name·min_turns(1 이상 정수)·achievement_condition·epilogue. 성취 유형(해피·노말·배드)은 생성용 내부 기준일 뿐 미출력 |

### 5-3. 세분 → 통글 변환 규칙

세분 명세의 필드를 ERD 4테이블로 재구성합니다.

| ERD 테이블 | 출력 필드 | 세분 명세 출처 |
|---|---|---|
| stories | stories | meta(genre 제외) |
| story_settings | story_settings(통글 4필드) | prompt_settings 7필드를 4통글로 재구성 |
| story_start_settings | story_start_settings | start |
| story_suggested_inputs | story_suggested_inputs | suggested_inputs |
| story_main_events | story_main_events | main_events(항목별 그대로, 통글 아님) |
| story_endings | story_endings | endings(항목별 그대로, 통글 아님) |

`story_settings` 4개 통글 필드의 구성과 마크다운 구조는 다음과 같습니다.

| 통글 필드 | 구성(세분 출처) | 마크다운 구조 |
|---|---|---|
| world_setting | world_setting + plot_setting | `# 세계관` / `# 전제` / `# 갈등` |
| character_setting | character_setting[] | `# 등장인물` + 인물마다 `## 이름` / `### 성별`·`### 성격`·`### 말투`·`### 동기`·`### 주인공을 대하는 태도` |
| user_role_setting | user_role_setting | `# 주인공` / `## 호칭`·`## 성별`·`## 역할`·`## 배경`·`## 성격`·`## 입력 선호` |
| rule_setting | rule_setting + tone_setting + length_ratio | `# 전개 규칙` / `# 문체 톤` / `# 분량 배분` |

---

## 6. 프롬프트 요구사항

전문은 `prompt/story/COMPILE-TEMPLATE.md`(OpenAI)와 `prompt/story/COMPILE-TEMPLATE-gemini.md`(Gemini)이며, 핵심 요구사항은 다음과 같습니다.

**역할 매핑**: 스토리라인의 구성 요소를 올바른 대상에 귀속시킵니다.

| 구성 요소 | 귀속 대상 |
|---|---|
| 주인공(사용자가 1인칭으로 연기) | `user_role_setting` (절대 `character_setting`에 넣지 않음) |
| 주변 인물(주인공이 아닌 등장인물) | `character_setting` (AI가 연기할 NPC) |
| 세계관·전개·분위기 | `world_setting` / `plot_setting` / `rule_setting` / `tone_setting` / `length_ratio` |

**필드별 작성 규칙**(요지):

- `user_role_setting`: 주인공 프로필. `name`은 입력 이름이 있으면 그대로 쓰고 없으면 추가정보를 반영한 자연스러운 호칭을 짓는다(최종 값은 입력이 있을 때 서버가 덮어씀). `gender`도 `남성`·`여성`으로만 쓴다.

- `meta.title`: 기존 웹소설처럼 자극적으로, 가장 센 한 방을 앞세워 짓는다(설명조 금지). 한 문장·공백 포함 25자 이내 권장.
- `plot_setting.conflict`: 앞으로 일어날 수 있는 갈등·분기만 적고, 확정된 결과처럼 쓰지 않는다.
- `length_ratio`: 묘사와 대사의 비중을 `묘사 N : 대사 M` 형식으로 적는다.
- `character_setting`: **입력의 주변 인물은 빠짐없이 전원 카드로 만든다** — 이름은 준 그대로 쓰고, 성별·특징이 정해져 있으면 카드에 반영한다. 카드는 **최대 5명**이고, 입력 인물로 5명이 안 차면 이야기에 필요한 인물로 남는 자리를 채워도 되며 그 밖의 단역은 `world_setting` 배경으로 흡수한다. `gender`는 `남성`·`여성`으로만 쓴다. 인물마다 말투·성격이 서로 구분되게 한다.
- `suggested_inputs`: 첫 입력 추천 문구 **정확히 3개**. 행동 묘사는 `*...*`로 감쌀 수 있다.
- `main_events`: 주요 사건 3~5개(name·description·key_sentence). 이야기의 갈림길로 짜되 기본 순서만 두고 건너뛰기를 허용한다. `key_sentence`는 "사용자가 ~한다" 사용자 시점의 유도 문장으로, 사용자가 자연스럽게 떠올려 입력할 만하게 직관적으로 쓴다.
- `endings`: 엔딩 3개. 성취 유형(해피·노말·배드)을 **내부 기준으로만** 삼아 하나씩 만들되 **유형은 출력하지 않고 `name`으로 식별**한다. 사건들의 조합·해결에 뿌리내리게 하되, 성취 스펙트럼(온전한 성공 / 그 사이 전부 / 파멸)으로 나눠 결말 상태를 빈틈없이 덮는다(상호배타+총망라, 노말이 중간대 흡수). 조건은 `min_turns`(최소 턴, 정수)와 `achievement_condition`(목적·거친 사건을 한 문장에 담되 특정 사건 경유 비강제)로 나누고, `epilogue`엔 완성 글이 아니라 방향을 담되 "사용자의 행적을 반드시 반영해 그 행동이 세계를 바꾼 결과로 마무리하라"는 지시를 포함한다.

**가독성**: 모든 서술형 값은 채팅 플레이에 그대로 노출되므로, 어려운 한자어·번역체를 피하고 쉬운 말·자연스러운 어순으로 쓴다. 한 명사 앞에 관형어를 3개 이상 쌓지 않는다. 여러 문장으로 이루어진 값은 문장마다 이중 개행(`\n\n`)으로 한 문장씩 출력한다.

**특징 반영**: 인물의 특징은 형용사를 그대로 옮기지 말고, 그 특징이 드러나는 구체적 행동·습관·선택·말버릇으로 풀어 쓴다. 입력에 없는 특징을 임의로 지어내지 않는다.

**출력 형식**: 코드 펜스·머리말 없이 JSON만 반환한다. 모든 값은 한국어로 쓰고 외국어를 섞지 않되, 입력 인물 이름은 예외로 외국어여도 그대로 쓴다.

---

## 7. 테스트 기준

| 항목 | 기준 |
|---|---|
| 응답 형식 | 응답이 4테이블 nested 구조의 유효한 JSON인지 확인 |
| 필수 필드 | meta·story_settings·story_start_settings 슬롯이 비어 있지 않은지 확인 |
| 인물 카드 | character_setting이 1~5명이고 각 카드 6필드가 채워졌는지 확인 |
| 입력 인물 등장 | 이름 지은 주변 인물이 카드에 있는지, 없으면 카드 블록만 재호출하는지 확인 |
| 추천 입력 | story_suggested_inputs가 정확히 3개인지 확인 |
| genre 주입 | 노출 genre가 LLM 출력이 아니라 입력 태그로 채워졌는지 확인 |
| 주인공 주입 | 입력한 주인공 이름·성별이 통글의 최종 값인지, 비운 항목은 LLM 값이 남는지, 재호출 뒤에도 유지되는지 확인 |
| 통글 변환 | story_settings 4필드가 약속된 마크다운 헤더 구조로 조립됐는지 확인 |
| 부분 재호출 | 문제 블록과 인물 이름·외형 필드를 한 호출에 함께 요청하고, 요청한 값만 병합하는지 확인 |
| 인물 이름 | 빈값·공백·중복 이름만 다시 받고, 2회 후에도 해결되지 않으면 502인지 확인 |
| 외형 부분 재호출 | null·빈 문자열·공백뿐인 외형 필드만 다시 받고, 다른 카드 내용은 보존하는지 확인 |
| 주요 사건 | story_main_events가 3~5개이고 각 항목 name·description·key_sentence가 채워졌는지 확인 |
| 엔딩 | story_endings가 3개이고 각 항목 name·min_turns·achievement_condition·epilogue가 채워졌는지 확인 |
| 엔딩 폴백 | 재호출 후에도 온전한 3개를 못 채우면 502가 아니라 빈 배열(`[]`)로 200을 반환하는지 확인 |
| 엔딩 개수 | endings가 0개(폴백) 또는 3개가 아니면(2·4개 등) StorySpec 파싱에서 거부되는지 확인 |
| 응답 메타 | `meta`에 model·prompt_versions·provider·토큰 수·retry_count가 실리는지 확인 |
| 에러 처리 | 호출 실패·파싱 실패·스키마 검증 실패 시 502 반환 |
| 인물 이미지 | character_images가 인물 수만큼 반환되고, 성공한 인물은 image_base64와 content_type이 채워졌는지 확인 |
| 이미지 실패 격리 | 한 인물의 이미지 생성이 실패해도 나머지 인물과 컴파일 전체가 200으로 성공하는지 확인 |
| 이미지 에러 코드 | 실패한 인물의 error에 공급자 원문이 아닌 분류된 코드(timeout·rejected 등)가 실리는지 확인 |
| 외형 필드 부족 | 부분 재호출 2회 후에도 외형이 비어 있으면 컴파일은 성공하고 해당 인물 이미지만 건너뛰는지 확인 |

---

## 부록. 프롬프트 템플릿 전문

전체 내용은 `prompt/story/COMPILE-TEMPLATE.md`(OpenAI용)와 `prompt/story/COMPILE-TEMPLATE-gemini.md`(Gemini용)를 참조합니다.

서버는 `[SYSTEM]` 블록을 `system` 역할 메시지로, `[USER]` 블록의 `{{...}}` 자리표시자를 실제 입력값으로 치환한 뒤 `user` 역할 메시지로 전달합니다. Gemini 템플릿은 같은 슬롯·스키마를 따르되, Gemini 프롬프팅 특성에 맞게 지시 방식을 조정했습니다(KNK-958).
