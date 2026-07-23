# manyak-ai 배포 이력 (history)

> 배포할 때마다 여기에 한 항목을 추가한다. 절차는 `SKILL.md`, 사실·명령은 `reference.md`.
> 레포가 PUBLIC이다. 계정번호·비밀값은 적지 않는다(키 이름과 절차까지만).

---

## 다음 배포 때 볼 것

- 흐름은 늘 같다: `dev → release/vX.Y.Z → main`(Merge Commit) → 자동 배포 → 태그 → 역류(Merge Commit) → 브랜치 삭제.
- **Langfuse 활성화(KNK-654)는 릴리스가 아니다.** 코드는 이미 나가 있고 키만 없어 no-op이다. 순서:
  1. `manyak-terraform` apply — **EC2 교체**가 일어난다(다운타임 + 이미지 핀이 `:latest`로 리셋되는 것 주의).
  2. 백엔드 KNK-621 배포 — 커스텀 장르 400 차단. 안 하면 사용자 자유입력이 트레이스 태그로 유입된다.
  3. Secrets에 `AI_LANGFUSE_PUBLIC_KEY`·`AI_LANGFUSE_SECRET_KEY`·`AI_LANGFUSE_HOST` 추가 — **기존 8키를 전부 포함해 저장**한다(`put-secret-value`는 전체 덮어쓰기).
     - **HOST는 정확히 `https://jp.cloud.langfuse.com`이어야 한다.** 코드가 이 문자열과 일치할 때만 켠다(`src/core/langfuse.py`의 `_ALLOWED_HOST`, 후행 슬래시는 무시). ⚠️ `.env.example`의 기본값은 EU(`https://cloud.langfuse.com`)라 그대로 복사하면 **에러 로그만 남고 조용히 꺼진 채 기동**한다.
  4. SSM `manyak-prod-ai-deploy`로 AI만 재배포.
  5. 기동 로그에서 `Langfuse 활성 — host=… env=prod` 확인 + server 컨테이너에 `AI_LANGFUSE_*`가 없는지 확인.

---

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
