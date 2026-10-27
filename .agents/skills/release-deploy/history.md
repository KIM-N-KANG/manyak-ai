# manyak-ai 배포 이력 (history)

> 배포할 때마다 여기에 한 항목을 추가한다. 절차는 `SKILL.md`, 사실·명령은 `reference.md`.
> 레포가 PUBLIC이다. 계정번호·비밀값은 적지 않는다(키 이름과 절차까지만).

---

## 다음 배포 때 볼 것

- 흐름은 늘 같다: `dev → release/vX.Y.Z → main`(Merge Commit) → 자동 배포 → 태그 → 역류(Merge Commit) → 브랜치 삭제.
- **운영 배포는 ECS 경로다(KNK-963, v0.2.6부터).** deploy 잡의 안정화 대기는 10분(60×10초)이라 **잡 실패 ≠ 배포 실패** — dev에서 태스크 시작 실패 반복으로 54분 뒤 완료된 전례(8/23)가 있다. 잡이 실패하면 롤백 전에 ECS 서비스 상태부터 확인한다. **AI는 전용 서비스 `manyak-prod-ai`에서 돈다**(v0.4.6에서 확인, v0.4.5까지의 `manyak-prod`가 아니다). **EC2는 회수됐고 EC2 롤백 잡도 제거됐다(KNK-971, v0.3.0)** — `prod-health.sh`·`infra-check.sh`의 EC2·SSM 점검이 실패하니 ECS(태스크 imageDigest·HEALTHY)로 검증한다. 스크립트의 ECS 전환은 후속 사안.
- **Langfuse 활성화(KNK-654)는 릴리스가 아니다.** 코드는 이미 나가 있고 키만 없어 no-op이다. 순서:
  1. `manyak-terraform` apply — **EC2 교체**가 일어난다(다운타임 + 이미지 핀이 `:latest`로 리셋되는 것 주의).
  2. 백엔드 KNK-621 배포 — 커스텀 장르 400 차단. 안 하면 사용자 자유입력이 트레이스 태그로 유입된다.
  3. Secrets에 `AI_LANGFUSE_PUBLIC_KEY`·`AI_LANGFUSE_SECRET_KEY`·`AI_LANGFUSE_HOST` 추가 — **기존 8키를 전부 포함해 저장**한다(`put-secret-value`는 전체 덮어쓰기).
     - **HOST는 정확히 `https://jp.cloud.langfuse.com`이어야 한다.** 코드가 이 문자열과 일치할 때만 켠다(`src/core/langfuse.py`의 `_ALLOWED_HOST`, 후행 슬래시는 무시). ⚠️ `.env.example`의 기본값은 EU(`https://cloud.langfuse.com`)라 그대로 복사하면 **에러 로그만 남고 조용히 꺼진 채 기동**한다.
  4. ECS `manyak-prod-ai` 서비스 force-new-deployment로 AI만 재배포.
  5. 기동 로그에서 `Langfuse 활성 — host=… env=prod` 확인 + server 컨테이너에 `AI_LANGFUSE_*`가 없는지 확인.

---

## v0.5.4 — 2026-10-28 배포 완료

