# release-deploy 참고 자료 (reference)

> `SKILL.md`가 절차를 들고, 이 문서가 **사실·근거·명령**을 든다. 필요할 때만 펼쳐 본다.
> 배포 이력은 `history.md`.
> **레포가 PUBLIC이다.** AWS 계정번호·비밀값은 여기 적지 않고 `../manyak-terraform`(비공개)이나
> GitHub Variables를 가리킨다. 리소스 이름과 절차만 둔다.
> 최종 갱신: 2026-07-22 (KNK-664로 로컬 런북 `scripts/spec/spec-docs/deploy-spec.md`를 여기로 통합)

---

## 1. 검증된 인프라 사실

| 항목 | 값 |
|---|---|
| AWS 계정 / 리전 | 계정번호는 `../manyak-terraform`(비공개) 참조 — 이 문서에 적지 않는다 / `ap-northeast-2`(서울) |
| EC2 (운영 서버) | 태그 `manyak-prod-app`, t3.small. **id는 적어두지 않는다** — user-data가 바뀌면 EC2가 통째로 교체된다(2026-07-11 실제 교체). 태그로 조회할 것 |
| ECR (이미지 창고) | `<계정>.dkr.ecr.ap-northeast-2.amazonaws.com/manyak-ai` |
| SSM 문서 (배포 명령) | `manyak-prod-ai-deploy` (파라미터 `ImageUri`) |
| Secrets Manager | `manyak/prod/app` — `DEEPSEEK_API_KEY` 포함 8키 |
| GitHub Variables | `AWS_ROLE_ARN` — 레포 Settings → Variables에 설정됨. 값은 `arn:aws:iam::<계정>:role/manyak-prod-gha-ai` 형태 |
| GitHub Secrets | **배포용은 없다** — AWS 인증은 OIDC라 불필요. 현재 있는 `CLAUDE_CODE_OAUTH_TOKEN` 1개는 배포와 무관(2026-07-22 확인) |

> 비밀값(키 실제 값), 콘솔 비밀번호, 개인 액세스 키는 **여기에도 적지 않는다.** 위는 식별자·존재 여부만.

**인프라 코드 정본은 `../manyak-terraform` 레포다.** ECR·EC2·SSM 문서·AI 전용 OIDC 역할, 운영 `docker-compose.prod.yml`, EC2 user-data(= `/opt/manyak/deploy.sh` 생성기)가 전부 거기 있다. 예전엔 `manyak-server`의 `infra/terraform/`이었으나 분리됐다. `manyak-infra` 레포는 **로컬 GHCR 개발용 compose**일 뿐 운영 인프라가 아니다 — 헷갈리기 쉽다.

---

## 2. 배포 동작 원리

- **트리거는 `main` push**(= `release → main` 머지). dev push는 GHCR 개발 이미지만 만들고 배포하지 않는다.
- **CI**: `.github/workflows/docker-image.yml` — `test` → `docker`(build/push) → `deploy`.
  - `dev` push → GHCR `ghcr.io/kim-n-kang/manyak-ai` (`:dev`, `:<short-sha>`)
  - `main` push → ECR `manyak-ai` (`:latest`, `:<short-sha>`) + `deploy` 잡 실행
- **deploy 잡**: stale-SHA 게이트(더 새 main이 있으면 skip) → OIDC 로그인 → EC2 탐색(태그) → `aws ssm send-command`로 `manyak-prod-ai-deploy` 호출 → 상태 폴링(최대 60×10s).
- **EC2 측**: `AI_IMAGE_OVERRIDE=<ImageUri> bash /opt/manyak/deploy.sh` → `docker compose pull ai` + `up -d --wait ai`. **헬스 게이트**가 `/api/v1/health`의 `status==ok`를 통과할 때까지 기다린다.
- **배포 이미지 태그는 short-sha**(`:latest` 아님 — 추적성).
- **부트스트랩 안전장치**: ECR에 이미지가 없어도 `deploy.sh`가 부재를 감지해 graceful skip. server↔ai `depends_on`도 끊어둬 순서 사고가 없다.
- **운영 컨테이너 env의 출처**: `deploy.sh`가 실행될 때마다 Secrets Manager(`manyak/prod/app`)를 새로 읽어 `.env`를 쓴다. 그래서 **시크릿 값만 바꿀 때는 코드 릴리스가 불필요**하고 AI 재배포(SSM)만으로 반영된다. 단 `deploy.sh` 자체를 바꾸려면 user-data 변경 → `terraform apply` → **EC2 교체**가 필요하다.

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

