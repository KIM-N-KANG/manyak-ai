# 스킬 배치 규칙

이 레포의 에이전트 스킬이 어디에 있고 어떻게 나뉘는지 정한다. 스킬을 만들거나 고칠 때만 읽으면 된다.

## 한 소스, 두 에이전트

스킬 정본은 `.agents/skills/`다. Codex는 이 디렉터리를 직접 읽고, Claude Code는
`.claude/skills/*`가 `.agents/skills/*`를 가리키는 심링크로 같은 스킬을 읽는다.

새 스킬을 추가할 때는 `.agents/skills/<이름>/SKILL.md`를 만들고 `.claude/skills/<이름>` 심링크를 건다.

## 프로젝트 스킬과 하네스 공용 스킬

구조로 판단한다. `.agents/skills/<이름>`이

- **실디렉터리면 프로젝트 스킬** — 이 레포가 정본이다.
  예: create-jira-subtasks, planning, prompt-authoring, request-codex-review, readability-audit
- **심링크면 하네스 공용 스킬** — 정본은 `../../../knk-harness/.agents/skills/<이름>`이다.
  예: create-branch, create-commit, create-pr, karpathy-guidelines, technical-writing

하네스 공용 스킬은 복사하지도, 수정하지도 않는다. 복사하면 하네스 개정을 못 따라간다.

## 스킬은 폴더째 gitignore하지 않는다

전부 git으로 추적한다. 폴더째 무시하면 브랜치를 오갈 때 git이 파일을 지우는데, 무시 대상이라
다시 만들어주지도 없어졌다고 알려주지도 않는다 — 2026-07-22에 `SKILL.md` 3개가 그렇게
사라졌다(KNK-668).

비밀이 걸리면 폴더가 아니라 **값 단위로 막는다.** AWS 계정번호 같은 식별자는 스킬 문서에 적지 말고
`../manyak-terraform`(비공개)이나 GitHub Variables를 가리킨다. 선례: `release-deploy/reference.md`.

## 스킬은 파일 하나가 아니라 폴더 전체다

`SKILL.md`는 짧게 유지하고, 긴 설명은 `reference.md`, 실행 코드는 `scripts/`로 뺀다.
참고 문서는 필요할 때만 읽히고 스크립트는 내용 대신 실행만 되므로 컨텍스트를 아낀다.

선례: `release-deploy`(SKILL.md + reference.md·history.md·scripts/).

`scripts/`에 넣은 스크립트는 **종료코드가 판정**이 되게 만든다. 에이전트가 출력을 읽고
해석하는 대신 코드로 갈라지게 하려는 것이다.

## 형제 레포가 필요하다

하네스 공용 스킬과 제품 명세는 `../knk-harness`가 함께 체크아웃돼 있어야 동작한다.
이 레포만 클론하면 프로젝트 스킬만 로드되고 하네스 스킬·제품 명세는 빠진다.

**Windows 주의**: `git config core.symlinks true`(+ 개발자 모드나 관리자 권한) 없이 클론하면
심링크가 실제 링크가 아니라 대상 경로가 담긴 텍스트 파일로 체크아웃돼 스킬 로딩이 조용히 깨진다.