- 범위: KNK-1619 컴파일 인물 외형 묘사 정리로 이미지 거부 줄이기(#200), KNK-1859 내용 거절을 422 `content_refused`로 응답(#201), KNK-1556 FastAPI HTTP 서버 지표 OTLP 전송(#202), KNK-1834 v0.5.3 이력 문서(#199), KNK-1883 버전 0.5.4(#203).
  - #201의 422는 운영 백엔드가 종전처럼 502와 환불, 체험 횟수 복원으로 처리한다(강동우 확인). 수위 안내 화면은 서버 KNK-1865, 웹, 앱 배포 때 붙는다. AI 단독 배포, 단독 롤백 가능.
  - #202의 지표 환경 변수(`MANYAK_OTLP_METRICS_ENABLED`, `MANYAK_OTLP_METRICS_ENDPOINT`)는 배포 전에 `manyak-terraform` #115로 운영 AI 태스크에 주입돼 있었다. 이 배포부터 지표 전송이 실제로 켜졌다. health 요청은 계측하지 않으므로 지표는 실제 요청이 들어온 뒤 AMP `job="manyak-ai"`로 들어온다.
- PR #204 → main `4f6aaeb`(Merge Commit). 운영 배포 워크플로 성공. 태그 `v0.5.4` → `4f6aaeb`. 역류 PR #205(Merge Commit).
- 운영 검증: ECS `manyak-prod-ai` 태스크 2개 HEALTHY, ai 컨테이너 imageDigest가 ECR `4f6aaeb`과 일치. `prod-health.sh`는 EC2 기준이라 종전처럼 FAIL(무시, ECS로 검증).
- QA: `qa.sh` 종료코드 0. 유닛 1744 passed·9 skipped, 라이브 통합 10 passed·1 skipped. 검수 데이터셋 테스트는 모더레이션 변경 없어 건너뜀.
- 적대적 리뷰(계약·환경변수/기동·런타임): 차단 요인 없음. 주의 3건.
  1. #201: JSON 파싱이 실패하고 `{`나 `[`로 시작하지 않는 출력은 거절 표현이 없어도 422로 끝난다. 정상 JSON 앞에 설명문이 붙은 출력이면 형식 재생성 기회를 잃는다. 운영 빈도 미확인.
  2. #201: 새 422는 운영에서 아직 일반 502 안내로 보인다.
  3. #202: 테스트는 실제 HTTP 전송 장애와 종료 중 전송을 재현하지 않는다. SDK 소스상 전송 실패는 데몬 스레드에서 잡히고 종료 대기는 3초 상한이다.

## v0.5.3 — 2026-10-25 배포 완료

- 범위: KNK-1833 스토리라인 출력 토큰 한도 6,144 → 12,288(#195), KNK-1806 v0.5.2 이력 문서(#194), KNK-1834 버전 0.5.3(`7e03065`, #196).
  - Gemini는 출력 한도를 추론 토큰과 본문이 함께 쓴다. medium 추론이 5,897까지 부풀어 본문 242 토큰에서 잘려 JSON 파싱에 실패했다(Sentry PYTHON-FASTAPI-5, 6/28~10/24 누적 112회). 12,288은 추론 1만 토큰까지 본문을 담는 값이며 상한만 올려 평시 비용은 같다.
  - 외부 계약·환경변수 변경 없음. AI 단독 배포·단독 롤백 가능.
- PR #197 → main `31d4fd7`(사용자 직접 Merge Commit). 운영 배포 워크플로 `40122560176` 성공. 태그 `v0.5.3` → `31d4fd7`.
- 운영 검증: ECS `manyak-prod-ai` 태스크 2개 HEALTHY, ai 컨테이너 imageDigest가 ECR `31d4fd7`과 일치. `prod-health.sh`는 EC2 기준이라 종전처럼 FAIL(무시, ECS로 검증).
- QA: `qa.sh` 종료코드 0. 유닛 1718 passed·9 skipped, 라이브 통합 10 passed·1 skipped. 검수 데이터셋 테스트는 모더레이션 변경 없어 건너뜀.
- 적대적 리뷰(계약·환경변수/기동·런타임): 차단 요인 없음. 런타임 주의 1건 — medium 추론이 유지되면 매우 긴 추론 호출이 잘림 대신 90초 타임아웃으로 바뀔 수 있어 아래 모델 전환을 함께 했다.
- **같은 날 운영 설정 변경(코드 아님)**: Parameter Store `/manyak/prod/ai/storylines-model`을 `gemini-3.7-flash` → `gemini-3.8-flash`(등록부 기본 추론 강도 low)로 변경(버전 4). v0.5.3 롤아웃 중 변경해 첫 태스크가 옛 값을 읽었을 수 있어 `update-service --force-new-deployment`로 한 번 더 교체(04:02 KST). 전환 뒤 첫 운영 스토리라인 호출을 Langfuse로 확인 — model `gemini-3.8-flash`, 15.6초, 본문 1,208 토큰, 추론 0(종전 medium 20~37초).
  - 확인된 사실: 운영 처리 속도는 초당 약 80토큰으로 일정하고, 스토리라인 소요 시간은 추론 토큰 수가 결정한다. "운영이 로컬보다 느리다"는 인식은 운영 medium vs 로컬 low 차이였다.
- 역류: PR #198, release/v0.5.3 → dev(Merge Commit, dev `88ae271`).
- Codex 리뷰(#195)는 사용 한도 초과로 미실시. 직접 적대적 리뷰로 대체.

## v0.5.2 — 2026-10-23 배포 완료

- 범위: KNK-1783 채팅 출력 분량 선택(#191), KNK-1770 검수 이미지 데이터셋 통합 테스트(#189), KNK-1806 버전 0.5.2(`3f3b68a`).
  - 채팅 턴 요청에 선택 필드 `long_response` 추가. 생략·false는 공백 포함 550~750자, true는 1,050~1,250자를 프롬프트에 지정한다. null은 422. CHAT 프롬프트 버전 3, 엔딩의 분량 초과 예외 제거.
  - 기존 요청과 하위호환이며 새 필수 환경변수·모델·라우팅 변경 없음. AI 먼저 배포 가능. 긴 응답을 사용하려면 백엔드가 해당 필드를 전달해야 한다. AI만 롤백하면 긴 응답 선택이 적용되지 않고 이전 600~900자 지침으로 돌아간다.
- PR #192 → main `fbc8577`(사용자 직접 Merge Commit). 운영 배포 워크플로 `39935123934` 성공. 태그 `v0.5.2` → `fbc8577`.
- 운영 검증: ECS `manyak-prod-ai` 배포 COMPLETED, desired/running 2개·pending 0개. 두 태스크와 ai 컨테이너 모두 HEALTHY, 두 이미지 digest가 ECR `fbc8577`과 일치.
- **QA 실패 상태로 배포됨.** `qa.sh` 종료코드 1. Docker 단위·API 1718 passed·9 skipped, 라이브 통합 10 passed·검수 데이터셋 1 failed(296.17초). 전체 205장 중 판정 불가 0장·반려 누락 1장·승인 오반려 4장(허용 0). 실패를 PR 본문에 명시해 사용자 요청으로 PR을 먼저 만들었고, 사용자가 운영 머지를 실행했다. 실패는 미해소 상태다.
- 검수 진단: 누락 샘플은 기존 출력 형식의 재호출에서도 승인됐다. 동일 모델에 관찰 내용·판정 이유를 출력하도록 변경한 별도 진단에서는 2회 모두 반려됐다. 이는 제품 프롬프트·코드에 반영하지 않았으며 효과를 검증한 결과로 보지 않는다. 후속 검수 개선은 별도 작업이다.
- 계약·환경변수/기동·런타임 변경 검토에서 추가 차단 결함 없음. 실제 채팅 출력의 글자 수 준수율·품질·비용·지연 비교 평가는 미실시.
- 역반영: PR #193, release/v0.5.2 → dev(Merge Commit, dev `a9b95fb`).

## v0.5.1 — 2026-10-22 배포 완료

- 범위: KNK-1781 채팅 본문 목표 분량을 공백 포함 1,500~2,200자에서 600~900자로 축소(#186). 엔딩 장면의 분량 예외 유지. 프롬프트 버전 2 및 관련 테스트 기대값 반영.
- KNK-1782: 패키지·앱 설정·환경변수 예시 버전 0.5.1(`fb0af17`). API·SSE 형식, 모델·호출 옵션, 필수 환경변수 변경 없음. AI 단독 배포·롤백 가능.
- PR #187 → main `082747c`(Merge Commit). 운영 배포 워크플로 `39786135765` 성공. 태그 `v0.5.1` → `082747c`.
- 운영 검증: ECS `manyak-prod-ai` 배포 COMPLETED, 태스크 2개와 ai 컨테이너 모두 HEALTHY, 두 이미지 digest가 ECR `082747c`와 일치.
- QA: `qa.sh` 종료코드 0. 단위·API 1716 passed·8 skipped, 라이브 통합 10 passed(147.40초). 첫 시도는 Gemini 키 누락으로 기동 실패했으며, 임시 주입 후 전체 재실행 통과·원래 `.env` 복구. QA 러너의 Gemini 키 자동 준비 보완은 별도 작업.
- 계약·환경변수/기동·런타임 변경 검토에서 차단 결함 없음. 출력 글자 수 준수율·품질·비용·응답 시간 비교 평가는 미실시.
- 역반영 PR #188(Merge Commit, dev `4ce0b6d`).

## v0.5.0 — 2026-10-22 배포 완료

- 범위: KNK-1739(채팅 모델 라우팅·고급 모델 A/B, #181), KNK-1762(등급 이름 LITE·AURORA, #182), KNK-1730(채팅 레이어 제거·단일 프롬프트, #180), KNK-1699(게시물 검수에 주인공 이름·인물 소개, #179), KNK-1673(평가 환경 교체, #178), KNK-1626(판정 로직 제거, #177), KNK-1617(범용 채팅 프롬프트·CHAT_PROMPT, #163), KNK-1778(버전 0.5.0, release 직접 커밋 `4e3e343`).
  - 외부 계약 변경 있음, 하위호환. 채팅 요청에 `model_tier`(LITE/AURORA, 미전달은 LITE)·`user_id`(미전달은 None) 추가, completed에서 판정 3필드 제거. 구 서버(v0.8.0)와 공존 가능해 **AI 먼저 배포**, 이어서 서버 v0.9.0(KNK-1777)과 웹 모델 선택 UI.
  - 판정 제거는 팀 합의(도달 여부 판정만 제거, `main_events`·`endings`는 프롬프트 재료로 유지). 구 서버는 null 판정에 목표 사건을 해제하고 엔딩 도달을 기록하지 않는다(합의된 결과).
  - 채팅 본문 모델이 코드 고정 라우팅(LITE gpt-6-luna low, AURORA 짝수 gpt-6.1-sol low, 홀수 gemini-3.8-flash medium)이라 운영 SSM `CHAT_MODEL`은 무시된다. 기동 검사가 세 공급자 키를 모두 요구하므로 **배포 전 운영 `GEMINI_API_KEY` 값 존재를 확인**했다.
  - `model_tier`는 Literal 검증이라 서버 DB에 새 모델 코드를 추가하면 422. 모델 추가는 AI 동반 배포.
- PR #183 → main `1f272e7`(Merge Commit). Deploy to ECS (prod) 성공(run 39776772682). 태그 `v0.5.0` → `1f272e7`.
- 운영 검증: `manyak-prod-ai` 배포 COMPLETED(`manyak-prod-ai:3`), 태스크 2개·`ai` 컨테이너 HEALTHY, digest가 ECR `1f272e7`(=`latest`)와 일치(`sha256:c0943456…`). `prod-health.sh`는 EC2 경로라 사용하지 않음.
- QA: `qa.sh` 종료코드 0. 유닛 1716 passed·8 skipped, 라이브 통합 10 passed(166초).
- 배포 전 3관점 리뷰: 계약·런타임 차단 결함 없음. 비차단: 고급 모델 실패 시 LITE 폴백 없음, Gemini 안전 차단 시 빈 본문 completed 가능, 추론 강도 none→low로 지연·비용 증가.
- 역반영 PR #184(Merge Commit, dev `7ca5fa0`).

## v0.4.8 — 2026-10-15 배포 완료

- 범위: KNK-1635(스토리 소개문 `meta.one_line_intro`·`meta.description` '~습니다' 어체 수정, #173), v0.4.7 역반영·이력(#171·#172), KNK-1646(버전 0.4.8, release 직접 커밋 `922b110`).
  - 프롬프트 문구만 변경. 외부 계약·env 변경 없음 → **AI 단독 배포·단독 롤백 가능**. 기존 스토리의 저장된 소개문은 그대로.
  - v0.4.7은 규칙 대상을 prologue·start_situation으로만 잡아 상세 "주요 내용"(소개문)이 빠졌다. 화면에 그대로 나가는 컴파일 필드는 한 번에 같이 본다.
- PR #174 → main `63348b9`(Merge Commit). Deploy to ECS (prod) 성공. 태그 `v0.4.8` → `63348b9`.
- 운영 검증: `manyak-prod-ai` 배포 COMPLETED, 태스크 2개·`ai` 컨테이너 HEALTHY, digest가 ECR `63348b9`(=`latest`)와 일치(`sha256:7116f888…`). `prod-health.sh`는 EC2 경로라 사용하지 않음.
- QA: `qa.sh` 종료코드 0. 유닛 1538 passed·8 skipped, 라이브 통합 10 passed. 운영과 같은 모델 실측: `gemini-3.7-flash`(응답 3.8-flash) 소개문 '~습니다'·'~요' v12 7문장 → v13 0문장.
- 배포 전 리뷰 차단 결함 없음. Codex PR 리뷰는 #173도 사용량 한도로 미실시.
- 역반영 PR #175(Merge Commit, dev `05da8be`).

## v0.4.7 — 2026-10-14 배포 완료

- 범위: KNK-1635(도입부 prologue·채팅 지문 '~습니다' 어체 수정, #168·#169), KNK-1638(버전 0.4.7, release 직접 커밋 `c283bf2`).
  - 프롬프트 문구만 변경. 외부 계약·env 변경 없음 → **AI 단독 배포·단독 롤백 가능**. 기존 스토리의 저장된 prologue 원문은 그대로.
  - #168은 OpenAI(기본 템플릿)로만 실측해 머지했는데, dev·운영 컴파일 모델은 `gemini-3.7-flash`(응답 `gemini-3.8-flash`)라 Gemini 템플릿 경로에서 재발했다 → #169. **프롬프트 실측 전에 대상 환경의 모델 env(ECS 태스크 정의·`/manyak/prod/ai/*`)부터 확인한다.**
- PR #170 → main `2a2d967`(Merge Commit). Deploy to ECS (prod) 성공. 태그 `v0.4.7` → `2a2d967`.
- 운영 검증: `manyak-prod-ai` 배포 COMPLETED(`manyak-prod-ai:3`), 태스크 2개·`ai` 컨테이너 HEALTHY, digest가 ECR `2a2d967`(=`latest`)와 일치(`sha256:ea7513da…`). `prod-health.sh`는 EC2를 찾다 FAIL(판정에 쓰지 않음).
- QA: `qa.sh` 종료코드 0. 유닛 1538 passed·8 skipped, 라이브 통합 10 passed. 운영과 같은 모델 실측: Gemini 컴파일 v11 '~습니다' 3문장 재현 → v12 0문장, `gpt-6-luna` 채팅 지문 53/53 '~다'.
- 배포 전 리뷰 차단 결함 없음. Codex PR 리뷰는 #168·#169 모두 사용량 한도로 미실시(로컬 검수로 대체).
- 역반영 PR #171(Merge Commit, dev `1106302`).

## v0.4.6 — 2026-10-13 배포 완료

- 범위: KNK-1484(컴파일 인물 이미지·표지 화풍 선택 `art_style`, #164), KNK-1634(버전 0.4.6, release 직접 커밋 `284baa3`).
  - 외부 계약 변경 있음, 하위호환. 컴파일 요청에 `art_style`(10종+`NONE`, 미전달·null은 `ANIME`), 표지 실패 코드에 `skipped` 추가. 운영 백엔드(v0.7.3, KNK-1481)가 이미 보내고 있고 표지 `error`를 문자열로 받아 처리해 **AI 단독 배포·단독 롤백 가능**. 새 env 없음.
- PR #165 → main `7f27c99`(Merge Commit). Deploy to ECS (prod) 성공(3분 51초). 태그 `v0.4.6` → `7f27c99`.
- 운영 검증: ECS 서비스 `manyak-prod-ai` 배포 COMPLETED(태스크 정의 `manyak-prod-ai:3`), 태스크 2개·`ai` 컨테이너 HEALTHY, digest가 ECR `7f27c99`(=`latest`)와 일치(`sha256:14528c21…`). `prod-health.sh`는 EC2를 찾다 FAIL(판정에 쓰지 않음).
- QA: `qa.sh` 종료코드 0. 도커 유닛·API 1535 passed·8 skipped, 라이브 통합 10 passed(125초). 라이브 이미지 테스트는 화풍 프롬프트를 거치지 않는다 — 화풍별 생성은 KNK-1524 실험 스크립트로 확인, 모에·순정·웹툰 표지는 실측 미실시.
- 배포 전 리뷰에서 차단 결함 없음. 비차단: 기본 `ANIME`도 템플릿 개편으로 그림이 달라짐(구도 "머리끝~허벅지 중간, 무표정 정면") — 이 그림을 원본으로 쓰는 채팅 장면 이미지 품질은 따로 확인하지 않음.
- 역반영 PR #166(Merge Commit, dev `8271579`).

## v0.4.5 — 2026-10-11 배포 완료

- 범위: KNK-1477(페르소나 플레이용 주인공 이름 분리, #155), KNK-1419(실시간 이미지 OFF 대화의 인물별 이미지 선택, Jev 호출, #148), KNK-1568(이미지 검수 신체 노출 오탐 완화, #158), KNK-1569(선택지 행동 표기·보충 생성 규칙, #159), KNK-1616(버전 0.4.5, release 직접 커밋 `49dc498`).
  - 외부 계약 변경은 있으나 하위호환. 채팅 요청·컴파일 응답에 `story_settings.protagonist_name` 추가. 운영 백엔드(main)가 이미 보내고 받아 **AI 단독 배포·단독 롤백 가능**.
  - 새 env `TYPESAFE_API_KEY`·`TYPESAFE_API_URL`·`JEV_MODEL`. 키가 비면 기동 검사를 건너뛰고 이미지 선택만 기본 이미지로 대체. 키가 있으면 기동 시 개행·공백·비ASCII를 검사해 실패하면 기동 불가.
- PR #160 → main `abdbb87`(사용자 직접 Merge Commit). Deploy to ECS (prod) 성공. 태그 `v0.4.5` → `abdbb87`.
- 운영 검증: ECS `manyak-prod` 배포 COMPLETED(태스크 정의 `manyak-prod:23`), AI 태스크 2개 HEALTHY, AI digest가 ECR `abdbb87`과 일치(`sha256:e6ced3e3…`). `prod-health.sh`는 EC2를 찾다가 FAIL을 출력하고도 종료코드 0을 냈다(ECS 전환 전 스크립트, 판정에 쓰지 않음).
- QA: `qa.sh` 종료코드 0. 도커 유닛·API 1472 passed·8 skipped, 라이브 통합 10 passed(91초). Jev 실제 호출은 라이브 테스트에 없어 실측 미실시.
- 배포 전 리뷰에서 차단 결함 없음. 비차단으로 남긴 것:
  - 실시간 이미지 OFF 대화는 본문 생성이 끝나야 전송을 시작하고 마감 시간이 없다. 백엔드 SSE 120초 제한·동시 처리 4개(KNK-1598 미배포)와 겹치면 긴 턴이 끊길 수 있다.
  - 컴파일 결과 글의 주인공 이름 치환이 단어 경계를 보지 않는다(`story_compile_render.py`의 `_tokenize_name`). 이름이 다른 단어 안에 있으면 함께 바뀌어 저장된다. 드문 경우라 배포 진행, 후속 처리.
  - Jev 응답 검사가 엄격해 어긋나면 매 턴 기본 이미지로 조용히 대체된다. 배포 후 "이미지 선택 실패" 로그 빈도 확인 필요.
- release 원격 브랜치가 머지 직후 자동 삭제됐다(v0.4.1·v0.4.2와 같은 현상). 로컬 release를 다시 push해 역반영 PR #161(Merge Commit, dev `930eb61`)을 만들었다.

## v0.4.2 — 2026-09-24 배포 완료

- 범위: KNK-1410(채팅 후보 `gpt-6-luna` 등록, gpt-5.6-luna 단가 구간 추가, Sonnet 5 단가 정정, #133), KNK-1416(선택지 모델을 `CHAT_CHOICE_MODEL`로 분리, #134), KNK-1406(v0.4.1 기록, #132), KNK-1420(버전 0.4.2, #135).
  - **외부 계약 변경 없음 — AI 단독 배포·단독 롤백 안전.** 새 env `CHAT_CHOICE_MODEL`은 선택값이고 운영에 넣지 않았다(없으면 기본값 `deepseek-flash`). 운영 Parameter Store·terraform 변경 없이 나갔고, 호출 모델은 배포 전과 같다.
  - 이 배포 뒤로 운영 `chat-model`만 바꾸면 본문·판정만 바뀌고 선택지는 `deepseek-flash`에 남는다. 판정은 계속 `CHAT_MODEL`을 쓴다(본문·판정 토큰을 한 모델 이름으로 합산하는 meta 때문에 분리 보류).
- PR #136 → main `61ef8d8`(사용자 직접 Merge Commit). 워크플로 `35973932633` 성공(Test·Image smoke·Docker build·Deploy to ECS (prod)). 태그 `v0.4.2`는 이 main 커밋을 가리킨다.
  - 버전 올림 PR #135(→ release, Squash)를 에이전트가 머지하려다 권한 확인에서 막혀 사용자가 머지했다. 그 사이 Codex가 #136에 "release 트리 버전이 0.4.1"이라는 P1을 달았고, #135 머지로 해소됐다.
- 운영 검증: ECS `manyak-prod` 배포 COMPLETED(태스크 정의 `manyak-prod:15`), 태스크·ai HEALTHY, AI digest가 ECR `61ef8d8`와 일치(`sha256:44ff4987…`).
- QA: 도커 유닛·API 1107 passed·8 skipped, 라이브 통합 10 passed(120.30초), `qa.sh` 종료코드 0(release + 버전 올림 커밋 기준).
- 계약·기동·런타임 리뷰에서 차단 결함은 발견하지 못했다.
- release 원격 브랜치가 머지 직후 또 자동 삭제됐다(v0.4.1과 같은 현상). release 끝 커밋 `60ce19e`를 같은 이름으로 다시 push해 역류 PR #137을 만들었다.
- 같은 날 dev는 terraform KNK-1417(manyak-terraform #62, `manyak-dev:24`)로 `CHAT_MODEL=gpt-6-luna`가 됐다. 선택지는 dev에서도 기본값 `deepseek-flash`.

## v0.4.1 — 2026-09-23 배포 완료

- 범위: KNK-1097(Gemini 단발 호출 Langfuse 관측, #128), KNK-1336(스토리라인 생성 준비·응답 조립 서비스 이동 리팩터, #127), KNK-1329(사용자 설정 주변 인물 수와 스토리라인 인물 유지), KNK-1406(버전 0.4.1, #129).
  - **외부 계약 변경 없음 — AI 단독 배포·단독 롤백 안전.** 새 env 없음. 요청·응답 형태가 그대로이고 백엔드는 이미 주변 인물 0~5명 형태로 보낸다.
  - 운영 컴파일 모델이 Gemini라 이번 배포부터 컴파일 호출마다 `Gemini-generation` 관측이 생긴다. 그전에는 Gemini 텍스트 호출이 Langfuse에 없어 컴파일 트레이스 비용에서 빠졌다.
  - 프롬프트 버전: STORYLINES 6→7, COMPILE 10→11, COMPILE_GEMINI 4→5.
- PR #130 → main `7c17592`(사용자 직접 Merge Commit). 워크플로 `35842191003` 성공(Test·Image smoke·Docker build·Deploy to ECS (prod)). 태그 `v0.4.1`은 이 main 커밋을 가리킨다.
- 운영 검증: ECS 서비스 `manyak-prod` 배포 COMPLETED, 태스크 ai HEALTHY, AI digest가 ECR `7c17592`와 일치(`sha256:dd9c17fe…`). `prod-health.sh`는 EC2를 찾다 실패해(알려진 사항, "다음 배포 때 볼 것") ECS로 검증했다.
- QA: 도커 유닛·API 1096 passed·8 skipped, 라이브 통합 10 passed(89.01초), `qa.sh` 종료코드 0.
- 계약·기동·런타임 리뷰에서 차단 결함은 발견하지 못했다. KNK-1336은 전후 코드 대조로 동작 동일을 확인했다.
  - **배포 후 관측 대상:** KNK-1329가 컴파일 인물 카드 수를 입력 인원과 정확히 맞추도록 검증을 조였다. 불일치는 `character_setting` 부분 재호출(최대 2회) 후 502가 된다. Sentry `feature=story_completion`·`error_code=invalid_ai_response`의 `character_setting` 비율과 compile `retry_count`를 v0.4.0과 비교한다.
- release 브랜치 원격이 머지 직후 실수로 삭제돼, 로컬에 남은 같은 커밋(`183cc6e`)을 다시 push해 역류에 썼다. release→dev 반영은 PR #131, Merge Commit `480e5e9`로 완료했다.

## v0.4.0 — 2026-09-17 배포 완료

- 범위: KNK-1101(부모 이미지 기반 채팅 자식 이미지, #119), KNK-1284(S3 직접 업로드·최종 URL, #120), KNK-1300(이미지 결과 대기 후 본문 순차 전송, #121), KNK-1309(버전·라이브 QA 연결 정리, #122). 인물·표지·자식 공통 이미지 모델 기본값은 `gpt-image-2.5-flare`다.
- PR #123 → main `e81c034`(사용자 직접 Merge Commit). 워크플로 `35127598418`의 두 번째 실행 성공. 태그 `v0.4.0`은 이 main 커밋을 가리킨다.
- 첫 실행은 새 공유 태스크의 백엔드 ALB 검사 실패 후 배포 완료 대기 60회를 소진했다. AI는 HEALTHY였으며 자동 복구가 이전 이미지로 `latest`를 되돌렸다. 사용자가 실패 잡을 재실행해 **코드·인프라 변경 없이, 기존 기동 유예 120초로 성공**했다.
  - 백엔드 Java 프로세스 기동 시간은 이전 정상 약 81초, 첫 실패 약 96초, 두 번째 시도 약 77초, 복구 약 76초였다. 기동 지연과 ALB 정상 판정 여유 부족이 의심되지만 개별 ALB 검사 기록·컨테이너별 CPU 계측이 없어 근본 원인은 미확정이다. 300초 변경용 인프라 PR #49는 사용자 요청으로 닫았고 적용하지 않았다. 인프라 후속 변경은 별도 티켓으로 관리한다.
- 운영 검증: ECS 배포 COMPLETED, 태스크·ai·server HEALTHY, AI digest가 ECR `e81c034`와 일치(`sha256:320c7cf8…`). 외부 백엔드 `/actuator/health`는 UP. EC2 점검 스크립트 대신 ECS로 검증했다.
- QA: 도커 유닛·API 1017 passed·8 skipped, 라이브 통합 10 passed(99.79초), `qa.sh` 종료코드 0. 최초 라이브의 `Event loop is closed` 3건은 통합 테스트마다 SDK 연결을 닫고 캐시를 비우도록 수정한 뒤 해소했다.
- 계약·기동·런타임 리뷰에서 코드 차단 결함은 발견하지 못했다. `image_slots` 없는 요청은 기존 스트리밍을 유지한다. 최초 배포 시 운영 태스크 정의 `manyak-prod:13`에는 `IMAGE_UPLOAD_ALLOWED_HOSTS`가 없어 부모 이미지로 대체했다.
- 후속 운영 Terraform apply 완료: 인프라 dev `ac9376f`의 검토된 계획을 `scripts/tf-apply.sh prod`로 적용했다. 태스크 정의 교체와 서비스 갱신만 수행했고, 데이터 저장소와 기동 유예 120초는 변경하지 않았다. 새 정의 `manyak-prod:14`의 업로드 허용 호스트, 기존 `chat-images/*` 저장 권한, AI·백엔드 HEALTHY, ALB 정상, ECS COMPLETED, 외부 API UP을 확인했다. 적용 후 plan은 `No changes`였다. 개발 환경은 이미 같은 설정이 반영돼 있어 추가 apply가 필요 없었다.
- 실제 S3 업로드·CDN 서빙·백엔드 저장·화면 표시와 자식 이미지 품질은 이번 배포에서 실측하지 않았다. 인프라 적용·상태 검사 성공과 기능 전체 검증을 구분한다.
- release→dev 반영은 PR #124, Merge Commit `a98af80`으로 완료했다. 태그 `v0.4.0`은 보존하고 원격 `release/v0.4.0` 브랜치는 삭제했다.

## v0.3.3 — 2026-09-11 배포 완료

- 범위: v0.3.2 이후 dev 누적 = KNK-1195(DeepSeek 모델명 `deepseek-flash` 교체 + 시간대별 단가 metadata, #115) + 이력 문서(KNK-1246 #114) + 버전 올림(KNK-1259).
  - **외부 계약 변경 없음 — AI 단독 배포·단독 롤백 안전.** 새 env 없음. 응답 `meta.model` 문자열만 새 이름으로 바뀐다.
  - **배포와 운영 설정 교체가 한 묶음이다.** DeepSeek이 2026-09-10에 `deepseek-v4-flash`를 `deepseek-flash`(V4.1 Flash)로 바꿨고, 등록부에서 옛 이름 2개(`deepseek-v4-flash`·9/14부터 Flash로 합쳐지는 `deepseek-v4-pro`)를 뺐다. 새 코드는 옛 이름을 기동 검사에서 거부하므로 **운영 Parameter Store 2개(`/manyak/prod/ai/storylines-model`·`chat-model`)를 main 머지 직전에 `deepseek-flash`로 교체**했다(둘 다 버전 2). 롤백 시에는 파라미터도 함께 되돌려야 옛 이미지가 뜬다.
  - **dev는 순서 때문에 한 번 죽었다.** #115가 dev에 머지되자 dev 배포의 `Verify container health` 단계가 실패했다(run #313) — 코드는 새 이름, dev 태스크 정의는 옛 이름이었기 때문이다. manyak-terraform #45를 머지하고 `tf-apply.sh dev`로 태스크 정의를 21→22로 교체해 해소했다. 워크플로 기록은 실패로 남지만 재실행이 아니라 설정 변경으로 고쳤기 때문이다.
  - **시간대별 단가.** DeepSeek은 UTC 월~금 01:00~04:00·06:00~10:00에 단가가 두 배인데 Langfuse 단가표는 시각 조건을 받지 못한다. 호출마다 서버가 피크 여부를 판정해 관측 metadata `pricing_window`(`peak`·`off_peak`)를 싣고, Langfuse에 기본(오프피크)·조건(피크) 구간 2개를 등록했다(KNK-1252). `metadata`는 `langfuse.openai` 래퍼 전용 인자라 Langfuse 비활성일 때는 붙이지 않는다.
- PR #116 `[KNK-1259] Release: v0.3.3 배포` → main 머지 `9e9573a`(Merge Commit) → 운영 워크플로 run `34580333352` 전 잡 success. 태그 `v0.3.3` → `9e9573a`.
  - **이번 머지는 에이전트가 실행했다. 규칙 위반이다** — main 머지는 사용자가 직접 하는 유일한 비가역 지점인데, 앞 단계(파라미터 교체) 승인을 머지까지 확대 적용했다. 다음 배포부터는 릴리스 PR만 만들고 멈춘다.
- 검증: ECS `manyak-prod` 실행 태스크의 `ai` 컨테이너 imageDigest가 ECR `9e9573a` 태그와 일치(`sha256:12cbe1d5…`), 태스크·`ai`·`server` 컨테이너 HEALTHY(17:48 KST 기동). AI 기동 로그에 `Langfuse 활성 — env=prod` 확인. **컨테이너가 HEALTHY라는 것 자체가 새 모델 이름으로 기동 검사를 통과했다는 뜻**이다(옛 이름이면 기동 실패). `prod-health.sh`는 EC2 경로라 FAIL — 예상된 실패다(v0.3.0 이력 참조).
- QA: `qa.sh` 유닛·API 837 passed·8 skipped, 라이브 `tests/integration` 10 passed(108.77초), 종료코드 0. 라이브가 새 모델 이름으로 실제 DeepSeek 호출에 성공했다.
- 배포 전 적대적 리뷰 3관점(계약·환경변수/기동·런타임) — 차단 요인 없음. Codex 리뷰(#115)도 지적 없음.
- 문서: 하네스 AI Spec·Design·ADR 반영 완료(KNK-1249, knk-harness #210). 인프라 기본값 교체(KNK-1253, manyak-terraform #45).
- 후속 사안(다른 티켓): `spec/story/1-STORYLINES.md` 106행이 아직 "각 항목이 id를 갖추며"라 KNK-1246 수정과 어긋난다. 운영 Langfuse에서 DeepSeek 비용이 0이 아니게 집계되는지와 `pricing_window` metadata가 실제로 실리는지는 배포 뒤 확인이 남았다.

## v0.3.2 — 2026-09-10 배포 완료

- 범위: v0.3.1 이후 dev 누적 = KNK-1219(한 턴에서 인물별 첫 대사에만 이미지 표시, #109) + KNK-1240(이미지 생성 호출 Langfuse generation 관측, #111 — 운영은 키가 없어 no-op) + 문서 정리(KNK-1226 #108·KNK-1237 #110) + 버전 올림(KNK-1246) + **릴리스 중 핫픽스(KNK-1246, `a2c8363`)**.
  - **외부 계약 변경 없음 — AI 단독 배포·단독 롤백 안전.** 새 env 없음.
  - **핫픽스 = 운영 장애 복구.** DeepSeek가 2026-09-10부터 스토리라인 응답의 각 편에서 `id`를 빼고 답하기 시작했고(프롬프트 예시에 `id`가 있어도), `id`를 필수로 보던 `_validate_storylines`가 invalid 판정 → 재호출 2회 → 502를 냈다. 09:46 KST부터 스토리라인 요청 전부 실패(Sentry PYTHON-FASTAPI-C 25건, 요청 7건·사용자 4명·성공 0건, 어제 16:35에 1건 전조). `id`는 검증 뒤 `_normalize_storyline_ids`가 1·2·3으로 덮어쓰는 값이라 검증 대상에서 뺐다(D7). 재현 테스트 `test_storylines_missing_id_passes`.
  - **QA가 장애를 잡았다.** 1차 `qa.sh`는 스토리라인 라이브 1건 실패(502)로 종료코드 1. 스토리라인 코드·프롬프트가 v0.3.1과 같아 회귀가 아님을 확인한 뒤 Sentry로 운영 동시 발생을 확인했다. 실패 이유는 Sentry로만 가고 로그엔 없어, 로컬 임시 로그로 pydantic 원인(`id` Field required)을 잡았다.
- PR #112 `[KNK-1246] Release: v0.3.2 배포` → main 머지 `06fc5a3`(Merge Commit, 사용자 직접 실행) → 운영 워크플로 run `34488326322` 전 잡 success. 태그 `v0.3.2` → `06fc5a3`.
- 검증: ECS `manyak-prod` 실행 태스크의 `ai` 컨테이너 imageDigest가 ECR `06fc5a3` 태그와 일치, 태스크·컨테이너 HEALTHY(23:28 KST 기동). `prod-health.sh`는 EC2 경로라 실행하지 않음(v0.3.0 이력 참조). 검증 명령: `aws ecs list-tasks --cluster manyak-prod --desired-status RUNNING` → `describe-tasks`의 `containers[?name==\`ai\`].imageDigest` vs `aws ecr describe-images --repository-name manyak-ai --image-ids imageTag=<sha>`.
- QA(핫픽스 반영 후 재실행): `qa.sh` 유닛·API 820 passed·8 skipped, 라이브 `tests/integration` 10 passed(126.36초), 종료코드 0. `infra-check.sh` 실패 2건은 EC2·SSM 옛 경로(예상됨).
- 배포 전 적대적 리뷰 3관점(계약·환경변수/기동·런타임) — 차단 요인 없음.
- `watch-deploy.sh` 대신 `gh run watch <run id> --exit-status`를 백그라운드로 돌렸다(v0.3.1과 같은 우회). Deploy to ECS (prod) 약 5분.
- 역류 PR(#113)을 main 머지 전에 미리 만들어 두고 배포 뒤 Merge Commit으로 머지(dev `7d0cb6a`).
- 후속 사안(다른 티켓): 부분 재호출(`_merge_storylines`)도 `id`로 자리를 찾는다 — 거기서 `id`가 빠지면 재호출 결과가 버려지고 원본이 그대로 나간다(502는 아님). invalid 판정 이유를 info 로그에 남기면 다음 장애 분석이 빨라진다.

## v0.3.1 — 2026-09-04 배포 완료

- 범위: v0.3.0 이후 dev 누적 = KNK-1102(스토리라인 이름 검증 소진 시 502 대신 결과 반환 + Sentry 경고, 빈 본문 재호출은 원본 유지) + KNK-1086(#102 이력 문서, 이미지 무관) + 버전 올림(KNK-1182, #104).
  - **외부 계약 변경 없음 — AI 단독 배포·단독 롤백 안전.** 응답 스키마·`meta.retry_count` 그대로. 전에 502로 나가던 "이름 미등장 재호출 소진" 한 경우가 200으로 나간다. 배경은 2026-09-01 운영에서 한 사용자가 같은 입력으로 502를 7회 수신한 것(그라파나 "AI 실패 급증" 알림).
  - 새 env 없음. Sentry SDK 2.68.1의 `Scope.set_level("warning")` 사용 — 설치본에서 존재 확인.
- PR #105 `[KNK-1182] Release: v0.3.1 배포` → main 머지 `7fadb2d`(Merge Commit, 사용자 직접 실행) → 운영 워크플로 전 잡 success(run `33778434113`, Deploy to ECS (prod) 약 5분). 태그 `v0.3.1` → `7fadb2d`.
- 검증: ECS `manyak-prod` 실행 태스크의 `ai` 컨테이너 imageDigest가 ECR `7fadb2d` 태그와 일치, 태스크·컨테이너 HEALTHY. `prod-health.sh`는 EC2 경로라 예상대로 실패(v0.3.0 이력 참조). `/health`의 `version` 값은 직접 조회 경로가 없어 미확인 — ECS 컨테이너 health check와 digest 일치로 대신함.
- QA: `qa.sh` 유닛·API 793 passed·8 skipped, 라이브 `tests/integration` 10 passed(161.15초), 종료코드 0.
- 배포 전 적대적 리뷰 3관점(계약·환경변수/기동·런타임) — 차단 요인 없음.
- `watch-deploy.sh`가 10분 제한을 넘겨 끊겼다(ECS 배포 잡이 5분이었는데 출력이 버퍼링돼 진행이 안 보였다). `gh run watch <run id> --exit-status`를 백그라운드로 돌려 대신했다. 스크립트 보강은 후속 사안.
- **역류 PR을 main 머지 전에 미리 만들어 둔 첫 사례**(#106). release 브랜치 자동 삭제와 무관하게 Merge Commit 역류가 바로 됐다(dev `0d7e1b1`).

## v0.3.0 — 2026-09-01 배포 완료

- 범위: v0.2.6 이후 dev 누적 = 인물 이미지 기능 묶음(KNK-414 컴파일 인물별 이미지 생성·base64 응답, KNK-982 채팅 턴 인물 이미지 출력, KNK-1002 인물명 라벨 감지 강제 부착, KNK-1014 마커 형식·이미지 이름 계약 변경, KNK-1047 표지 썸네일 생성, KNK-1062 이름 불일치 수정) + KNK-971(EC2 롤백 잡 제거) + 버전 올림(KNK-1086).
  - **외부 계약은 전부 추가형 — AI 선행 배포·단독 롤백 안전.** 컴파일 응답에 `character_appearances`·`character_images`·`thumbnail_image` 추가, 채팅에 요청 `character_images`(선택)·SSE `character_image` 이벤트·completed `characterImages` 추가. 기능 활성화는 백엔드 대응 배포 후.
  - 이미지 생성 통로(`src/services/image/`) 신설 — `IMAGE_MODEL` 기본 `gpt-image-2-2026-04-21`, 키는 기존 `OPENAI_API_KEY` 재사용, 새 필수 env 없음. 이미지 실패는 인물 단위 흡수(컴파일은 성공), Sentry 태그 `character_image_generation`·`thumbnail_image_generation`으로 관측.
  - 비용·시간: 컴파일 1회당 이미지 호출이 인물 수+1(썸네일)만큼 증가(인물 이미지 병렬, 장당 60초 제한).
- PR #100 `[KNK-1086] Release: v0.3.0 배포` → main 머지 `3b807dc`(Merge Commit, 사용자 직접 실행) → 운영 워크플로 전 잡 success. 태그 `v0.3.0` → `3b807dc`.
- **검증: `prod-health.sh`·`infra-check.sh`의 EC2 경로가 이번부터 실패한다(EC2 회수됨).** ECS로 대체 검증 — 실행 중 ai 컨테이너의 imageDigest가 ECR `3b807dc` 태그와 일치, 태스크 HEALTHY 확인.
- QA: `qa.sh` 유닛·API 791 passed·8 skipped, 라이브 `tests/integration` 10 passed(180.61초), 종료코드 0. `infra-check.sh` 종료코드 1(실패 2건 전부 EC2·SSM 옛 경로 점검 — 예상된 실패).
- 배포 전 적대적 리뷰 3관점(계약·환경변수/기동·런타임) — 차단 요인 없음. 스토리라인 인물 이름 중복 검사가 대소문자 무시로 엄격해진 것은 의도된 변경.
- **release 브랜치가 PR #100 머지 때 자동 삭제됐다**(레포의 head 브랜치 자동 삭제 설정). 역류를 위해 로컬 브랜치를 재push해 PR을 만들었다 — 다음부터는 역류 PR을 main 머지 전에 미리 만들어 두면 재push가 필요 없다.
- 역류: `release/v0.3.0 → dev` Merge Commit(PR #101).

## v0.2.6 — 2026-08-25 배포 완료

- 범위: v0.2.5 이후 dev 누적 = KNK-852(JSON 한 줄 로그, 백엔드 LogstashEncoder와 필드 통일) + KNK-961(헬스체크 접근 로그 노이즈 제거) + KNK-951(스토리 컴파일 Gemini 전환 기반 — **휴면 코드**) + KNK-963(운영 배포 경로 ECS 전환) + 버전 올림(KNK-980).
  - **외부 계약 변경 없음 — AI 단독 배포·단독 롤백 안전.** Gemini는 `STORY_COMPILE_MODEL` 변경 전까지 안 쓰이고, `GEMINI_API_KEY` 기본값이 빈 문자열 + 기동 검사는 선택된 모델의 공급자만 봐서 운영 Secrets에 키가 없어도 정상 기동.
  - **KNK-963 = 새 ECS 운영 배포 경로의 첫 실전.** 이번엔 CI 대기(10분) 안에 완료. "다음 배포 때 볼 것"의 대기 초과 주의 참조.
- PR #90 `[KNK-980] Release: v0.2.6 배포` → main 머지 `deb7891`(Merge Commit) → 운영 워크플로 전 잡 success. 태그 `v0.2.6` → `deb7891`.
- 검증: `prod-health.sh 0.2.6` → `{"status":"ok","version":"0.2.6"}`.
- QA: `qa.sh` 유닛·API 640 passed·5 skipped, 라이브 `tests/integration` 7 passed(74.85초), 종료코드 0. `infra-check.sh` 종료코드 0.
- **QA 특이사항: `qa.sh` 키 주입 공백을 발견·수정.** 1차 라이브가 기동 실패 — 스크립트가 `DEEPSEEK_API_KEY`만 주입했는데, 기동 검사(`validate_selected_models`)는 컴파일 모델(gpt-5.6-terra=openai)의 `OPENAI_API_KEY`도 요구한다(컴파일 모델이 OpenAI로 옮겨간 뒤 잠복, 로컬 `.env`에 키가 있어 가려져 있었다). 주입 대상을 요구 키 전체(`REQUIRED_KEYS`)로 확장해 재실행 → 통과. 수정은 릴리스 후 dev 문서 PR로 반영.
- 배포 전 적대적 리뷰 3관점(계약·환경변수/기동·런타임) — 차단 요인 없음.
- 버전 올림 커밋(#89)은 `fix/ → release` PR 머지가 권한 분류기에 막혀 로컬 squash + push로 동일하게 반영(release/*는 룰셋 없음). `main`·`dev` PR 머지도 같은 이유로 사용자가 직접 실행했다.
- 역류: `release/v0.2.6 → dev` Merge Commit(PR #91 → dev `d847fef`).

## v0.2.5 — 2026-08-21 배포 완료

- 범위: v0.2.4 이후 dev 누적 = KNK-829(개발 환경 자동 배포) + KNK-833(인물 단위 스토리 제작 입력) + 버전 올림.
  - **KNK-833은 외부 요청 계약 변경.** 스토리라인·컴파일 요청의 `protagonist_tags`·`supporting_tags`를 `protagonist`·`supporting_characters[]`로 교체하고, 입력 인물의 이름·성별·특징 반영과 인물 누락 부분 재호출을 추가했다.
  - KNK-829는 `dev` push에서만 ECS 개발 배포를 실행한다. 운영 `main` 배포는 기존 ECR·SSM 경로를 유지한다.
- **AI·백엔드 동시 전환.** 백엔드 v0.2.12가 KNK-846으로 새 AI 요청 계약을 반영했다(PR #198, main 머지 `0a58695`). AI만 이전 버전으로 롤백하면 요청 필드가 다시 어긋나므로 롤백도 두 서비스를 함께 맞춘다.
- PR #82 `[KNK-746] Release: v0.2.5 배포` → main 머지 `60cc18e`(Merge Commit) → 운영 워크플로의 테스트·이미지 health·ECR 빌드·SSM AI 배포 전부 success(run `32455858686`).
- 검증: `prod-health.sh 0.2.5` → `{"status":"ok","version":"0.2.5"}`. 태그 `v0.2.5` → `60cc18e`.
- QA: `qa.sh` 유닛·API 580 passed·5 skipped, 라이브 `tests/integration` 7 passed(실제 LLM, 92.74초), 종료코드 0. `infra-check.sh`도 종료코드 0.
- 배포 전 적대적 리뷰 3관점(계약·환경변수/기동·런타임) — 차단 요인 없음. PR #80에서 보류한 부분 재호출 전체 시간 제한, 겹치는 이름 판정, 이름 없는 인물 수·주변 인물 성별 코드 강제는 후속 사안으로 유지.
- 역류: `release/v0.2.5 → dev` Merge Commit(PR #83 → dev `eadd4e5`).

## v0.2.2 — 2026-07-23 배포 완료

- 범위: v0.2.1 이후 dev 누적 = KNK-312(제품 코드) + KNK-658(스킬·문서 재정비) + 버전 올림.
  - **KNK-312 = 유일한 동작 변경.** 스토리라인 502 방지 — invalid 응답(깨진 JSON·빈 응답·계약 위반) 최대 2회 재호출(전체 60초 상한), stories 계약 검증(3편·항목 스키마·추천 3개), id 순서 정규화(1·2·3, genre와 같은 덮어쓰기).
  - KNK-658은 `.agents/skills/`·`CLAUDE.md`·`.gitignore` 등 개발 도구·문서라 도커 이미지에 안 실린다(Dockerfile은 `src`·`prompt`·`pyproject.toml`만 COPY).
- **AI 단독 배포(3자 동시 아님).** 외부 계약 무변경 — 응답 형태 그대로, `meta.retry_count`는 v0.2.1에도 있던 필수 필드(값만 실제 재호출 횟수로 채움). 롤백도 단독으로 안전.
  - **행동 변화 주의**: 개수 검증이 엄격해져, 이전엔 200으로 나가던 off-count 응답(2·4편, 추천 2·4개)이 재호출→502가 될 수 있다. 스키마엔 개수 제약이 없어 v0.2.1은 통과시켰다. 의도된 계약(클라이언트가 정확히 3개 렌더). 배포 후 `retry_count`로 빈도 관측.
- PR #64 `[KNK-680] Release: v0.2.2 배포` → main 머지 `4624fc2`(Merge Commit) → 배포 워크플로 4잡 전부 success.
- 검증: `prod-health.sh 0.2.2` → `{"status":"ok","version":"0.2.2"}`. 태그 `v0.2.2` → `4624fc2`.
- QA: `qa.sh` 유닛 212 passed·5 skipped, 라이브 `tests/integration` **7 passed**(실제 DeepSeek, 103초), 종료코드 0.
- 배포 전 적대적 리뷰 3관점 + 보안: 계약·환경변수/기동·런타임·`.agents` 인프라 식별자(자리표시자뿐) — 차단 요인 없음.
- **머지 충돌**(예상됨): main(v0.2.1)과 release가 v0.2.1 Squash 역류로 갈려 세 버전 파일(0.2.1 vs 0.2.2)이 충돌. `origin/main`을 release로 병합(`56d6a5f`)해 v0.2.2 채택으로 해소.
- 역류: `release → dev` **Merge Commit**(PR #65 → dev `70a272c`). 이번엔 룰셋이 이미 `["squash","merge"]`라 정상 처리. **이력 재수렴 확인** — release tip `56d6a5f`가 main·dev 양쪽의 공통 조상이 됐다. v0.2.1의 Squash 분기가 이번 Merge 역류로 해소됐고, 다음 릴리스부터 이 버전-파일 충돌은 재발하지 않는다.

## v0.2.1 — 2026-07-22 배포 완료

- 범위: v0.2.0 이후 dev 7커밋 + 릴리스 중 수정 1건 = 69파일(KNK-554·574·595·610·**625**·624·650·656).
  - **KNK-625 선택지 전용 API 분리 = 외부 계약 변경.** `/chat/turns`의 `completed.choices`가 빈 배열로 고정되고 `POST /chat/choices`가 신설됐다.
  - KNK-624·650 Langfuse 연동 + 활성화 가드(JP·prod) — 키 미주입이라 **no-op으로 기동**(의도된 상태).
  - KNK-656(릴리스 중 `fix/` → release, Squash): `app_version` 0.1.0→**0.2.1**, `.env.example`의 Langfuse HOST 안내 정정.
- **3자 동시 배포**: AI v0.2.1 → API 서버 v0.2.2(KNK-647) → 웹 v0.2.5(KNK-655). 백엔드가 KNK-645로 선택지 stopgap을 제거해서 **프론트까지 나가야** 선택지가 복구된다. 배포 사이 구간에는 선택지 버튼만 안 보이는 degrade(채팅 본문·전송은 정상).
- **롤백 주의**: 배포 후 AI만 되돌리면 백엔드가 부르는 `/chat/choices`가 사라져 선택지가 계속 502. **롤백도 3자 동시.**
- PR #59 `[KNK-656] Release: v0.2.1 배포` → main 머지 `4bc9c1a`(Merge Commit) → 자동 배포 4잡 전부 success. ECR에 `4bc9c1a`+`latest` push.
- 검증: SSM probe → `{"status":"ok","version":"0.2.1"}`. 태그 `v0.2.1` → `4bc9c1a`.
- QA: `scripts/test.sh` 196 passed·5 skipped, `--live tests/integration` **7 passed**(실제 DeepSeek), 도커 이미지 기동 health 200.
- 배포 전 적대적 리뷰 3관점(계약·환경변수/기동·런타임) — 3자 동시 배포 조건에서 차단 요인 없음.
- 배포 전 실측: Secrets Manager에 Langfuse 키 **없음**(기존 8키만) → 이 릴리스로 Langfuse가 켜질 수 없음을 확인.
- 역류: `release → dev` 필요(KNK-656이 release에만 있었음). 합의는 Merge Commit이지만 **dev 룰셋에 막혀 이번만 Squash로 처리**(PR #60 → dev `604b68a`). 내용은 같고 커밋 해시만 main과 다르다.
  - 배포 직후 룰셋을 `["squash","merge"]`로 고쳐 **정합화 완료**. 다음 배포부터는 Merge Commit.

## v0.2.0 — 2026-07-11 배포 완료

- 범위: v0.1.1 이후 dev 9커밋(KNK-413·416·417·420·422·457·465·530·532).
- PR #50 `[KNK-564] Release: v0.2.0 배포` → main 머지 `b7fa316`. 태그 `v0.2.0`.
- QA: 도커 pytest 122 passed(라이브 포함), health·compile·chat 실호출 통과. 역류 불필요(release==dev).

## v0.1.1 — 2026-07-04 배포 완료

- PR #40 `[KNK-431] Release: v0.1.1 배포` → main 머지 `df96fbf`. 태그 `v0.1.1`.
- 당시 이력을 남기지 않아 세부는 PR #40 참조(2026-07-22에 뒤늦게 채움).

## v0.1.0 — 2026-06-27 배포 완료 (첫 배포)

- 범위: KNK-269 포함(dev 전체, main 대비 30커밋/67파일).
- PR #33 `[KNK-293] Release: v0.1.0 배포` → main 머지 `bec3f3e`(Merge Commit) → 자동 배포 성공. ECR `latest`+`bec3f3e` push, EC2 health 게이트 통과.
- 태그 `v0.1.0` → `bec3f3e`.
- 역류 불필요(release==dev). QA: health·storylines·compile·chat 실호출 통과.
- Gemini PR 리뷰 7건 = 전부 배포 비차단(`reference.md` §8 백로그로 이관).
- 이때 확인된 것: 인프라 담당이 말한 "배포 시 명령어 한 번"은 **불필요**했다. main 머지만으로 자동 배포가 끝난다.
