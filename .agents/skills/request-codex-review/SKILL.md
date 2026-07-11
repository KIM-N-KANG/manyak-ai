---
name: request-codex-review
description: 이미 올라간 PR을 ready로 전환하고 `@codex review` 코멘트로 Codex 리뷰를 호출한 뒤, 리뷰가 달릴 때까지 기다렸다가 지적을 판단·반영하는 리뷰 루프를 수행할 때 사용합니다. "ready pr 올리고 @codex review 코멘트 달아서 리뷰 기다려줘", "코덱스가 리뷰 달건데 계속 확인하다가 달리면 반영할지 판단해봐", "@codex review 코멘트로 호출", "리뷰 반영하고 재리뷰 받아줘"처럼 요청할 때 사용합니다. Codex는 **draft PR을 무시**하므로 ready 전환이 선행돼야 하고, 호출은 PR 코멘트로만 트리거되며, 리뷰 도착은 폴링으로 감지해야 하는 비자명한 규칙이 있으니 직접 추측하지 말고 이 스킬을 따르세요. 새 PR을 만드는 일은 `create-pr`로, 이 스킬은 그다음 단계입니다.
---

# Codex 리뷰 요청·반영 루프

## 목적

`create-pr`로 만든 PR에 Codex 리뷰를 받고, 지적을 판단해 반영하고, 필요하면 재리뷰까지 받는 한 사이클을 끝냅니다. 핵심은 "코멘트만 남기는 것"이 아니라, **Codex가 실제로 리뷰를 시작하게 만들고(draft 함정 회피), 리뷰가 도착할 때까지 기다렸다가, 지적의 경중을 판단해 반영하는 것**입니다.

## 비자명한 규칙 (반드시 지킴)

- **Codex는 draft PR을 리뷰하지 않습니다.** 반드시 ready로 먼저 전환합니다. (메모리: codex-review-needs-ready-pr)
- 리뷰 호출은 PR 본문이 아니라 **PR 코멘트 `@codex review`** 로만 트리거됩니다.
- 리뷰는 즉시 오지 않습니다. **폴링으로 도착을 감지**합니다(보통 수 분~20분).
- Codex 봇 계정은 `chatgpt-codex-connector[bot]`입니다.
- **Codex는 지적이 있을 때만 정식 review(`pulls/{n}/reviews`)를 만듭니다. 지적이 없으면 "Didn't find any major issues" 같은 이슈 코멘트(`issues/{n}/comments`)나 호출 코멘트의 👍 리액션으로만 응답합니다.** 그래서 `reviews` 수만 폴링하면 "지적 없음" 응답을 놓쳐 timeout이 납니다. **리뷰 도착은 리뷰 수 증가 · Codex 봇 이슈 코멘트 증가 · 호출 코멘트의 Codex 봇 👍 세 신호 중 하나로 감지**합니다. 👍는 반드시 **봇 계정 리액션만** 세야 합니다. 사람이 호출 코멘트에 먼저 👍를 누르면 오탐하므로 `.content=="+1"`에 더해 `.user.login==$BOT`로 필터합니다. (메모리: codex-review-as-issue-comment)
- `issues/{n}/comments`·`pulls/{n}/reviews`·`pulls/{n}/comments`·`issues/comments/{id}/reactions`는 기본 30개씩 페이지네이션됩니다. 코멘트·리액션이 많은 PR에서는 Codex의 새 응답이나 봇 👍가 2페이지 이상으로 밀려 1페이지만 세면 감지에 실패하므로 **항상 `--paginate`(+`per_page=100`)로 전 페이지를 훑습니다.** 단, `--slurp`는 `--jq`와 함께 못 씁니다(gh 에러). **카운트는 `--paginate --jq "…|length"`가 페이지마다 수를 뱉으니 `awk`로 합산**하되, `gh api | awk`로 파이프하면 gh 실패가 awk의 성공에 가려져 조용히 0이 됩니다. 기준선이 거짓 0이면 이전 라운드의 옛 코멘트를 현재 응답으로 오인해 조기 종료하므로, **`codex_count` 헬퍼처럼 gh 출력을 먼저 받아 실패 시 `return 1`로 전파**하고(기준선은 실패 시 중단, 폴링은 이전 값 유지) 그 뒤에 `awk`로 합산합니다. **조회는 `--paginate --jq 'select(…)'`**로 페이지별 필터 결과를 이어 받습니다.

## 작업 흐름

### 1. PR을 ready로 전환

```bash
gh pr view <PR번호> --json number,state,isDraft,url
gh pr ready <PR번호>     # isDraft=true일 때만
```

- 이미 ready면 전환을 건너뜁니다.
- PR이 없으면 이 스킬을 멈추고 `create-pr`로 먼저 PR을 만들도록 안내합니다.

### 2. Codex 리뷰 호출

