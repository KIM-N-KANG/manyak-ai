import pytest

from src.services import chat_llm
from src.services.chat_llm import _strip_speaker_bold, stream_chat_turn


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


# ── 스트리밍(본문 전용, 선택지 없음) — async, LLM mock ────────────────────────
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


async def test_stream_streams_all_tokens(mock_stream) -> None:
    # 본문은 마커 처리 없이 받은 델타를 그대로 흘린다. 선택지는 이 호출이 만들지 않는다.
    mock_stream(["*지문*\n레이: 말한다.\n", "이어지는 본문."])
    events = [e async for e in stream_chat_turn([])]

    tokens = "".join(e["text"] for e in events if e["event"] == "token")
    completed = next(e for e in events if e["event"] == "completed")

    assert tokens == "*지문*\n레이: 말한다.\n이어지는 본문."
    assert completed["ai_output"] == "*지문*\n레이: 말한다.\n이어지는 본문."
    # 본문 호출은 더 이상 choices를 만들지 않는다(선택지는 별도 호출 담당).
    assert "choices" not in completed


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
    mock_stream(["*등불이 흔들린다.*\n**설하:** 늦었군요."])
    events = [e async for e in stream_chat_turn([])]
    completed = next(e for e in events if e["event"] == "completed")
    assert "**" not in completed["ai_output"]
    assert "설하: 늦었군요." in completed["ai_output"]


# ── 로깅 메타 재료 수집(KNK-243) — model·usage ───────────────────────────────
class _FakeUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _MetaChunk:
    """model·usage 속성을 가진 청크. usage 전용 청크는 choices가 비어 온다."""

    def __init__(self, content: str | None = None, model: str | None = None, usage=None) -> None:
        self.choices = [] if content is None else [_FakeChoice(content)]
        self.model = model
        self.usage = usage


async def test_stream_captures_model_and_usage(monkeypatch) -> None:
    # 본문 청크 + (choices 빈) usage 전용 마지막 청크에서 model·토큰을 취득해 completed에 싣는다.
    async def _agen():
        yield _MetaChunk(content="본문", model="deepseek-v4-flash")
        yield _MetaChunk(content=None, model="deepseek-v4-flash", usage=_FakeUsage(11, 22))

    async def _create(**kwargs):
        assert kwargs.get("stream_options") == {"include_usage": True}  # 토큰 동봉 플래그
        return _agen()

    monkeypatch.setattr(chat_llm._client.chat.completions, "create", _create)

    events = [e async for e in stream_chat_turn([])]
    completed = next(e for e in events if e["event"] == "completed")
    assert completed["model"] == "deepseek-v4-flash"
    assert completed["input_tokens"] == 11
    assert completed["output_tokens"] == 22


# ── Sentry 캡처 경계(KNK-262) — 성공은 조용, 실패만 보고 ──────────────────────
async def test_stream_success_does_not_capture(mock_stream, monkeypatch) -> None:
    """정상 스트림(completed)에서는 Sentry capture를 호출하지 않는다."""
    calls: list = []
    monkeypatch.setattr(chat_llm, "capture_ai_exception", lambda *a, **k: calls.append(1))
    mock_stream(["*지문*\n레이: 안녕."])
    events = [e async for e in stream_chat_turn([])]
    assert any(e["event"] == "completed" for e in events)
    assert calls == []  # 성공 경로 — 미호출


async def test_stream_error_captures(monkeypatch) -> None:
    """스트림 중 OpenAIError가 나면 error 이벤트와 함께 chat_response feature로 캡처한다."""
    from openai import OpenAIError

    async def _create(**kwargs):
        raise OpenAIError("boom")

    monkeypatch.setattr(chat_llm._client.chat.completions, "create", _create)
    calls: list = []
    monkeypatch.setattr(chat_llm, "capture_ai_exception", lambda *a, **k: calls.append(k))

    events = [e async for e in stream_chat_turn([])]
    err_event = next(e for e in events if e["event"] == "error")
    assert err_event["code"] == "LLM_ERROR"
    assert "boom" not in err_event["message"]  # provider 원문(str(e)) 미노출 — AN-4-10
    assert len(calls) == 1
    assert calls[0]["feature"] == "chat_response"
    # AN-4-8 컨텍스트 — 실패 캡처에 재호출 횟수·소요 시간이 실린다(KNK-529)
    assert calls[0]["retry_count"] == 0
    assert isinstance(calls[0]["latency_ms"], int) and calls[0]["latency_ms"] >= 0
