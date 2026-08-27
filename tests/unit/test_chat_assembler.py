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


def test_history_removes_character_image_syntax_only_from_llm_copy() -> None:
    stored = (
        "[[세린:https://cdn.example.com/serin.webp]]세린: 기다렸어?\n"
        "[character:미라]미라: 나도 왔어."
    )
    history = [ChatHistoryItem(role="ASSISTANT", content=stored)]

    messages = assemble(_request(history=history))

    assert messages[1]["content"] == "세린: 기다렸어?\n미라: 나도 왔어."
    assert history[0].content == stored


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
    for phrase in ("이미지 대상", "이미지 보유", "인물 이미지", "이미지 태그"):
        assert phrase not in blob
    assert "[character:" not in blob
    assert "{{character_image_names}}" not in blob
    assert "https://cdn.example.com/rei.webp" not in blob
    assert "https://cdn.example.com/serin.webp" not in blob


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


def test_phi_keeps_dialogue_rules_without_tag_instructions() -> None:
    phi = assemble(_request(character_images=[]))[-1]["content"]

    assert "매 답변에 주변 인물이나 단역·배경 인물의 대사를 최소 한 줄" in phi
    assert "출력은 `*지문*` + `인물명: 대사`로만 구성한다" in phi
    assert "태그" not in phi
    for phrase in ("이미지 대상", "이미지 보유", "인물 이미지"):
        assert phrase not in phi


def test_core_keeps_the_rules_the_speaker_label_parser_relies_on() -> None:
    # 이미지는 AI 서버가 출력의 `인물명:` 줄을 찾아 붙인다(KNK-1005). 그 근거가 되는
    # 표기 규칙이 프롬프트에서 조용히 사라지면 이미지가 안 뜨는데 다른 테스트는 통과하므로
    # 여기서 문구를 고정한다(Codex 리뷰 지적, KNK-1006).
    messages = assemble(_request())
    front, phi = messages[0]["content"], messages[-1]["content"]

    assert "대사는 반드시 `인물명: 대사` 형식으로 쓴다" in front
    assert "이름은 CHARACTER 설정에 적힌 글자 그대로 쓰고" in front
    assert "이름은 줄 맨 앞에서 콜론 직전까지 장식 없는 평문으로 적는다" in front
    assert "`나레이터:` 같은 역할어 라벨을 쓰지 않는다" in front
    assert "각 대사를 그 인물 이름으로 시작하는 **별도 줄**로 적는다" in front
    assert "화자 이름은 줄 맨 앞 장식 없는 평문으로 매 줄 붙인다" in phi
    # 표기 규약의 좋은 예·나쁜 예는 남기고, 응답 전체를 흉내 내는 예시 블록은 두지 않는다.
    assert "\n  세린: 기다렸어?\n" in front
    assert "서린: 기다렸어?" in front  # 이름 글자가 다른 나쁜 예
    assert "복도 끝에서 인기척이 났습니다" not in front
    assert "[character:" not in front


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
