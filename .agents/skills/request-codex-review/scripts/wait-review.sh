#!/usr/bin/env bash
# Codex 응답 도착까지 폴링 — request-review.sh가 남긴 상태 파일을 이어받는다.
#
#   bash .agents/skills/request-codex-review/scripts/wait-review.sh <PR번호> [--max-wait <초>]
#
# 기본 대기 540초(9분). Bash 도구 상한이 10분이라 그 안에 끝나게 잡았다.
# 아직 안 왔으면(3) 같은 명령을 그대로 다시 실행하면 이어서 기다린다.
#
# 종료코드: 0  = 봇 thumbs-up (추가 지적 없음, 확정)
#           10 = 정식 리뷰 도착  (본문 확인 필요 -> show-review.sh)
#           11 = 이슈 코멘트 도착(본문 확인 필요 -> show-review.sh)
#           3  = 아직 안 옴 / 1 = 실패 / 2 = 사용법 오류
# 왜 세 신호를 다 보는지는 ../reference.md §1
set -uo pipefail

MAX_WAIT=540
PR=""
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    --max-wait) MAX_WAIT="${2:-}"; shift 2 || { echo "FAIL: --max-wait 뒤에 초를 주세요." >&2; exit 2; } ;;
    -*) echo "FAIL: 모르는 인자 '$1'. 쓸 수 있는 것: --max-wait, --help" >&2; exit 2 ;;
    *) [ -z "$PR" ] || { echo "FAIL: PR 번호는 하나만 받습니다." >&2; exit 2; }; PR="$1"; shift ;;
  esac
done
[[ "$PR" =~ ^[0-9]+$ ]] || { echo "FAIL: PR 번호를 숫자로 넘기세요. 예: wait-review.sh 57" >&2; exit 2; }
[[ "$MAX_WAIT" =~ ^[0-9]+$ ]] || { echo "FAIL: --max-wait 값이 숫자가 아닙니다: '$MAX_WAIT'" >&2; exit 2; }

STATE="${TMPDIR:-/tmp}/codex-review-$PR.env"
[ -f "$STATE" ] || { echo "FAIL: 상태 파일이 없습니다($STATE). request-review.sh를 먼저 실행하세요." >&2; exit 1; }

# 상태 파일을 source하지 않는다 — 그러면 파일 내용이 그대로 명령으로 실행된다.
# 아는 키만, 안전한 문자만 받아들인다.
REPO=""; PREV_REVIEWS=""; PREV_COMMENTS=""; CMTID=""; HEAD_SHA=""
while IFS= read -r line; do
  case "$line" in
    REPO=*|PR=*|PREV_REVIEWS=*|PREV_COMMENTS=*|CMTID=*|HEAD_SHA=*) ;;
    *) continue ;;
  esac
  key=${line%%=*}; value=${line#*=}
  case "$value" in ''|*[!A-Za-z0-9/._-]*) continue ;; esac
  printf -v "$key" '%s' "$value"
done < "$STATE"
for v in REPO PREV_REVIEWS PREV_COMMENTS CMTID; do
  [ -n "${!v}" ] || { echo "FAIL: 상태 파일에 $v 가 없습니다($STATE). request-review.sh를 다시 실행하세요." >&2; exit 1; }
done

# 부분 일치로 보면 `evil-codex-connector-fan` 같은 계정도 봇으로 세어 도착을 오판한다.
BOT_FILTER='select(.user.login=="chatgpt-codex-connector[bot]" or .user.login=="chatgpt-codex-connector")'
INTERVAL=30
ROUNDS=$(( MAX_WAIT / INTERVAL ))
[ "$ROUNDS" -ge 1 ] || ROUNDS=1
ALLFAIL=0   # 세 조회가 모두 실패한 라운드가 연속 몇 번인지

codex_count() {  # $1=endpoint, $2=jq  (gh 실패는 삼키지 않고 전파)
  local pages
  pages=$(gh api --paginate "$1" --jq "$2") || return 1
  printf '%s\n' "$pages" | awk '{s+=$1} END{print s+0}'
}

echo ">>> PR #$PR 응답 대기 (최대 ${MAX_WAIT}초, ${INTERVAL}초 간격) — 기준선 reviews=$PREV_REVIEWS comments=$PREV_COMMENTS"
for i in $(seq 1 "$ROUNDS"); do
  # 조회 실패는 이전 값을 유지한다 — 0으로 떨어뜨리면 도착을 놓치거나 오탐한다.
  # 다만 셋 다 실패하는 라운드가 이어지면 '아직 안 옴'이 아니라 장애다(아래에서 걸러낸다).
  fails=0
  rv=$(codex_count "repos/$REPO/pulls/$PR/reviews?per_page=100"   "[.[]|$BOT_FILTER]|length")   || { rv=$PREV_REVIEWS;  fails=$((fails+1)); }
  cm=$(codex_count "repos/$REPO/issues/$PR/comments?per_page=100" "[.[]|$BOT_FILTER]|length")   || { cm=$PREV_COMMENTS; fails=$((fails+1)); }
  # thumbs-up은 반드시 봇 것만 — 사람이 먼저 누르면 오탐한다
  tu=$(codex_count "repos/$REPO/issues/comments/$CMTID/reactions?per_page=100" \
       "[.[]|select(.content==\"+1\")|$BOT_FILTER]|length") || { tu=0; fails=$((fails+1)); }

  if [ "$fails" -eq 3 ]; then
    ALLFAIL=$((ALLFAIL + 1))
    # 변수 뒤에 한글이 바로 붙으면 bash가 이름의 일부로 읽는다 — 반드시 ${}로 감싼다.
    echo "  조회 전부 실패 (${ALLFAIL}회 연속)"
    if [ "$ALLFAIL" -ge 3 ]; then
      echo "FAIL: GitHub 조회가 3회 연속 전부 실패했습니다 — 응답이 늦은 게 아니라 장애입니다." >&2
      echo "      'gh auth status'와 네트워크를 확인하세요." >&2
      exit 1
    fi
  else
    ALLFAIL=0
    if [ "${rv:-0}" -gt "$PREV_REVIEWS" ]; then
      echo "REVIEW_ARRIVED reviews=$rv — 정식 리뷰입니다(지적 있음). show-review.sh로 본문을 읽으세요."
      exit 10
    fi
    if [ "${cm:-0}" -gt "$PREV_COMMENTS" ]; then
      echo "COMMENT_ARRIVED comments=$cm — 이슈 코멘트입니다. 지적 없음일 수도, 질문이 섞였을 수도 있으니 본문을 읽으세요."
      exit 11
    fi
    if [ "${tu:-0}" -ge 1 ]; then
      echo "THUMB_UP — 봇이 호출 코멘트에 thumbs-up을 눌렀습니다. 추가 지적 없음(확정)."
      exit 0
    fi
    echo "  대기 $i/$ROUNDS: reviews=$rv comments=$cm thumb=$tu"
  fi
  [ "$i" -lt "$ROUNDS" ] && sleep "$INTERVAL"
done

echo "NOT_YET — ${MAX_WAIT}초 안에 응답이 없습니다. 같은 명령을 다시 실행하면 이어서 기다립니다."
exit 3
