import pytest

from tests.conftest import FakeStream

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
        self.finish_reason = None


class _FakeChunk:
    def __init__(self, content: str | None) -> None:
        # content=None → choices가 빈 청크(메타데이터·필터 청크)를 흉내 낸다.
        self.choices = [] if content is None else [_FakeChoice(content)]
        self.model = None
        self.usage = None


@pytest.fixture
def mock_stream(install_llm_sdk):
    """청크 리스트를 받아 SDK 경계에 가짜 스트림을 심는다(목 지점은 어댑터 아래 — KNK-673)."""

    def _set(chunks: list[str | None]) -> None:
        async def _create(**kwargs):
            return FakeStream([_FakeChunk(c) for c in chunks])

        install_llm_sdk(_create)

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


async def test_stream_captures_model_and_usage(install_llm_sdk) -> None:
    # 본문 청크 + (choices 빈) usage 전용 마지막 청크에서 model·토큰을 취득해 completed에 싣는다.
    chunks = [
        _MetaChunk(content="본문", model="deepseek-v4-flash"),
        _MetaChunk(content=None, model="deepseek-v4-flash", usage=_FakeUsage(11, 22)),
    ]

    async def _create(**kwargs):
        assert kwargs.get("stream_options") == {"include_usage": True}  # 토큰 동봉 플래그
        return FakeStream(chunks)

    install_llm_sdk(_create)

    events = [e async for e in stream_chat_turn([])]
    completed = next(e for e in events if e["event"] == "completed")
    assert completed["model"] == "deepseek-v4-flash"
    assert completed["input_tokens"] == 11
    assert completed["output_tokens"] == 22
    # provider는 응답에 없는 값이라 모델 이름을 등록부로 해석해 싣는다(KNK-674).
    assert completed["provider"] == "deepseek"


# ── 토큰 누락은 0이 아니라 null (KNK-673 리뷰) ───────────────────────────────
# 백엔드 계약이 "누락 시 null"이다. 0으로 채우면 "정보를 못 받았다"와 "0개 썼다"가 같은 값으로
# 적재돼 사용량 통계가 조용히 틀어진다. 성공 경로의 가짜가 늘 usage를 채워 주면 이 회귀를
# 못 잡으므로(변이 `input_tokens or 0`이 통과했다), 없는 경우를 따로 태운다.
async def test_stream_keeps_missing_usage_as_null(mock_stream) -> None:
    mock_stream(["usage 청크가 없는 응답"])
    events = [e async for e in stream_chat_turn([])]
    completed = next(e for e in events if e["event"] == "completed")

    assert completed["input_tokens"] is None
    assert completed["output_tokens"] is None
    # 모델명은 응답이 안 알려줘도 요청에 쓴 이름으로 채워진다(폴백은 통로가 맡는다).
    assert completed["model"] == chat_llm.settings.chat_model


async def test_stream_keeps_usage_without_token_fields_as_null(install_llm_sdk) -> None:
    """usage 객체는 왔는데 토큰 칸이 없는 경우도 null이다(0으로 접지 않는다)."""

    class _FieldlessUsage:
        pass

    chunks = [
        _MetaChunk(content="본문", model="deepseek-v4-flash"),
        _MetaChunk(content=None, model="deepseek-v4-flash", usage=_FieldlessUsage()),
    ]

    async def _create(**kwargs):
        return FakeStream(chunks)

    install_llm_sdk(_create)

    events = [e async for e in stream_chat_turn([])]
    completed = next(e for e in events if e["event"] == "completed")
    assert completed["input_tokens"] is None
    assert completed["output_tokens"] is None