**키를 손으로 만지지 않는다.** `qa.sh`가 `.env`에 필요한 키(스크립트의 `REQUIRED_KEYS` — 현재 `DEEPSEEK_API_KEY`·`OPENAI_API_KEY`)가 하나라도 없으면 Secrets Manager에서 받아 넣고, 끝나면 내용을 원상복구한다(파일 권한은 0600으로 유지). 값은 변수에만 담기고 화면·로그에 찍히지 않는다. 컴파일 모델의 공급자가 바뀌면 `REQUIRED_KEYS`도 같이 고친다 — v0.2.6 QA에서 DEEPSEEK만 주입하다 OPENAI 키 부재로 라이브가 기동 실패한 전례가 있다.

> 예전 런북에는 `aws secretsmanager … | python -c "print(...)"`를 실행한 뒤 "출력을 화면에 남기지 말라"고 적혀 있었다. **그 명령은 실행하는 순간 이미 키를 터미널에 뿌린다**(대화 기록·스크롤백에 남는다). 쓰지 말 것.

- 통합 테스트: `qa.sh`가 부르는 `scripts/test.sh --live tests/integration`. 따로 돌릴 일이 있으면 이 형태를 쓴다.
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

**`qa.sh`는 필요한 키(`REQUIRED_KEYS`)가 없으면 Secrets Manager에서 가져와 `.env`에 넣고, 끝나면 내용을 원상복구한다**(파일 권한은 0600으로 유지 — 기본이 0666이라 되돌리면 다음 주입 때 또 위험해진다). 값은 어디에도 출력하지 않는다(`test.sh`가 docker 인자를 화면에 그대로 찍기 때문에 `-e`로 넘기면 키가 노출된다 — 그래서 `.env` 경유다).

**운영 헬스체크는 SSM으로만 된다.** AI 서버는 외부에 노출돼 있지 않아 브라우저·curl로 못 두드린다. `prod-health.sh`가 EC2를 태그로 찾아 `/api/v1/health`를 찌른다. `curl`은 `-f`가 없으면 HTTP 500에도 종료코드 0이라, 스크립트가 응답 본문의 `status`·`version`을 직접 검사한다.

> AWS 자격: 로컬 `aws configure`로 배포 권한이 있는 IAM 사용자 키가 등록돼 있어야 한다. 없으면 `NoCredentials`.

---

## 7. 트러블슈팅

- **성공 신호**: `deploy` 잡 success + ECR에 새 short-sha 태그 + 운영 health가 `ok`이고 version이 이번 릴리스 값.
- **deploy 잡 실패**: `gh run view <id> --log-failed -R KIM-N-KANG/manyak-ai`
  - `"running 상태의 manyak-prod-app EC2를 찾지 못했습니다"` → terraform apply 선행 필요.
  - SSM 실패(Failed/TimedOut) → EC2의 `deploy.sh` 또는 compose health 실패. AI 컨테이너 로그 확인 필요(인프라 담당·EC2 접근).
- **AI가 응답 못 함**: Secrets의 `DEEPSEEK_API_KEY` 확인.
- **롤백**: 직전 정상 short-sha 이미지로 SSM 재배포(수동). manyak-ai 전용 롤백 절차는 아직 미정 — 필요 시 인프라 담당과 협의. ⚠️ 외부 계약이 바뀐 릴리스는 **AI만 되돌리면 안 된다**(3자 동시 롤백 — `history.md` v0.2.1 참조).

---

## 8. 미해결 / 주의 (백로그)

- [ ] `AI_SENTRY_DSN` — 빈 값이어도 배포는 정상(Sentry no-op). **책임 분담**: AI팀은 Sentry 프로젝트 생성→DSN 발급→전달까지. **Secrets Manager에 값 입력은 백엔드·인프라 담당 몫.**
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
