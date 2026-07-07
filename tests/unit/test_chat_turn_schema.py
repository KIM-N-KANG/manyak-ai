"""채팅 턴 요청 스키마 계약 테스트 (KNK-482).

주요 사건·엔딩 재료 4필드는 전부 선택이다 — 백엔드 런타임 전달(4-backend §4-3-10)이
미구현이라, 재료 없는 기존 요청이 그대로 통과해야 AI를 선행 배포할 수 있다(하위호환).
"""

import pytest
from pydantic import ValidationError

from src.schemas.chat_turn import ChatTurnRequest

# 기존 계약(재료 필드 없음)의 최소 유효 페이로드 — 와이어(snake_case) 형태.
_BASE_PAYLOAD = {
    "genre": "판타지",
    "story_settings": {
        "world_setting": "# 세계관\n아르덴 왕국.",
        "character_setting": "# 등장인물\n## 레이",
        "user_role_setting": "# 주인공\n## 호칭\n카이",
        "rule_setting": "# 전개 규칙",
    },
    "start_settings": {
        "name": "장례식 밤의 방문",
        "prologue": "선왕의 장례가 끝난 깊은 밤.",
        "start_situation": "빈소에 레이가 들어선다.",
    },
    "history": [{"role": "ASSISTANT", "content": "*레이가 들어선다.*"}],
    "user_input": "*문을 연다*",
    "summary": "",
}

_MAIN_EVENT = {
    "name": "반란의 서막",
    "description": "귀족 연합이 왕좌를 노린다.",
    "key_sentence": "주인공이 반란의 증거를 손에 넣는다.",
}


# ── 하위호환: 재료 없는 기존 요청 ───────────────────────────────────────────
def test_request_without_event_materials_passes_with_defaults() -> None:
    req = ChatTurnRequest.model_validate(_BASE_PAYLOAD)
    assert req.main_events == []
    assert req.target_main_event is None
    assert req.occurred_main_event_names == []
    assert req.endings == []


# ── 재료 포함 요청 파싱 ─────────────────────────────────────────────────────
def test_request_with_event_materials_parses() -> None:
    payload = {
        **_BASE_PAYLOAD,
        "main_events": [_MAIN_EVENT],
        "target_main_event": {"name": "반란의 서막", "progress_turns": 3},
        "occurred_main_event_names": ["선왕의 죽음"],
        "endings": [
            {
                "name": "왕좌를 되찾다",
                "achievement_condition": "반란군을 규합해 왕좌를 되찾는다.",
                "epilogue": "대관식 장면으로 마무리한다.",
            }
        ],
    }
    req = ChatTurnRequest.model_validate(payload)
    assert req.main_events[0].key_sentence == _MAIN_EVENT["key_sentence"]
    assert req.target_main_event is not None
    assert req.target_main_event.progress_turns == 3
    assert req.occurred_main_event_names == ["선왕의 죽음"]
    assert req.endings[0].name == "왕좌를 되찾다"


# ── 형식 위반 거부 ──────────────────────────────────────────────────────────
def test_negative_progress_turns_rejected() -> None:
    payload = {
        **_BASE_PAYLOAD,
        "target_main_event": {"name": "반란의 서막", "progress_turns": -1},
    }
    with pytest.raises(ValidationError):
        ChatTurnRequest.model_validate(payload)


def test_main_events_over_ten_rejected() -> None:
    # 계약상 주요 사건은 스토리당 최대 10 — 백엔드 저작 상한과 동일(§5-3-4).
    payload = {**_BASE_PAYLOAD, "main_events": [_MAIN_EVENT] * 11}
    with pytest.raises(ValidationError):
        ChatTurnRequest.model_validate(payload)


def test_ending_candidate_has_no_min_turns_field() -> None:
    # min_turns는 백엔드가 결정적으로 걸러 보내는 값이라 후보 계약에 없다(D11 분담).
    # 모르는 필드는 pydantic 기본(ignore)으로 무시된다 — 실려 와도 파싱이 깨지지 않는다.
    payload = {
        **_BASE_PAYLOAD,
        "endings": [
            {
                "name": "왕좌를 되찾다",
                "achievement_condition": "조건",
                "epilogue": "가이드",
                "min_turns": 5,
            }
        ],
    }
    req = ChatTurnRequest.model_validate(payload)
    assert "min_turns" not in type(req.endings[0]).model_fields
