# manyak-ai 배포 이력 (history)

> 배포할 때마다 여기에 한 항목을 추가한다. 절차는 `SKILL.md`, 사실·명령은 `reference.md`.
> 레포가 PUBLIC이다. 계정번호·비밀값은 적지 않는다(키 이름과 절차까지만).

---

## 다음 배포 때 볼 것

- 흐름은 늘 같다: `dev → release/vX.Y.Z → main`(Merge Commit) → 자동 배포 → 태그 → 역류(Merge Commit) → 브랜치 삭제.
- **운영 배포는 ECS 경로다(KNK-963, v0.2.6부터).** deploy 잡의 안정화 대기는 10분(60×10초)이라 **잡 실패 ≠ 배포 실패** — dev에서 태스크 시작 실패 반복으로 54분 뒤 완료된 전례(8/23)가 있다. 잡이 실패하면 롤백 전에 ECS 서비스 상태부터 확인한다. **EC2는 회수됐고 EC2 롤백 잡도 제거됐다(KNK-971, v0.3.0)** — `prod-health.sh`·`infra-check.sh`의 EC2·SSM 점검이 실패하니 ECS(태스크 imageDigest·HEALTHY)로 검증한다. 스크립트의 ECS 전환은 후속 사안.
- **Langfuse 활성화(KNK-654)는 릴리스가 아니다.** 코드는 이미 나가 있고 키만 없어 no-op이다. 순서:
  1. `manyak-terraform` apply — **EC2 교체**가 일어난다(다운타임 + 이미지 핀이 `:latest`로 리셋되는 것 주의).
  2. 백엔드 KNK-621 배포 — 커스텀 장르 400 차단. 안 하면 사용자 자유입력이 트레이스 태그로 유입된다.
  3. Secrets에 `AI_LANGFUSE_PUBLIC_KEY`·`AI_LANGFUSE_SECRET_KEY`·`AI_LANGFUSE_HOST` 추가 — **기존 8키를 전부 포함해 저장**한다(`put-secret-value`는 전체 덮어쓰기).
     - **HOST는 정확히 `https://jp.cloud.langfuse.com`이어야 한다.** 코드가 이 문자열과 일치할 때만 켠다(`src/core/langfuse.py`의 `_ALLOWED_HOST`, 후행 슬래시는 무시). ⚠️ `.env.example`의 기본값은 EU(`https://cloud.langfuse.com`)라 그대로 복사하면 **에러 로그만 남고 조용히 꺼진 채 기동**한다.
  4. ECS(manyak-prod) 서비스 force-new-deployment로 AI만 재배포.
  5. 기동 로그에서 `Langfuse 활성 — host=… env=prod` 확인 + server 컨테이너에 `AI_LANGFUSE_*`가 없는지 확인.

---

## v0.3.1 — 2026-09-04 배포 완료