# ── 중도 이탈 시 하위 스트림 정리 (KNK-673 리뷰) ─────────────────────────────
async def test_consumer_early_exit_closes_underlying_stream(install_llm_sdk) -> None:
    """사용자가 채팅 창을 닫으면 통로 아래 스트림도 함께 닫혀 커넥션이 반납된다.

    `async for`는 중도 이탈 때 안쪽 제너레이터를 닫아주지 않는다. 그래서 이 함수가
    `aclosing`으로 감싸지 않으면, 어댑터가 커넥션 반납용으로 넣어둔 정리 코드가 쓰레기 수집
    시점까지 밀린다 — 스트리밍 경로는 채팅 하나뿐이라 그 정리가 실제로는 한 번도 제때
    작동하지 않게 된다.
    """
    holder: dict = {}

    async def _create(**kwargs):
        holder["stream"] = FakeStream([_FakeChunk("가"), _FakeChunk("나"), _FakeChunk("다")])
        return holder["stream"]

    install_llm_sdk(_create)

    turn = stream_chat_turn([])
    async for _event in turn:
        break  # 첫 조각만 받고 떠난다
    await turn.aclose()  # 연결이 끊겨 제너레이터가 회수되는 시점

    assert holder["stream"].closed is True


# ── Sentry 캡처 경계(KNK-262) — 성공은 조용, 실패만 보고 ──────────────────────
async def test_stream_success_does_not_capture(mock_stream, monkeypatch) -> None:
    """정상 스트림(completed)에서는 Sentry capture를 호출하지 않는다."""
    calls: list = []
    monkeypatch.setattr(chat_llm, "capture_ai_exception", lambda *a, **k: calls.append(1))
    mock_stream(["*지문*\n레이: 안녕."])
    events = [e async for e in stream_chat_turn([])]
    assert any(e["event"] == "completed" for e in events)
    assert calls == []  # 성공 경로 — 미호출


async def test_stream_error_captures(monkeypatch, install_llm_sdk) -> None:
    """스트림 중 공급자 오류가 나면 error 이벤트와 함께 chat_response feature로 캡처한다.

    가짜는 SDK 예외를 던지고, 어댑터가 그것을 공급자 중립 예외로 접어 여기까지 올린다.
    """
    from openai import OpenAIError

    async def _create(**kwargs):
        raise OpenAIError("boom")

    install_llm_sdk(_create)
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
    # 스트림이 오류로 끝나면 종료 이벤트가 없다 — 그래도 provider 태그는 채워져야 한다(KNK-674).
    assert calls[0]["provider"] == "deepseek"


# ── 스트림 도중의 실패·취소 (KNK-673) ────────────────────────────────────────
# 시작하자마자 실패(위)와 다른 경계다: **토큰을 이미 흘려보낸 뒤** 끊기는 경우.
# 사용자는 글이 나오다 멈추는 것을 보므로, 조용히 끝나면 안 되고 error 이벤트로 닫혀야 한다.
async def test_stream_error_after_tokens_still_yields_error_event(
    monkeypatch, install_llm_sdk
) -> None:
    from openai import APIConnectionError

    monkeypatch.setattr(chat_llm, "capture_ai_exception", lambda *a, **k: None)
    request = __import__("httpx").Request("POST", "https://api.deepseek.com/v1")

    async def _create(**kwargs):
        return FakeStream([_FakeChunk("첫 문장")], error=APIConnectionError(request=request))

    install_llm_sdk(_create)

    events = [e async for e in stream_chat_turn([])]
    names = [e["event"] for e in events]

    assert names == ["token", "error"]  # 흘린 토큰 뒤에 error로 닫는다
    assert events[0]["text"] == "첫 문장"
    assert not any(e["event"] == "completed" for e in events)  # 완료로 위장하지 않는다


async def test_stream_cancellation_is_not_reported_as_error(monkeypatch, install_llm_sdk) -> None:
    """사용자가 창을 닫아 취소되면 error 이벤트를 만들지 않는다 — 취소는 오류가 아니다.

    여기서 error를 내면 없는 장애가 Sentry에 쌓이고, 이미 끊긴 연결로 이벤트를 쓰려다
    또 다른 오류가 난다.
    """
    import asyncio

    calls: list = []
    monkeypatch.setattr(chat_llm, "capture_ai_exception", lambda *a, **k: calls.append(k))

    async def _create(**kwargs):
        return FakeStream([_FakeChunk("첫 문장")], error=asyncio.CancelledError())

    install_llm_sdk(_create)

    events: list = []
    with pytest.raises(asyncio.CancelledError):
        async for e in stream_chat_turn([]):
            events.append(e)

    assert [e["event"] for e in events] == ["token"]  # 토큰까지만, error 없음
    assert calls == []  # 장애로 보고하지 않는다


