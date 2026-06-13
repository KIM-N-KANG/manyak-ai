---
name: notion-version-control
description: |
  마냑(Manyak) 프로젝트의 story·chat 프롬프트(prompt/)와 명세(reference/) 파일 변경을
  감지해 Notion "AI·SW MAESTRO" 워크스페이스 "prompt/spec-version-control" 하위
  파일별 전용 데이터베이스에 버전 스냅샷을 저장하는 스킬.

  다음 상황에서 반드시 사용하라:
  - "버전 저장", "노션에 저장", "프롬프트 버전 관리", "spec 버전 컨트롤" 등의 요청
  - "prompt 변경됐는데 노션에 올려줘", "이번 프롬프트 변경 기록해줘" 등의 요청
  - prompt/ 또는 reference/ 파일을 수정한 후 변경 이력을 남기고 싶을 때
  - story 또는 chat 명세·프롬프트의 변경 내역을 추적하고 싶을 때
  - Notion 버전 히스토리, 명세 백업, 프롬프트 이력 관련 모든 요청
---

# Notion 프롬프트·명세 버전 컨트롤

## 개요
마냑 AI 서버의 story·chat 프롬프트와 명세를 Notion 파일별 전용 데이터베이스에
버전 스냅샷으로 저장해 변경 이력을 체계적으로 추적한다.

각 파일은 독립된 데이터베이스를 가지며, 새 버전 저장 시 기존 `active` 항목을
`archived`로 전환하고 새 행을 `active`로 추가한다.

프로젝트 루트: `c:\Users\dohyeong0423\Desktop\asm\Project\AI`

---

## Notion 구조

```
prompt/spec-version-control
│  (https://app.notion.com/p/37dc7821f72c8039ad1fc9c42cd5750a)
│
├── story
│   └── Story Prompt Versions (DB)
│
├── chat
│   ├── SAFETY    → SAFETY Versions (DB)
│   ├── CORE      → CORE Versions (DB)
│   ├── STORY     → STORY Versions (DB)
│   ├── CHARACTER → CHARACTER Versions (DB)
│   ├── USER      → USER Versions (DB)
│   └── MEMORY    → MEMORY Versions (DB)
│
├── story-spec
│   └── Story Spec Versions (DB)
│
└── chat-spec
    ├── SAFETY    → SAFETY Versions (DB)
    ├── CORE      → CORE Versions (DB)
    ├── STORY     → STORY Versions (DB)
    ├── CHARACTER → CHARACTER Versions (DB)
    ├── USER      → USER Versions (DB)
    └── MEMORY    → MEMORY Versions (DB)
```

---

## 파일 → Notion 데이터베이스 매핑

| 파일 경로 | Notion 위치 | 데이터베이스 | Name 형식 |
|-----------|------------|-------------|----------|
| `prompt/story/STORY-PROMPT-TEMPLATE.md` | story | Story Prompt Versions | `STORY-PROMPT-TEMPLATE-v{N}` |
| `prompt/chat/SAFETY-PROMPT.md` | chat/SAFETY | SAFETY Versions | `SAFETY-v{N}` |
| `prompt/chat/CORE-PROMPT.md` | chat/CORE | CORE Versions | `CORE-v{N}` |
| `prompt/chat/STORY-PROMPT.md` | chat/STORY | STORY Versions | `STORY-v{N}` |
| `prompt/chat/CHARACTER-PROMPT.md` | chat/CHARACTER | CHARACTER Versions | `CHARACTER-v{N}` |
| `prompt/chat/USER-PROMPT.md` | chat/USER | USER Versions | `USER-v{N}` |
| `prompt/chat/MEMORY-PROMPT.md` | chat/MEMORY | MEMORY Versions | `MEMORY-v{N}` |
| `reference/story/1-BACKGROUND.md` | story-spec | Story Spec Versions | `1-BACKGROUND-v{N}` |
| `reference/story/2-RESULT-TEST.md` | story-spec | Story Spec Versions | `2-RESULT-TEST-v{N}` |
| `reference/chat/1-PROMPT-LAYER.md` | chat-spec | *(담당 DB 확인 필요)* | `1-PROMPT-LAYER-v{N}` |
| `reference/chat/2-LAYER-PLACEMENT.md` | chat-spec | *(담당 DB 확인 필요)* | `2-LAYER-PLACEMENT-v{N}` |
| `reference/chat/3-CONTEXT-ARCHITECTURE.md` | chat-spec | *(담당 DB 확인 필요)* | `3-CONTEXT-ARCHITECTURE-v{N}` |

---

## 데이터베이스 스키마

### 공통 스키마 (모든 Versions DB)

