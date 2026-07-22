#!/usr/bin/env bash
# 이번 라운드의 Codex 응답만 뽑아 보여준다 — 정식 리뷰 / 이슈 코멘트 / 인라인 코멘트 셋 다.
#
#   bash .agents/skills/request-codex-review/scripts/show-review.sh <PR번호>
#
# 옛 라운드 리뷰와 사람 리뷰를 현재 결과로 오인하지 않도록, 상태 파일의 head 커밋으로 좁힌다.
# "지적 없음" 응답은 이슈 코멘트로만 오므로 정식 리뷰가 비어도 코멘트를 반드시 본다.
# 종료코드: 0 = 출력 완료 / 1 = 실패 / 2 = 사용법 오류
set -uo pipefail

case "${1:-}" in
  -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
esac
[ $# -eq 1 ] || { echo "FAIL: 인자는 PR 번호 하나입니다. 예: show-review.sh 57" >&2; exit 2; }
PR="$1"
[[ "$PR" =~ ^[0-9]+$ ]] || { echo "FAIL: PR 번호가 숫자가 아닙니다: '$PR'" >&2; exit 2; }

STATE="${TMPDIR:-/tmp}/codex-review-$PR.env"
[ -f "$STATE" ] || { echo "FAIL: 상태 파일이 없습니다($STATE). request-review.sh를 먼저 실행하세요." >&2; exit 1; }
# shellcheck source=/dev/null
source "$STATE"
: "${REPO:?상태 파일에 REPO가 없습니다}" "${HEAD_SHA:?상태 파일에 HEAD_SHA가 없습니다}"

BOT_FILTER='select(.user.login|contains("codex-connector"))'

echo "############ 정식 리뷰 (head ${HEAD_SHA:0:7} 기준) ############"
gh api --paginate "repos/$REPO/pulls/$PR/reviews?per_page=100" \
  --jq ".[] | $BOT_FILTER | select(.commit_id==\"$HEAD_SHA\") | {state, submitted_at, body}" \
  || { echo "FAIL: 리뷰 조회 실패" >&2; exit 1; }

echo
echo "############ 가장 최근 Codex 이슈 코멘트 ############"
gh api --paginate "repos/$REPO/issues/$PR/comments?per_page=100" \
  --jq "[.[] | $BOT_FILTER] | sort_by(.created_at) | last | {created_at, body}" \
  || { echo "FAIL: 코멘트 조회 실패" >&2; exit 1; }

echo
echo "############ 인라인(파일 위) 코멘트 (head ${HEAD_SHA:0:7} 기준) ############"
gh api --paginate "repos/$REPO/pulls/$PR/comments?per_page=100" \
  --jq ".[] | $BOT_FILTER | select((.commit_id==\"$HEAD_SHA\") or (.original_commit_id==\"$HEAD_SHA\")) | {path, line, body}" \
  || { echo "FAIL: 인라인 코멘트 조회 실패" >&2; exit 1; }
