"""선택지 생성 전용 API(/chat/choices)의 입출력 계약 — KNK-625.

선택지(다음 행동 3개)를 `/chat/turns`에서 분리한 전용 동기 REST 엔드포인트가 쓴다.
분리 이유: 본문 completed가 선택지 호출 완료까지 대기하던 지연을 없애고(D6 트레이드오프
해소), 추천 입력 토글(프론트가 이 API를 부르냐로 on/off)의 기반을 만든다.

- 입력: 백엔드 → AI. 턴 요청과 같은 재료 전체 + 방금 생성된 본문(ai_output).
  AI는 무상태라 백엔드가 턴 요청 조립 로직을 재사용해 재료를 다시 실어 보낸다.
- 출력: AI → 백엔드. 항상 정확히 3개(생성 실패는 흡수되어 폴백으로 채워진다 → 항상 200).
- 표기: 동기 REST라 story 계열과 같은 snake_case다 — camelCase는 chat SSE completed
  페이로드만의 공식 예외(5-ai-server §5-1)라 여기로 넓히지 않는다.
"""

from pydantic import BaseModel

from src.schemas.chat_turn import ChatTurnRequest
from src.schemas.response_meta import StoryResponseMeta


class ChatChoicesRequest(ChatTurnRequest):
    """선택지 생성 입력 — 턴 요청 재료 전체 + 방금 생성된 본문.

    ChatTurnRequest를 상속해 백엔드가 턴 요청과 같은 조립 로직을 재사용하게 한다.
    ⚠️ history는 **메인 턴 요청과 동일한 스냅샷(이번 턴 제외)**이어야 한다 — 선택지
    호출 시점에는 이번 턴이 이미 DB에 저장돼 있으므로, 그대로 재조립하면 방금 장면이
    history와 ai_output에 중복 삽입된다(제외는 백엔드 책임 — 4-backend 재생성 조립과
    같은 1..N-1 잘라내기).
    """

    ai_output: str


class ChatChoicesResponse(BaseModel):
    """선택지 생성 출력.

    choices는 항상 정확히 3개다 — 부족·실패는 generate_choices가 재호출·폴백으로
    흡수하므로 이 응답은 실패하지 않는다(백엔드는 타임아웃 외 에러 처리가 필요 없다).
    meta는 백엔드 ai_call_logs의 choice_generation 행 적재 재료다(retry_count는
    선택지 재호출 횟수, prompt_versions 키는 레거시 연속성으로 NEXT_ACTIONS 유지).
    """

    choices: list[str]
    meta: StoryResponseMeta
