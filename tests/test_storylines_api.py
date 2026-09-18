import json
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest
from httpx import AsyncClient, Request
from openai import APITimeoutError

from src.api.v1 import story as story_module
from src.schemas.story import StorylinesRequest, StorylinesResponse
from src.services.prompt import STORYLINES_VERSION, build_storylines_prompt
from src.services import story_llm

# storylines 엔드포인트의 정상 요청 본문(장르 태그 + 인물 세트, KNK-833).
_REQUEST = {
    "genre_tags": ["무협", "생존"],
    "protagonist": {"name": "무영", "gender": "MALE", "features": ["천마신교", "계획적인"]},
    "supporting_characters": [
        {"name": "서린", "gender": "FEMALE", "features": ["다정한"]},
        {"name": None, "gender": None, "features": ["정파"]},
    ],
}

# storylines 출력 스키마({"stories":[{id, storyline, recommended_infos}]})를 흉내 낸 가짜 결과.
# 이름 지은 주변 인물(서린)은 세 편 모두에 나와야 한다 — 빠지면 부분 재호출을 탄다(KNK-840).
_FAKE = {
    "stories": [
        {"id": 1, "storyline": "서린과 함께한 스토리 1", "recommended_infos": ["가", "나", "다"]},
        {"id": 2, "storyline": "서린이 등장하는 스토리 2", "recommended_infos": ["가", "나", "다"]},
        {"id": 3, "storyline": "서린을 떠나는 스토리 3", "recommended_infos": ["가", "나", "다"]},
    ]
}


