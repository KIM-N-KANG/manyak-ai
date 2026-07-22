#!/usr/bin/env bash
# 배포 인프라 읽기 전용 점검. 아무것도 바꾸지 않는다.
# 시크릿 "값"은 절대 출력하지 않는다 — 키 이름만 센다.
#
# 종료코드 0 = 점검 전부 성공. 하나라도 실패하면 1.
# (set -e를 쓰지 않는 이유: 하나 실패해도 나머지를 계속 훑어 전체 그림을 보여주기 위함.
#  대신 실패를 세어 마지막에 반영한다 — 안 그러면 전부 실패해도 "성공"으로 끝난다.)
set -uo pipefail

REGION="${AWS_REGION:-ap-northeast-2}"
REPO="${GH_REPO:-KIM-N-KANG/manyak-ai}"
FAILS=0

# 한 점검을 돌리고 실패하면 세어 둔다.
run() { # $1=이름, 나머지=명령
  local name="$1"; shift
  if "$@"; then return 0; fi
  echo "  !! 실패: $name"
  FAILS=$((FAILS + 1))
  return 1
}

# 명령 성공만 보면 안 되는 점검용. aws/gh는 "그런 자원 없음"에도 종료코드 0에 빈 출력을 낸다
# — EC2 0대, 이미지 0개, 변수 없음이 전부 '통과'로 새어 나간다. 출력 내용까지 확인한다.
fail() { echo "  !! 실패: $1"; FAILS=$((FAILS + 1)); }

# 계정번호·ARN을 그대로 찍으면 에이전트 대화 기록에 인프라 식별자가 남는다(레포가 PUBLIC).
mask() { sed -E 's/[0-9]{12}/<계정>/g'; }

echo "== AWS =="
if ! command -v aws >/dev/null; then
  echo "aws CLI 없음 (brew install awscli) — AWS를 점검하지 못했습니다"
  FAILS=$((FAILS + 1))
else
  echo "-- 자격 --"
  if ident=$(aws sts get-caller-identity --query "{Account:Account,Arn:Arn}" --output json); then
    printf '%s\n' "$ident" | mask
  else
    fail "AWS 자격(aws configure 필요)"
  fi

  echo "-- EC2 --"
  # running 상태만 센다. 꺼진 인스턴스가 남아 있어도 배포는 안 된다.
  if ec2=$(aws ec2 describe-instances --region "$REGION" \
             --filters "Name=tag:Name,Values=manyak-prod-app" "Name=instance-state-name,Values=running" \
             --query "Reservations[].Instances[].InstanceId" --output text); then
    if [ -z "$ec2" ] || [ "$ec2" = "None" ]; then
      fail "running 상태의 manyak-prod-app EC2가 0대 (terraform apply 선행 필요)"
    else
      echo "  running EC2 $(printf '%s' "$ec2" | wc -w | tr -d ' ')대"
    fi
  else
    fail "EC2 조회"
  fi

  echo "-- ECR (최근 태그 이미지 5개) --"
  # 태그 없는 항목은 멀티아치 manifest의 자식이라 건너뛴다.
  if ecr=$(aws ecr describe-images --region "$REGION" --repository-name manyak-ai \
             --query "reverse(sort_by(imageDetails[?imageTags],&imagePushedAt))[:5].[imagePushedAt,join(',',imageTags)]" \
             --output text); then
    if [ -z "$ecr" ]; then fail "ECR에 태그 달린 이미지가 0개"; else printf '%s\n' "$ecr"; fi
  else
    fail "ECR 조회"
  fi

  echo "-- SSM 배포 문서 --"
  run "SSM 문서 조회" aws ssm describe-document --region "$REGION" --name manyak-prod-ai-deploy \
    --query "Document.Status" --output text

  echo "-- Secrets (키 이름만) --"
  if command -v python3 >/dev/null; then
    # 중첩 셸(`bash -c "…$REGION…"`)을 쓰지 않는다 — 바깥 변수를 문자열에 끼워 넣으면
    # 값에 따옴표가 섞였을 때 명령이 주입되고, 새 셸이라 pipefail도 물려받지 않는다.
    # 출력을 먼저 받아 종료코드를 보고, 그다음에 파싱한다. 값은 출력하지 않는다.
    if secret_json=$(aws secretsmanager get-secret-value --region "$REGION" \
                       --secret-id manyak/prod/app --query SecretString --output text) \
       && printf '%s' "$secret_json" \
          | python3 -c "import sys,json;k=sorted(json.load(sys.stdin));print(len(k),'keys:',', '.join(k))"; then
      :
    else
      echo "  !! 실패: Secrets 조회"
      FAILS=$((FAILS + 1))
    fi
    unset secret_json
  else
    echo "python3 없음 — 키 목록을 확인하지 못했습니다"
    FAILS=$((FAILS + 1))
  fi
fi

echo
echo "== GitHub =="
if ! command -v gh >/dev/null; then
  echo "gh CLI 없음 — GitHub를 점검하지 못했습니다"
  FAILS=$((FAILS + 1))
else
  echo "-- Actions 변수 --"
  # 값(ARN)은 찍지 않는다. 배포에 반드시 필요한 AWS_ROLE_ARN의 존재만 확인한다 —
  # 목록이 비어 있어도 gh는 종료코드 0이라, 이름을 직접 찾지 않으면 누락이 통과된다.
  if vars=$(gh api "repos/$REPO/actions/variables" --jq '.variables[].name'); then
    printf '%s\n' "${vars:-(없음)}"
    printf '%s\n' "$vars" | grep -qx 'AWS_ROLE_ARN' \
      || fail "AWS_ROLE_ARN 변수가 없습니다 — OIDC 로그인이 실패해 배포 잡이 죽습니다"
  else
    fail "Actions 변수 조회"
  fi

  echo "-- 최근 워크플로 실행 --"
  run "워크플로 실행 조회" gh api "repos/$REPO/actions/runs?per_page=10" \
    --jq '.workflow_runs[]|"\(.head_branch)\t\(.event)\t\(.conclusion)\t\(.display_title)"'

  echo "-- 브랜치 룰셋 허용 머지 방식 --"
  for b in main dev; do
    printf "%s: " "$b"
    # 룰셋이 아예 없는 것은 정상 상태일 수 있어 실패로 세지 않는다(조회 자체가 깨질 때만 실패).
    gh api "repos/$REPO/rules/branches/$b" \
      --jq '[.[]|select(.type=="pull_request")|.parameters.allowed_merge_methods]|if length==0 then "룰셋 없음" else .[0] end' \
      2>/dev/null || { echo "조회 실패"; FAILS=$((FAILS + 1)); }
  done
fi

echo
if [ "$FAILS" -gt 0 ]; then
  echo "== 점검 실패 ${FAILS}건 — 위 !! 표시를 확인하세요 =="
  exit 1
fi
echo "== 점검 전부 통과 =="
exit 0
