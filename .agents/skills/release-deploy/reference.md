# release-deploy 참고 자료 (reference)

> `SKILL.md`가 절차를 들고, 이 문서가 **사실·근거·명령**을 든다. 필요할 때만 펼쳐 본다.
> 배포 이력은 `history.md`.
> **레포가 PUBLIC이다.** AWS 계정번호·비밀값은 여기 적지 않고 `../manyak-terraform`(비공개)이나
> GitHub Variables를 가리킨다. 리소스 이름과 절차만 둔다.
> 최종 갱신: 2026-11-22 (KNK-2127, 운영 앱 호스트 기준으로 배포 설명 갱신)

---

## 1. 검증된 인프라 사실

| 항목 | 값 |
|---|---|
| 운영 호스트 | OCI VM 한 대(KNK-2126). server, ai, notification이 이 한 대에서 돈다. 접속 정보는 이 문서에 적지 않는다 |
| 이미지 창고 | GHCR `ghcr.io/kim-n-kang/manyak-ai`(private). 운영은 `:prod` 태그를 따라간다 |
| 배포 명령 | 없음. CI가 `:prod`를 옮기면 호스트의 updater가 스스로 받아 교체한다 |
| 운영 비밀값 | 호스트 `/opt/manyak/secrets/ai.env` 파일(키 이름은 manyak-terraform `oci/prod/secrets/ai.env.example`) |
| GitHub Secrets | **배포용은 없다** — GHCR은 워크플로의 `GITHUB_TOKEN`으로 쓴다. 현재 있는 `CLAUDE_CODE_OAUTH_TOKEN` 1개는 배포와 무관(2026-07-22 확인) |

> 비밀값(키 실제 값), 콘솔 비밀번호, 개인 액세스 키는 **여기에도 적지 않는다.** 위는 식별자·존재 여부만.

**인프라 정본은 `../manyak-terraform` 레포의 `oci/prod`다.** 호스트 `deploy.sh`, updater(`scripts/update.sh`), 서비스 설정(`services/ai.env`, `ai.conf`, `ai.healthcmd`)이 거기 있다. `manyak-infra` 레포는 **로컬 GHCR 개발용 compose**일 뿐 운영 인프라가 아니다 — 헷갈리기 쉽다.

---

## 2. 배포 동작 원리

- **트리거는 `main` push**(= `release → main` 머지). dev push는 GHCR `:dev`만 옮기고, dev 환경(`dev-api.manyak.app`)이 그 이미지를 자동으로 받아 교체한다.
- **CI**: `.github/workflows/docker-image.yml`. `test`, `smoke` → `docker`(build/push) → `deploy-dev` 또는 `deploy-prod`.
  - `dev` push → GHCR `ghcr.io/kim-n-kang/manyak-ai:<short-sha>`. `deploy-dev`가 최신 dev SHA 게이트 뒤 `:dev`로 옮긴다
  - `main` push → GHCR `ghcr.io/kim-n-kang/manyak-ai:<short-sha>`. `:prod`는 빌드에서 붙이지 않고 `deploy-prod`가 옮긴다
- **deploy-prod 잡**: 배포 대상은 이 실행의 커밋이 아니라 현재 main HEAD다 → HEAD 이미지가 GHCR에 있는지 확인(아직 없으면 그 커밋의 실행이 배포하므로 건너뜀) → 지금 `:prod` 다이제스트를 기록하고 `:prod`를 HEAD로 옮김 → 3분 대기(updater 주기와 기동) → `api.manyak.app/actuator/health` 스모크. 스모크가 실패하면 `:prod`를 직전 다이제스트로 되돌린다. ai는 공개 주소가 없어 잡이 교체를 직접 보지 못한다.
- **호스트 측**: updater(1분 주기)가 `:prod` 다이제스트가 바뀐 것을 보면 `deploy.sh ai <image>`가 새 ai 컨테이너를 띄우고, 헬스(`/api/v1/health`, 모델과 공급자 키 기동 검사 포함)가 통과하면 내부 프록시(`ai.internal`)를 넘긴 뒤 옛 컨테이너의 진행 중 스트림을 드레인(240초)하고 내린다. 헬스를 못 넘으면 새 컨테이너만 지우고 실패로 끝나며 옛 ai가 계속 서비스한다. 실패한 다이제스트는 기록해 두고 다시 시도하지 않는다. 세 서비스의 배포는 호스트에서 `flock`으로 직렬화된다. 결과는 updater가 운영 알림으로 보낸다.
- **호스트는 다이제스트로 배포한다**(`<repo>@sha256:...`). 현재와 직전 이미지를 `/data/state`에 기록한다.
- **운영 컨테이너 env의 출처**: 비밀이 아닌 값은 manyak-terraform `oci/prod/services/ai.env`, 비밀값은 호스트 `/opt/manyak/secrets/ai.env`다. 바꾼 뒤에는 호스트에서 `deploy.sh sync`(설정일 때)와 `deploy.sh ai current`로 반영한다. 코드 릴리스는 필요 없다.

