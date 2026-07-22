---
name: release-deploy
description: manyak-ai를 운영에 배포하는 릴리즈 루틴을 수행할 때 사용합니다. dev에서 release/vX.Y.Z 브랜치를 파고, 배포 전 적대적 리뷰와 QA(유닛+라이브 LLM)를 거쳐, main에 Merge Commit으로 머지(=이 순간이 실제 배포)하고, 태그·release→dev 역류·브랜치 삭제까지 마무리합니다. "릴리즈 배포해줘", "release 브랜치 파서 배포", "v0.1.x 배포 진행", "배포 전에 문제 없는지 보고 배포까지"처럼 요청할 때 사용하세요. 사용자에게 묻는 것은 버전 번호와 main 머지 승인 두 가지뿐이고 나머지는 스킬이 알아서 수행합니다. 단, 일반 기능 PR(→create-pr), dev 머지용 커밋(→create-commit)에는 사용하지 않습니다.
---

# 릴리즈 배포 (release-deploy)

## 사용자에게 묻는 것은 딱 둘

1. **버전 번호** (vX.Y.Z) — 릴리스 Jira 티켓은 사용자가 직접 만드니, 그 **티켓 키**도 함께 받는다
2. **main 머지 승인** — 이 순간이 곧 운영 배포다. 되돌리기 어렵고, AI·백엔드·웹 3자 동시 배포라 타이밍은 사용자가 안다.

**그 밖의 것은 묻지 않고 진행한다.** 브랜치·버전 파일·적대적 리뷰·QA(라이브 LLM 포함)·
태그·역류·브랜치 삭제·문서 갱신 전부 스킬이 한다. 중간에 확인을 구하지 말고, 결과만 보고한다.
**단 Jira는 건드리지 않는다** — 릴리스 티켓의 생성도 완료 처리도 사용자 몫이다(§3-5).

**릴리스 QA의 라이브 LLM 호출은 상시 승인이다**(2026-07-22 사용자 결정). 과금 승인을 다시 묻지 않는다 —
배포 전에 반드시 확인해야 하는 것이라 묻는 게 의미가 없다. QA 밖의 실측(프롬프트 A/B, 실험 러너)은
종전대로 승인이 필요하다.

> `CLAUDE.md`·`AGENTS.md`의 실측 승인 원칙에도 이 예외가 명시돼 있다(KNK-668). 두 문서와 이 스킬이
> 같은 말을 한다 — 한쪽만 고치면 어긋나므로 바꿀 때는 셋을 함께 본다.

## 같이 쓰는 파일

경로는 레포 루트 기준이다. 레포의 `scripts/`(테스트 러너)와 헷갈리지 않게 항상 전체 경로로 부른다.

- `.agents/skills/release-deploy/reference.md` — 인프라 사실·동작 원리·브랜치 규칙·QA 상세·트러블슈팅. 해당 절만 펼쳐 본다.
- `.agents/skills/release-deploy/history.md` — 배포 이력. Phase A(범위)와 Phase D(기록)에서 읽는다.
- `.agents/skills/release-deploy/scripts/` — `qa.sh` · `watch-deploy.sh` · `prod-health.sh` · `infra-check.sh`.
  **읽지 말고 실행한다.** 넷 다 **종료코드가 판정**이다.

**레포가 PUBLIC이다.** AWS 계정번호·비밀값을 이 폴더 어디에도 적지 않는다 —
`../manyak-terraform`(비공개)이나 GitHub Variables를 가리킨다.

## 하드 룰

- **main 머지 전 사용자 승인.** 유일한 비가역 지점이다.
- 적대적 리뷰(Phase B)와 QA를 건너뛰지 않는다. 실패를 안은 채 다음으로 가지 않는다.
- `main`·`dev`에 직접 push하지 않는다(룰셋으로도 막혀 있다). `release/*`는 룰셋이 없으니 컨벤션으로 지킨다.
- 머지 방식을 바꾸지 않는다: `release→main`·`release→dev`는 Merge Commit, `feat/*→dev`·`fix/*→release`는 Squash.
- 시크릿 값을 화면·문서에 남기지 않는다.
- 실패·미실시를 숨기지 않는다. 라이브 QA를 못 돌렸으면 "실측 미실시"라고 적는다.

