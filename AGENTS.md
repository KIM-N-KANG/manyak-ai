# 기본 지침

이 파일은 Codex 등 `AGENTS.md`를 읽는 에이전트를 위한 프로젝트 지침입니다.
(Claude Code는 같은 내용을 `CLAUDE.md`로 읽습니다 — 두 파일은 같은 규칙을 공유하며,
지침을 바꾸면 두 파일을 함께 갱신합니다.)

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

`../knk-harness` 같은 상위 공통 하네스는 참조만 하고 수정하지 않습니다. 두 가지 예외가 있습니다.

1. 구현이 `dev`에 머지된 뒤 `sync-ai-spec` 스킬로 AI 서버 스펙
   `../knk-harness/docs/product-specs/5-ai-server.md`를 동기화하는 것(아래 "작업 워크플로" 참조).
2. AI 서버 변경 때문에 다른 제품 스펙을 고쳐야 하면, 사용자 허락을 맡고 그 부분만 고칩니다.
   임의로 고치지 않고, 고칠 곳과 문구를 먼저 보여준 뒤 승인받습니다. 다른 서비스(백엔드·프론트)에도
   걸리는 규칙이면 그 사실을 함께 알립니다. 선례: Langfuse 도입으로 `6-analytics.md §6-7`의
   원문 비수집 규칙에 AI 관측 예외를 추가(KNK-624).

프로젝트별 지침 변경은 이 레포지토리의 `AGENTS.md`(및 짝인 `CLAUDE.md`)에만 기록합니다.

## Manyak AI 전용 지침

### 서비스 개요
- FastAPI 기반 AI 서비스로, 스토리 제작(story)과 채팅 플레이(chat)의 LLM 호출을 담당합니다.
  완전 stateless — 상태는 백엔드가 들고, 이 서버는 요청마다 받은 컨텍스트로 프롬프트를 조립해 LLM을 호출합니다.
- Python 3.11+, Pydantic(+pydantic-settings) 기반입니다.
- LLM 호출은 공통 통로(`src/services/llm/`)를 지납니다. 호출부는 "무엇을 원하는지"만 넘기고 회사별 SDK 차이는 어댑터가 흡수합니다(KNK-667). 어느 모델을 어느 회사로 보낼지는 등록부(`registry.py`)가 정하고, 등록되지 않은 모델은 호출 대상이 될 수 없습니다.
  - 지금 등록된 모델은 DeepSeek뿐이고 OpenAI SDK(호환 클라이언트)로 호출합니다.
  - Anthropic SDK 어댑터도 있지만 이 공급자를 쓰는 모델은 아직 등록부에 없습니다(KNK-675). 채팅 자리(`CHAT_MODEL`)에는 이 공급자를 쓸 수 없습니다 — 이 회사는 지시문 칸이 하나뿐이라 채팅이 뒤에 두는 안전 지시(PHI)가 버려집니다(`registry.BLOCKED_PROVIDERS`).
  - `deepseek-v4-pro`(env `STORY_COMPILE_MODEL`) — 스토리 컴파일 전용.
  - `deepseek-v4-flash` — 스토리라인 생성(env `STORYLINES_MODEL`)과 채팅 턴·선택지·판정(env `CHAT_MODEL`)에 각각. 지금은 같은 flash 기본이지만 용도별 독립 설정이 가능하도록 env를 3개로 분리했습니다(KNK-595). manyak-infra의 Compose env 이름 동기화는 짝 작업입니다.
