#!/usr/bin/env bash
# main 머지로 시작된 배포 워크플로를 끝까지 지켜본다.
#
#   watch-deploy.sh            # 최신 main 실행을 찾아 감시
#   watch-deploy.sh <run-id>   # 특정 실행을 감시
#
# `gh run watch`를 맨손으로 부르면 안 된다 — run ID 없이 부르면 비대화형 환경에서
# "run ID required when not running interactively"로 즉시 실패한다(실측 확인).
# 그래서 여기서 최신 main 실행을 찾아 넘기고, --exit-status로 실패를 종료코드에 싣는다.
#
# 종료코드 0 = 배포 워크플로 성공.
set -uo pipefail

REPO="${GH_REPO:-KIM-N-KANG/manyak-ai}"

case "${1:-}" in
  -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
esac
[ $# -le 1 ] || { echo "FAIL: 인자는 run ID 하나입니다." >&2; exit 2; }
RUN_ID="${1:-}"
# 인자 검증이 없으면 `--help` 같은 값이 그대로 run ID로 넘어가고, gh가 0으로 끝나
# "배포 성공"으로 보고된다(실측 확인).
[ -z "$RUN_ID" ] || [[ "$RUN_ID" =~ ^[0-9]+$ ]] \
  || { echo "FAIL: run ID는 숫자여야 합니다: '$RUN_ID'" >&2; exit 2; }

command -v gh >/dev/null || { echo "FAIL: gh CLI가 없습니다" >&2; exit 1; }

# 숫자이기만 하면 받아주면, dev의 지난 성공 실행 ID를 넘겨도 "배포 성공"이 된다.
# 손으로 넘긴 ID도 자동 탐색과 같은 조건(main 브랜치 + Docker 워크플로)을 만족해야 한다.
if [ -n "$RUN_ID" ]; then
  meta=$(gh run view "$RUN_ID" -R "$REPO" --json headBranch,name,event \
           --jq '"\(.headBranch)\t\(.name)\t\(.event)"' 2>/dev/null) \
    || { echo "FAIL: run $RUN_ID 을 찾을 수 없습니다" >&2; exit 1; }
  br=${meta%%$'\t'*}; rest=${meta#*$'\t'}; nm=${rest%$'\t'*}; ev=${rest##*$'\t'}
  [ "$br" = "main" ] \
    || { echo "FAIL: run $RUN_ID 은 '$br' 브랜치입니다 — 배포는 main에서만 일어납니다" >&2; exit 2; }
  printf '%s' "$nm" | grep -qi docker \
    || { echo "FAIL: run $RUN_ID 은 '$nm' 워크플로입니다 — 배포(Docker)가 아닙니다" >&2; exit 2; }
  [ "$ev" = "push" ] \
    || { echo "FAIL: run $RUN_ID 은 '$ev' 실행입니다 — deploy 잡은 push에서만 돕니다" >&2; exit 2; }
fi

if [ -z "$RUN_ID" ]; then
  # '최신 main 실행'을 그냥 집으면, 배포 워크플로가 아직 안 생겼을 때 지난 성공 실행을
  # 이번 배포로 착각한다. 지금 main이 가리키는 커밋의 실행만 인정한다.
  WANT_SHA=$(gh api "repos/$REPO/commits/main" --jq .sha 2>/dev/null)
  [ -n "$WANT_SHA" ] || { echo "FAIL: main의 최신 커밋을 확인하지 못했습니다" >&2; exit 1; }
  echo "기다리는 대상: main ${WANT_SHA:0:7}"
  for i in $(seq 1 20); do
    # 배포 워크플로(Docker) **만** 인정한다. 같은 커밋에 유닛 테스트 같은 다른 워크플로가
    # 먼저 등록되는데, 그걸 집으면 배포가 아직 안 끝났는데 "배포 성공"으로 보고하게 된다.
    # event까지 본다. 워크플로가 workflow_dispatch도 허용하는데, deploy 잡은
    # `github.event_name == 'push'`로 막혀 있다 — 수동 실행분을 집으면 배포가 아예 안 돌았는데
    # 워크플로는 success라 "배포 성공"으로 보고하게 된다.
    RUN_ID=$(gh run list -R "$REPO" --branch main -L 30 --json databaseId,headSha,name,event \
      --jq "[.[] | select(.headSha==\"$WANT_SHA\") | select(.event==\"push\")
                 | select(.name | test(\"Docker\";\"i\"))]
            | .[0].databaseId // empty" 2>/dev/null)
    [ -n "$RUN_ID" ] && break
    echo "main ${WANT_SHA:0:7} 의 배포(Docker) 워크플로를 기다리는 중 ($i/20)"
    sleep 6
  done
  [ -n "$RUN_ID" ] \
    || { echo "FAIL: main ${WANT_SHA:0:7} 의 배포(Docker) 워크플로를 2분 안에 찾지 못했습니다" >&2; exit 1; }
fi

echo "== 배포 워크플로 감시 (run $RUN_ID, $REPO) =="
gh run view "$RUN_ID" -R "$REPO" --json displayTitle,headSha,status \
  --jq '"제목: \(.displayTitle)\ncommit: \(.headSha[0:7])\n상태: \(.status)"'
echo

# --exit-status: 워크플로가 실패하면 이 명령도 실패한다(없으면 실패해도 0으로 끝난다).
gh run watch "$RUN_ID" -R "$REPO" --exit-status
rc=$?

echo
if [ "$rc" -eq 0 ]; then
  echo "== 배포 워크플로 성공 — 다음은 운영 헬스체크 =="
  echo "   bash .agents/skills/release-deploy/scripts/prod-health.sh <버전>"
else
  echo "== 배포 워크플로 실패(exit $rc) =="
  echo "   원인: gh run view $RUN_ID -R $REPO --log-failed"
  echo "   대응: reference.md 7절"
fi
exit "$rc"
