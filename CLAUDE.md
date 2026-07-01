# 기본 지침

작업을 시작하기 전에 다음 문서를 먼저 확인하세요.

- `../knk-harness/CLAUDE.md`

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

### 프롬프트·명세 변경
- `prompt/`·`spec/` 파일은 frontmatter의 `version`·`updated`로 버전을 관리합니다.
- 프롬프트·명세 수정 후에는 `notion-version-control` 스킬로 Notion에 버전 스냅샷을 저장합니다.
- 파일은 LF 줄바꿈으로 저장합니다(CRLF면 frontmatter 파싱이 깨질 수 있음).

### 테스트
- 테스트는 도커 격리 환경에서 실행합니다: `scripts/test.ps1`.
- 로컬 anaconda 등에는 `pytest-asyncio`가 없어 async 테스트가 스킵될 수 있습니다.
