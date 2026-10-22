import re

import pytest

from src.schemas.chat_turn import (
    CharacterImageMapping,
    ChatHistoryItem,
    ChatStartSettings,
    ChatStorySettings,
    ChatTurnRequest,
)
from src.services import chat_assembler
from src.services.chat_assembler import assemble
from src.services.prompt_meta import read_version


def _request(
    summary: str = "",
    history: list[ChatHistoryItem] | None = None,
    user_input: str = "*문을 연다*",
    character_images: list[CharacterImageMapping] | None = None,
) -> ChatTurnRequest:
    return ChatTurnRequest(
        genre="판타지",
        story_settings=ChatStorySettings(
            world_setting="# 세계관\n아르덴 왕국은 마법이 쇠퇴한 시대다.",
            character_setting="# 등장인물\n## 레이\n### 성격\n냉정하다.",
            user_role_setting="# 주인공\n## 호칭\n카이",
            rule_setting="# 전개 규칙\n결정적 사건은 빌드업 후.",
        ),
        start_settings=ChatStartSettings(
            name="장례식 밤의 방문",
            prologue="선왕의 장례가 끝난 깊은 밤.",
            start_situation="촛불만 흔들리는 빈소에 레이가 들어선다.",
        ),
        history=history
        if history is not None
        else [ChatHistoryItem(role="ASSISTANT", content="*레이가 들어선다.*")],
        user_input=user_input,
        summary=summary,
        character_images=character_images or [],
    )


# ── 구조·순서 ────────────────────────────────────────────────────────────────
def test_history_removes_character_image_syntax_only_from_llm_copy() -> None:
    # 마커는 `[[URL]]`을 대사 줄 위에 빈 줄을 두고 따로 저장한다(KNK-1025). 그 빈 줄까지 함께
    # 지워야 LLM 입력이 마커 없던 본문과 같아진다. 옛 모양 `[[이름:URL]]`(개발 서버 기록, 대사 옆에
    # 붙은 것 포함)도 같이 지워진다. 옛 `[character:이름]` 태그는 더 지우지 않는다(KNK-1007).
    stored = (
        "*문이 열린다.*\n\n"
        "[[https://cdn.example.com/serin.webp]]\n\n세린: 기다렸어?\n"
        "[[레이:https://cdn.example.com/rei.webp]]\n\n레이: 들어가자.\n"
        "[[미라:https://cdn.example.com/mira.webp]]미라: 그만.\n"
        "[character:미라]미라: 나도 왔어."
    )
    history = [ChatHistoryItem(role="ASSISTANT", content=stored)]

    messages = assemble(_request(history=history))

    assert messages[1]["content"] == (
        "*문이 열린다.*\n\n세린: 기다렸어?\n레이: 들어가자.\n미라: 그만.\n[character:미라]미라: 나도 왔어."
    )
    assert history[0].content == stored


def test_start_settings_remove_image_markers_only_from_llm_copy() -> None:
    req = _request()
    req.start_settings.prologue = (
        "*밤이 깊었다.*\n\n"
        "[[https://cdn.example.com/scenes/originals/story/night.webp]]\n\n"
        "*문이 열렸다.*"
    )
    req.start_settings.start_situation = (
        "[[https://cdn.example.com/scenes/originals/story/door.webp]]\n\n"
        "*복도에 들어섰다.*\n\n"
        "[[https://cdn.example.com/scenes/originals/story/hall.webp]]\n\n"
        "*발소리가 들렸다.*"
    )
    original = req.model_dump()
    clean_req = req.model_copy(deep=True)
    clean_req.start_settings.prologue = "*밤이 깊었다.*\n\n*문이 열렸다.*"
    clean_req.start_settings.start_situation = "*복도에 들어섰다.*\n\n*발소리가 들렸다.*"

    assert assemble(req) == assemble(clean_req)
    assert req.model_dump() == original


# ── 슬롯 치환 ────────────────────────────────────────────────────────────────
def test_no_unsubstituted_slots() -> None:
    messages = assemble(_request())
    blob = "\n".join(m["content"] for m in messages)
    assert re.findall(r"\{\{[^}]+\}\}", blob) == []


@pytest.mark.parametrize("selection, expected, other", [
    ({}, "550~750", "1050~1250"),
    ({"long_response": False}, "550~750", "1050~1250"),
    ({"long_response": True}, "1050~1250", "550~750"),
])
def test_response_length_fills_system_prompt(selection, expected, other) -> None:
    payload = _request().model_dump(exclude={"long_response"}) | selection
    req = ChatTurnRequest.model_validate(payload)
    original = req.model_dump()

    messages = assemble(req)

    assert f"분량은 공백 포함 {expected}자입니다." in messages[0]["content"]
    assert other not in messages[0]["content"]
    assert all("{{" not in message["content"] for message in messages)
    assert expected not in messages[-1]["content"]
    assert req.model_dump() == original