---

## 3. 브랜치 · 버전 규칙 (팀 합의)

```
dev → feat/* → dev            (Squash and Merge)
dev → release/vX.Y.Z          (분기, 분기 전 dev 최신화 필수)
release/* → fix/* → release   (Squash and Merge — QA 중 발견 버그)
release → main                (Merge Commit)  = 배포
release → dev                 (Merge Commit)  = QA 수정 역류
release/vX.Y.Z                머지 끝나면 삭제 (영구 유지 안 함)
```

### 3-1. 버전 표기와 태그를 다는 위치

**git annotated tag `vX.Y.Z` + `[KNK-xx] Release: vX.Y.Z 배포` 머지 커밋.** 배포 이미지 태그는 이와 별개로 short-sha다.

태그는 **반드시 `main`을 pull한 뒤 그 위에서** 단다. release 브랜치 끝과 main 머지 커밋은 **내용이 같아도 다른 커밋**이라, release 브랜치에서 태그를 달면 태그가 "실제로 배포된 커밋"을 가리키지 않게 된다. 나중에 "이 버전이 뭐였지"를 되짚을 때 어긋난다.

### 3-2. 패키지 버전 파일도 릴리스마다 올린다

대상: `pyproject.toml`, `src/core/config.py`의 `app_version`, `.env.example`.

- `/api/v1/health`의 `version`이 **"새 이미지가 실제로 떴는지" 판별하는 유일한 눈**이다. 안 올리면 배포 검증이 무의미해진다.
- Langfuse 트레이스의 `release` 라벨이 이 값이다. 안 올리면 어느 버전에서 난 문제인지 못 가른다.
- **실제 사고**: v0.2.1 직전까지 안 올려서 `/health`가 계속 `0.1.0`을 가리켰다(KNK-656에서 뒤늦게 정정).
- 방법: `fix/KNK-xx → release`(Squash)로 3파일을 한 커밋에. 릴리스 티켓 키를 쓴다 — 예: `[KNK-656] Chore: 앱 버전 0.2.1 반영`.
  - release 분기 뒤에 해도 되고 분기 전 dev에서 해도 된다. 분기 전이면 `feat/`가 아니라 `chore/`로 판단한다.
- 팀 현황: `manyak-web`도 릴리스마다 `package.json`을 올린다(`[KNK-655] Chore: 패키지 버전 v0.2.5로 변경`). `manyak-server`만 `0.0.1-SNAPSHOT` 고정이라 안 올린다. 예전 런북의 "굳이 안 올린다(server 팀 관행)"는 **server만 보고 적은 것**이었다 — AI는 web 쪽을 따른다.

### 3-3. 머지가 거부될 때

브랜치별 허용 머지 방식(2026-07-22 실측):

| 브랜치 | `allowed_merge_methods` | 팀 규칙과 |
|---|---|---|
| `main` | `["merge","rebase"]` | 맞음 — `release → main`은 Merge Commit |
| `dev` | `["squash","merge"]` | 맞음 — `feat/* → dev`는 Squash, 역류는 Merge Commit |

`dev`는 2026-07-22에 고쳤다. 그전에는 `["squash","rebase"]`라 `release → dev` 역류가 `GraphQL: Merge commits are not allowed on this repository`로 막혔다(manyak-web과 같은 설정으로 맞춤).

**막히면 레포 설정이 아니라 브랜치 룰셋을 본다.** 그때 레포 설정(`allow_merge_commit`)은 `true`였는데도 거부돼 원인 파악이 늦었다 — 진짜 제한은 브랜치별 룰셋에 걸린다.

```bash
gh api repos/KIM-N-KANG/manyak-ai/rules/branches/dev \
  --jq '.[] | select(.type=="pull_request") | .parameters.allowed_merge_methods'
```

### 3-4. 그 밖

