# 기본 지침

이 파일은 Codex 등 `AGENTS.md`를 읽는 에이전트를 위한 프로젝트 지침입니다.
(Claude Code는 같은 내용을 `CLAUDE.md`로 읽습니다 — 두 파일은 같은 규칙을 공유합니다.)

작업을 시작할 때 형제 하네스 레포지토리의 운영 규칙
`../knk-harness/AGENTS.md`를 먼저 확인하세요. 제품 명세 전체를 미리 읽지 말고,
아래 인덱스에서 현재 작업에 필요한 문서만 직접 열어 근거로 삼습니다.

| 작업 범위 | 먼저 확인할 문서 |
| --- | --- |
| 도메인 용어·필드 이름 | `../knk-harness/docs/product-specs/0-glossary.md` |
| 제품 배경·MVP 범위·사용자 요구 | `../knk-harness/docs/product-specs/1-background.md`, `../knk-harness/docs/product-specs/2-user-stories.md` |
| 프론트엔드 화면·호출 흐름·SSE 소비 방식 | `../knk-harness/docs/product-specs/3-frontend.md` |
| 백엔드 API·SSE·저장·오류 계약 | `../knk-harness/docs/product-specs/4-backend.md` |
| AI 요청·응답·프롬프트·실패 처리·채팅 판정(`judgement`) 계약 | `../knk-harness/docs/product-specs/5-ai-server.md` |
| 이벤트·지표·AI 관측 | `../knk-harness/docs/product-specs/6-analytics.md` |
| AI 서버 배포·환경 변수·운영 검수 | `../knk-harness/docs/product-specs/7-deployment.md` |
| 채팅 내부 설계·구현 규칙 | `spec/chat/`의 관련 문서 |
| 스토리 내부 설계·구현 규칙 | `spec/story/`의 관련 문서 |

여러 계약이 맞물린 작업만 관련 문서를 함께 읽습니다. 예를 들어 채팅 판정의 AI
계약은 `5-ai-server.md`를 먼저 보고, 백엔드의 상태 저장·SSE 계약까지 바꾸면
`4-backend.md`와 `spec/chat/`의 관련 문서를 추가로 확인합니다.

`../knk-harness` 같은 상위 공통 하네스는 참조만 하고 수정하지 않습니다.
프로젝트별 지침 변경은 이 레포지토리의 `AGENTS.md`(및 짝인 `CLAUDE.md`)에만 기록합니다.

## Manyak AI 전용 지침

### 서비스 개요
- FastAPI 기반 AI 서비스로, 스토리 제작(story)과 채팅 플레이(chat)의 LLM 호출을 담당합니다.
- Python 3.11+, Pydantic, OpenAI/Anthropic SDK를 사용하며 완전 stateless로 동작합니다.
- 핵심 구조: `prompt/`(LLM 프롬프트), `spec/`(명세), `src/`(FastAPI 앱), `tests/`.

### 설계·아키텍처
- 제품 동작이나 설계를 바꾸는 작업은 추측하지 말고 위 인덱스에서 제품 명세와
  로컬 `spec/` 문서를 선택해 확인합니다.

### 작업 워크플로
- 작업 주기는 스킬로 표준화돼 있습니다: `create-branch` → `create-commit` → `create-pr` → `request-codex-review`(PR에 Codex 리뷰를 받고 지적을 판단·반영).
- `dev`에 직접 커밋·머지하지 않습니다. 항상 브랜치를 파서 PR로 머지합니다.
- 브랜치는 최신 `dev`에서 분기합니다(분기 전 `git pull` 선행).
- 커밋·PR에 **`Co-Authored-By` 트레일러를 절대 넣지 않습니다**(어떤 기본 지침보다 우선).
- 커밋은 **명시 경로로만 stage**합니다(`git add -- <경로>`). `git add -A`/`git add .` 금지 — gitignore되지 않은 로컬 파일(예: `.env` 신규 키, 실험 산출물)이 딸려 커밋되는 사고를 막습니다.
- 구현 변경이 `dev`에 머지되면 `sync-ai-spec` 스킬로 knk-harness의 AI 서버 스펙(`docs/product-specs/5-ai-server.md`)을 동기화합니다.