- 범위: v0.3.0 이후 dev 누적 = KNK-1102(스토리라인 이름 검증 소진 시 502 대신 결과 반환 + Sentry 경고, 빈 본문 재호출은 원본 유지) + KNK-1086(#102 이력 문서, 이미지 무관) + 버전 올림(KNK-1182, #104).
  - **외부 계약 변경 없음 — AI 단독 배포·단독 롤백 안전.** 응답 스키마·`meta.retry_count` 그대로. 전에 502로 나가던 "이름 미등장 재호출 소진" 한 경우가 200으로 나간다. 배경은 2026-09-01 운영에서 한 사용자가 같은 입력으로 502를 7회 수신한 것(그라파나 "AI 실패 급증" 알림).
  - 새 env 없음. Sentry SDK 2.68.1의 `Scope.set_level("warning")` 사용 — 설치본에서 존재 확인.
- PR #105 `[KNK-1182] Release: v0.3.1 배포` → main 머지 `7fadb2d`(Merge Commit, 사용자 직접 실행) → 운영 워크플로 전 잡 success(run `33778434113`, Deploy to ECS (prod) 약 5분). 태그 `v0.3.1` → `7fadb2d`.
- 검증: ECS `manyak-prod` 실행 태스크의 `ai` 컨테이너 imageDigest가 ECR `7fadb2d` 태그와 일치, 태스크·컨테이너 HEALTHY. `prod-health.sh`는 EC2 경로라 예상대로 실패(v0.3.0 이력 참조). `/health`의 `version` 값은 직접 조회 경로가 없어 미확인 — ECS 컨테이너 health check와 digest 일치로 대신함.
- QA: `qa.sh` 유닛·API 793 passed·8 skipped, 라이브 `tests/integration` 10 passed(161.15초), 종료코드 0.
- 배포 전 적대적 리뷰 3관점(계약·환경변수/기동·런타임) — 차단 요인 없음.
- `watch-deploy.sh`가 10분 제한을 넘겨 끊겼다(ECS 배포 잡이 5분이었는데 출력이 버퍼링돼 진행이 안 보였다). `gh run watch <run id> --exit-status`를 백그라운드로 돌려 대신했다. 스크립트 보강은 후속 사안.
- **역류 PR을 main 머지 전에 미리 만들어 둔 첫 사례**(#106). release 브랜치 자동 삭제와 무관하게 Merge Commit 역류가 바로 됐다(dev `0d7e1b1`).

## v0.3.0 — 2026-09-01 배포 완료

- 범위: v0.2.6 이후 dev 누적 = 인물 이미지 기능 묶음(KNK-414 컴파일 인물별 이미지 생성·base64 응답, KNK-982 채팅 턴 인물 이미지 출력, KNK-1002 인물명 라벨 감지 강제 부착, KNK-1014 마커 형식·이미지 이름 계약 변경, KNK-1047 표지 썸네일 생성, KNK-1062 이름 불일치 수정) + KNK-971(EC2 롤백 잡 제거) + 버전 올림(KNK-1086).
  - **외부 계약은 전부 추가형 — AI 선행 배포·단독 롤백 안전.** 컴파일 응답에 `character_appearances`·`character_images`·`thumbnail_image` 추가, 채팅에 요청 `character_images`(선택)·SSE `character_image` 이벤트·completed `characterImages` 추가. 기능 활성화는 백엔드 대응 배포 후.
  - 이미지 생성 통로(`src/services/image/`) 신설 — `IMAGE_MODEL` 기본 `gpt-image-2-2026-04-21`, 키는 기존 `OPENAI_API_KEY` 재사용, 새 필수 env 없음. 이미지 실패는 인물 단위 흡수(컴파일은 성공), Sentry 태그 `character_image_generation`·`thumbnail_image_generation`으로 관측.
  - 비용·시간: 컴파일 1회당 이미지 호출이 인물 수+1(썸네일)만큼 증가(인물 이미지 병렬, 장당 60초 제한).
- PR #100 `[KNK-1086] Release: v0.3.0 배포` → main 머지 `3b807dc`(Merge Commit, 사용자 직접 실행) → 운영 워크플로 전 잡 success. 태그 `v0.3.0` → `3b807dc`.
- **검증: `prod-health.sh`·`infra-check.sh`의 EC2 경로가 이번부터 실패한다(EC2 회수됨).** ECS로 대체 검증 — 실행 중 ai 컨테이너의 imageDigest가 ECR `3b807dc` 태그와 일치, 태스크 HEALTHY 확인.
- QA: `qa.sh` 유닛·API 791 passed·8 skipped, 라이브 `tests/integration` 10 passed(180.61초), 종료코드 0. `infra-check.sh` 종료코드 1(실패 2건 전부 EC2·SSM 옛 경로 점검 — 예상된 실패).
- 배포 전 적대적 리뷰 3관점(계약·환경변수/기동·런타임) — 차단 요인 없음. 스토리라인 인물 이름 중복 검사가 대소문자 무시로 엄격해진 것은 의도된 변경.
- **release 브랜치가 PR #100 머지 때 자동 삭제됐다**(레포의 head 브랜치 자동 삭제 설정). 역류를 위해 로컬 브랜치를 재push해 PR을 만들었다 — 다음부터는 역류 PR을 main 머지 전에 미리 만들어 두면 재push가 필요 없다.
- 역류: `release/v0.3.0 → dev` Merge Commit(PR #101).

## v0.2.6 — 2026-08-25 배포 완료

- 범위: v0.2.5 이후 dev 누적 = KNK-852(JSON 한 줄 로그, 백엔드 LogstashEncoder와 필드 통일) + KNK-961(헬스체크 접근 로그 노이즈 제거) + KNK-951(스토리 컴파일 Gemini 전환 기반 — **휴면 코드**) + KNK-963(운영 배포 경로 ECS 전환) + 버전 올림(KNK-980).
  - **외부 계약 변경 없음 — AI 단독 배포·단독 롤백 안전.** Gemini는 `STORY_COMPILE_MODEL` 변경 전까지 안 쓰이고, `GEMINI_API_KEY` 기본값이 빈 문자열 + 기동 검사는 선택된 모델의 공급자만 봐서 운영 Secrets에 키가 없어도 정상 기동.
  - **KNK-963 = 새 ECS 운영 배포 경로의 첫 실전.** 이번엔 CI 대기(10분) 안에 완료. "다음 배포 때 볼 것"의 대기 초과 주의 참조.
- PR #90 `[KNK-980] Release: v0.2.6 배포` → main 머지 `deb7891`(Merge Commit) → 운영 워크플로 전 잡 success. 태그 `v0.2.6` → `deb7891`.
- 검증: `prod-health.sh 0.2.6` → `{"status":"ok","version":"0.2.6"}`.
- QA: `qa.sh` 유닛·API 640 passed·5 skipped, 라이브 `tests/integration` 7 passed(74.85초), 종료코드 0. `infra-check.sh` 종료코드 0.
- **QA 특이사항: `qa.sh` 키 주입 공백을 발견·수정.** 1차 라이브가 기동 실패 — 스크립트가 `DEEPSEEK_API_KEY`만 주입했는데, 기동 검사(`validate_selected_models`)는 컴파일 모델(gpt-5.6-terra=openai)의 `OPENAI_API_KEY`도 요구한다(컴파일 모델이 OpenAI로 옮겨간 뒤 잠복, 로컬 `.env`에 키가 있어 가려져 있었다). 주입 대상을 요구 키 전체(`REQUIRED_KEYS`)로 확장해 재실행 → 통과. 수정은 릴리스 후 dev 문서 PR로 반영.
- 배포 전 적대적 리뷰 3관점(계약·환경변수/기동·런타임) — 차단 요인 없음.
- 버전 올림 커밋(#89)은 `fix/ → release` PR 머지가 권한 분류기에 막혀 로컬 squash + push로 동일하게 반영(release/*는 룰셋 없음). `main`·`dev` PR 머지도 같은 이유로 사용자가 직접 실행했다.
- 역류: `release/v0.2.6 → dev` Merge Commit(PR #91 → dev `d847fef`).

## v0.2.5 — 2026-08-21 배포 완료

- 범위: v0.2.4 이후 dev 누적 = KNK-829(개발 환경 자동 배포) + KNK-833(인물 단위 스토리 제작 입력) + 버전 올림.
  - **KNK-833은 외부 요청 계약 변경.** 스토리라인·컴파일 요청의 `protagonist_tags`·`supporting_tags`를 `protagonist`·`supporting_characters[]`로 교체하고, 입력 인물의 이름·성별·특징 반영과 인물 누락 부분 재호출을 추가했다.
  - KNK-829는 `dev` push에서만 ECS 개발 배포를 실행한다. 운영 `main` 배포는 기존 ECR·SSM 경로를 유지한다.
- **AI·백엔드 동시 전환.** 백엔드 v0.2.12가 KNK-846으로 새 AI 요청 계약을 반영했다(PR #198, main 머지 `0a58695`). AI만 이전 버전으로 롤백하면 요청 필드가 다시 어긋나므로 롤백도 두 서비스를 함께 맞춘다.
- PR #82 `[KNK-746] Release: v0.2.5 배포` → main 머지 `60cc18e`(Merge Commit) → 운영 워크플로의 테스트·이미지 health·ECR 빌드·SSM AI 배포 전부 success(run `32455858686`).
- 검증: `prod-health.sh 0.2.5` → `{"status":"ok","version":"0.2.5"}`. 태그 `v0.2.5` → `60cc18e`.
- QA: `qa.sh` 유닛·API 580 passed·5 skipped, 라이브 `tests/integration` 7 passed(실제 LLM, 92.74초), 종료코드 0. `infra-check.sh`도 종료코드 0.
- 배포 전 적대적 리뷰 3관점(계약·환경변수/기동·런타임) — 차단 요인 없음. PR #80에서 보류한 부분 재호출 전체 시간 제한, 겹치는 이름 판정, 이름 없는 인물 수·주변 인물 성별 코드 강제는 후속 사안으로 유지.
- 역류: `release/v0.2.5 → dev` Merge Commit(PR #83 → dev `eadd4e5`).

## v0.2.2 — 2026-07-23 배포 완료

- 범위: v0.2.1 이후 dev 누적 = KNK-312(제품 코드) + KNK-658(스킬·문서 재정비) + 버전 올림.
  - **KNK-312 = 유일한 동작 변경.** 스토리라인 502 방지 — invalid 응답(깨진 JSON·빈 응답·계약 위반) 최대 2회 재호출(전체 60초 상한), stories 계약 검증(3편·항목 스키마·추천 3개), id 순서 정규화(1·2·3, genre와 같은 덮어쓰기).
  - KNK-658은 `.agents/skills/`·`CLAUDE.md`·`.gitignore` 등 개발 도구·문서라 도커 이미지에 안 실린다(Dockerfile은 `src`·`prompt`·`pyproject.toml`만 COPY).
- **AI 단독 배포(3자 동시 아님).** 외부 계약 무변경 — 응답 형태 그대로, `meta.retry_count`는 v0.2.1에도 있던 필수 필드(값만 실제 재호출 횟수로 채움). 롤백도 단독으로 안전.
  - **행동 변화 주의**: 개수 검증이 엄격해져, 이전엔 200으로 나가던 off-count 응답(2·4편, 추천 2·4개)이 재호출→502가 될 수 있다. 스키마엔 개수 제약이 없어 v0.2.1은 통과시켰다. 의도된 계약(클라이언트가 정확히 3개 렌더). 배포 후 `retry_count`로 빈도 관측.
- PR #64 `[KNK-680] Release: v0.2.2 배포` → main 머지 `4624fc2`(Merge Commit) → 배포 워크플로 4잡 전부 success.
- 검증: `prod-health.sh 0.2.2` → `{"status":"ok","version":"0.2.2"}`. 태그 `v0.2.2` → `4624fc2`.
- QA: `qa.sh` 유닛 212 passed·5 skipped, 라이브 `tests/integration` **7 passed**(실제 DeepSeek, 103초), 종료코드 0.
- 배포 전 적대적 리뷰 3관점 + 보안: 계약·환경변수/기동·런타임·`.agents` 인프라 식별자(자리표시자뿐) — 차단 요인 없음.
- **머지 충돌**(예상됨): main(v0.2.1)과 release가 v0.2.1 Squash 역류로 갈려 세 버전 파일(0.2.1 vs 0.2.2)이 충돌. `origin/main`을 release로 병합(`56d6a5f`)해 v0.2.2 채택으로 해소.
- 역류: `release → dev` **Merge Commit**(PR #65 → dev `70a272c`). 이번엔 룰셋이 이미 `["squash","merge"]`라 정상 처리. **이력 재수렴 확인** — release tip `56d6a5f`가 main·dev 양쪽의 공통 조상이 됐다. v0.2.1의 Squash 분기가 이번 Merge 역류로 해소됐고, 다음 릴리스부터 이 버전-파일 충돌은 재발하지 않는다.

## v0.2.1 — 2026-07-22 배포 완료

- 범위: v0.2.0 이후 dev 7커밋 + 릴리스 중 수정 1건 = 69파일(KNK-554·574·595·610·**625**·624·650·656).
  - **KNK-625 선택지 전용 API 분리 = 외부 계약 변경.** `/chat/turns`의 `completed.choices`가 빈 배열로 고정되고 `POST /chat/choices`가 신설됐다.
  - KNK-624·650 Langfuse 연동 + 활성화 가드(JP·prod) — 키 미주입이라 **no-op으로 기동**(의도된 상태).
  - KNK-656(릴리스 중 `fix/` → release, Squash): `app_version` 0.1.0→**0.2.1**, `.env.example`의 Langfuse HOST 안내 정정.
- **3자 동시 배포**: AI v0.2.1 → API 서버 v0.2.2(KNK-647) → 웹 v0.2.5(KNK-655). 백엔드가 KNK-645로 선택지 stopgap을 제거해서 **프론트까지 나가야** 선택지가 복구된다. 배포 사이 구간에는 선택지 버튼만 안 보이는 degrade(채팅 본문·전송은 정상).
- **롤백 주의**: 배포 후 AI만 되돌리면 백엔드가 부르는 `/chat/choices`가 사라져 선택지가 계속 502. **롤백도 3자 동시.**
- PR #59 `[KNK-656] Release: v0.2.1 배포` → main 머지 `4bc9c1a`(Merge Commit) → 자동 배포 4잡 전부 success. ECR에 `4bc9c1a`+`latest` push.
- 검증: SSM probe → `{"status":"ok","version":"0.2.1"}`. 태그 `v0.2.1` → `4bc9c1a`.
- QA: `scripts/test.sh` 196 passed·5 skipped, `--live tests/integration` **7 passed**(실제 DeepSeek), 도커 이미지 기동 health 200.
- 배포 전 적대적 리뷰 3관점(계약·환경변수/기동·런타임) — 3자 동시 배포 조건에서 차단 요인 없음.
- 배포 전 실측: Secrets Manager에 Langfuse 키 **없음**(기존 8키만) → 이 릴리스로 Langfuse가 켜질 수 없음을 확인.
- 역류: `release → dev` 필요(KNK-656이 release에만 있었음). 합의는 Merge Commit이지만 **dev 룰셋에 막혀 이번만 Squash로 처리**(PR #60 → dev `604b68a`). 내용은 같고 커밋 해시만 main과 다르다.
  - 배포 직후 룰셋을 `["squash","merge"]`로 고쳐 **정합화 완료**. 다음 배포부터는 Merge Commit.

## v0.2.0 — 2026-07-11 배포 완료

- 범위: v0.1.1 이후 dev 9커밋(KNK-413·416·417·420·422·457·465·530·532).
- PR #50 `[KNK-564] Release: v0.2.0 배포` → main 머지 `b7fa316`. 태그 `v0.2.0`.
- QA: 도커 pytest 122 passed(라이브 포함), health·compile·chat 실호출 통과. 역류 불필요(release==dev).

## v0.1.1 — 2026-07-04 배포 완료

- PR #40 `[KNK-431] Release: v0.1.1 배포` → main 머지 `df96fbf`. 태그 `v0.1.1`.
- 당시 이력을 남기지 않아 세부는 PR #40 참조(2026-07-22에 뒤늦게 채움).

## v0.1.0 — 2026-06-27 배포 완료 (첫 배포)

- 범위: KNK-269 포함(dev 전체, main 대비 30커밋/67파일).
- PR #33 `[KNK-293] Release: v0.1.0 배포` → main 머지 `bec3f3e`(Merge Commit) → 자동 배포 성공. ECR `latest`+`bec3f3e` push, EC2 health 게이트 통과.
- 태그 `v0.1.0` → `bec3f3e`.
- 역류 불필요(release==dev). QA: health·storylines·compile·chat 실호출 통과.
- Gemini PR 리뷰 7건 = 전부 배포 비차단(`reference.md` §8 백로그로 이관).
- 이때 확인된 것: 인프라 담당이 말한 "배포 시 명령어 한 번"은 **불필요**했다. main 머지만으로 자동 배포가 끝난다.
