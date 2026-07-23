#!/usr/bin/env bash
# 릴리스 QA를 한 번에 돌린다. 사람이 단계를 나눠 확인할 필요 없이 이것만 실행한다.
#
#   qa.sh              # 유닛 + 라이브(실제 LLM) 전부 — 릴리스 게이트
#   qa.sh --no-live    # 유닛만. 릴리스 게이트로는 부족하므로 종료코드 3을 낸다
#
# 라이브 구간은 실제 DeepSeek를 호출한다(릴리스 QA 1회당 통합 테스트 7건 규모).
# 이 호출은 사전 승인된 범위다 — 다시 묻지 않는다.
#
# 종료코드: 0 = 릴리스 진행 가능 / 1 = 테스트 실패 / 2 = 사용법 오류 / 3 = 라이브 미실시
set -uo pipefail

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "FAIL: git 레포 안에서 실행하세요" >&2; exit 1; }

# 인자를 엄격히 검사한다. `--no-lvie` 같은 오타를 조용히 무시하면
# 라이브를 빼려던 사람이 유료 호출을 실행하게 된다.
RUN_LIVE=1
for arg in "$@"; do
  case "$arg" in
    --no-live) RUN_LIVE=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "FAIL: 모르는 인자 '$arg'. 쓸 수 있는 것: --no-live, --help" >&2; exit 2 ;;
  esac
done

command -v docker >/dev/null || { echo "FAIL: docker가 없습니다. Docker Desktop을 켜세요" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "FAIL: docker 데몬이 꺼져 있습니다. Docker Desktop을 켜세요" >&2; exit 1; }

BACKUP="$ROOT/.env.bak-qa"
INJECTED=0

# 우리가 넣은 prod 키는 우리가 치운다.
# 복구 실패를 성공으로 보고하면 prod 키가 .env에 남은 채 "치웠다"가 되므로, 반드시 상태를 본다.
cleanup_injected_key() {
  [ "$INJECTED" = "1" ] || return 0
  if [ ! -f "$BACKUP" ]; then
    # 키를 넣어 놓고 백업이 사라졌다 = 되돌릴 원본이 없다. 조용히 성공으로 끝내면 안 된다.
    echo "!! 위험: 백업($BACKUP)이 사라졌습니다 — .env에 주입한 prod 키가 남아 있을 수 있습니다." >&2
    echo "!! .env에서 DEEPSEEK_API_KEY 줄을 직접 확인하세요." >&2
    exit 1
  fi
  if mv "$BACKUP" "$ROOT/.env"; then
    # 내용은 원래대로 돌아가지만 파일 권한은 0600으로 남는다. 되돌리지 않는 게 낫다 —
    # 이 레포의 .env는 기본이 0666(누구나 읽기)이라, 그 상태로 복원하면 다음 주입 때 또 위험해진다.
    echo ">>> .env 내용을 QA 전 상태로 되돌렸습니다(주입한 prod 키 제거). 권한은 0600으로 유지합니다."
    return 0
  fi
  echo "!! 위험: .env 복구 실패 — 주입한 prod DEEPSEEK_API_KEY가 .env에 남아 있습니다." >&2
  echo "!! 손으로 되돌리세요: mv '$BACKUP' '$ROOT/.env'" >&2
  exit 1   # EXIT trap 안에서 종료코드를 덮어써, 이 QA를 성공으로 보고하지 못하게 한다
}
trap cleanup_injected_key EXIT

