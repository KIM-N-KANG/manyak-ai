import re

from src.schemas.chat_turn import (
    CharacterImageMapping,
    ChatHistoryItem,
    ChatStartSettings,
    ChatStorySettings,
    ChatTurnRequest,
)
from src.services import chat_assembler
from src.services.chat_assembler import assemble


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
def test_message_order_and_roles() -> None:
    history = [
        ChatHistoryItem(role="ASSISTANT", content="*오프닝 장면*"),
        ChatHistoryItem(role="USER", content="검을 잡는다"),
    ]
    messages = assemble(_request(history=history, user_input="*문을 연다*"))

    # [시스템 앞] → history(2) → user_input → [Depth] → [PHI] = 6개
    assert len(messages) == 1 + 2 + 1 + 2
    assert messages[0]["role"] == "system"
    # history는 그 순서대로, 대문자 role → 소문자 변환
    assert messages[1] == {"role": "assistant", "content": "*오프닝 장면*"}
    assert messages[2] == {"role": "user", "content": "검을 잡는다"}
    # user_input은 history와 별도로 마지막 user 턴에 붙는다
    assert messages[3] == {"role": "user", "content": "*문을 연다*"}
    # Depth는 끝에서 2번째, PHI는 끝 — 둘 다 system
    assert messages[-2]["role"] == "system" and "[현재 상태]" in messages[-2]["content"]
    assert messages[-1]["role"] == "system"


# ── 슬롯 치환 ────────────────────────────────────────────────────────────────
def test_no_unsubstituted_slots() -> None:
    messages = assemble(_request())
    blob = "\n".join(m["content"] for m in messages)
    assert re.findall(r"\{\{[^}]+\}\}", blob) == []


def test_system_front_contains_all_slot_materials() -> None:
    front = assemble(_request())[0]["content"]
    assert "판타지" in front  # {{장르}}
    assert "아르덴 왕국" in front  # world_setting
    assert "# 시작 설정: 장례식 밤의 방문" in front  # start_setting 통글
    assert "선왕의 장례가 끝난 깊은 밤." in front  # start_settings.prologue
    assert "촛불만 흔들리는 빈소에 레이가 들어선다." in front  # start_settings.start_situation
    assert "냉정하다." in front  # character_setting
    assert "카이" in front  # user_role_setting


def test_character_image_names_are_injected_without_urls() -> None:
    images = [
        CharacterImageMapping(name="레이", image_url="https://cdn.example.com/rei.webp"),
        CharacterImageMapping(name="세린", image_url="https://cdn.example.com/serin.webp"),
    ]
    messages = assemble(_request(character_images=images))
    blob = "\n".join(message["content"] for message in messages)

    assert chat_assembler.format_character_image_names(images) == "- 레이\n- 세린"
    assert "# 인물 이미지 태그 대상\n\n- 레이\n- 세린" in messages[0]["content"]
    assert "[character:세린]세린: 기다렸어?" in blob
    assert "[character:인물 이름]" not in blob
    assert "[character:선택한 이름]" not in blob
    assert "https://cdn.example.com/rei.webp" not in blob
    assert "https://cdn.example.com/serin.webp" not in blob


def test_empty_character_images_disable_tag() -> None:
    front = assemble(_request(character_images=[]))[0]["content"]

    assert "# 인물 이미지 태그 대상\n\n(없음)" in front
    assert "위 목록이 `(없음)`이면 어떤 대사도 이미지 대상으로 정하지 않는다" in front
    assert "[character:none]" not in front


def test_system_front_layer_order() -> None:
    """[시스템 앞]은 SAFETY → CORE → STORY → CHARACTER → USER 순이어야 한다."""
    front = assemble(_request())[0]["content"]
    # 각 레이어 고유 문구의 등장 위치로 순서를 검증
    i_safety = front.index("안전 방어선")
    i_core = front.index("출력 형식 규약")
    i_story = front.index("# 시작 설정")
    i_character = front.index("연기하는 주변 인물")
    i_user = front.index("주인공의 프로필")
    assert i_safety < i_core < i_story < i_character < i_user


# ── Depth(summary) ──────────────────────────────────────────────────────────
def test_empty_summary_leaves_blank() -> None:
    depth = assemble(_request(summary=""))[-2]["content"]
    assert "[현재 상태]" in depth
    # 빈 문자열이면 슬롯이 빈 칸으로 치환된다(명세 5.1)
    assert "{{summary}}" not in depth