호출 **전에** 현재 Codex 봇 리뷰 수와 이슈 코멘트 수를 기준선(`PREV_REVIEWS`, `PREV_COMMENTS`)으로 기록합니다. 사람 리뷰·코멘트가 먼저 달려 있을 수 있으므로 전체 수가 아니라 **Codex 봇 것만** 세야, 폴링이 사람 것을 Codex 응답으로 오판하지 않습니다. 호출 코멘트 id(`CMTID`)도 확보해 👍 리액션 감지에 씁니다.

```bash
REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
BOT="chatgpt-codex-connector[bot]"; PR=<PR번호>
# 전 페이지를 훑어 봇 응답 수를 합산한다. gh 실패 시 0이 아니라 return 1로 알려, 기준선이 거짓 0으로 깔리는 것을 막는다.
codex_count() {  # $1=endpoint, $2=jq
  local pages; pages=$(gh api --paginate "$1" --jq "$2") || return 1
  printf '%s\n' "$pages" | awk '{s+=$1} END{print s+0}'
}
PREV_REVIEWS=$(codex_count "repos/$REPO/pulls/$PR/reviews?per_page=100"   "[.[]|select(.user.login==\"$BOT\")]|length") || { echo "기준선(reviews) 조회 실패 — 중단"; exit 1; }
PREV_COMMENTS=$(codex_count "repos/$REPO/issues/$PR/comments?per_page=100" "[.[]|select(.user.login==\"$BOT\")]|length") || { echo "기준선(comments) 조회 실패 — 중단"; exit 1; }
CMT_URL=$(gh pr comment $PR --body "@codex review")
CMTID=${CMT_URL##*-}   # .../pull/<PR>#issuecomment-<id> → <id>
echo "baseline reviews=$PREV_REVIEWS comments=$PREV_COMMENTS trigger_comment=$CMTID"
```

- `CMTID`는 3단계·5단계 폴링에서 👍 리액션(=추가 지적 없음) 감지에 씁니다.

### 3. 첫 응답 도착까지 폴링 (최대 ~20분)

**리뷰 수 증가 · Codex 봇 이슈 코멘트 증가 · 호출 코멘트 👍** 세 신호 중 하나라도 잡히면 응답 도착으로 봅니다. `reviews`만 보면 "지적 없음"이 이슈 코멘트/👍로 오는 경우를 놓칩니다.

```bash
for i in $(seq 1 40); do
  rv=$(codex_count "repos/$REPO/pulls/$PR/reviews?per_page=100"    "[.[]|select(.user.login==\"$BOT\")]|length") || rv=$PREV_REVIEWS
  cm=$(codex_count "repos/$REPO/issues/$PR/comments?per_page=100"  "[.[]|select(.user.login==\"$BOT\")]|length") || cm=$PREV_COMMENTS
  tu=$(codex_count "repos/$REPO/issues/comments/$CMTID/reactions?per_page=100" "[.[]|select(.content==\"+1\" and .user.login==\"$BOT\")]|length") || tu=0
  if [ "${rv:-0}" -gt "$PREV_REVIEWS" ];  then echo "CODEX_REVIEW_DETECTED reviews=$rv (정식 리뷰 — 지적 있음)"; exit 0; fi
  if [ "${cm:-0}" -gt "$PREV_COMMENTS" ]; then echo "CODEX_COMMENT_DETECTED comments=$cm (이슈 코멘트 도착 — 4단계 본문 확인 필요)"; exit 0; fi
  if [ "${tu:-0}" -ge 1 ];                then echo "CODEX_THUMB_DETECTED (👍 — 지적 없음)"; exit 0; fi
  echo "poll $i/40: reviews=$rv comments=$cm thumb=$tu (baseline rev=$PREV_REVIEWS cmt=$PREV_COMMENTS) — 30s 대기"
  sleep 30
done
echo "TIMEOUT_20MIN_NO_RESPONSE"; exit 0
```

- `CODEX_COMMENT_DETECTED`/`CODEX_THUMB_DETECTED`는 대개 "지적 없음"이지만, 이슈 코멘트에 요약·질문이 섞일 수 있으니 4단계에서 본문을 반드시 읽고 판단합니다.
- timeout이면 무한정 기다리지 말고 현재 상태를 사용자에게 보고하고 어떻게 할지 묻습니다.

### 4. 리뷰 내용 확인·판단

세 곳을 모두 봅니다. "지적 없음" 응답은 이슈 코멘트로 오므로, 정식 리뷰·인라인 코멘트가 비어 있어도 **이슈 코멘트를 반드시 확인**합니다.

```bash
REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
# 정식 리뷰 본문
gh pr view $PR --json reviews --jq '.reviews[] | {author: .author.login, state, body}'
# Codex 봇 이슈 코멘트("Didn't find any major issues" 같은 요약·지적 없음 응답이 여기로 온다)
gh api --paginate "repos/$REPO/issues/$PR/comments?per_page=100" --jq '.[] | select(.user.login=="chatgpt-codex-connector[bot]") | {created_at, body}'
# 인라인(파일 위) 코멘트 본문
gh api --paginate "repos/$REPO/pulls/$PR/comments?per_page=100"  --jq '.[] | select(.user.login=="chatgpt-codex-connector[bot]") | {path, line, body}'
```

