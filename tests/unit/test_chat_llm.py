import pytest

from tests.conftest import FakeStream

from src.schemas.chat_turn import CharacterImageMapping
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


def test_strip_speaker_bold_does_not_cross_lines() -> None:
    # 볼드 라벨 뒤 줄바꿈 너머의 콜론을 같은 라벨로 묶지 않는다(KNK-1005 퍼징에서 발견 —
    # 옛 정규식은 `\s*`로 줄을 건너 `세린:: 콜론시작`을 만들었고 스트리밍 파서와 어긋났다).
    assert _strip_speaker_bold("**세린:**\n: 콜론시작") == "세린: \n: 콜론시작"
    assert _strip_speaker_bold("**세린**\n: 다음 줄") == "**세린**\n: 다음 줄"


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


async def test_stream_strips_speaker_bold_in_tokens_too(mock_stream) -> None:
    # 실시간 token에서도 볼드를 떼 저장 본문과 같은 글자가 화면에 나간다(KNK-1005).
    mock_stream(["*등불이 흔들린다.*\n**설하:** 늦었군요.\n**장천**: 거래합시다."])
    events = [e async for e in stream_chat_turn([])]
    tokens = "".join(e["text"] for e in events if e["event"] == "token")
    completed = next(e for e in events if e["event"] == "completed")
    assert tokens == "*등불이 흔들린다.*\n설하: 늦었군요.\n장천: 거래합시다."
    assert completed["ai_output"] == tokens


# ── 인물명 라벨 감지 → 이미지 이벤트·저장 마커(KNK-1005) ─────────────────────
def _character_images() -> list[CharacterImageMapping]:
    return [
        CharacterImageMapping(name="세린", image_url="https://cdn.example.com/serin.webp"),
        CharacterImageMapping(name="레이", image_url="https://cdn.example.com/rei.webp"),
    ]


def _image_names(events: list[dict]) -> list[str]:
    return [e["name"] for e in events if e["event"] == "character_image"]


def _visible(events: list[dict]) -> str:
    return "".join(e["text"] for e in events if e["event"] == "token")


async def test_stream_emits_image_before_every_label_across_chunk_boundaries(
    mock_stream,
) -> None:
    # 태그 없이 `인물명:` 줄만으로 이미지가 뜨고, 같은 인물이 다시 말하면 다시 뜬다.
    # 라벨이 델타 경계에서 쪼개져도 잡는다.
    mock_stream(
        [
            "*문이 열린다.*\n세",
            "린: 기다렸어?\n레이",
            ": 들어가자.\n세린: 다시 확인할게.",
        ]
    )

    events = [
        event
        async for event in stream_chat_turn([], character_images=_character_images())
    ]
    completed = next(event for event in events if event["event"] == "completed")

    assert _visible(events) == (
        "*문이 열린다.*\n세린: 기다렸어?\n레이: 들어가자.\n세린: 다시 확인할게."
    )
    assert _image_names(events) == ["세린", "레이", "세린"]
    assert completed["ai_output"] == (
        "*문이 열린다.*\n"
        "[[https://cdn.example.com/serin.webp]]\n\n세린: 기다렸어?\n"
        "[[https://cdn.example.com/rei.webp]]\n\n레이: 들어가자.\n"
        "[[https://cdn.example.com/serin.webp]]\n\n세린: 다시 확인할게."
    )
    assert completed["character_images"] == [
        {"name": "세린", "image_url": "https://cdn.example.com/serin.webp"},
        {"name": "레이", "image_url": "https://cdn.example.com/rei.webp"},
        {"name": "세린", "image_url": "https://cdn.example.com/serin.webp"},
    ]


async def test_stream_image_event_precedes_the_label_token(mock_stream) -> None:
    # 이미지 이벤트는 라벨 글자가 담긴 token보다 먼저 나간다 — 프론트가 그 자리에 이미지를 그린다.
    mock_stream(["*지문*\n세린: 왔어."])
    events = [
        event
        async for event in stream_chat_turn([], character_images=_character_images())
    ]
    kinds = [e["event"] for e in events]
    assert kinds == ["token", "character_image", "token", "completed"]
    assert events[0]["text"] == "*지문*\n"
    assert events[2]["text"] == "세린: 왔어."