def test_summary_injected() -> None:
    depth = assemble(_request(summary="카이는 레이를 신뢰하게 됐다."))[-2]["content"]
    assert "카이는 레이를 신뢰하게 됐다." in depth


# ── PHI ─────────────────────────────────────────────────────────────────────
def test_phi_excludes_user_layer() -> None:
    """PHI는 CHARACTER→STORY→CORE→SAFETY만 — USER는 갓모딩 방지로 제외(명세 4.1)."""
    phi = assemble(_request())[-1]["content"]
    assert "# CHARACTER" in phi
    assert "# STORY" in phi
    assert "# CORE" in phi
    assert "# SAFETY" in phi
    assert "# USER" not in phi


def test_phi_order() -> None:
    phi = assemble(_request())[-1]["content"]
    assert (
        phi.index("# CHARACTER")
        < phi.index("# STORY")
        < phi.index("# CORE")
        < phi.index("# SAFETY")
    )


def test_phi_places_character_image_tag_before_every_eligible_dialogue() -> None:
    phi = assemble(_request(character_images=[]))[-1]["content"]

    assert "이미지 대상으로 정한 모든 대사의 `인물명:` 바로 앞" in phi
    assert "같은 인물이 다시 말하면 태그도 다시 붙인다" in phi
    assert "매 답변에 주변 인물이나 단역·배경 인물의 대사를 최소 한 줄" in phi
    assert "이미지 대상이 아닌 대사에는 태그를 쓰지 않는다" in phi


def test_core_has_tagged_and_untagged_response_examples() -> None:
    front = assemble(_request())[0]["content"]

    assert "*세린이 젖은 소매를 걷으며 복도로 들어온다.*" in front
    assert "[character:세린]세린: 기다렸어?" in front
    assert "*복도 끝의 어둠 속에서 레이가 젖은 어깨를 털며 걸어 나온다.*" in front
    assert "[character:세린]세린: 그럼 먼저 확인해 보자." in front
    assert "[character:레이]레이: 복도 끝에서 인기척이 났습니다." in front
    assert "*세린이 복도 끝으로 고개를 돌린다.*" in front
    assert "경비병: 이쪽에는 아무것도 없습니다." in front
    assert "[character:경비병]" not in front
    assert "[character:none]" not in front


# ── 헬퍼 ────────────────────────────────────────────────────────────────────
def test_start_setting_blob_format() -> None:
    start = ChatStartSettings(name="N", prologue="P", start_situation="S")
    assert chat_assembler._start_setting_blob(start) == "# 시작 설정: N\n\nP\n\nS"


def test_empty_history_only_user_input() -> None:
    messages = assemble(_request(history=[], user_input="시작"))
    # [시스템 앞] → user_input → [Depth] → [PHI] = 4개
    assert len(messages) == 4
    assert messages[1] == {"role": "user", "content": "시작"}


# ── 사건·엔딩 슬롯 재료 (KNK-485) ───────────────────────────────────────────
def test_slot_map_event_materials_empty_defaults() -> None:
    """재료 없는 요청(현행 트래픽)은 사건·엔딩 슬롯이 '(없음)' 문구다."""
    slots = chat_assembler._slot_map(_request())
    assert slots["{{main_events}}"] == "(없음)"
    assert slots["{{target_main_event}}"] == "(없음)"
    assert slots["{{endings}}"] == "(없음)"


def test_slot_map_event_materials_formatted() -> None:
    from src.schemas.chat_turn import EndingCandidate, MainEvent, TargetMainEvent

    req = _request()
    req = req.model_copy(
        update={
            "main_events": [
                MainEvent(name="반란의 서막", description="귀족 연합.", key_sentence="증거를 손에 넣는다.")
            ],
            "target_main_event": TargetMainEvent(name="반란의 서막", progress_turns=2),
            "endings": [
                EndingCandidate(
                    name="왕좌를 되찾다", achievement_condition="왕좌를 되찾는다.", epilogue="대관식."
                )
            ],
        }
    )
    slots = chat_assembler._slot_map(req)
    assert "반란의 서막" in slots["{{main_events}}"] and "증거를 손에 넣는다." in slots["{{main_events}}"]
    assert slots["{{target_main_event}}"] == "이름: 반란의 서막 (진행 2턴)"
    # STORY 슬롯 엔딩은 본문이 엔딩 응답을 생성해야 하므로 에필로그 가이드까지 포함한다.
    assert "달성 조건" in slots["{{endings}}"] and "에필로그 가이드: 대관식." in slots["{{endings}}"]
