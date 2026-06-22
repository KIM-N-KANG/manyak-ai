import pytest

from src.services import chat_llm
from src.services.chat_llm import (
    _parse_choices,
    _split_output,
    _strip_speaker_bold,
    stream_chat_turn,
)


# ── 출력 분리(B안 파싱) — 동기 ───────────────────────────────────────────────
def test_split_output_with_marker() -> None:
    full = "*레이가 다가선다.*\n레이: 안녕.\n[다음 행동]\n1. 인사한다\n2. 검을 뽑는다\n3. 돌아선다"
    body, choices = _split_output(full)
    assert body == "*레이가 다가선다.*\n레이: 안녕."
    assert "[다음 행동]" not in body
    assert choices == ["인사한다", "검을 뽑는다", "돌아선다"]


def test_split_output_no_marker() -> None:
    body, choices = _split_output("선택지 없는 본문만")
    assert body == "선택지 없는 본문만"
    assert choices == []


def test_parse_choices_paren_and_dot() -> None:
    assert _parse_choices("\n1) 가\n2. 나\n3) 다") == ["가", "나", "다"]


# ── 화자 볼드 라벨 정규화(KNK-194) — 동기 ────────────────────────────────────
def test_strip_speaker_bold_outer_colon() -> None:
    assert _strip_speaker_bold("**설하**: 차라도 드세요.") == "설하: 차라도 드세요."


def test_strip_speaker_bold_inner_colon() -> None:
    assert _strip_speaker_bold("**설하:** 차라도 드세요.") == "설하: 차라도 드세요."


def test_strip_speaker_bold_keeps_emphasis() -> None:
    # 콜론 없는 본문 강조는 화자 라벨이 아니므로 건드리지 않는다.
    assert _strip_speaker_bold("그것은 **중요한** 단서다") == "그것은 **중요한** 단서다"


def test_strip_speaker_bold_multiline() -> None:
    text = "*등불이 흔들린다.*\n**설하:** 늦었군요.\n**장천**: 거래합시다."
    expected = "*등불이 흔들린다.*\n설하: 늦었군요.\n장천: 거래합시다."
    assert _strip_speaker_bold(text) == expected


# ── 스트리밍(B안) — async, LLM mock ─────────────────────────────────────────
class _FakeDelta:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.delta = _FakeDelta(content)


class _FakeChunk:
    def __init__(self, content: str | None) -> None:
        # content=None → choices가 빈 청크(메타데이터·필터 청크)를 흉내 낸다.
        self.choices = [] if content is None else [_FakeChoice(content)]


@pytest.fixture
def mock_stream(monkeypatch):
    """청크 리스트를 받아 _client.chat.completions.create를 가짜 스트림으로 바꾼다."""

    def _set(chunks: list[str | None]) -> None:
        async def _agen():
            for c in chunks:
                yield _FakeChunk(c)

        async def _create(**kwargs):
            return _agen()

        monkeypatch.setattr(chat_llm._client.chat.completions, "create", _create)

    return _set


async def test_stream_splits_body_and_choices(mock_stream) -> None:
    # 마커가 토큰 경계에 걸치도록 쪼갬("[다음" + " 행동]")
    mock_stream(["*지문*\n레이: 말한다.\n", "[다음", " 행동]\n1. 가\n2. 나\n3. 다"])
    events = [e async for e in stream_chat_turn([])]

    tokens = "".join(e["text"] for e in events if e["event"] == "token")
    completed = next(e for e in events if e["event"] == "completed")

    # B안: 선택지(마커 이후)는 token으로 흘리지 않는다
    assert "[다음 행동]" not in tokens
    assert "1. 가" not in tokens
    # 본문은 흘렸고, 선택지는 completed에만
    assert "레이: 말한다." in tokens
    assert "[다음 행동]" not in completed["ai_output"]
    assert completed["choices"] == ["가", "나", "다"]


async def test_stream_no_marker_flushes_body(mock_stream) -> None:
    mock_stream(["선택지 없는 ", "응답"])
    events = [e async for e in stream_chat_turn([])]
    tokens = "".join(e["text"] for e in events if e["event"] == "token")
    completed = next(e for e in events if e["event"] == "completed")
    assert tokens == "선택지 없는 응답"
    assert completed["ai_output"] == "선택지 없는 응답"
    assert completed["choices"] == []


async def test_stream_skips_empty_choices_chunk(mock_stream) -> None:
    # choices가 빈 청크(메타/필터)가 섞여도 IndexError 없이 건너뛰고 본문만 흘린다.
    mock_stream([None, "본문 ", None, "이어짐"])
    events = [e async for e in stream_chat_turn([])]
    tokens = "".join(e["text"] for e in events if e["event"] == "token")
    completed = next(e for e in events if e["event"] == "completed")
    assert tokens == "본문 이어짐"
    assert completed["ai_output"] == "본문 이어짐"


async def test_stream_strips_speaker_bold_in_completed(mock_stream) -> None:
    # 화자 라벨 볼드는 completed의 ai_output에서 제거된다(저장·표시값 정규화).
    mock_stream(["*등불이 흔들린다.*\n**설하:** 늦었군요.\n[다음 행동]\n1. 가\n2. 나\n3. 다"])
    events = [e async for e in stream_chat_turn([])]
    completed = next(e for e in events if e["event"] == "completed")
    assert "**" not in completed["ai_output"]
    assert "설하: 늦었군요." in completed["ai_output"]
    assert completed["choices"] == ["가", "나", "다"]