# ── 호출 인자 계약 단언 (KNK-584 재감사 #8) ───────────────────────────────────
# 가짜가 kwargs를 버리면 model·stream·thinking 설정 회귀를 못 잡는다. 넘긴 인자를
# 통째로 붙잡아, 본문 경로가 스트리밍·usage 동봉·비추론으로 호출하는지 고정한다.
async def test_stream_call_contract(install_llm_sdk) -> None:
    captured: dict = {}

    async def _create(**kwargs):
        captured.update(kwargs)
        return FakeStream([_FakeChunk("본문")])

    install_llm_sdk(_create)
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    _ = [e async for e in stream_chat_turn(msgs)]

    assert captured["model"] == chat_llm.settings.chat_model
    assert captured["messages"] is msgs  # 조립한 messages를 가공 없이 그대로 넘긴다
    assert captured["stream"] is True
    assert captured["stream_options"] == {"include_usage": True}  # 토큰 로깅용
    # 추론 끄기는 이제 호출부가 아니라 등록부(use_thinking=False)의 뜻을 어댑터가 옮긴 것이다.
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
    # 타임아웃을 호출마다 넘긴다 — 비우면 상한이 SDK 기본값(10분)으로 늘어난다(KNK-673).
    assert captured["timeout"] == chat_llm._TIMEOUT_SECONDS == 90.0
    # 본문은 지문·대사를 자유 생성하므로 json 모드·출력 상한을 걸지 않는다.
    assert "response_format" not in captured
    assert "max_tokens" not in captured


# ── provider는 고정값이 아니라 지금 쓰는 모델의 공급자다 (KNK-674) ────────────
# 모든 테스트가 DeepSeek이면 "그냥 'deepseek'을 적어둔 코드"와 구분되지 않는다.
# 다른 회사 모델을 하나 끼워 넣어, 값이 모델을 따라 바뀌는지 본다.
async def test_provider_follows_the_selected_model(other_provider_model, install_llm_sdk) -> None:
    other_provider_model(chat_llm)

    async def _create(**kwargs):
        return FakeStream([_FakeChunk("본문")])

    install_llm_sdk(_create)

    events = [e async for e in stream_chat_turn([])]
    completed = next(e for e in events if e["event"] == "completed")

    assert completed["provider"] == "not-deepseek"


async def test_failure_capture_provider_follows_the_selected_model(
    monkeypatch, other_provider_model, install_llm_sdk
) -> None:
    """스트림이 오류로 끝나도 같은 값이 실린다 — 종료 이벤트가 없는 경로다."""
    from openai import OpenAIError

    other_provider_model(chat_llm)
    calls: list = []
    monkeypatch.setattr(chat_llm, "capture_ai_exception", lambda *a, **k: calls.append(k))

    async def _create(**kwargs):
        raise OpenAIError("boom")

    install_llm_sdk(_create)

    events = [e async for e in stream_chat_turn([])]

    assert any(e["event"] == "error" for e in events)
    assert calls[0]["provider"] == "not-deepseek"


async def test_provider_is_resolved_before_the_llm_call(monkeypatch, install_llm_sdk) -> None:
    """provider 조회는 LLM 호출 **전에** 한다 — 실패하면 헛돈이 안 나가야 한다(KNK-674).

    판정에 있는 같은 테스트의 짝이다(KNK-674 2차 리뷰 3번). 조회를 스트림 뒤나 except 안으로
    옮기면 except의 `provider=provider`가 UnboundLocalError가 나 여러 테스트가 함께 깨지는데,
    그건 **우연히** 잡히는 것이라 규칙을 직접 말하는 테스트를 따로 둔다.

    조회가 호출 전에 있는지를 **LLM을 한 번도 부르지 않았다**로 확인한다.
    """
    calls = {"n": 0}

    async def _create(**kwargs):
        calls["n"] += 1
        return FakeStream([_FakeChunk("본문")])

    install_llm_sdk(_create)

    def _boom(_model: str) -> str:
        raise RuntimeError("등록부 조회 실패")

    monkeypatch.setattr(chat_llm.llm, "provider_of", _boom)

    with pytest.raises(RuntimeError):
        [e async for e in stream_chat_turn([])]

    assert calls["n"] == 0  # LLM을 부르기도 전에 막힌다