- 지적을 **메이저(반영 필요)** 와 **마이너/제안(보류 가능)** 으로 분류합니다.
- 이슈 코멘트가 "no major issues"류이고 정식 리뷰·인라인 지적이 없으면 "메이저 없음 — 머지 가능"으로 보고합니다.
- 반영 여부가 애매한 지적은 임의로 고치지 말고 사용자 판단을 받습니다.

### 5. 반영 후 재리뷰 (지적을 고친 경우)

수정·커밋·push 후 **재호출 직전에 기준선을 다시 기록**하고 `@codex review` 코멘트를 답니다. 재리뷰도 3단계와 같은 세 신호(리뷰 수 증가 · 이슈 코멘트 증가 · 새 호출 코멘트 👍)로 감지합니다.

```bash
REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
BOT="chatgpt-codex-connector[bot]"; PR=<PR번호>
codex_count() {  # $1=endpoint, $2=jq (gh 실패 시 return 1 → 거짓 0 방지)
  local pages; pages=$(gh api --paginate "$1" --jq "$2") || return 1
  printf '%s\n' "$pages" | awk '{s+=$1} END{print s+0}'
}
PREV_REVIEWS=$(codex_count "repos/$REPO/pulls/$PR/reviews?per_page=100"   "[.[]|select(.user.login==\"$BOT\")]|length") || { echo "기준선(reviews) 조회 실패 — 중단"; exit 1; }
PREV_COMMENTS=$(codex_count "repos/$REPO/issues/$PR/comments?per_page=100" "[.[]|select(.user.login==\"$BOT\")]|length") || { echo "기준선(comments) 조회 실패 — 중단"; exit 1; }
CMT_URL=$(gh pr comment $PR --body "@codex review"); CMTID=${CMT_URL##*-}
for i in $(seq 1 40); do
  rv=$(codex_count "repos/$REPO/pulls/$PR/reviews?per_page=100"    "[.[]|select(.user.login==\"$BOT\")]|length") || rv=$PREV_REVIEWS
  cm=$(codex_count "repos/$REPO/issues/$PR/comments?per_page=100"  "[.[]|select(.user.login==\"$BOT\")]|length") || cm=$PREV_COMMENTS
  tu=$(codex_count "repos/$REPO/issues/comments/$CMTID/reactions?per_page=100" "[.[]|select(.content==\"+1\" and .user.login==\"$BOT\")]|length") || tu=0
  if [ "${rv:-0}" -gt "$PREV_REVIEWS" ];  then echo "NEW_REVIEW reviews=$rv"; exit 0; fi
  if [ "${cm:-0}" -gt "$PREV_COMMENTS" ]; then echo "NEW_COMMENT comments=$cm (이슈 코멘트 도착 — 4단계 본문 확인 필요)"; exit 0; fi
  if [ "${tu:-0}" -ge 1 ];                then echo "APPROVED_THUMB (👍 — 추가 지적 없음)"; exit 0; fi
  echo "poll $i/40: reviews=$rv comments=$cm thumb=$tu — 30s 대기"
  sleep 30
done
echo "TIMEOUT_NO_RERESPONSE"; exit 0
```

- **오직 봇 👍(`APPROVED_THUMB`)만이 "추가 지적 없음"의 확정 신호입니다.** `NEW_REVIEW`·`NEW_COMMENT`는 승인으로 단정하지 말고 반드시 **4단계로 돌아가 본문을 읽고** 판단합니다. 재리뷰의 이슈 코멘트에도 질문이나 새 지적이 섞일 수 있으므로, 본문을 읽기 전에는 머지 가능으로 보고하지 않습니다.

## 금지 사항

- draft 상태로 `@codex review`를 호출하지 않습니다(무시됨). 먼저 ready로 전환합니다.
- 리뷰가 도착하지 않았는데 "리뷰 통과"로 단정하지 않습니다. 반드시 폴링으로 확인합니다.
- 메이저 지적을 사용자 확인 없이 임의 판단으로 무시하지 않습니다.
- 반영 여부가 애매한 제안을 사용자 확인 없이 코드에 반영하지 않습니다.
- 사용자 지시 없이 머지하지 않습니다(머지=배포로 이어질 수 있음). 머지는 사용자가 판단·수행합니다.

## 완료 보고

- ready 전환 여부와 PR URL을 보고합니다.
- Codex 응답 도착 여부와 응답 유형(정식 리뷰 / 이슈 코멘트 / 👍), 메이저/마이너 지적 요약을 알립니다.
- 반영한 항목과 보류한 항목을 구분해 알립니다.
- 재리뷰 결과(새 리뷰 / 새 이슈 코멘트 / 👍)를 알리고, 머지 가능 여부에 대한 판단 근거를 남깁니다.
- timeout 등으로 확인하지 못한 부분이 있으면 그 상태를 그대로 알립니다.
