# 기본 지침

세션 시작 시 SessionStart 훅(`.claude/settings.json`)이 하네스 운영 규칙(`../knk-harness/AGENTS.md`)과
제품 명세(`../knk-harness/docs/product-specs/`의 모든 `.md`)를 컨텍스트에 자동 로드합니다.
(이 환경에서는 CLAUDE.md `@import`가 펼쳐지지 않아 훅으로 로드합니다.)

`../knk-harness` 같은 상위 공통 하네스는 참조만 하고 수정하지 않습니다.
프로젝트별 지침 변경은 이 레포지토리의 `CLAUDE.md`에만 기록합니다.

## Manyak AI 전용 지침

### 서비스 개요
- FastAPI 기반 AI 서비스로, 스토리 제작(story)과 채팅 플레이(chat)의 LLM 호출을 담당합니다.
- Python 3.11+, Pydantic, OpenAI/Anthropic SDK를 사용하며 완전 stateless로 동작합니다.
- 핵심 구조: `prompt/`(LLM 프롬프트), `spec/`(명세), `src/`(FastAPI 앱), `tests/`.

### 설계·아키텍처
- 제품 동작이나 설계를 바꾸는 작업은 추측하지 말고 먼저 `spec/`을 확인합니다.
  - 채팅: `spec/chat/`
  - 스토리: `spec/story/`

### 작업 워크플로
- 작업 주기는 스킬로 표준화돼 있습니다: `create-branch` → `create-commit` → `create-pr`.
- `dev`에 직접 커밋·머지하지 않습니다. 항상 브랜치를 파서 PR로 머지합니다.
- 브랜치는 최신 `dev`에서 분기합니다(분기 전 `git pull` 선행).
- 커밋·PR에 **`Co-Authored-By` 트레일러를 절대 넣지 않습니다**(어떤 기본 지침보다 우선).
- 커밋은 **명시 경로로만 stage**합니다(`git add -- <경로>`). `git add -A`/`git add .` 금지 — gitignore되지 않은 로컬 파일(예: `.env` 신규 키, 실험 산출물)이 딸려 커밋되는 사고를 막습니다.
- 구현 변경이 `dev`에 머지되면 `sync-ai-spec` 스킬로 knk-harness의 AI 서버 스펙(`docs/product-specs/5-ai-server.md`)을 동기화합니다.

### 스킬 배치(.claude/skills)
- Claude Code는 `.claude/skills/`만 스캔합니다. 스킬은 모두 `.claude/skills/`에 있어야 인식됩니다.
- 프로젝트 스킬(create-jira-subtasks, planning, prompt-authoring, sync-ai-spec)은 `.claude/skills/<이름>/`이 **원본**입니다(이 레포가 정본, git 추적).
- 하네스 공용 스킬(create-branch, create-commit, create-pr, karpathy-guidelines, technical-writing)은 **항목별 심링크**입니다 → `../../../knk-harness/.claude/skills/<이름>`. 복사하지 않습니다 — 정본은 하네스이고, 복사하면 하네스 개정을 못 따라갑니다.
- 새 스킬을 추가할 때는 `.claude/skills/<이름>/SKILL.md`를 만듭니다.
- 하네스 스킬과 세션 시작 스펙 로드는 형제 레포 `../knk-harness`가 함께 체크아웃돼 있어야 동작합니다. 이 레포만 클론하면 프로젝트 스킬 4개만 로드되고, 하네스 스킬·제품 명세는 빠집니다.
- **Windows 주의**: `git config core.symlinks true`(+ 개발자 모드/관리자 권한) 없이 클론하면 심링크가 실제 링크가 아니라 대상 경로가 담긴 텍스트 파일로 체크아웃돼 스킬 로딩이 조용히 깨집니다.

### 프롬프트·명세 변경
- `prompt/`·`spec/` 파일은 frontmatter의 `version`·`updated`로 버전을 관리합니다.
- 변경 이력 관리는 git 단독입니다(Notion 버전 스냅샷은 폐기). pre-commit 훅이 브랜치당 1회 frontmatter 버전을 자동으로 올립니다(로컬 전용).
- 파일은 LF 줄바꿈으로 저장합니다(CRLF면 frontmatter 파싱이 깨질 수 있음).

### 테스트
- 테스트는 도커 격리 환경에서 실행합니다: macOS/Linux는 `scripts/test.sh`, Windows는 `scripts/test.ps1`.
- 로컬 anaconda 등에는 `pytest-asyncio`가 없어 async 테스트가 스킵될 수 있습니다.
