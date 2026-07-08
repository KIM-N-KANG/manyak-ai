# manyak-ai 테스트를 도커 격리 환경에서 실행한다.
# CI와 동일한 python:3.11-slim + dev 의존성으로 돌리므로, 로컬 인터프리터(anaconda 등)에
# pytest-asyncio가 없어 async 테스트가 조용히 스킵되는 문제가 생기지 않는다.
#
# 사용법:
#   .\scripts\test.ps1                         # 평소 테스트(라이브 제외)
#   .\scripts\test.ps1 -Live                   # 라이브 테스트 포함(실제 LLM 호출, .env 키 필요)
#   .\scripts\test.ps1 -PytestArgs "tests/unit"          # pytest 인자 전달
#   .\scripts\test.ps1 -Live -PytestArgs "tests/integration"  # 라이브만 실행
#
# 실행 정책에 막히면:
#   powershell -ExecutionPolicy Bypass -File scripts\test.ps1
#
# manyak-pip-cache 볼륨에 pip 다운로드를 캐시하므로 2회차부터 빨라진다.
# (manyak-infra의 네트워크/볼륨과 이름이 달라 충돌하지 않는다. --rm이라 컨테이너도 안 남는다.)
param(
    [switch]$Live,
    [string]$PytestArgs = ""
)

$inner = "pip install -q -e '.[dev]' && pytest -q $PytestArgs".Trim()

$dockerArgs = @(
    "run", "--rm",
    "-v", "${PWD}:/app",
    "-v", "manyak-pip-cache:/root/.cache/pip",
    "-w", "/app"
)
if ($Live) {
    $dockerArgs += @("-e", "RUN_LIVE_TESTS=1")
}
$dockerArgs += @("python:3.11-slim", "sh", "-c", $inner)

Write-Host "docker $($dockerArgs -join ' ')" -ForegroundColor DarkGray
docker @dockerArgs
