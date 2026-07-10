#!/usr/bin/env bash
# manyak-ai 테스트를 도커 격리 환경에서 실행한다. (test.ps1의 bash 포팅)
# CI와 동일한 python:3.11-slim + dev 의존성으로 돌리므로, 로컬 인터프리터(anaconda 등)에
# pytest-asyncio가 없어 async 테스트가 조용히 스킵되는 문제가 생기지 않는다.
#
# 사용법:
#   ./scripts/test.sh                          # 평소 테스트(라이브 제외)
#   ./scripts/test.sh --live                   # 라이브 테스트 포함(실제 LLM 호출, .env 키 필요)
#   ./scripts/test.sh tests/unit               # pytest 인자 전달
#   ./scripts/test.sh --live tests/integration # 라이브만 실행
#
# manyak-pip-cache 볼륨에 pip 다운로드를 캐시하므로 2회차부터 빨라진다.
# (manyak-infra의 네트워크/볼륨과 이름이 달라 충돌하지 않는다. --rm이라 컨테이너도 안 남는다.)
set -euo pipefail

LIVE=0
PYTEST_ARGS=()
for arg in "$@"; do
    if [ "$arg" = "--live" ] || [ "$arg" = "-Live" ]; then
        LIVE=1
    else
        PYTEST_ARGS+=("$arg")
    fi
done

# 리포지토리 루트로 이동(어디서 호출하든 -v $PWD가 프로젝트 루트를 가리키도록).
cd "$(dirname "$0")/.."

# ${arr[*]:-}의 :-는 macOS 기본 bash 3.2에서 필수다 — set -u 아래서 빈 배열 전개가
# unbound variable로 죽는다(bash 4.4+에서는 문제없어 리눅스 CI에선 재현 안 됨).
inner="pip install -q -e '.[dev]' && pytest -q ${PYTEST_ARGS[*]:-}"

docker_args=(
    run --rm
    -v "${PWD}:/app"
    -v "manyak-pip-cache:/root/.cache/pip"
    -w /app
)
if [ "$LIVE" -eq 1 ]; then
    docker_args+=(-e RUN_LIVE_TESTS=1)
fi
docker_args+=(python:3.11-slim sh -c "$inner")

printf '\033[90mdocker %s\033[0m\n' "${docker_args[*]}"
exec docker "${docker_args[@]}"