- API는 `/api/v1` 아래 health·story·chat 라우터(`src/api/v1/`)이고, 채팅 턴은 SSE 스트리밍을 지원합니다.
- git 추적 구조: `prompt/`(LLM 프롬프트 템플릿), `spec/`(내부 설계 명세), `src/`(FastAPI 앱: api/v1·core·schemas·services), `tests/`(unit + API 테스트).
- 로컬 전용(git 무시) 디렉터리 — 존재하지만 커밋 대상이 아닙니다. 커밋에 딸려 들어가면 사고입니다:
  - `scripts/*` — 프롬프트 미리보기·실측 스크립트 창고(`test.sh`·`test.ps1`만 예외로 git 추적).
  - `experiment/` — 프롬프트·모델 변경의 품질·시간·비용을 baseline과 비교하는 실험 환경.
    사용법은 `experiment/README.md`, 설계 정본은 `scripts/spec/spec-docs/experiment-spec.md`.
  - `references/` — 정제되지 않은 참고 원천 자료(크롤·발췌).
  - `.env`·`.env.jira` — 시크릿. 절대 커밋하거나 문서·예시에 값을 옮기지 않습니다.
- **스킬은 폴더째 gitignore하지 않습니다.** 브랜치를 오갈 때 git이 조용히 지우고 복구해주는 것이
  없습니다(2026-07-22에 `SKILL.md` 3개가 그렇게 사라졌습니다, KNK-668). 비밀은 폴더가 아니라
  값 단위로 막습니다 — AWS 계정번호 같은 식별자는 스킬 문서에 적지 말고
  `../manyak-terraform`(비공개)이나 GitHub Variables를 가리킵니다.
- **`git clean`에 `-x`나 `-X`를 절대 쓰지 않습니다.** 위 로컬 전용 자산이 한 번에 사라지는데
  git에 없어 복구 수단이 아예 없습니다(2026-07-22 기준 삭제 대상 50개). 무엇이 지워질지 볼 때는
  `git clean -ndX`(dry-run)만 쓰고, 빌드 찌꺼기는 `rm -rf .pytest_cache .coverage`로 지웁니다.

### 설계·아키텍처
- 코드 스타일 규칙(프로젝트 레이아웃·네이밍·임포트 순서·응답 모델·에러 처리·금지 패턴)은
  `.agents/STYLEGUIDE.md`를 따릅니다. 코드를 쓸 때도 리뷰할 때도 같은 기준입니다.
- 제품 동작이나 설계를 바꾸는 작업은 추측하지 말고 위 인덱스에서 제품 명세와
  로컬 `spec/` 문서를 선택해 확인합니다.
  - 채팅 내부 설계: `spec/chat/` (레이어 책임 → 배치 → 컨텍스트 → 서비스 구현 4부)
  - 스토리 내부 설계: `spec/story/` (1-STORYLINES, 2-COMPILE)
  - 외부 계약(요청·응답·판정·SSE)의 SSOT: `../knk-harness/docs/product-specs/5-ai-server.md`.
- 채팅 프롬프트는 레이어 8종(CORE·SAFETY·STORY·CHARACTER·USER·MEMORY·JUDGEMENT·CHOICES)으로
  나뉘어 `prompt/chat/`에 템플릿으로 존재합니다. 어떤 내용이 어느 레이어에 속하는지는
  `spec/chat/1-PROMPT-LAYER.md`·`2-LAYER-PLACEMENT.md`가 정합니다 — 임의 배치 금지.

### 작업 워크플로
- 작업 주기는 스킬로 표준화돼 있습니다: `create-branch` → `create-commit` → `create-pr` → `request-codex-review`(PR에 Codex 리뷰를 받고 지적을 판단·반영).
- 워크플로 스킬은 지침을 기억한다고 생각해도 매번 해당 `SKILL.md`를 실제로 열어 따릅니다(요약 기억으로 대체하지 않습니다).
- `dev`에 직접 커밋·머지하지 않습니다. 항상 브랜치를 파서 PR로 머지합니다.
- 브랜치는 최신 `dev`에서 분기합니다(분기 전 `git pull` 선행).
- 커밋·PR에 **`Co-Authored-By` 트레일러를 절대 넣지 않습니다**(어떤 기본 지침보다 우선).
- 커밋은 **명시 경로로만 stage**합니다(`git add -- <경로>`). `git add -A`/`git add .` 금지 — gitignore되지 않은 로컬 파일(예: `.env` 신규 키, 실험 산출물)이 딸려 커밋되는 사고를 막습니다.
- **수정이 끝났다고 바로 커밋하지 않습니다.** 변경 요지를 먼저 보고하고 사용자 확인을 받은 뒤 커밋합니다.
- 구현 변경이 `dev`에 머지되면 `sync-ai-spec` 스킬로 knk-harness의 AI 서버 스펙(`docs/product-specs/5-ai-server.md`)을 동기화합니다.