- `main`·`dev` 직접 push는 **기술적으로 막혀 있다**(2026-07-22 실측). 두 브랜치 모두 룰셋에 `pull_request`·`deletion`·`non_fast_forward` 규칙이 걸려 있어 PR로만 들어간다.
  - `release/*`에는 룰셋이 없다 — 여기만 컨벤션으로 지켜진다.
  - 옛 런북에 "`main` 보호 규칙 미설정"이라 적혀 있었으나 **사실이 아니다.** 그 사이 룰셋이 생겼다.
- 분기 전 `git checkout dev && git pull`. 묵은 로컬 dev가 충돌의 주원인이다.

### 3-5. 릴리스 Jira 티켓 — **사용자가 만든다**

**이 스킬은 릴리스 티켓을 만들지도, 상태를 바꾸지도 않는다**(2026-07-22 사용자 결정). 티켓 키를 받아 PR 제목에 쓰기만 한다. 키를 아직 못 받았으면 물어본다.

- 그렇게 정한 이유: 이 스킬은 "main 머지 승인 말고는 묻지 않는다"인데, `create-jira-subtasks`는 "상태를 바꾸기 전에 반드시 확인받는다"이다. 스킬이 티켓을 건드리면 둘 중 하나를 반드시 어긴다. 티켓을 사용자 몫으로 두면 충돌 자체가 사라진다.
- 참고(사용자가 만들 때의 형식): 부모 **KNK-449 "서비스 배포"** 아래 서브태스크, 제목 `AI 서버 vX.Y.Z 배포`.
- 세 서비스가 같은 부모를 공유한다(`웹 vX.Y.Z 배포`·`API 서버 vX.Y.Z 배포`·`AI 서버 vX.Y.Z 배포`). 서비스마다 버전이 따로 굴러가므로 **번호를 맞추려 하지 않는다.**
- **티켓 제목과 PR 제목은 형식이 다르다.** 티켓 `AI 서버 v0.2.1 배포`, PR `[KNK-656] Release: v0.2.1 배포`.

---

## 4. 배포 전 적대적 리뷰 — 3관점

`main...release/vX.Y.Z` diff를 놓고 "이대로 나가면 뭐가 깨지나"를 통과 전제 없이 본다.
관점을 나누는 이유는 **한 관점으로 훑으면 다른 층의 사고를 놓치기 때문**이다.

> **`main...dev`가 아니다.** 리뷰는 Phase B라, 그 앞 Phase A에서 올린 버전 파일과
> QA 중 `fix/*`로 고친 것이 이미 release 브랜치에만 있다. `dev`와 비교하면 **가장 마지막에
> 손댄 것들이 검토에서 빠진다.** 실제로 나가는 것은 release 브랜치다.

- **계약** — 외부 API 요청·응답·SSE 이벤트가 바뀌었나. 바뀌었으면 백엔드·프론트가 같이 나가야 하나(→ 3자 동시 배포·동시 롤백 판단).
- **환경변수·기동** — 새 env가 생겼나. 운영 Secrets/compose에 그 값이 실제로 있나. 없으면 기동이 죽나, no-op으로 살아 있나.
- **런타임** — 새로 도는 코드가 예외를 삼키나 던지나. 실패 경로가 응답을 막나.

차단급이 나오면 배포를 멈추고 `fix/KNK-xx → release`(Squash)로 해소한 뒤 재검토한다.

---

## 5. QA 절차 상세 (release 브랜치에서)

manyak-ai는 AI 서버라 QA = "서버 띄워 실제로 답하는지" 확인. dev 코드는 PR마다 CI를 통과했으므로 부담은 낮다.

### 5-1. 자동 검사
- 도커 격리 환경: `scripts/test.sh`(macOS/Linux) / `scripts/test.ps1`(Windows). 로컬 anaconda엔 `pytest-asyncio`가 없어 async 테스트가 조용히 스킵된다 — 로컬 pytest 결과로 통과를 주장하지 않는다.
- Docker Desktop이 꺼져 있으면 켠다.

### 5-2. 서버 health (LLM 불필요)
```bash
uvicorn src.main:app --port 8000        # 별도 터미널
curl http://localhost:8000/api/v1/health
```
기동 검사(`validate_selected_models`)가 선택된 모델의 공급자 키를 전부 요구해, 기동에도 `.env`에 `DEEPSEEK_API_KEY`(채팅·스토리라인)와 `OPENAI_API_KEY`(스토리 컴파일)가 있어야 한다.

### 5-3. 실제 AI 기능 (LLM 호출 — 과금)