## Phase A — 준비

- [ ] `.agents/skills/release-deploy/history.md`로 직전 배포를 확인하고, dev 누적 커밋으로 배포 범위를 요약 보고
- [ ] 릴리스 Jira 티켓 키를 사용자에게 받는다 — **티켓은 사용자가 만든다. 이 스킬은 Jira를 건드리지 않는다**
      → `.agents/skills/release-deploy/reference.md` §3-5
- [ ] `git checkout dev && git pull` → `release/vX.Y.Z` 분기·push
- [ ] 패키지 버전 올리기 → `.agents/skills/release-deploy/reference.md` §3-2

## Phase B — 리뷰와 QA

- [ ] 적대적 리뷰 3관점 → `.agents/skills/release-deploy/reference.md` §4. 차단급이면 멈추고 `fix/KNK-xx → release`로 해소 후 재검토
- [ ] `bash .agents/skills/release-deploy/scripts/qa.sh` — 유닛과 라이브를 한 번에 돌린다.
      **종료코드 0일 때만 진행한다.** 1=테스트 실패 / 2=인자 오류 / 3=라이브 미실시(게이트 불충분).
      3이면 라이브를 채워 0을 받아낸 뒤 진행한다. 끝내 못 채우면 **멈추고 사용자에게 판단을 구한다**
      — "실측 미실시"를 적었다고 통과가 되는 것이 아니다.

## Phase C — 배포 (유일한 확인 게이트)

- [ ] `gh pr create --base main --head release/vX.Y.Z --title "[KNK-xx] Release: vX.Y.Z 배포" --body-file <파일>`
      **`--body`(또는 `--body-file`)를 반드시 준다.** 없으면 gh가 본문을 물어보려고 멈춰,
      머지 승인 게이트에 닿기도 전에 끊긴다. 본문은 배포 범위·3자 동시 배포 여부·QA 결과를 담는다
- [ ] **여기서 멈추고 머지 승인을 받는다**
- [ ] Merge Commit으로 머지 ← 이 순간이 배포
- [ ] `bash .agents/skills/release-deploy/scripts/watch-deploy.sh` — 배포 워크플로를 끝까지 감시.
      `gh run watch`를 맨손으로 부르지 않는다(run ID 없이는 비대화형에서 즉시 실패한다)
- [ ] `bash .agents/skills/release-deploy/scripts/prod-health.sh <버전>` — 버전을 인자로 넘겨야 "새 이미지가 실제로 떴는지"까지 검사한다.
      종료코드 0이 아니면 `.agents/skills/release-deploy/reference.md` §7

## Phase D — 사후

- [ ] `git checkout main && git pull` 후 `git tag -a vX.Y.Z -m "vX.Y.Z 배포" && git push origin vX.Y.Z`
      (main을 먼저 받는 이유 → `.agents/skills/release-deploy/reference.md` §3-1)
- [ ] `release → dev` 역류 PR(Merge Commit). release==dev면 불필요
- [ ] `.agents/skills/release-deploy/history.md`에 이번 배포 기록. 새로 확인된 사실은
      `.agents/skills/release-deploy/reference.md`에 반영.
      **이 파일들은 git 추적 대상이다** — `main`에 직접 push할 수 없으니 `docs/KNK-xx-...` 브랜치를
      따로 파서 `dev`로 PR을 올린다. 브랜치는 **`dev`에서 딴다**(`git checkout dev && git pull` 선행 —
      역류를 했다면 그것이 끝난 뒤의 `dev`다). 바로 위에서 `main`을 받아 둔 상태라 거기서 따면,
      문서 PR이 릴리스 머지를 통째로 `dev`에 다시 싣는다. 브랜치 삭제(아래)보다 **먼저** 한다
- [ ] `git push origin --delete release/vX.Y.Z`
- [ ] 릴리스 티켓 완료 처리는 **사용자 몫** — 상태를 대신 바꾸지 말고, 배포 결과만 보고한다

## 완료 보고

버전·범위·머지 커밋·태그 / 적대적 리뷰 결과(해소·보류) / QA 결과 그대로(라이브 미실시면 명시) /
운영 health 검증과 배포 잡 상태 / 사후 정리 각 항목의 완료 여부.