async def test_bold_label_still_triggers_image_and_is_normalized(mock_stream) -> None:
    # 모델이 이름을 볼드로 감싸도(두 형태 모두) 이미지가 뜨고 화면·저장 모두 평문이 된다.
    mock_stream(["**세린:** 기다렸어?\n**레이**:   늦었네.\n**미라:** 안녕."])
    events = [
        event
        async for event in stream_chat_turn([], character_images=_character_images())
    ]
    completed = next(event for event in events if event["event"] == "completed")

    assert _visible(events) == "세린: 기다렸어?\n레이: 늦었네.\n미라: 안녕."
    assert _image_names(events) == ["세린", "레이"]
    assert completed["ai_output"] == (
        "[[https://cdn.example.com/serin.webp]]\n\n세린: 기다렸어?\n"
        "[[https://cdn.example.com/rei.webp]]\n\n레이: 늦었네.\n"
        "미라: 안녕."
    )
    assert [i["name"] for i in completed["character_images"]] == ["세린", "레이"]


async def test_label_must_match_a_registered_name_exactly(mock_stream) -> None:
    # 이미지가 없는 인물, 지문 속 이름, 이름으로 시작하는 서술, 줄 중간의 `이름:`은 이미지가 아니다.
    mock_stream(
        [
            "미라: 안녕.\n*세린이 웃는다.*\n세린은 말이 없다.\n"
            "그때 세린: 이라고 적힌 쪽지가 보였다.\n세린아: 부르는 소리."
        ]
    )
    events = [
        event
        async for event in stream_chat_turn([], character_images=_character_images())
    ]
    completed = next(event for event in events if event["event"] == "completed")

    raw = (
        "미라: 안녕.\n*세린이 웃는다.*\n세린은 말이 없다.\n"
        "그때 세린: 이라고 적힌 쪽지가 보였다.\n세린아: 부르는 소리."
    )
    assert _visible(events) == raw
    assert _image_names(events) == []
    assert completed["ai_output"] == raw
    assert completed["character_images"] == []


async def test_longer_registered_name_wins_over_its_prefix(mock_stream) -> None:
    images = [
        CharacterImageMapping(name="세린", image_url="https://cdn.example.com/serin.webp"),
        CharacterImageMapping(name="세린아", image_url="https://cdn.example.com/serina.webp"),
    ]
    mock_stream(["세린아: 여기야.\n세린: 응."])
    events = [event async for event in stream_chat_turn([], character_images=images)]
    completed = next(event for event in events if event["event"] == "completed")

    assert _image_names(events) == ["세린아", "세린"]
    assert completed["ai_output"] == (
        "[[https://cdn.example.com/serina.webp]]\n\n세린아: 여기야.\n"
        "[[https://cdn.example.com/serin.webp]]\n\n세린: 응."
    )


async def test_thirty_char_name_is_recognized_plain_and_bold(mock_stream) -> None:
    # 이름 상한(30자)까지 평문·볼드 모두 잡는다 — 옛 볼드 정규식의 20자 제한을 없앴다.
    long_name = "가" * 30
    images = [
        CharacterImageMapping(name=long_name, image_url="https://cdn.example.com/long.webp")
    ]
    mock_stream([f"{long_name}: 늦었어.\n**{long_name}:** 다시."])
    events = [event async for event in stream_chat_turn([], character_images=images)]
    completed = next(event for event in events if event["event"] == "completed")

    assert _visible(events) == f"{long_name}: 늦었어.\n{long_name}: 다시."
    assert _image_names(events) == [long_name, long_name]
    assert completed["ai_output"] == (
        f"[[https://cdn.example.com/long.webp]]\n\n{long_name}: 늦었어.\n"
        f"[[https://cdn.example.com/long.webp]]\n\n{long_name}: 다시."
    )


async def test_deep_indent_does_not_count_toward_the_buffer_limit(mock_stream) -> None:
    # 리뷰 지적: 들여쓰기 11칸 + 30자 이름은 41자라 옛 상한(40)에서 실시간만 포기했다.
    # 상한은 들여쓰기를 뺀 길이로 세므로 실시간·저장이 같은 줄을 잡는다.
    long_name = "가" * 30
    images = [CharacterImageMapping(name=long_name, image_url="https://cdn.example.com/long.webp")]
    mock_stream([f"x\n{' ' * 11}{long_name}: 안녕."])
    events = [event async for event in stream_chat_turn([], character_images=images)]
    completed = next(event for event in events if event["event"] == "completed")

    assert _image_names(events) == [long_name]
    assert [i["name"] for i in completed["character_images"]] == [long_name]


async def test_excess_inner_whitespace_is_not_a_label_on_either_side(mock_stream) -> None:
    # 리뷰 지적: 옛 저장 정규식은 `**`와 이름 사이 공백에 제한이 없어 실시간(상한에서 포기)과
    # 갈렸다. 라벨 안 공백은 양쪽 모두 최대 2칸이다.
    raw = "**" + " " * 39 + "세린:**"
    mock_stream([raw])
    events = [
        event
        async for event in stream_chat_turn([], character_images=_character_images())
    ]
    completed = next(event for event in events if event["event"] == "completed")

    assert _visible(events) == raw
    assert _image_names(events) == []
    assert completed["ai_output"] == raw
    assert completed["character_images"] == []
    # 2칸까지는 양쪽 다 라벨이다.
    assert _strip_speaker_bold("**  세린  **  : 응") == "세린: 응"