**키를 손으로 만지지 않는다.** `qa.sh`가 `.env`에 필요한 키(스크립트의 `REQUIRED_KEYS` — 현재 `DEEPSEEK_API_KEY`·`OPENAI_API_KEY`)가 하나라도 없으면 Secrets Manager에서 받아 넣고, 끝나면 내용을 원상복구한다(파일 권한은 0600으로 유지). AWS를 정리한 뒤로는 이 자동 주입이 실패하므로 키를 로컬 `.env`에 미리 넣어 둔다(§6). 값은 변수에만 담기고 화면·로그에 찍히지 않는다. 컴파일 모델의 공급자가 바뀌면 `REQUIRED_KEYS`도 같이 고친다 — v0.2.6 QA에서 DEEPSEEK만 주입하다 OPENAI 키 부재로 라이브가 기동 실패한 전례가 있다.

> 예전 런북에는 `aws secretsmanager … | python -c "print(...)"`를 실행한 뒤 "출력을 화면에 남기지 말라"고 적혀 있었다. **그 명령은 실행하는 순간 이미 키를 터미널에 뿌린다**(대화 기록·스크롤백에 남는다). 쓰지 말 것.

- 통합 테스트: `qa.sh`가 부르는 `scripts/test.sh --live tests/integration`. 따로 돌릴 일이 있으면 이 형태를 쓴다.
- 검수 데이터셋 테스트(KNK-1770): 운영(main) 대비 모더레이션 프롬프트·코드·`config.py`의 `moderation_` 설정이 바뀐 릴리스에서만 `qa.sh`가 켠다. 이미지 장수만큼 검수 호출이 늘어난다. 이미지는 공개 레포에 올리지 않고 `evaluation/moderation/datasets/images/{approve,reject}/`에 로컬로 두며, 켜졌는데 데이터셋이 없으면 테스트가 실패한다. 따로 돌릴 때는 `RUN_MODERATION_DATASET=1 scripts/test.sh --live tests/integration/test_moderation_dataset_live.py`.
- 또는 서버를 띄우고 직접 호출: `POST /story/storylines` → 이야기 3편·추천 9개 / `POST /story/compile` → 스토리 명세 / `POST /chat/turns`(SSE) → 본문 스트림 / `POST /chat/choices` → 선택지 3개.
- prod 키를 로컬에 쓰는 것이라 소량 비용이 든다. QA가 끝나면 `.env`의 키를 정리한다.

---

## 6. 확인 명령

스크립트로 묶어 뒀다 — 내용을 읽지 말고 실행한다.

```bash
bash .agents/skills/release-deploy/scripts/qa.sh                 # 릴리스 QA(유닛 + 라이브 LLM)
bash .agents/skills/release-deploy/scripts/watch-deploy.sh       # 배포 워크플로 감시(최신 main 실행 자동 탐색)
bash .agents/skills/release-deploy/scripts/prod-health.sh 0.2.1  # 운영 헬스체크(SSM). 버전을 넘기면 일치까지 검사
bash .agents/skills/release-deploy/scripts/infra-check.sh        # 읽기 전용 인프라·GitHub 점검
```

넷 다 **종료코드가 판정**이다. `qa.sh`만 값이 여럿이다 — 0=진행 가능, 1=테스트 실패, 2=인자 오류, **3=라이브 미실시**(`--no-live`. 경고만 하고 0을 내면 자동화가 라이브 없이 통과시킨다).

`settings.json`의 `permissions.allow`에 등록돼 있어 권한 창이 뜨지 않는다 — 단 **위 형태 그대로, 레포 루트에서** 부를 때만 매칭된다(절대 경로로 부르면 매칭되지 않는다).

`watch-deploy.sh`가 따로 있는 이유: `gh run watch`를 run ID 없이 부르면 비대화형 환경에서 `run ID required when not running interactively`로 즉시 실패한다. 스크립트가 최신 main 실행을 찾아 넘기고 `--exit-status`로 워크플로 실패를 종료코드에 싣는다.

**`qa.sh`는 필요한 키(`REQUIRED_KEYS`)를 `.env`에서 읽는다.** AWS를 정리한 뒤로는 Secrets Manager가 없으니 키를 로컬 `.env`에 직접 넣어 둔다. 예전 동작: 키가 없으면 Secrets Manager에서 가져와 `.env`에 넣고, 끝나면 내용을 원상복구한다**(파일 권한은 0600으로 유지 — 기본이 0666이라 되돌리면 다음 주입 때 또 위험해진다). 값은 어디에도 출력하지 않는다(`test.sh`가 docker 인자를 화면에 그대로 찍기 때문에 `-e`로 넘기면 키가 노출된다 — 그래서 `.env` 경유다).

