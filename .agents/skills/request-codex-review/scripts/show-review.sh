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
  -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
esac
[ $# -eq 1 ] || { echo "FAIL: 인자는 PR 번호 하나입니다. 예: show-review.sh 57" >&2; exit 2; }
PR="$1"
[[ "$PR" =~ ^[0-9]+$ ]] || { echo "FAIL: PR 번호가 숫자가 아닙니다: '$PR'" >&2; exit 2; }

STATE="${TMPDIR:-/tmp}/codex-review-$PR.env"
[ -f "$STATE" ] || { echo "FAIL: 상태 파일이 없습니다($STATE). request-review.sh를 먼저 실행하세요." >&2; exit 1; }

# 상태 파일을 source하지 않는다 — 그러면 파일 내용이 그대로 명령으로 실행된다.
REPO=""; HEAD_SHA=""; CMTID=""
while IFS= read -r line; do
  case "$line" in
    REPO=*|PR=*|PREV_REVIEWS=*|PREV_COMMENTS=*|CMTID=*|HEAD_SHA=*) ;;
    *) continue ;;
  esac
  key=${line%%=*}; value=${line#*=}
  case "$value" in ''|*[!A-Za-z0-9/._-]*) continue ;; esac
  printf -v "$key" '%s' "$value"
done < "$STATE"
[ -n "$REPO" ]     || { echo "FAIL: 상태 파일에 REPO가 없습니다($STATE)." >&2; exit 1; }
[ -n "$HEAD_SHA" ] || { echo "FAIL: 상태 파일에 HEAD_SHA가 없습니다($STATE)." >&2; exit 1; }
[ -n "$CMTID" ]    || { echo "FAIL: 상태 파일에 CMTID가 없습니다($STATE)." >&2; exit 1; }

# 부분 일치로 보면 `evil-codex-connector-fan` 같은 계정의 글도 Codex 결과로 읽게 된다.
BOT_FILTER='select(.user.login=="chatgpt-codex-connector[bot]" or .user.login=="chatgpt-codex-connector")'

# 상태 파일의 head가 PR의 현재 head와 어긋나면 정식·인라인 리뷰가 전부 걸러져 빈 화면이 나온다.
# 그런데 "지적 없음"일 때도 똑같이 빈 화면이라, 경고가 없으면 통과로 오독한다.
# 리뷰를 요청한 뒤 커밋을 하나 더 push하면 바로 이 상황이 된다.
CUR_SHA=$(gh pr view "$PR" --json headRefOid --jq .headRefOid) || CUR_SHA=""
if [ -z "$CUR_SHA" ]; then
  echo "!!! 주의: PR의 현재 head를 조회하지 못했습니다. 아래 결과가 최신 커밋 것인지 확인하지 못했습니다." >&2
elif [ "$CUR_SHA" != "$HEAD_SHA" ]; then
  echo "!!! 경고: 리뷰를 요청한 커밋과 PR의 현재 커밋이 다릅니다." >&2
  echo "      요청 당시: ${HEAD_SHA:0:7} / 현재: ${CUR_SHA:0:7}" >&2
  echo "      아래 '정식 리뷰'와 '인라인 코멘트'가 비어 보여도 지적이 없다는 뜻이 아닙니다." >&2
  echo "      request-review.sh $PR 를 다시 실행해 현재 커밋으로 리뷰를 받으세요." >&2
fi

echo "############ 정식 리뷰 (head ${HEAD_SHA:0:7} 기준) ############"
gh api --paginate "repos/$REPO/pulls/$PR/reviews?per_page=100" \
  --jq ".[] | $BOT_FILTER | select(.commit_id==\"$HEAD_SHA\") | {state, submitted_at, body}" \
  || { echo "FAIL: 리뷰 조회 실패" >&2; exit 1; }

echo "############ 이번 라운드 Codex 이슈 코멘트 (호출 코멘트 $CMTID 이후) ############"
# 두 가지를 함께 지킨다.
#  - --paginate는 jq를 페이지마다 따로 적용하므로 sort_by|last 같은 집계를 쓰지 않는다
#    (썼더니 "페이지별 최근"이 페이지 수만큼 나와 옛 라운드 요약이 최신인 척 섞였다).
#  - 정식·인라인 리뷰는 head 커밋으로 좁히는데 코멘트만 안 좁히면 지난 라운드의 "no issues"가
#    이번 결과처럼 보인다. 코멘트 id는 증가하므로 호출 코멘트보다 큰 것만 이번 라운드다.
LATEST=$(gh api --paginate "repos/$REPO/issues/$PR/comments?per_page=100" \
  --jq ".[] | $BOT_FILTER | select(.id > $CMTID) | {created_at, body}") \
  || { echo "FAIL: 코멘트 조회 실패" >&2; exit 1; }
if [ -z "$LATEST" ]; then
  echo "(이번 라운드에 온 Codex 이슈 코멘트 없음)"
else
  printf '%s\n' "$LATEST" | tail -1
fi

echo
echo "############ 인라인(파일 위) 코멘트 (head ${HEAD_SHA:0:7} 기준) ############"
gh api --paginate "repos/$REPO/pulls/$PR/comments?per_page=100" \
  --jq ".[] | $BOT_FILTER | select((.commit_id==\"$HEAD_SHA\") or (.original_commit_id==\"$HEAD_SHA\")) | {path, line, body}" \
  || { echo "FAIL: 인라인 코멘트 조회 실패" >&2; exit 1; }