def test_system_front_contains_all_slot_materials() -> None:
    front = assemble(_request())[0]["content"]
    assert "판타지" in front  # {{장르}}
    assert "아르덴 왕국" in front  # world_setting
    assert "# 시작 설정: 장례식 밤의 방문" in front  # start_setting 통글
    assert "선왕의 장례가 끝난 깊은 밤." in front  # start_settings.prologue
    assert "촛불만 흔들리는 빈소에 레이가 들어선다." in front  # start_settings.start_situation
    assert "냉정하다." in front  # character_setting
    assert "카이" in front  # user_role_setting


def test_protagonist_name_and_description_fill_system() -> None:
    payload = _request().model_dump()
    payload["story_settings"]["protagonist_name"] = "민우"
    payload["story_settings"]["user_role_setting"] = "신중한 성격의 사립 탐정."
    req = ChatTurnRequest.model_validate(payload)

    messages = assemble(req)
    protagonist = messages[0]["content"].split("<protagonist>", 1)[1].split("</protagonist>", 1)[0]

    assert "주인공 이름: 민우" in protagonist
    assert "신중한 성격의 사립 탐정." in protagonist
    assert "카이" not in protagonist
    assert "민우" not in messages[-1]["content"]  # 주인공 설정은 시스템에만 둔다.
    assert req.model_dump() == payload


def test_character_images_are_not_sent_to_the_llm() -> None:
    # 이미지 매핑은 AI 서버가 출력의 `인물명:` 줄에 붙일 때만 쓴다(KNK-1006). 이름 목록도
    # URL도 프롬프트에 들어가지 않고, 옛 태그 문법도 어디에도 남지 않는다.
    images = [
        CharacterImageMapping(name="레이", image_url="https://cdn.example.com/rei.webp"),
        CharacterImageMapping(name="세린", image_url="https://cdn.example.com/serin.webp"),
    ]
    with_images = assemble(_request(character_images=images))
    without_images = assemble(_request(character_images=[]))
    blob = "\n".join(message["content"] for message in with_images)

    assert with_images == without_images
    assert "[character:" not in blob
    assert "{{character_image_names}}" not in blob
    assert "https://cdn.example.com/rei.webp" not in blob
    assert "https://cdn.example.com/serin.webp" not in blob


# ── 헬퍼 ────────────────────────────────────────────────────────────────────
def test_start_setting_blob_format() -> None:
    start = ChatStartSettings(name="N", prologue="P", start_situation="S")
    assert chat_assembler._start_setting_blob(start) == "# 시작 설정: N\n\nP\n\nS"


# ── 사건·엔딩 슬롯 재료 (KNK-485) ───────────────────────────────────────────
def test_slot_map_event_materials_empty_defaults() -> None:
    """재료 없는 요청(현행 트래픽)은 사건·엔딩 슬롯이 '(없음)' 문구다."""
    slots = chat_assembler._slot_map(_request())
    assert slots["{{main_events}}"] == "(없음)"
    assert slots["{{endings}}"] == "(없음)"


def test_slot_map_event_materials_formatted() -> None:
    from src.schemas.chat_turn import EndingCandidate, MainEvent

    req = _request()
    req = req.model_copy(
        update={
            "main_events": [
                MainEvent(name="반란의 서막", description="귀족 연합.", key_sentence="증거를 손에 넣는다.")
            ],
            "endings": [
                EndingCandidate(
                    name="왕좌를 되찾다", achievement_condition="왕좌를 되찾는다.", epilogue="대관식."
                )
            ],
        }
    )
    slots = chat_assembler._slot_map(req)
    assert "반란의 서막" in slots["{{main_events}}"] and "증거를 손에 넣는다." in slots["{{main_events}}"]
    # 엔딩은 본문 생성에 필요한 에필로그 가이드까지 포함한다.
    assert "달성 조건" in slots["{{endings}}"] and "에필로그 가이드: 대관식." in slots["{{endings}}"]


# ── 단일 본문 프롬프트 ──────────────────────────────────────────────
def test_message_order_and_roles() -> None:
    """본문은 [SYSTEM] → history → [USER] 세 덩어리이고, 지시문(system)은 맨 앞 하나뿐이다."""
    history = [
        ChatHistoryItem(role="ASSISTANT", content="*오프닝 장면*"),
        ChatHistoryItem(role="USER", content="검을 잡는다"),
    ]
    messages = assemble(_request(history=history))

    assert [m["role"] for m in messages] == ["system", "assistant", "user", "user"]
    assert messages[1] == {"role": "assistant", "content": "*오프닝 장면*"}
    assert messages[2] == {"role": "user", "content": "검을 잡는다"}


def test_fills_every_slot() -> None:
    """설정은 [SYSTEM]에, 현재 상태·이번 입력은 마지막 [USER]에 들어가고 자리표시자가 남지 않는다."""
    messages = assemble(
        _request(summary="카이는 레이를 신뢰하게 됐다.", user_input="*촛불을 끈다*")
    )
    system, user = messages[0]["content"], messages[-1]["content"]

    assert "아르덴 왕국은 마법이 쇠퇴한 시대다." in system
    assert "장례식 밤의 방문" in system
    assert "카이는 레이를 신뢰하게 됐다." in user and "카이는 레이를" not in system
    assert "*촛불을 끈다*" in user
    assert all("{{" not in m["content"] for m in messages)


