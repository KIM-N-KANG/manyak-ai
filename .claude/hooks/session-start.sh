#!/usr/bin/env bash
# 세션 시작 시 하네스 운영 규칙(AGENTS.md)과 제품 명세(product-specs 전체)를
# 컨텍스트에 로드한다. 이 환경에서는 CLAUDE.md @import가 펼쳐지지 않아 훅으로 로드한다.
#
# 스크립트 위치(.claude/hooks/) 기준으로 레포 루트로 이동해, 호출 시 cwd와 무관하게
# 상대 경로(../knk-harness)가 항상 형제 하네스를 가리키게 한다.
cd "$(dirname "$0")/../.." || exit 0

HARNESS="../knk-harness"
SPECS_DIR="$HARNESS/docs/product-specs"

# 하네스가 형제 디렉토리로 없으면 조용히 넘어가지 않고 안내한다(stderr).
if [ ! -d "$HARNESS" ]; then
    echo "[session-start] 경고: 하네스($HARNESS)가 없어 운영 규칙·제품 명세를 로드하지 못했습니다. knk-harness를 형제 디렉토리로 클론하세요." >&2
    exit 0
fi

printf '%s\n\n' "【필수 시작 절차】 아래에는 하네스 운영 규칙(knk-harness/AGENTS.md)과 knk-harness 제품 스펙(docs/product-specs)의 모든 문서 전문이 포함되어 있다. 파일을 다시 열 필요 없이 이 내용을 근거로, 단순 기계적 변경이 아닌 모든 작업을 진행하라."

# 하네스 운영 규칙 — 파일이 있을 때만 출력한다.
if [ -f "$HARNESS/AGENTS.md" ]; then
    echo '===== KNK-HARNESS AGENTS.md (하네스 운영 규칙) ====='
    cat "$HARNESS/AGENTS.md"
    echo
fi

# 제품 명세 — glob이 안 펼쳐질 때 리터럴 패턴이 새지 않도록 nullglob을 켜고,
# 실제 파일이 있을 때만 헤더와 본문을 출력한다.
shopt -s nullglob
specs=("$SPECS_DIR"/*.md)
if [ "${#specs[@]}" -gt 0 ]; then
    echo '===== PRODUCT SPECS (product-specs 전체 상시 참조) ====='
    for f in "${specs[@]}"; do
        echo
        echo "===== product-specs/$(basename "$f") ====="
        cat "$f"
    done
else
    echo "[session-start] 경고: $SPECS_DIR 에 로드할 .md 스펙이 없습니다." >&2
fi