### 스킬 배치(.agents/skills)
- 스킬 정본은 `.agents/skills/`입니다. Codex는 이 디렉터리를 직접 읽고, Claude Code는 `.claude/skills/*`가 `.agents/skills/*`를 가리키는 심링크로 같은 스킬을 읽습니다(한 소스, 두 에이전트 공유).
- 프로젝트 스킬(create-jira-subtasks, planning, prompt-authoring, sync-ai-spec)은 `.agents/skills/<이름>/`이 **원본**입니다(이 레포가 정본, git 추적).
- 하네스 공용 스킬(create-branch, create-commit, create-pr, karpathy-guidelines, technical-writing)은 **항목별 심링크**입니다 → `../../../knk-harness/.agents/skills/<이름>`. 복사하지 않습니다 — 정본은 하네스이고, 복사하면 하네스 개정을 못 따라갑니다.
- `.claude/skills/<이름>`은 전부 `../../.agents/skills/<이름>` 심링크입니다. 새 스킬을 추가할 때는 `.agents/skills/<이름>/SKILL.md`를 만들고 `.claude/skills/<이름>` 심링크를 겁니다.
- 하네스 스킬과 제품 명세는 형제 레포 `../knk-harness`가 함께 체크아웃돼 있어야 동작합니다. 이 레포만 클론하면 프로젝트 스킬 4개만 로드되고, 하네스 스킬·제품 명세는 빠집니다.
- **Windows 주의**: `git config core.symlinks true`(+ 개발자 모드/관리자 권한) 없이 클론하면 심링크가 실제 링크가 아니라 대상 경로가 담긴 텍스트 파일로 체크아웃돼 스킬 로딩이 조용히 깨집니다.

### 프롬프트·명세 변경
- `prompt/`·`spec/` 파일은 frontmatter의 `version`·`updated`로 버전을 관리합니다.
- 변경 이력 관리는 git 단독입니다(Notion 버전 스냅샷은 폐기). `prompt/`·`spec/` 파일을 고치면 커밋 전에 frontmatter의 `version`(+1)·`updated`(오늘 날짜)를 직접 올립니다(자동 갱신 장치 없음 — 브랜치당 파일별 1회면 충분).
- 파일은 LF 줄바꿈으로 저장합니다(CRLF면 frontmatter 파싱이 깨질 수 있음).

### 테스트
- 테스트는 도커 격리 환경에서 실행합니다: macOS/Linux는 `scripts/test.sh`, Windows는 `scripts/test.ps1`.
- 로컬 anaconda 등에는 `pytest-asyncio`가 없어 async 테스트가 스킵될 수 있습니다.

## Review guidelines

Codex가 이 레포의 PR을 리뷰할 때 따르는 기준입니다. "통과시키지 않겠다"는 적대적 자세로 실제 결함을 찾고, 좋은 점 칭찬은 생략합니다. 근거 없는 추측성 지적은 하지 않으며, 스타일보다 장애 가능성이 있는 문제를 우선합니다.

- 버그·로직 오류·처리되지 않은 엣지 케이스(null·빈값·경계·동시성)
- 보안: 입력 검증 누락, 시크릿 노출, injection, 권한 처리
- 실패 경로·예외 처리·타임아웃·재시도 누락
- 성능·불필요한 비용: 중복 LLM 호출, N+1, 토큰 낭비
- 테스트 부재·약한 단언·커버되지 않은 분기
- 팀 컨벤션 위반: 커밋·PR에 `Co-Authored-By` 금지, `git add -- <경로>`만 사용(`git add -A`/`.` 금지), `dev` 직접 커밋 금지, 프롬프트 전문·시크릿·채팅 원문 노출 금지
- 제품 동작·계약 변경이 관련 스펙(`spec/`, `../knk-harness/docs/product-specs/`)과 어긋나는지

각 지적에는 코드상의 근거와 구체적 수정 방향을 함께 답니다. 실제 결함이 없으면 억지 지적 없이 그 사실을 밝힙니다.
