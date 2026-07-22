#!/usr/bin/env bash
# 운영 AI 서버 헬스체크.
# AI는 외부에 노출돼 있지 않아 curl로 못 두드린다 — SSM으로 EC2 안에서 찌른다.
#
#   prod-health.sh            # status만 검사
#   prod-health.sh 0.2.1      # status + version 일치까지 검사 (배포 검증은 이쪽을 쓴다)
#
# 종료코드 0은 "서비스가 실제로 건강함"을 뜻한다 — SSM이 돌았다는 뜻이 아니다.
set -uo pipefail

REGION="${AWS_REGION:-ap-northeast-2}"
TAG="${EC2_NAME_TAG:-manyak-prod-app}"
# 여분 인자를 조용히 무시하면 오타(`prod-health.sh 0.2.1 typo`)가 정상 통과로 끝난다.
[ $# -le 1 ] || { echo "FAIL: 인자는 기대 버전 하나입니다 (예: prod-health.sh 0.2.1)" >&2; exit 2; }
# --help가 버전 문자열로 흘러가면 도움말을 보려다 실제 AWS 호출이 나간다.
case "${1:-}" in -h|--help) sed -n '2,8p' "$0"; exit 0 ;; esac
# 태그는 vX.Y.Z, app_version은 X.Y.Z다. 둘을 헷갈려 넘겨도 거짓 실패가 나지 않게 앞의 v를 떼어낸다.
WANT_VERSION="${1:-}"
WANT_VERSION="${WANT_VERSION#v}"

fail() { echo "FAIL: $*" >&2; exit 1; }

command -v aws >/dev/null || fail "aws CLI가 없습니다 (brew install awscli)"

echo "== 운영 헬스체크 (region=$REGION, tag=$TAG) =="

# EC2 id는 하드코딩하지 않는다 — user-data가 바뀌면 인스턴스가 통째로 교체된다.
IID=$(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:Name,Values=$TAG" "Name=instance-state-name,Values=running" \
  --query "Reservations[].Instances[].InstanceId" --output text) \
  || fail "EC2 조회 실패 (자격 확인: aws sts get-caller-identity)"

[ -n "$IID" ] && [ "$IID" != "None" ] \
  || fail "running 상태의 $TAG EC2가 없습니다. terraform apply가 선행돼야 할 수 있습니다."
echo "EC2: $IID"

CID=$(aws ssm send-command --region "$REGION" --instance-ids "$IID" \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["curl -s -m 5 http://localhost:8000/api/v1/health"]' \
  --query "Command.CommandId" --output text) \
  || fail "SSM send-command 실패"

# SSM은 비동기다. 확정 상태가 될 때까지 최대 30초 기다린다.
SSM_STATUS="Pending"
for _ in $(seq 1 10); do
  sleep 3
  SSM_STATUS=$(aws ssm get-command-invocation --region "$REGION" \
    --command-id "$CID" --instance-id "$IID" --query "Status" --output text 2>/dev/null) \
    || SSM_STATUS="Pending"
  case "$SSM_STATUS" in Success|Failed|TimedOut|Cancelled) break ;; esac
done

[ "$SSM_STATUS" = "Success" ] \
  || fail "SSM 실행이 끝나지 않았거나 실패했습니다 ($SSM_STATUS). 30초 안에 안 끝난 것일 수도 있으니 재실행해 보세요."

OUT=$(aws ssm get-command-invocation --region "$REGION" \
  --command-id "$CID" --instance-id "$IID" --query "StandardOutputContent" --output text)
echo "응답: $OUT"

# curl은 -f가 없으면 HTTP 500을 받아도 종료코드 0이다. 그래서 SSM Success만으로는
# 서비스가 건강한지 알 수 없다 — 응답 본문을 직접 본다.
GOT_STATUS=$(printf '%s' "$OUT"  | sed -n 's/.*"status":"\([^"]*\)".*/\1/p')
GOT_VERSION=$(printf '%s' "$OUT" | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')

[ "$GOT_STATUS" = "ok" ] || fail "health status가 ok가 아닙니다: '${GOT_STATUS:-응답 파싱 실패}'"

if [ -n "$WANT_VERSION" ]; then
  [ "$GOT_VERSION" = "$WANT_VERSION" ] \
    || fail "version 불일치 — 기대 '$WANT_VERSION', 실제 '${GOT_VERSION:-없음}'. 새 이미지가 안 떴을 수 있습니다."
  echo "OK: status=ok, version=$GOT_VERSION (기대값과 일치)"
else
  echo "OK: status=ok, version=${GOT_VERSION:-확인불가}  ※ 배포 검증이면 기대 버전을 인자로 넘기세요"
fi
