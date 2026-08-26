"""채팅 턴 스키마 계약 테스트 (KNK-482 요청 재료 · KNK-483 completed 판정 메타).

주요 사건·엔딩 재료 4필드는 전부 선택이다 — 백엔드 런타임 전달(4-backend §4-3-10)이
미구현이라, 재료 없는 기존 요청이 그대로 통과해야 AI를 선행 배포할 수 있다(하위호환).
completed 판정 메타 3필드는 camelCase 와이어 키로 직렬화되고 기본 null이다.
"""

import pytest
from pydantic import ValidationError

from src.schemas.chat_turn import (
    EVENT_CHARACTER_IMAGE,
    CharacterImageData,
    ChatTurnRequest,
    CompletedData,
    TargetMainEventOut,
)

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
    assert req.user_source is None
    assert req.main_events == []
    assert req.target_main_event is None
    assert req.occurred_main_event_names == []
    assert req.endings == []
    assert req.character_images == []


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


def test_request_with_character_images_parses() -> None:
    req = ChatTurnRequest.model_validate(
        {
            **_BASE_PAYLOAD,
            "character_images": [
                {
                    "name": "레이",
                    "image_url": "https://cdn.example.com/characters/rei.webp",
                }
            ],
        }
    )

    assert req.character_images[0].name == "레이"
    assert req.character_images[0].image_url.endswith("/rei.webp")


@pytest.mark.parametrize("user_source", ["choice", "edited_choice", "typed"])
def test_request_accepts_known_user_source(user_source: str) -> None:
    req = ChatTurnRequest.model_validate({**_BASE_PAYLOAD, "user_source": user_source})
    assert req.user_source == user_source


def test_request_omits_unknown_user_source_without_rejecting(caplog) -> None:
    req = ChatTurnRequest.model_validate({**_BASE_PAYLOAD, "user_source": "guessed"})

    assert req.user_source is None
    assert "Langfuse user_source 값 무시" in caplog.text
    assert "guessed" not in caplog.text


@pytest.mark.parametrize("user_source", [None, "", " ", "unknown"])
def test_request_omits_missing_user_source(user_source: str | None) -> None:
    req = ChatTurnRequest.model_validate({**_BASE_PAYLOAD, "user_source": user_source})
    assert req.user_source is None


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


def test_character_images_over_five_accepted() -> None:
    # 개수 상한 없음 — 인물당 표정별 다중 이미지가 오면 5개를 넘는다(KNK-943 백엔드 리뷰).
    image = {
        "name": "레이",
        "image_url": "https://cdn.example.com/characters/rei.webp",
    }
    req = ChatTurnRequest.model_validate(
        {**_BASE_PAYLOAD, "character_images": [image] * 6}
    )
    assert len(req.character_images) == 6


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


# ── completed 판정 메타 직렬화 (KNK-483) ────────────────────────────────────
def test_completed_judgement_meta_defaults_to_null_camel_case() -> None:
    # 재료 없는 턴(현행 트래픽)에서는 3필드가 camelCase 키의 null로 나가야 한다.
    payload = CompletedData(ai_output="본문").model_dump(by_alias=True)
    assert payload["aiOutput"] == "본문"
    assert payload["targetMainEvent"] is None
    assert payload["occurredMainEventName"] is None
    assert payload["endingName"] is None
    assert payload["characterImages"] == []


def test_completed_judgement_meta_serializes_camel_case() -> None:
    payload = CompletedData(
        ai_output="본문",
        target_main_event=TargetMainEventOut(name="반란의 서막", progress_turns=4),
        occurred_main_event_name="선왕의 죽음",
        ending_name="왕좌를 되찾다",
    ).model_dump(by_alias=True)
    assert payload["targetMainEvent"] == {"name": "반란의 서막", "progressTurns": 4}
    assert payload["occurredMainEventName"] == "선왕의 죽음"
    assert payload["endingName"] == "왕좌를 되찾다"


def test_character_images_serialize_in_display_order_with_duplicates() -> None:
    rei = CharacterImageData(
        name="레이",
        image_url="https://cdn.example.com/characters/rei.webp",
    )
    serin = CharacterImageData(
        name="세린",
        image_url="https://cdn.example.com/characters/serin.webp",
    )

    assert EVENT_CHARACTER_IMAGE == "character_image"
    assert rei.model_dump(by_alias=True) == {
        "name": "레이",
        "imageUrl": "https://cdn.example.com/characters/rei.webp",
    }
    completed = CompletedData(
        ai_output=(
            "[[레이:https://cdn.example.com/characters/rei.webp]]레이: 들어가자.\n"
            "[[세린:https://cdn.example.com/characters/serin.webp]]세린: 기다려.\n"
            "[[레이:https://cdn.example.com/characters/rei.webp]]레이: 시간이 없어."
        ),
        character_images=[rei, serin, rei],
    ).model_dump(by_alias=True)
    assert "characterImage" not in completed
    assert completed["characterImages"] == [
        {"name": "레이", "imageUrl": "https://cdn.example.com/characters/rei.webp"},
        {
            "name": "세린",
            "imageUrl": "https://cdn.example.com/characters/serin.webp",
        },
        {"name": "레이", "imageUrl": "https://cdn.example.com/characters/rei.webp"},
    ]


# ── completed choices 빈 배열 강제 (KNK-625 선택지 분리) ────────────────────
def test_completed_choices_defaults_to_empty_and_rejects_items() -> None:
    # 선택지는 /chat/choices로 분리됐다 — completed의 choices는 하위호환 빈 배열 고정이며,
    # 스키마(max_length=0)가 강제한다. 선택지가 다시 섞이는 회귀를 여기서 잡는다.
    assert CompletedData(ai_output="본문").choices == []
    with pytest.raises(ValidationError):
        CompletedData(ai_output="본문", choices=["가"])