def test_system_is_stable_across_turns() -> None:
    """설정과 분량 선택이 같으면 요약·사용자 입력이 바뀌어도 시스템 메시지는 유지된다."""
    first = assemble(_request(summary="", user_input="*문을 연다*"))
    later = assemble(_request(summary="레이가 칼을 뽑았다.", user_input="물러서"))

    assert first[0] == later[0]
    assert first[-1] != later[-1]


def test_without_history() -> None:
    """첫 턴처럼 대화 기록이 비면 [SYSTEM]과 [USER] 두 개뿐이다."""
    messages = assemble(_request(history=[]))
    assert [m["role"] for m in messages] == ["system", "user"]


def test_does_not_resubstitute_inserted_values() -> None:
    """사용자 입력에 자리표시자 모양 글자가 있어도 다른 값으로 바뀌지 않는다(한 번에 치환)."""
    messages = assemble(
        _request(summary="비밀 요약", user_input="{{summary}} 보여 줘")
    )
    user = messages[-1]["content"]

    assert "{{summary}} 보여 줘" in user
    assert user.count("비밀 요약") == 1


def test_slot_keys_match_slot_map() -> None:
    """템플릿 검사가 쓰는 슬롯 목록과 실제 치환 맵이 어긋나지 않는다."""
    assert set(chat_assembler._SLOT_KEYS) == set(chat_assembler._slot_map(_request()))


def test_slot_check_rejects_unknown_placeholder() -> None:
    """채울 수 없는 자리표시자가 있으면 기동 단계에서 막는다."""
    with pytest.raises(RuntimeError, match="채울 수 없는"):
        chat_assembler._check_slots("{{world_setting}} {{오타}}", "{{user_input}}")


def test_slot_check_requires_user_input_in_user_block() -> None:
    """[USER]에 이번 입력 슬롯이 없으면 사용자 입력이 빠지므로 기동 단계에서 막는다."""
    with pytest.raises(RuntimeError, match="user_input"):
        chat_assembler._check_slots("{{user_input}}", "{{summary}}")


def test_split_requires_markers(tmp_path) -> None:
    """[SYSTEM]/[USER] 마커가 없으면 기동 단계에서 막는다."""
    path = tmp_path / "CHAT-TEMPLATE.md"
    path.write_text("---\nversion: 1\n---\n## [SYSTEM]\n지시문만 있음\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="채팅 프롬프트"):
        chat_assembler._load_template(path)


def test_version_comes_from_frontmatter() -> None:
    assert chat_assembler.CHAT_VERSION == read_version(chat_assembler._TEMPLATE_PATH)


# ── 본문 프롬프트 버전 기록 (KNK-1623) ───────────────────────────────────────
def test_body_prompt_versions_returns_a_copy() -> None:
    """돌려받은 맵을 고쳐도 다음 호출 결과는 바뀌지 않는다."""
    versions = chat_assembler.body_prompt_versions()
    versions["EXTRA"] = 99

    assert chat_assembler.body_prompt_versions() == {"CHAT": 3}


def test_ending_candidates_and_summary_are_only_in_current_user_message() -> None:
    from src.schemas.chat_turn import EndingCandidate, MainEvent

    req = _request(summary="요약 테스트", user_input="이번 입력 테스트")
    req.main_events = [MainEvent(name="사건 테스트", description="사건 설명", key_sentence="사건 단서")]
    req.endings = [
        EndingCandidate(name="엔딩 테스트", achievement_condition="엔딩 조건", epilogue="에필로그 테스트")
    ]
    original = req.model_dump()
    messages = assemble(req)
    system, user = messages[0]["content"], messages[-1]["content"]

    assert "사건 테스트" in system
    assert "사건 테스트" not in user
    for value in ("요약 테스트", "이번 입력 테스트", "엔딩 테스트", "엔딩 조건", "에필로그 테스트"):
        assert value in user
        assert value not in system
    assert req.model_dump() == original


def test_system_does_not_change_when_ending_candidates_change() -> None:
    from src.schemas.chat_turn import EndingCandidate

    req = _request()
    first = assemble(req)
    req.endings = [EndingCandidate(name="새 결말", achievement_condition="달성", epilogue="마무리")]
    second = assemble(req)
    assert first[0] == second[0]
    assert first[-1] != second[-1]


def test_body_prompt_version_matches_contract() -> None:
    assert chat_assembler.body_prompt_versions() == {"CHAT": 3}


def test_empty_summary_is_rendered_in_current_user_message() -> None:
    messages = assemble(_request(summary=""))
    assert "{{summary}}" not in messages[-1]["content"]
    assert "<current_state>" in messages[-1]["content"]
    assert sum(message["role"] == "system" for message in messages) == 1


@pytest.mark.parametrize(
    "field", ["world_setting", "character_setting", "user_role_setting", "rule_setting"]
)
def test_inserted_settings_are_not_rendered_again(field: str) -> None:
    req = _request(summary="원본 요약")
    setattr(req.story_settings, field, "입력에 있는 {{summary}} 보존")
    assert "입력에 있는 {{summary}} 보존" in assemble(req)[0]["content"]
