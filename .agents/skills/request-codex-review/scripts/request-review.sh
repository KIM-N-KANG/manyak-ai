#!/usr/bin/env bash
# Codex 리뷰 호출 — PR을 ready로 올리고, 기준선을 기록하고, @codex review 코멘트를 단다.
#
#   bash .agents/skills/request-codex-review/scripts/request-review.sh <PR번호>
#
# 첫 호출과 재리뷰가 같은 명령이다(재리뷰는 상태 파일을 덮어쓴다).
# 종료코드: 0 = 호출 완료(이어서 wait-review.sh) / 1 = 실패 / 2 = 사용법 오류
# 왜 이렇게 짰는지는 ../reference.md
set -uo pipefail

case "${1:-}" in
  -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
esac
[ $# -eq 1 ] || { echo "FAIL: 인자는 PR 번호 하나입니다. 예: request-review.sh 57" >&2; exit 2; }
PR="$1"
[[ "$PR" =~ ^[0-9]+$ ]] || { echo "FAIL: PR 번호가 숫자가 아닙니다: '$PR'" >&2; exit 2; }

REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner) \
  || { echo "FAIL: 레포를 확인할 수 없습니다 — 'gh auth status'를 보세요." >&2; exit 1; }
STATE="${TMPDIR:-/tmp}/codex-review-$PR.env"
BOT_FILTER='select(.user.login|contains("codex-connector"))'

# 전 페이지를 훑어 Codex 봇 응답 수를 합산한다.
# gh 실패를 0으로 삼키면 기준선이 거짓 0으로 깔려 옛 코멘트를 새 응답으로 오인하므로, 실패는 그대로 전파한다.
codex_count() {  # $1=endpoint, $2=jq
  local pages
  pages=$(gh api --paginate "$1" --jq "$2") || return 1
  printf '%s\n' "$pages" | awk '{s+=$1} END{print s+0}'
}

# 1) draft면 ready로 — Codex는 draft PR을 아예 무시한다
DRAFT=$(gh pr view "$PR" --json isDraft --jq .isDraft) \
  || { echo "FAIL: PR #$PR 을 찾을 수 없습니다. PR이 없으면 create-pr이 먼저입니다." >&2; exit 1; }
if [ "$DRAFT" = "true" ]; then
  gh pr ready "$PR" || { echo "FAIL: ready 전환 실패" >&2; exit 1; }
  echo ">>> draft였던 PR #$PR 을 ready로 전환했습니다."
else
  echo ">>> PR #$PR 은 이미 ready입니다."
fi

# 2) 기준선 — 사람 리뷰·코멘트를 Codex 응답으로 오판하지 않도록 봇 것만 센다
PREV_REVIEWS=$(codex_count "repos/$REPO/pulls/$PR/reviews?per_page=100" "[.[]|$BOT_FILTER]|length") \
  || { echo "FAIL: 기준선(reviews) 조회 실패 — 중단합니다." >&2; exit 1; }
PREV_COMMENTS=$(codex_count "repos/$REPO/issues/$PR/comments?per_page=100" "[.[]|$BOT_FILTER]|length") \
  || { echo "FAIL: 기준선(comments) 조회 실패 — 중단합니다." >&2; exit 1; }

# 3) 호출 — 리뷰는 PR 본문이 아니라 코멘트로만 트리거된다
CMT_URL=$(gh pr comment "$PR" --body "@codex review") || { echo "FAIL: 코멘트 작성 실패" >&2; exit 1; }
CMTID=${CMT_URL##*-}   # .../pull/<PR>#issuecomment-<id> -> <id>
[[ "$CMTID" =~ ^[0-9]+$ ]] || { echo "FAIL: 코멘트 ID를 뽑지 못했습니다: $CMT_URL" >&2; exit 1; }

# 4) 이번 라운드의 head 커밋 — show-review.sh가 옛 라운드를 걸러내는 기준
HEAD_SHA=$(gh pr view "$PR" --json headRefOid --jq .headRefOid) \
  || { echo "FAIL: head SHA 조회 실패" >&2; exit 1; }

# 5) 다음 스크립트가 새 셸이라 변수를 못 물려받는다 — 파일로 넘긴다
cat > "$STATE" <<EOF
REPO=$REPO
PR=$PR
PREV_REVIEWS=$PREV_REVIEWS
PREV_COMMENTS=$PREV_COMMENTS
CMTID=$CMTID
HEAD_SHA=$HEAD_SHA
EOF

echo ">>> 호출했습니다. 기준선 reviews=$PREV_REVIEWS comments=$PREV_COMMENTS / 호출코멘트=$CMTID / head=${HEAD_SHA:0:7}"
echo ">>> 이어서: bash .agents/skills/request-codex-review/scripts/wait-review.sh $PR"