### 스킬 배치(.agents/skills)
- 스킬 정본은 `.agents/skills/`이고, `.claude/skills/*`는 그것을 가리키는 심링크입니다(한 소스, 두 에이전트).
- 배치 규칙 상세(프로젝트 스킬과 하네스 공용 스킬 구분, 추적 여부 확인법, 폴더 구성, Windows 주의)는
  스킬을 만들거나 고칠 때 `.agents/skills/README.md`를 엽니다.
- 하네스 공용 스킬은 복사하지도 수정하지도 않습니다(하네스 수정 금지 규칙과 동일).

### 프롬프트·명세 변경
- `prompt/` 파일 수정은 반드시 `prompt-authoring` 스킬의 설계 원칙을 적용합니다.
- `prompt/`·`spec/` 파일은 frontmatter의 `version`·`updated`로 버전을 관리합니다.
  (`prompt/` frontmatter에는 `layer`·`priority`·`placement`·`slots` 등 배치 메타도 있습니다 — 의미를 모르면 `spec/chat/2-LAYER-PLACEMENT.md`를 먼저 봅니다.)
- 변경 이력 관리는 git 단독입니다(Notion 버전 스냅샷은 폐기). `prompt/`·`spec/` 파일을 고치면 커밋 전에 frontmatter의 `version`(+1)·`updated`(오늘 날짜)를 직접 올립니다(자동 갱신 장치 없음 — 브랜치당 파일별 1회면 충분).
- 파일은 LF 줄바꿈으로 저장합니다(CRLF면 frontmatter 파싱이 깨질 수 있음).
- 프롬프트·판정 규칙처럼 실제 LLM 출력으로만 검증되는 변경은 유닛 테스트 통과로 "검증됨"을 주장하지 않습니다.
  실측(라이브 호출) 결과가 있으면 그것을, 없으면 "실측 미실시"를 보고에 명시합니다.

### 실측·실험 (로컬 도구)
- 라이브 LLM 호출은 과금됩니다. 실측을 실행하기 전에 예상 호출 규모를 보고하고 승인받습니다
  (`experiment/` CLI의 `max_calls_without_confirm` 승인 게이트와 같은 원칙).
  - **예외: 릴리스 배포 QA는 상시 승인입니다**(사용자 결정). `release-deploy` 스킬의 `qa.sh`가 돌리는
    라이브 호출은 다시 묻지 않습니다 — 배포 전에 반드시 확인해야 하는 것이라 묻는 게 의미가 없습니다.
    QA 밖의 실측(프롬프트 A/B, 실험 러너)은 종전대로 승인이 필요합니다.
- 단발 실측·미리보기: `scripts/`의 로컬 스크립트를 씁니다 — 예: `preview_chat_prompt.py`(프롬프트 조립 확인, 무과금),
  `simulate_chat_ending.py`(판정·엔딩 시나리오), `verify_*.py`(판정·선택지·멀티턴 라이브 검증).
- 조건 비교 실험(프롬프트 A/B, 모델·파라미터 변경): `experiment/` 러너를 씁니다.
  시작 전 `experiment/README.md`의 개시 체크리스트를 따르고, 본 실행 전 `--smoke`로 싼 시운전을 먼저 돌립니다.

### 테스트
- 테스트는 도커 격리 환경에서 실행합니다: macOS/Linux는 `scripts/test.sh`, Windows는 `scripts/test.ps1`.
- 라이브 테스트(실제 LLM 호출, `.env` 키 필요)는 `scripts/test.sh --live`로 분리 실행합니다 — 위 실측 승인 원칙이 여기에도 적용됩니다.
- 로컬 anaconda 등에는 `pytest-asyncio`가 없어 async 테스트가 스킵될 수 있습니다 — 로컬 pytest 직접 실행 결과로 통과를 주장하지 않습니다.
- **버그를 고치면 그 버그를 재현하는 테스트를 같은 PR에 함께 남깁니다**(재발 방지). 고친 버그가 조용히 재발하는 것을 막는 회귀 그물입니다 — 커밋·PR에 어떤 버그를 고정했는지 적습니다(선례: KNK-465 엔딩 테스트).