async def test_storylines_endpoint_attaches_meta(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """200 경로: 응답에 로깅 메타(snake_case)가 붙고 prompt_versions 키가 STORYLINES인지 확인."""

    captured: dict = {}

    @contextmanager
    def fake_observe(name: str, **kwargs: object):
        captured["name"] = name
        captured.update(kwargs)

        class _Trace:
            def set_metadata(self, **_kwargs: object) -> None: ...

        yield _Trace()

    async def fake_complete(system: str, user: str, **_kwargs: object):
        return _FAKE, story_llm.LlmUsage("deepseek-test", 50, 80, provider="not-deepseek")

    monkeypatch.setattr(story_module, "observe_request", fake_observe)
    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    response = await client.post(
        "/api/v1/story/storylines",
        json=_REQUEST,
        headers={
            "X-Manyak-Creation-Id": "11111111-1111-1111-1111-111111111111",
            "X-Manyak-Parent-Creation-Id": "00000000-0000-0000-0000-000000000000",
            "X-Manyak-Chat-Id": "22222222-2222-2222-2222-222222222222",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert [s["id"] for s in body["stories"]] == [1, 2, 3]

    # 로깅 메타(KNK-243): story는 snake_case 와이어, 재호출이 없었으면 retry_count=0
    meta = body["meta"]
    assert meta["model"] == "deepseek-test"
    # 주입한 값이 그대로 응답까지 온다 — 상수로 되돌리면 여기서 깨진다(KNK-674 리뷰 H1).
    assert meta["provider"] == "not-deepseek"
    assert list(meta["prompt_versions"]) == ["STORYLINES"]
    assert meta["prompt_versions"]["STORYLINES"] >= 1
    assert meta["input_token_count"] == 50
    assert meta["output_token_count"] == 80
    assert meta["retry_count"] == 0
    assert "promptVersions" not in meta  # camelCase 아님(story는 snake)
    assert captured["name"] == "스토리라인 생성"
    assert captured["input_data"] == StorylinesRequest.model_validate(_REQUEST).model_dump(
        mode="json"
    )
    assert captured["metadata"] == {
        "creation_id": "11111111-1111-1111-1111-111111111111",
        "parent_creation_id": "00000000-0000-0000-0000-000000000000",
        "prompt_versions": {"STORYLINES": meta["prompt_versions"]["STORYLINES"]},
        "retry_count": 0,
    }


async def test_storylines_trace_omits_missing_parent_creation_id(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    @contextmanager
    def fake_observe(_name: str, **kwargs: object):
        captured.update(kwargs)

        class _Trace:
            def set_metadata(self, **_kwargs: object) -> None: ...

        yield _Trace()

    async def fake_complete(system: str, user: str, **_kwargs: object):
        return _FAKE, story_llm.LlmUsage("deepseek-test", 50, 80, provider="deepseek")

    monkeypatch.setattr(story_module, "observe_request", fake_observe)
    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    response = await client.post(
        "/api/v1/story/storylines",
        json=_REQUEST,
        headers={"X-Manyak-Creation-Id": "11111111-1111-1111-1111-111111111111"},
    )

    assert response.status_code == 200
    assert captured["metadata"]["creation_id"] == "11111111-1111-1111-1111-111111111111"
    assert "parent_creation_id" not in captured["metadata"]


async def test_storylines_endpoint_serializes_missing_tokens_as_null(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """공급자가 토큰을 안 줬을 때 응답 본문에 **null**로 나가는지 HTTP 계층까지 확인한다.

    백엔드 계약이 "누락 시 null"(0이 아니다)이다. `_complete_json`이 None을 들고 오는 것만
    확인하면 응답 조립·직렬화가 None을 거부하도록 망가져도 안 잡힌다(KNK-672 리뷰).
    """

    async def fake_complete(system: str, user: str, **_kwargs: object):
        return _FAKE, story_llm.LlmUsage("deepseek-test", None, None, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    response = await client.post("/api/v1/story/storylines", json=_REQUEST)

    assert response.status_code == 200
    meta = response.json()["meta"]
    assert meta["input_token_count"] is None  # 0으로 뭉개지 않는다
    assert meta["output_token_count"] is None


async def test_storylines_endpoint_tolerates_meta_key_in_llm_result(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM이 변덕으로 'meta' 키를 섞어 보내도 kwarg 충돌(500) 없이 정상 응답해야 한다."""

    async def fake_complete(system: str, user: str, **_kwargs: object):
        return {**_FAKE, "meta": "LLM이 섞어 보낸 잡음"}, story_llm.LlmUsage("m", 1, 2, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    response = await client.post("/api/v1/story/storylines", json=_REQUEST)

    assert response.status_code == 200
    # 서버가 만든 메타로 덮어써지고, LLM이 보낸 잡음 문자열은 채택되지 않는다.
    assert response.json()["meta"]["provider"] == "deepseek"


async def test_storylines_endpoint_reports_actual_retry_count(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """재호출이 있었으면 meta.retry_count가 하드코딩 0이 아니라 실제 횟수를 싣는다(KNK-312)."""

    async def fake_complete(system: str, user: str, **_kwargs: object):
        return _FAKE, story_llm.LlmUsage("deepseek-test", 100, 160, retry_count=1, provider="deepseek")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)

    response = await client.post("/api/v1/story/storylines", json=_REQUEST)

    assert response.status_code == 200
    meta = response.json()["meta"]
    assert meta["retry_count"] == 1
    # 합산 토큰이 그대로 실린다(실패 시도분 포함 값)
    assert meta["input_token_count"] == 100
    assert meta["output_token_count"] == 160


async def test_storylines_endpoint_passes_request_to_service(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[StorylinesRequest] = []
    expected = StorylinesResponse.model_validate(_FAKE)

    async def fake_generate(request: StorylinesRequest) -> StorylinesResponse:
        seen.append(request)
        return expected

    monkeypatch.setattr(story_llm, "generate_storylines", fake_generate)
    response = await client.post("/api/v1/story/storylines", json=_REQUEST)
    assert response.status_code == 200
    assert seen == [StorylinesRequest.model_validate(_REQUEST)]
    assert response.json() == expected.model_dump(mode="json")


async def test_storylines_service_and_http_share_prompt_and_response(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP 밖에서도 정규화된 입력으로 같은 프롬프트·완성 응답을 만든다."""
    payload = deepcopy(_REQUEST)
    payload["supporting_characters"][0]["name"] = "  서린  "
    request = StorylinesRequest.model_validate(payload)
    expected_prompts = build_storylines_prompt(
        request.genre_tags, request.protagonist, request.supporting_characters,
    )
    calls: list[tuple[str, str]] = []

    async def fake_complete(system: str, user: str, **_kwargs: object):
        calls.append((system, user))
        return deepcopy(_FAKE), story_llm.LlmUsage(
            "actual-model", 10, None, provider="actual-provider", retry_count=2,
        )

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    direct = await story_llm.generate_storylines(request)
    http = await client.post("/api/v1/story/storylines", json=payload)
    expected = {
        **_FAKE,
        "meta": {
            "model": "actual-model", "provider": "actual-provider",
            "prompt_versions": {"STORYLINES": STORYLINES_VERSION},
            "input_token_count": 10, "output_token_count": None, "retry_count": 2,
        },
    }
    assert http.status_code == 200
    assert direct.model_dump(mode="json") == http.json() == expected
    # 이름 미정 인물까지 필수 이름으로 취급하거나 빌더 입력을 바꾸면 깨진다.
    assert calls == [expected_prompts, expected_prompts]


@pytest.mark.parametrize("scenario", ["recovered", "exhausted", "timeout", "refilled"])
async def test_storylines_http_preserves_retries_and_failure_metadata(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, install_llm_sdk, scenario: str,
) -> None:
    """SDK만 대체하고 라우터·서비스·검증·오류 변환·직렬화를 함께 실행한다."""
    monkeypatch.setattr(story_llm.settings, "storylines_model", "deepseek-flash")
    captured: dict = {}
    errors: list[dict] = []
    calls: list[dict] = []
    monkeypatch.setattr(story_llm, "capture_ai_exception", lambda _exc, **kw: errors.append(kw))

    @contextmanager
    def observe(_name: str, **kwargs: object):
        captured.update(kwargs["metadata"])

        class Trace:
            def set_metadata(self, **metadata: object) -> None:
                captured.update(metadata)

        yield Trace()

    monkeypatch.setattr(story_module, "observe_request", observe)
    valid = json.dumps(_FAKE)
    missing = deepcopy(_FAKE)
    missing["stories"][1]["storyline"] = "인물이 빠진 이야기"
    refill = json.dumps({"stories": [_FAKE["stories"][1]]})
    sequences = {
        "recovered": [json.dumps({"stories": []}), valid],
        "exhausted": [json.dumps({"stories": []})] * 3,
        "refilled": ["{broken", json.dumps(missing), refill],
        "timeout": [],
    }

    async def create(**kwargs: object):
        index = len(calls)
        calls.append(kwargs)
        if scenario == "timeout":
            raise APITimeoutError(request=Request("POST", "https://example.test"))
        return SimpleNamespace(
            model="actual-deepseek-model",
            choices=[SimpleNamespace(message=SimpleNamespace(content=sequences[scenario][index]), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
        )

    install_llm_sdk(create)
    response = await client.post("/api/v1/story/storylines", json=_REQUEST)
    expected_calls = {"recovered": 2, "exhausted": 3, "timeout": 1, "refilled": 3}[scenario]
    assert len(calls) == expected_calls
    assert captured["retry_count"] == expected_calls - 1
    assert captured["prompt_versions"] == {"STORYLINES": STORYLINES_VERSION}
    assert all(0 < call["timeout"] <= 90 for call in calls)
    if scenario in {"recovered", "refilled"}:
        assert response.status_code == 200
        assert response.json() == {
            **_FAKE,
            "meta": {
                "model": "actual-deepseek-model", "provider": "deepseek",
                "prompt_versions": {"STORYLINES": STORYLINES_VERSION},
                "input_token_count": 10 * expected_calls,
                "output_token_count": 20 * expected_calls,
                "retry_count": expected_calls - 1,
            },
        }
    else:
        assert response.status_code == 502
        detail = ("LLM 응답 시간이 초과되었습니다." if scenario == "timeout"
                  else "LLM이 올바른 형식의 응답을 반환하지 않았습니다.")
        assert response.json() == {"detail": detail}
    assert len(errors) == (3 if scenario == "exhausted" else 1)