| 속성명 | 타입 | 설명 |
|--------|------|------|
| Name | title | 버전 식별자 (예: `SAFETY-v1`) |
| Content | text | 파일 전체 내용 |
| Status | select | `active` (현재 운영 중) / `archived` (교체된 버전) |
| Changelog | text | 이전 버전 대비 변경 사항 |
| Created | created_time | 자동 생성 |

### Story Spec Versions 추가 속성

| 속성명 | 타입 | 선택지 |
|--------|------|--------|
| Spec Type | select | `STORYLINE-SPEC` / `QUESTION-SPEC` / `OUTPUT-FORMAT-SPEC` / `CHARACTER-SPEC` |

---

## 실행 순서

### 1단계: 변경 파일 감지

git으로 대상 파일의 변경 여부를 확인한다.

```bash
# 미커밋 변경(staged + unstaged) 파일 목록
git status --porcelain -- prompt/ reference/

# 최근 커밋 이후 변경 파일 목록
git diff HEAD -- prompt/ reference/ --name-only
```

**변경 파일이 없으면** 사용자에게 알리고 종료한다:
> "현재 변경된 프롬프트·명세 파일이 없습니다. 파일을 수정한 뒤 다시 실행해주세요."

### 2단계: 변경 파일별 대상 데이터베이스 식별

위 매핑 테이블을 참조해 각 변경 파일이 어느 Notion 데이터베이스에 저장될지 결정한다.

`notion-search` 또는 `notion-fetch`로 해당 페이지를 열고, 인라인 데이터베이스의
`data-source-url`(collection://...)을 확인한다.

### 3단계: 다음 버전 번호 결정 (파일별)

각 데이터베이스에서 `Name` 필드를 조회해 해당 파일의 가장 높은 버전 번호를 찾는다.

- 해당 파일 항목이 없으면 → **v1**
- 가장 높은 항목이 `SAFETY-v3`이면 → **v4**

버전 번호 파싱: Name에서 `-v` 뒤의 숫자를 추출해 비교. 숫자 없는 항목은 무시한다.

### 4단계: Changelog 요청

저장할 파일 목록과 버전 번호를 보여주고 한 줄 설명을 요청한다:

> "다음 파일을 저장합니다:
> - `SAFETY-PROMPT.md` → SAFETY-v{N}
> - `CORE-PROMPT.md` → CORE-v{N}
>
> 변경 내용을 간단히 설명해주세요.  
> 예: '다양성 규칙 강화', 'MEMORY 경계 수정', 'few-shot 예시 교체'  
> (건너뛰려면 Enter 또는 '없음'이라고 답해주세요)"

### 5단계: Notion에 버전 저장 (파일별 순차 처리)

변경된 파일 각각에 대해 아래를 실행한다.

#### 5-1. 기존 active 항목 archived로 전환

해당 데이터베이스에서 `Status = active`인 항목을 찾아 `notion-update-page`로
`Status`를 `archived`로 변경한다.

#### 5-2. 새 버전 행 추가

`notion-create-pages`로 해당 데이터베이스에 새 항목을 추가한다:

| 속성 | 값 |
|------|----|
| Name | `{KEY}-v{N}` |
| Content | 파일 전체 내용 |
| Status | `active` |
| Changelog | 사용자 입력 (없으면 빈 값) |

`Story Spec Versions` DB에 저장하는 경우 `Spec Type`도 설정한다.
파일 내용과 이름을 보고 가장 적합한 타입을 선택한다.

### 6단계: 완료 보고

```
✓ 버전 저장 완료

  날짜:      {YYYY-MM-DD}
  Changelog: {메모}
  저장 항목:
    - {KEY}-v{N} → {Notion 위치} ({DB명})
    - {KEY}-v{N} → {Notion 위치} ({DB명})
```

---

## 오류 처리

| 상황 | 대응 |
|------|------|
| Notion 페이지를 찾을 수 없음 | "'prompt/spec-version-control' 페이지를 찾을 수 없습니다. 페이지 이름 또는 접근 권한을 확인해주세요." |
| Notion MCP 도구 미활성화 | "Notion MCP 연결이 필요합니다. Claude Code 설정에서 Notion MCP가 활성화되어 있는지 확인해주세요." |
| 파일 읽기 실패 | 해당 파일을 건너뛰고 나머지는 계속 저장. 건너뛴 파일 목록 사용자에게 알림 |
| git 워킹 디렉토리 아님 | "프로젝트 루트(`c:\Users\dohyeong0423\Desktop\asm\Project\AI`)에서 실행해주세요." |
| reference/chat 파일의 chat-spec DB 매핑 불명확 | 사용자에게 어느 chat-spec 하위 DB(SAFETY/CORE/STORY/CHARACTER/USER/MEMORY)에 저장할지 확인 |
