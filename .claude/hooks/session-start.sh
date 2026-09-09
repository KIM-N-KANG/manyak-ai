#!/usr/bin/env bash
# 세션 시작 시 하네스 운영 규칙(knk-harness/AGENTS.md)만 컨텍스트에 로드한다.
# 제품 명세는 미리 읽지 않는다 — 작업에 필요한 문서만 CLAUDE.md의 인덱스에서 골라 연다.
# (이 환경에서는 CLAUDE.md @import가 펼쳐지지 않아 훅으로 로드한다.)
#
# 스크립트 위치(.claude/hooks/) 기준으로 레포 루트로 이동해, 호출 시 cwd와 무관하게
# 상대 경로(../knk-harness)가 항상 형제 하네스를 가리키게 한다.
cd "$(dirname "$0")/../.." || exit 0

HARNESS="../knk-harness"

# 하네스가 형제 디렉토리로 없으면 조용히 넘어가지 않고 안내한다(stderr).
if [ ! -f "$HARNESS/AGENTS.md" ]; then
    echo "[session-start] 경고: 하네스 운영 규칙($HARNESS/AGENTS.md)이 없어 로드하지 못했습니다. knk-harness를 형제 디렉토리로 클론하세요." >&2
    exit 0
fi

printf '%s\n\n' "【시작 절차】 아래는 하네스 운영 규칙(knk-harness/AGENTS.md) 전문이다. 제품 문서(docs/spec·docs/design·docs/adr)는 여기 포함하지 않았다 — 작업에 필요한 문서만 CLAUDE.md의 인덱스에서 골라 직접 열어 근거로 삼아라."
echo '===== KNK-HARNESS AGENTS.md (하네스 운영 규칙) ====='
cat "$HARNESS/AGENTS.md"