**`prod-health.sh`와 `infra-check.sh`는 옛 AWS 호스트(SSM, ECR)를 찾아 지금은 쓸 수 없다.** AI 서버는 공개 주소가 없다. 배포 확인은 `deploy-prod` 잡 success와 updater 운영 알림(`manyak 운영 배포 완료: ai <다이제스트>`)으로 한다. 알림이 `배포 실패`면 옛 ai가 계속 서비스 중이다.

---

## 7. 트러블슈팅

- **성공 신호**: `deploy-prod` 잡 success(`GHCR :prod -> <short-sha>`)와 updater 운영 알림 `manyak 운영 배포 완료: ai <다이제스트>`.
- **deploy-prod 잡 실패**: `gh run view <id> --log-failed -R KIM-N-KANG/manyak-ai`
  - `:prod` 이동 전 실패 → GHCR 권한이나 HEAD 이미지 문제. 운영은 그대로다.
  - 스모크 실패 → 잡이 `:prod`를 직전 다이제스트로 되돌렸다. updater가 1~2분 안에 다시 배포한다.
- **AI가 응답 못 함**: 호스트 비밀값(`ai.env`)의 `DEEPSEEK_API_KEY` 확인(인프라 담당).
- **롤백**: 수동 워크플로 `prod-rollback.yml`(입력 `sha`, main 커밋만)로 `:prod`를 직전 릴리스 커밋으로 옮기면 updater가 그 이미지로 교체한다. ⚠️ 외부 계약이 바뀐 릴리스는 **AI만 되돌리면 안 된다**(3자 동시 롤백, `history.md` v0.2.1 참조).

---

## 8. 미해결 / 주의 (백로그)

- [ ] `AI_SENTRY_DSN` — 빈 값이어도 배포는 정상(Sentry no-op). **책임 분담**: AI팀은 Sentry 프로젝트 생성→DSN 발급→전달까지. **운영 비밀값(호스트 `ai.env`) 입력은 백엔드·인프라 담당 몫.**
- [x] 스타일가이드의 옛 규칙 `release → dev (Rebase and Merge)` → `Merge Commit`으로 고침(KNK-665, 2026-07-22). `dev` 룰셋이 `["squash","merge"]`라 문서대로 rebase하면 머지가 막혔다. **원인은 설정만 고치고 문서를 안 고친 것** — 룰셋을 바꾸면 이 표(§3-3)와 스타일가이드를 함께 본다.
- [x] `main` 브랜치 보호 — 룰셋으로 설정돼 있음을 2026-07-22 확인(옛 "미설정" 기록은 오류였다).
- [x] Gemini 코드리뷰 제거 — 무료판이 2026-07-22 종료돼(PR #61에 봇이 종료 안내를 남김) `.gemini/` 폴더를 걷어냈다(KNK-665). 안에 있던 팀 코딩 규칙 190줄은 버리지 않고 `.agents/STYLEGUIDE.md`로 옮겼다. 이제 PR 리뷰는 Codex 하나다. GitHub 앱 설치가 남아 있다면 레포 설정에서 제거하는 것은 별개 작업이다.
- [ ] 도커 로그 회전 미설정(json-file 무제한 + 루트 30GB).
- [ ] CI 스모크 이미지와 publish 이미지가 별도 빌드라 의존성이 고정되지 않는다 — Langfuse를 켠 뒤엔 위험.

### v0.1.1 후속 패치 (Gemini PR #33 리뷰 — 전부 배포 비차단. 각 코멘트에 "후속 반영" 답글을 달아 둠)
- [ ] `src/services/chat_assembler.py` `_body_only` 정규식 CRLF 대응(`\r?\n`)
- [ ] `tests/conftest.py` `DEEPSEEK_API_KEY` 더미값 `setdefault` 추가
- [ ] `src/services/prompt.py` `_load_template`를 `utf-8-sig`로 통일
- [ ] `src/services/chat_llm.py` `_SPEAKER_BOLD_RE`를 비캡처그룹+백레퍼런스로 간결화
- [ ] `src/services/story_llm.py`·`chat_next_actions.py` `_strip_code_fence`를 `re.sub` 방식으로
- [ ] `src/services/prompt_meta.py` `_VERSION_RE`의 `\s*`→`[ \t]*`