### 완료의 정의 (끝내기 전 체크)
작업을 "완료"로 보고하기 전에 아래를 확인합니다. 하나라도 못 했으면 완료가 아니라 "미완(사유)"로 보고합니다.

- [ ] 제품 동작을 바꿨다면 관련 `spec/`·`5-ai-server.md`를 실제로 열어 근거로 삼았다(추측 아님).
- [ ] `scripts/test.sh`가 통과했다(코드 변경 시).
- [ ] `prompt/`·`spec/`을 고쳤다면 frontmatter `version`·`updated`를 올렸고 LF를 유지했다.
- [ ] 프롬프트·판정 동작 변경이면 실측 결과 또는 "실측 미실시"를 보고에 담았다.
- [ ] 스크립트·훅 같은 도구를 만들었다면 실패 경로까지 실제로 돌려봤다(정상 경로만 돌리면
      "실패를 성공으로 보고하는" 종류의 버그가 그대로 남습니다).
- [ ] 변경 요지를 보고했고, 커밋은 사용자 확인 후 명시 경로 stage로만 했다.
- [ ] 커밋·PR에 `Co-Authored-By`가 없다.

### 판단·소통 원칙
- 읽은 파일을 먼저 알립니다. 작업 전 필수 지침·명세·스킬 파일을 실제로 읽은 뒤,
  본 작업을 시작하기 전에 어떤 파일을 읽었는지 사용자에게 명시합니다.
- 추측하지 않습니다. 레포에 없는 제품 정책·계약·필드 이름은 지어내지 말고 명세를 찾거나 사용자에게 묻습니다.
  확인된 사실과 추정을 구분해 말하고, 추정은 "추정"이라고 밝힙니다.
- **결론 먼저, 쉬운 한국어로.** 보고는 결론 → 짧은 근거 순서로 하고, 낯선 용어는 풀어 씁니다
  (더 흔한 말이 있는데 굳이 어려운 용어를 쓰지 않습니다). 첫 설명부터 비전공자 기준으로 —
  "더 쉽게"라는 재요청이 나오면 이미 실패한 것입니다. 선택지를 나열해 되묻기보다
  추천 하나를 근거와 함께 제시합니다.
  쉽게 쓴다는 것은 **비유를 지어내는 게 아닙니다.** 냉장고·자물쇠·서랍 같은 비유는 원래 개념 위에
  설명을 한 겹 더 씌워 오히려 어렵게 만듭니다. 대신 그 일이 실제로 어떻게 벌어지는지를
  짧은 문장으로 적습니다 — 무엇이 입력이고, 무엇이 일어나고, 그래서 무엇이 곤란해지는지.
- 분석·리뷰·검토 요청의 산출물은 보고입니다. "정리해줘", "검증해줘", "리뷰해줘"는
  수정 지시가 아닙니다 — 지시 없이 파일 수정·저장·커밋으로 넘어가지 않고,
  결과는 파일로 만들지 말고 대화에 바로 출력합니다(사용자가 저장을 요청한 경우만 예외).
- **티켓 범위를 지킵니다.** 현재 티켓 밖의 개선거리를 발견하면 그 자리에서 고치지 말고
  "다른 티켓 사안"으로 보고만 합니다.
- 최소 변경. 요청 범위 밖 개선을 끼워넣지 않습니다(원칙은 `karpathy-guidelines` 스킬).
  기존 코드·문서의 톤과 구조를 따르고, 무관한 문장을 다듬고 싶어도 참습니다.
- **실패를 숨기지 않습니다.** 테스트 실패·스킵·미실시, 확인 못 한 부분은 그대로 보고합니다.

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
