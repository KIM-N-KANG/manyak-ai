#!/usr/bin/env bash
# 스펙 드리프트 감지 — 5-ai-server.md의 기준 코드 SHA 이후 dev에 쌓인 변경을 뽑는다.
#
#   bash .agents/skills/sync-ai-spec/scripts/detect-drift.sh
#
# 감시 경로와 SHA를 손으로 옮겨 적지 않게 하려고 만든 스크립트다.
# 어디서 실행하든 manyak-ai 레포 루트를 스스로 찾는다(두 레포를 오가는 스킬이라 작업 디렉터리를 믿지 않는다).
#
# 종료코드: 0  = 변경 없음(스펙 최신)      10 = 감시 경로 안에 변경 있음(층 판단으로)
#           11 = 감시 경로 밖 변경만 있음(커밋 메시지 확인 필요)
#           1  = 스펙 파일 없음            3  = 메타 표에 기준 코드 SHA 없음
#           4  = 기준 SHA가 로컬에 없음    5  = git fetch 실패    2  = 사용법 오류
# 각 코드의 대응은 ../reference.md §4
set -uo pipefail

case "${1:-}" in
  -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
esac
[ $# -eq 0 ] || { echo "FAIL: 인자를 받지 않습니다." >&2; exit 2; }

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null) \
  || { echo "FAIL: git 레포 안에서 실행해야 합니다." >&2; exit 1; }
SPEC="$ROOT/../knk-harness/docs/product-specs/5-ai-server.md"

# 감시 경로 — 스펙에 영향이 있는 곳. 표에서 눈으로 옮기지 않도록 여기 한 곳에만 둔다.
WATCH=(src/ prompt/ spec/ Dockerfile pyproject.toml .github/workflows/ .env.example)
# '감시 경로 밖' 질의용 제외 인자는 위 목록에서 만든다 — 두 벌로 두면 한쪽만 고쳐져 조용히 어긋난다.
EXCLUDE=()
for p in "${WATCH[@]}"; do EXCLUDE+=(":(exclude)$p"); done

[ -f "$SPEC" ] || {
  echo "FAIL: knk-harness 경로를 확인해주세요. '../knk-harness/docs/product-specs/5-ai-server.md'를 찾을 수 없습니다." >&2
  exit 1
}

# 메타 표의 '기준 코드' 행에서 dev 브랜치 SHA만 뽑는다(같은 줄의 main SHA와 헷갈리지 않게 '브랜치' 뒤로 한정).
BASE_SHA=$(grep -m1 '^| *기준 코드 *|' "$SPEC" | sed -nE 's/.*브랜치 `([0-9a-f]{7,40})`.*/\1/p')
[ -n "$BASE_SHA" ] || {
  echo "FAIL: 메타 표에서 기준 코드 SHA를 찾지 못했습니다 — 앵커가 없으면 감지가 불가능합니다." >&2
  echo "      사용자에게 기준 시점을 물어 SHA를 확정하고, 갱신할 때 메타 표에 앵커 행을 복구하세요." >&2
  exit 3
}

# fetch가 실패하면 로컬 origin/dev는 낡은 상태다. 그대로 비교하면 새 커밋을 못 보고
# "스펙 최신"이라 답하게 되므로(이 스킬이 막으려는 바로 그 사고) 경고가 아니라 중단이다.
git -C "$ROOT" fetch origin --quiet \
  || { echo "FAIL: git fetch 실패 — 낡은 origin/dev로 비교하면 드리프트를 놓칩니다. 네트워크·권한을 확인하세요." >&2; exit 5; }

git -C "$ROOT" cat-file -e "${BASE_SHA}^{commit}" 2>/dev/null || {
  echo "FAIL: 기준 SHA $BASE_SHA 가 로컬에 없습니다(force push 등). 사용자에게 보고하세요." >&2
  exit 4
}

NEW_SHA=$(git -C "$ROOT" rev-parse --short=12 origin/dev) || { echo "FAIL: origin/dev를 읽지 못했습니다." >&2; exit 1; }

echo "기준 코드 $BASE_SHA -> origin/dev $NEW_SHA"
echo "감시 경로: ${WATCH[*]}"
echo

echo "############ 감시 경로 안의 변경 (층 판단 대상) ############"
# 조회 실패와 '변경 없음'은 둘 다 빈 결과다 — 상태를 안 보면 실패를 "스펙 최신"으로 보고하게 된다.
IN=$(git -C "$ROOT" log "$BASE_SHA..origin/dev" --oneline -- "${WATCH[@]}") \
  || { echo "FAIL: 변경 목록 조회 실패 — '변경 없음'과 구별할 수 없어 중단합니다." >&2; exit 1; }
if [ -z "$IN" ]; then
  echo "(없음)"
else
  printf '%s\n' "$IN"
fi

echo
echo "############ 감시 경로를 전혀 건드리지 않은 변경 (참고) ############"
echo "# 원칙은 스펙 무영향이지만, 커밋 메시지가 설계 변경을 시사하면 예외로 포함한다."
OUT=$(git -C "$ROOT" log "$BASE_SHA..origin/dev" --oneline -- . "${EXCLUDE[@]}") \
  || { echo "FAIL: 변경 목록(감시 경로 밖) 조회 실패 — 중단합니다." >&2; exit 1; }
# 안팎을 함께 건드린 커밋은 위 목록에서 이미 검토된다 — 여기서는 뺀다.
if [ -n "$IN" ] && [ -n "$OUT" ]; then
  OUT=$(awk 'NR==FNR{seen[$1];next} !($1 in seen)' <(printf '%s\n' "$IN") <(printf '%s\n' "$OUT"))
fi
if [ -z "$OUT" ]; then
  echo "(없음)"
else
  printf '%s\n' "$OUT"
fi

echo
if [ -z "$IN" ] && [ -z "$OUT" ]; then
  echo ">>> 기준 SHA 이후 변경이 없습니다. 스펙은 최신입니다."
  exit 0
fi
if [ -z "$IN" ]; then
  # 종료코드 0은 "판단할 것 없음"이라 스킬이 곧장 끝낸다. 밖의 변경이 남아 있으면
  # 커밋 메시지를 사람이 훑어야 하므로 0과 구분되는 코드를 낸다.
  echo ">>> 감시 경로 안의 변경은 없지만, 밖의 변경이 남아 있습니다 — 위 목록의 커밋 메시지를 확인하세요."
  echo ">>> 설계 변경을 시사하면 층 판단으로, 아니면 '스펙 최신'으로 보고합니다."
  exit 11
fi
echo ">>> 갱신 후 메타 표에 적을 새 기준 코드: $NEW_SHA"
exit 10