async def test_indented_label_is_detected_and_indent_stays_on_dialogue_line(mock_stream) -> None:
    # 들여쓴 라벨도 감지한다. 마커는 대사 줄 위 별도 줄에 오고, 들여쓰기는 대사 줄에 남는다.
    # 첫 줄만은 완료 본문이 strip되므로 앞 공백이 사라진다(둘째 줄부터는 남는다).
    mock_stream(["*문이 열린다.*\n  세린: 들어와."])
    events = [
        event
        async for event in stream_chat_turn([], character_images=_character_images())
    ]
    completed = next(event for event in events if event["event"] == "completed")
    assert _image_names(events) == ["세린"]
    assert _visible(events) == "*문이 열린다.*\n  세린: 들어와."
    assert completed["ai_output"] == (
        "*문이 열린다.*\n[[https://cdn.example.com/serin.webp]]\n\n  세린: 들어와."
    )


async def test_empty_mapping_name_never_matches(mock_stream) -> None:
    images = [CharacterImageMapping(name="", image_url="https://cdn.example.com/empty.webp")]
    mock_stream([": 콜론으로 시작하는 줄\n세린: 안녕."])
    events = [event async for event in stream_chat_turn([], character_images=images)]
    assert _image_names(events) == []
    assert _visible(events) == ": 콜론으로 시작하는 줄\n세린: 안녕."


def test_line_head_is_released_without_waiting_for_a_colon() -> None:
    # 줄머리를 붙잡는 시간은 짧다. 등록된 이름의 앞글자와 다른 줄은 그 글자에서, 지문은
    # 둘째 글자에서, 볼드 후보는 상한에서 원문으로 나간다. 콜론까지 기다리지 않는다.
    def first_release(line: str) -> int:
        """한 글자씩 먹였을 때 몇 글자째에 처음 token이 나가는지."""
        p = chat_llm._SpeakerLabelStreamParser(_character_images())
        for i, ch in enumerate(line, start=1):
            if p.feed(ch):
                return i
        return -1

    assert first_release("문이 열린다.") == 1        # '문'은 어떤 이름의 앞글자도 아님
    assert first_release("*지문이 길게 이어진다*") == 2
    assert first_release("세린은 말이 없었다.") == 3  # '세린'까지 붙잡다 '은'에서 풀림
    limit = chat_llm._LABEL_BUFFER_MAX_CHARS
    assert first_release("**" + "강" * 45) == limit + 1


async def test_stream_flushes_pending_line_head_before_error_and_keeps_sent_image(
    monkeypatch, install_llm_sdk
) -> None:
    from openai import APIConnectionError

    monkeypatch.setattr(chat_llm, "capture_ai_exception", lambda *a, **k: None)
    request = __import__("httpx").Request("POST", "https://api.deepseek.com/v1")

    async def _create(**kwargs):
        return FakeStream(
            [_FakeChunk("세린: 말한다.\n레")],
            error=APIConnectionError(request=request),
        )

    install_llm_sdk(_create)
    events = [
        event
        async for event in stream_chat_turn([], character_images=_character_images())
    ]

    assert [event["event"] for event in events] == [
        "character_image",
        "token",
        "token",
        "error",
    ]
    assert events[0]["name"] == "세린"
    assert events[1]["text"] == "세린: 말한다.\n"
    assert events[2]["text"] == "레"


def test_stream_events_and_storage_markers_share_one_rule() -> None:
    # 실시간 이벤트 목록과 완료 마커 목록은 같은 규칙에서 나오므로 어떤 본문에서도 개수·순서가 같다.
    images = _character_images()
    text = (
        "*지문*\n**세린:** 하나.\n레이: 둘.\n미라: 셋.\n세린 : 넷.\n"
        "  **레이**: 다섯.\n세린은 여섯.\n세린: 일곱."
    )
    parser = chat_llm._SpeakerLabelStreamParser(images)
    streamed: list[dict] = []
    for ch in text:  # 한 글자씩 — 가장 잘게 쪼개진 델타
        streamed.extend(parser.feed(ch))
    streamed.extend(parser.flush())

    normalized = chat_llm._strip_speaker_bold(text)
    stored, markers = chat_llm._insert_storage_markers(normalized, images)

    assert _image_names(streamed) == [m["name"] for m in markers]
    assert _image_names(streamed) == ["세린", "레이", "세린", "레이", "세린"]
    assert _visible(streamed) == normalized
    assert stored.count("[[") == len(markers)


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