# SIGKILL·SIGQUIT에는 EXIT trap이 돌지 않는다. 그때 남은 백업을 다음 실행이 치운다 —
# 이게 없으면 "키가 이미 있네" 하고 지나가 prod 키가 .env에 영구히 남는다(실제 재현됨).
recover_orphan_backup() {
  rm -f "$BACKUP.tmp"   # 재작성 도중 죽었을 때 남는 조각(키가 들어 있다)
  [ -f "$BACKUP" ] || return 0
  # 현재 .env에 키가 없으면 주입이 이미 되돌려진 것이다. 그때도 덮어쓰면 사용자가 그 뒤에
  # 고친 .env를 말없이 날린다 — 백업만 남기고 손대지 않는다.
  if ! grep -q "^DEEPSEEK_API_KEY=..*" "$ROOT/.env" 2>/dev/null; then
    echo ">>> 이전 백업(.env.bak-qa)이 남아 있으나 현재 .env엔 주입 흔적이 없습니다." >&2
    echo "    덮어쓰지 않습니다. 필요하면 직접 확인하세요: '$BACKUP'" >&2
    return 0
  fi
  echo ">>> 이전 QA가 비정상 종료된 흔적(.env.bak-qa)을 찾았습니다 — .env를 복구합니다."
  mv "$BACKUP" "$ROOT/.env" && return 0
  echo "!! 위험: 이전 백업을 되돌리지 못했습니다. .env에 prod 키가 남아 있을 수 있습니다." >&2
  echo "!! 손으로 확인한 뒤 다시 실행하세요: '$BACKUP'" >&2
  exit 1
}
recover_orphan_backup

# 키가 없으면 Secrets Manager에서 끌어와 .env에 넣는다. 값은 어디에도 출력하지 않는다.
# (test.sh가 docker 인자를 화면에 그대로 찍으므로 -e로 넘기면 키가 노출된다 → .env 경유)
ensure_deepseek_key() {
  grep -q "^DEEPSEEK_API_KEY=..*" "$ROOT/.env" 2>/dev/null && return 0

  echo ">>> .env에 DEEPSEEK_API_KEY가 없습니다 — Secrets Manager에서 가져옵니다"
  command -v aws     >/dev/null || { echo "    aws CLI 없음"; return 1; }
  command -v python3 >/dev/null || { echo "    python3 없음"; return 1; }

  local key
  key=$(aws secretsmanager get-secret-value --region "${AWS_REGION:-ap-northeast-2}" \
          --secret-id manyak/prod/app --query SecretString --output text 2>/dev/null \
        | python3 -c "import sys,json;print(json.load(sys.stdin).get('DEEPSEEK_API_KEY',''))" 2>/dev/null)
  [ -n "$key" ] || { echo "    Secrets에서 키를 얻지 못했습니다(자격 확인: aws sts get-caller-identity)"; return 1; }

  # 쓰기 하나하나의 성공을 확인한다. 실패를 넘기면 키가 없거나 반쯤 쓰인 .env로 QA가 돌고,
  # 그래도 "주입 완료"라고 보고하게 된다.
  touch "$ROOT/.env"       || { echo "    .env를 만들지 못했습니다"; return 1; }
  cp "$ROOT/.env" "$BACKUP" || { echo "    백업(.env.bak-qa)을 만들지 못했습니다"; return 1; }
  INJECTED=1   # 백업 직후에 세운다 — 여기서 중단돼도 trap이 되돌리도록.
  # prod 키가 들어가는 동안은 소유자만 읽게 조인다(이 레포의 .env는 기본 0666이었다).
  # 실패를 삼키면 운영 키가 남들도 읽을 수 있는 파일에 쓰인 채 QA가 "성공"으로 끝난다.
  chmod 600 "$BACKUP" "$ROOT/.env" \
    || { echo "    권한을 0600으로 조이지 못했습니다 — 운영 키를 쓰지 않고 중단합니다"; return 1; }
  # 임시 파일 이름은 .env.bak-* 를 따른다 — 그래야 gitignore에 걸려 키가 커밋되지 않는다.
  if ! { grep -v "^DEEPSEEK_API_KEY=" "$BACKUP" || true
         printf 'DEEPSEEK_API_KEY=%s\n' "$key"; } > "$BACKUP.tmp"; then
    rm -f "$BACKUP.tmp"; echo "    .env 재작성 실패"; return 1
  fi
  chmod 600 "$BACKUP.tmp" \
    || { rm -f "$BACKUP.tmp"; echo "    임시 파일 권한을 조이지 못했습니다 — 중단합니다"; return 1; }
  mv "$BACKUP.tmp" "$ROOT/.env" || { rm -f "$BACKUP.tmp"; echo "    .env 교체 실패"; return 1; }
  unset key
  echo "    주입 완료(값 미출력). QA가 끝나면 .env를 원상복구합니다."
  return 0
}

