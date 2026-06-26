import re

from src.schemas.chat_turn import (
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


# ── 헬퍼 ────────────────────────────────────────────────────────────────────
def test_start_setting_blob_format() -> None:
    start = ChatStartSettings(name="N", prologue="P", start_situation="S")
    assert chat_assembler._start_setting_blob(start) == "# 시작 설정: N\n\nP\n\nS"


def test_empty_history_only_user_input() -> None:
    messages = assemble(_request(history=[], user_input="시작"))
    # [시스템 앞] → user_input → [Depth] → [PHI] = 4개
    assert len(messages) == 4
    assert messages[1] == {"role": "user", "content": "시작"}
