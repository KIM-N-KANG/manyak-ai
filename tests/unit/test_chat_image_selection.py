"""KNK-1505: 실제 선택 서비스부터 모의 HTTP까지 입력·선택·실패 경로를 검증한다."""

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest

from src.core.config import Settings
from src.schemas.chat_turn import CharacterImageMapping, ChatHistoryItem
from src.services import chat_image_selection
from src.services.chat_image_selection import select_images
from src.services.llm import registry, typesafe_api


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    value = Settings(
        _env_file=None,
        deepseek_api_key="test",
        openai_api_key="test",
        typesafe_api_key="test-typesafe-key",
        typesafe_api_url="https://api.typesafe.ai",
        jev_model="jev-1.13.0",
    )
    monkeypatch.setattr(registry, "settings", value)
    monkeypatch.setattr(chat_image_selection, "settings", value)
    return value


@pytest.fixture
def images() -> list[CharacterImageMapping]:
    return [
        CharacterImageMapping(name="세린", image_name=f"세린_{suffix}",
                              image_url=f"https://cdn.example/{index}.webp")
        for index, suffix in enumerate(("기본", "웃음", "분노"))
    ]


@pytest.fixture
def install_http(monkeypatch: pytest.MonkeyPatch) -> Callable:
    original_client = httpx.AsyncClient

    def install(handler: Callable) -> list[httpx.Request]:
        requests: list[httpx.Request] = []

        async def record(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            response = handler(request)
            return await response if asyncio.iscoroutine(response) else response

        monkeypatch.setattr(
            typesafe_api.httpx, "AsyncClient",
            lambda **kwargs: original_client(transport=httpx.MockTransport(record), **kwargs),
        )
        return requests

    return install


def response() -> dict:
    return {
        "model": "jev-1.13.0",
        "answers": {"1": {
            "type": "choice", "choice": "2", "confidence": 0.9,
            "probabilities": {"1": 0.1, "2": 0.8, "3": 0.1},
        }},
        "usage": {"input_tokens": 120, "output_tokens": 30},
    }


async def test_selects_original_mapping_and_sends_recent_two_turns(images, install_http) -> None:
    history = [ChatHistoryItem(role="ASSISTANT", content="오프닝")]
    for index in range(3):
        history.extend([
            ChatHistoryItem(role="USER", content=f"질문{index}"),
            ChatHistoryItem(role="ASSISTANT", content=f"[[https://cdn.example/old.webp]]\n\n답변{index}"),
        ])
    history.append(ChatHistoryItem(role="USER", content="짝 없는 입력"))
    original_history = [item.model_dump() for item in history]
    requests = install_http(lambda request: httpx.Response(200, json=response()))

    result = await select_images(
        character_images=images, history=history, user_input="반가워",
        ai_output="[[https://cdn.example/current.webp]]\n\n세린: 어서 와!\n*손을 흔든다.*",
    )

    assert result.images == [images[1]]
    assert result.images[0].image_url == "https://cdn.example/1.webp"
    assert result.usage.input_tokens == 120 and result.usage.output_tokens == 30
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["model"] == "jev-1.13.0"
    assert payload["state"] == {
        "recent_turns": [{"user": "질문1", "assistant": "답변1"},
                         {"user": "질문2", "assistant": "답변2"}],
        "current_turn": {"user": "반가워", "assistant": "세린: 어서 와!\n*손을 흔든다.*"},
    }
    question = payload["questions"]["1"]
    assert question["type"] == "choice"
    assert question["criteria"] == {
        str(index): image.image_name
        for index, image in enumerate(images, start=1)
    }
    assert '선택 대상 인물: "세린"' in question["instructions"]
    assert "{{character_name}}" not in question["instructions"]
    assert "## [INSTRUCTIONS]" not in question["instructions"]
    assert "cdn.example" not in requests[0].content.decode()
    assert [item.model_dump() for item in history] == original_history


@pytest.mark.parametrize("count", [0, 1, 256])
async def test_empty_single_and_excess_candidates_skip_api(count, images, install_http) -> None:
    requests = install_http(lambda request: pytest.fail("호출하지 않아야 함"))
    candidates = [images[0].model_copy() for _ in range(count)]
    result = await select_images(character_images=candidates, history=[], user_input="", ai_output="세린: 안녕")
    assert result.images == ([candidates[0]] if count == 1 else [])
    assert result.usage.input_tokens is None
    assert not requests


@pytest.mark.parametrize("field", ["name", "image_name", "image_url"])
async def test_blank_candidate_is_excluded(field, images, install_http) -> None:
    requests = install_http(lambda request: pytest.fail("호출하지 않아야 함"))
    invalid = images[0].model_copy(update={field: " "})
    result = await select_images(
        character_images=[invalid, images[1]], history=[], user_input="", ai_output="세린: 안녕",
    )
    assert result.images == [images[1]]
    assert not requests


@pytest.mark.parametrize("case", ["outside", "missing", "json", "server", "timeout", "key"])
async def test_failure_returns_no_image_without_retry(case, images, install_http, settings, caplog) -> None:
    data = response()
    if case == "outside":
        data["answers"]["1"]["choice"] = "999"
    if case == "missing":
        data["answers"] = {}
    if case == "key":
        settings.typesafe_api_key = ""

    def handler(request: httpx.Request) -> httpx.Response:
        if case == "timeout":
            raise httpx.ReadTimeout("private-input")
        if case == "server":
            return httpx.Response(500, text="private-input")
        if case == "json":
            return httpx.Response(200, text="private-input")
        return httpx.Response(200, json=data)

    requests = install_http(handler)
    result = await select_images(
        character_images=images, history=[], user_input="private-input", ai_output="세린: private-input",
    )
    assert result.images == []
    assert len(requests) == (0 if case == "key" else 1)
    assert "private-input" not in caplog.text
    assert "cdn.example" not in caplog.text


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
async def test_expired_or_invalid_budget_skips_api(timeout, images, install_http) -> None:
    requests = install_http(lambda request: pytest.fail("호출하지 않아야 함"))
    result = await select_images(
        character_images=images, history=[], user_input="", ai_output="세린: 안녕", timeout=timeout,
    )
    assert result.images == [] and not requests


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_stop_pending_request(cancel, images, install_http) -> None:
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    requests = install_http(handler)
    task = asyncio.create_task(select_images(
        character_images=images, history=[], user_input="", ai_output="세린: 안녕",
        timeout=10 if cancel else 0.03,
    ))
    await entered.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert (await task).images == []
    assert stopped.is_set() and len(requests) == 1


async def test_multiple_speakers_share_one_call_and_exclude_silent_character(images, install_http):
    others = [image.model_copy(update={"name": "민수", "image_name": f"민수_{i}"})
              for i, image in enumerate(images)]
    silent = images[0].model_copy(update={"name": "행인"})
    data = response()
    data["answers"]["2"] = {**data["answers"]["1"], "choice": "3",
                            "probabilities": {"1": 0.1, "2": 0.1, "3": 0.8}}
    requests = install_http(lambda request: httpx.Response(200, json=data))
    result = await select_images(
        character_images=images + others + [silent], history=[], user_input="안녕",
        ai_output="세린: 어서 와\n민수: 반가워\n세린: 앉아",
    )
    assert result.images == [images[1], others[2]]
    assert len(requests) == 1
    questions = json.loads(requests[0].content)["questions"]
    assert len(questions) == 2
    assert all("세린" in value for value in questions["1"]["criteria"].values())
    assert all("민수" in value for value in questions["2"]["criteria"].values())


async def test_five_speakers_get_matching_instructions_and_full_candidate_lists(install_http) -> None:
    names = ["서연", "민수", "지우", "하린", "도윤"]
    counts = [20, 40, 3, 2, 4]
    groups = [
        [CharacterImageMapping(
            name=name, image_name=f"편의점에서_짜증_{index}",
            image_url=f"https://cdn.example/{number}/{index}.webp",
        ) for index in range(count)]
        for number, (name, count) in enumerate(zip(names, counts))
    ]
    data = response()
    data["answers"] = {
        str(number): {
            "type": "choice", "choice": str(count), "confidence": 1.0,
            "probabilities": {str(index): float(index == count) for index in range(1, count + 1)},
        }
        for number, count in enumerate(counts, start=1)
    }
    requests = install_http(lambda request: httpx.Response(200, json=data))
    result = await select_images(
        character_images=[image for group in reversed(groups) for image in group],
        history=[], user_input="안녕",
        ai_output="\n".join(f"{name}: 반가워" for name in names),
    )
    assert result.images == [group[-1] for group in groups]
    assert len(requests) == 1
    questions = json.loads(requests[0].content)["questions"]
    assert len(questions) == 5
    for number, (name, group) in enumerate(zip(names, groups), start=1):
        question = questions[str(number)]
        assert f'선택 대상 인물: "{name}"' in question["instructions"]
        assert "{{character_name}}" not in question["instructions"]
        assert all(other not in question["instructions"] for other in names if other != name)
        assert question["criteria"] == {
            str(index): image.image_name for index, image in enumerate(group, start=1)
        }


async def test_no_speaker_skips_api(images, install_http):
    requests = install_http(lambda request: pytest.fail("호출하지 않아야 함"))
    result = await select_images(character_images=images, history=[], user_input="", ai_output="*조용하다.*")
    assert result.images == [] and not requests