FAILED=""

# 유닛 단계도 .env의 키를 요구한다 — conftest가 `src.main`을 import하는데 Settings의
# deepseek_api_key가 기본값 없는 필수 필드라 import 시점에 터진다. 그래서 키 없는 머신에서는
# 유닛이 먼저 깨지고, 정작 키를 채우려던 아래 ensure_deepseek_key까지 가지도 못했다.
# 라이브를 돌 계획이면 키를 먼저 확보한다(어차피 곧 필요하다).
if [ "$RUN_LIVE" = "1" ] && ! grep -q "^DEEPSEEK_API_KEY=..*" "$ROOT/.env" 2>/dev/null; then
  echo "############ 0/2 키 확보 (유닛도 키가 있어야 import된다) ############"
  ensure_deepseek_key || { echo ">>> 키를 확보하지 못했습니다 — QA를 진행할 수 없습니다"; exit 1; }
fi
# --no-live인데 키가 없으면 유닛조차 못 돈다. 원인을 유닛 실패로 뭉뚱그리지 않고 먼저 알린다.
if ! grep -q "^DEEPSEEK_API_KEY=..*" "$ROOT/.env" 2>/dev/null; then
  echo "FAIL: .env에 DEEPSEEK_API_KEY가 없어 테스트가 import 단계에서 실패합니다." >&2
  echo "      --no-live 없이 실행하면 Secrets Manager에서 자동으로 가져옵니다." >&2
  exit 1
fi

echo "############ 1/2 유닛·API 테스트 (도커 격리) ############"
if bash "$ROOT/scripts/test.sh"; then
  echo ">>> 유닛 통과"
else
  echo ">>> 유닛 실패"
  FAILED="$FAILED 유닛"
fi

echo
if [ -n "$FAILED" ]; then
  # 유닛이 깨졌으면 릴리스는 이미 막혔다. 유료 호출을 할 이유도, prod 키를 꺼낼 이유도 없다.
  echo "############ 2/2 라이브 — 건너뜀(유닛이 실패해 릴리스가 이미 막힘) ############"
elif [ "$RUN_LIVE" = "0" ]; then
  echo "############ 2/2 라이브 — --no-live로 건너뜀 ############"
else
  echo "############ 2/2 라이브 통합 테스트 (실제 LLM 호출 — 과금) ############"
  # 위 0/2에서 이미 확보했으면 여기선 grep 한 번으로 즉시 통과한다(재조회 없음).
  if ! ensure_deepseek_key; then
    echo ">>> 라이브 건너뜀 — DEEPSEEK_API_KEY를 .env에도 Secrets에도 확보하지 못했습니다"
    FAILED="$FAILED 라이브(키없음)"
  elif bash "$ROOT/scripts/test.sh" --live tests/integration; then
    echo ">>> 라이브 통과"
  else
    echo ">>> 라이브 실패"
    FAILED="$FAILED 라이브"
  fi
fi

echo
echo "================= QA 결과 ================="
if [ -n "$FAILED" ]; then
  echo "실패:$FAILED — 릴리스를 진행하지 않는다. 실패 내용을 그대로 보고할 것."
  exit 1
fi
if [ "$RUN_LIVE" = "0" ]; then
  # 종료코드 0은 "릴리스 진행 가능"을 뜻한다. 라이브를 안 돌린 QA는 그 자격이 없다.
  echo "유닛만 통과 / 라이브 미실시 — 릴리스 게이트로는 불충분(exit 3)."
  echo "보고에 '실측 미실시'라고 적는다. 릴리스하려면 --no-live 없이 다시 돌린다."
  exit 3
fi
echo "전부 통과 (유닛 + 라이브) — 릴리스 진행 가능"
exit 0
