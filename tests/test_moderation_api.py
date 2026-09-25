"""KNK-1369: HTTP 입력 검증부터 검수 판정까지 실제 공급자 호출 없이 검증한다."""

import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
import httpx
from httpx import ASGITransport, AsyncClient

from src.core.config import settings
from src.main import app
from src.services.llm.base import LlmResult, LlmUnavailable, TokenUsage
from src.services.moderation import images, service
from src.services.moderation.images import ImageDownloadFailed, ImageInvalid, ModerationImage, PreparedImages

URL = "/api/v1/moderation/story"
POST = {
    "storyId": "story-test-id", "title": "제목", "oneLineIntro": "한 줄 소개", "genres": ["판타지"],
    "storySettings": {
        "worldSetting": "세계", "characterSetting": "인물", "userRoleSetting": "역할", "ruleSetting": "규칙",
    },
    "startSettings": [{
        "name": "시작", "prologue": "프롤로그", "startSituation": "상황", "suggestedInputs": ["가", "나", "다"],
        "endings": [{"name": "엔딩", "requirement": {"achievementCondition": "조건"}, "epilogue": "결말"}],
    }],
    "mainEvents": [{"name": "사건", "description": "사건 설명", "keySentence": "핵심 문장"}],
    "thumbnailUrl": "https://cdn.example.com/cover.png",
    "characters": [{"name": "인물", "images": [{"imageName": "인물_평상시", "imageUrl": "https://cdn.example.com/a.png"}]}],
}


def output(decision="APPROVED", issues=None):
    return LlmResult(
        text=json.dumps({"decision": decision, "issues": issues or []}),
        model="test", provider="openai", usage=TokenUsage(), finish_reason="stop",
    )


@pytest.fixture
def dependencies(monkeypatch):
    async def fetch(sources, **kwargs):
        return PreparedImages(images=[ModerationImage(s.path, "image/png", b"image") for s in sources], errors=[])

    fetch_mock = AsyncMock(side_effect=fetch)
    complete = AsyncMock(return_value=output())
    monkeypatch.setattr(service, "fetch_images", fetch_mock)
    monkeypatch.setattr(service.llm, "complete", complete)
    return fetch_mock, complete


async def test_approval_and_camel_case_input_reach_service(client, dependencies):
    post = deepcopy(POST)
    post["visibility"] = "PRIVATE"
    post["startSettings"][0]["endings"][0]["requirement"]["minTurns"] = 5
    response = await client.post(URL, json=post)
    assert response.status_code == 200
    assert response.json() == {"decision": "APPROVED", "issues": [], "error_code": None, "image_errors": []}
    assert list(response.json()) == ["decision", "issues", "error_code", "image_errors"]
    fetch, complete = dependencies
    assert [s.path for s in fetch.call_args.args[0]] == ["thumbnailUrl", "characters[0].images[0].imageUrl"]
    messages = complete.call_args.args[0].messages
    text = messages[1]["content"][0]["text"]
    assert '"worldSetting": "세계"' in text
    assert "story-test-id" not in text and "visibility" not in text and "minTurns" not in text
    complete.assert_awaited_once()


async def test_optional_fields_can_be_omitted_and_nullable_text_is_accepted(client, dependencies):
    post = deepcopy(POST)
    for key in ("mainEvents", "characters", "thumbnailUrl"):
        post.pop(key)
    post["startSettings"][0].pop("endings")
    post["description"] = None
    response = await client.post(URL, json=post)
    assert response.status_code == 200
    assert response.json()["decision"] == "APPROVED"
    assert dependencies[0].call_args.args[0] == []


async def test_post_content_is_not_trimmed_or_truncated(client, dependencies):
    post = deepcopy(POST)
    post["title"] = "가" * 100
    post["oneLineIntro"] = "나" * 255
    post["genres"] = [f"{i:02d}" + "가" * 28 for i in range(20)]
    post["description"] = "  설명  "
    post["thumbnailUrl"] = None
    post["mainEvents"] = [dict(POST["mainEvents"][0], name=f"사건{i}") for i in range(10)]
    post["startSettings"][0]["endings"] = [
        dict(POST["startSettings"][0]["endings"][0], name=f"엔딩{i}") for i in range(10)
    ]
    post["characters"] = [{
        "name": f"인물{i}", "images": [
            {"imageName": f"인물{i}_{j}", "imageUrl": f"https://cdn.example.com/{i}-{j}.png"}
            for j in range(10)
        ],
    } for i in range(6)]
    response = await client.post(URL, json=post)
    assert response.status_code == 200
    assert len(dependencies[0].call_args.args[0]) == 60
    text = dependencies[1].call_args.args[0].messages[1]["content"][0]["text"]
    assert '"description": "  설명  "' in text


async def test_null_thumbnail_and_empty_character_images_allow_text_only_moderation(client, dependencies):
    post = deepcopy(POST)
    post["thumbnailUrl"] = None
    post["characters"][0]["images"] = []
    response = await client.post(URL, json=post)
    assert response.status_code == 200
    assert response.json()["decision"] == "APPROVED"
    assert dependencies[0].call_args.args[0] == []
    dependencies[1].assert_awaited_once()


@pytest.mark.parametrize("path", [("thumbnailUrl",), ("characters", 0, "images", 0, "imageUrl")])
@pytest.mark.parametrize("value", ["", " \t\n"])
async def test_blank_image_urls_are_ignored(client, dependencies, path, value):
    post = deepcopy(POST)
    target = post
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    response = await client.post(URL, json=post)
    assert response.status_code == 200
    assert response.json()["decision"] == "APPROVED"
    fetch, complete = dependencies
    assert len(fetch.call_args.args[0]) == 1
    assert all(source.url.strip() for source in fetch.call_args.args[0])
    complete.assert_awaited_once()


@pytest.mark.parametrize("count", [0, 9, 20])
async def test_simple_story_genres_reach_moderation(client, dependencies, count):
    post = deepcopy(POST)
    post["genres"] = [f"장르{i}" for i in range(count)]
    response = await client.post(URL, json=post)
    assert response.status_code == 200
    assert response.json()["decision"] == "APPROVED"
    dependencies[1].assert_awaited_once()
    text = dependencies[1].call_args.args[0].messages[1]["content"][0]["text"]
    assert json.dumps(post["genres"], ensure_ascii=False) in text


@pytest.mark.parametrize("empty", ["", " \t\n", None])
async def test_empty_content_fields_do_not_block_moderation(client, dependencies, empty):
    def empty_strings(value):
        if isinstance(value, dict):
            return {key: empty_strings(child) for key, child in value.items()}
        if isinstance(value, list):
            # 목록 자체도 비어 있을 수 있으며, 문자열 목록의 원소 타입은 유지한다.
            return [] if all(isinstance(child, str) for child in value) else [empty_strings(child) for child in value]
        return empty

    post = empty_strings(POST)
    post["storyId"] = POST["storyId"]
    response = await client.post(URL, json=post)
    assert response.status_code == 200
    assert response.json()["decision"] == "APPROVED"
    assert dependencies[0].call_args.args[0] == []
    dependencies[1].assert_awaited_once()


@pytest.mark.parametrize("content", [
    {},
    {"storySettings": {}, "startSettings": [{}], "characters": [{"images": [{}]}]},
    {"storySettings": None, "startSettings": None, "genres": None, "characters": None},
    {"startSettings": [], "genres": [], "characters": []},
])
async def test_missing_or_empty_post_sections_do_not_block_moderation(client, dependencies, content):
    response = await client.post(URL, json={"storyId": "story-test-id", **content})
    assert response.status_code == 200
    assert response.json()["decision"] == "APPROVED"
    assert dependencies[0].call_args.args[0] == []
    dependencies[1].assert_awaited_once()


async def test_backend_length_and_count_rules_are_not_revalidated(client, dependencies):
    post = deepcopy(POST)
    post["title"] = "가" * 101
    post["oneLineIntro"] = "나" * 256
    post["genres"] = ["장" * 31] * 21
    post["startSettings"][0]["suggestedInputs"] = [""]
    post["startSettings"][0]["endings"] *= 11
    post["mainEvents"] *= 11
    post["characters"][0]["images"][0]["imageName"] = "이" * 121
    post["characters"][0]["images"] *= 11
    post["characters"] *= 7
    response = await client.post(URL, json=post)
    assert response.status_code == 200
    assert response.json()["decision"] == "APPROVED"
    assert len(dependencies[0].call_args.args[0]) == 78
    text = dependencies[1].call_args.args[0].messages[1]["content"][0]["text"]
    assert post["title"] in text and post["oneLineIntro"] in text
    assert json.dumps(post["genres"], ensure_ascii=False) in text


async def test_content_rejection_is_200_without_fallback(client, dependencies):
    dependencies[1].return_value = output("REJECTED", [{
        "path": "characters[0].images[0].imageName", "rule": "DRUGS", "reason": "마약 사용을 권장합니다.",
    }])
    response = await client.post(URL, json=POST)
    assert response.status_code == 200
    assert response.json()["decision"] == "REJECTED"
    issue = response.json()["issues"][0]
    assert issue["type"] == "TEXT"
    assert set(issue) == {"path", "type", "rule", "reason"}
    assert response.json()["error_code"] is None
    dependencies[1].assert_awaited_once()


async def test_provider_failure_falls_back_and_both_failures_are_200(client, dependencies):
    failure = LlmUnavailable("unavailable", provider="openai", model="test")
    dependencies[1].side_effect = [failure, output()]
    response = await client.post(URL, json=POST)
    assert response.status_code == 200
    assert response.json()["decision"] == "APPROVED"
    assert dependencies[1].await_count == 2
    dependencies[1].reset_mock(side_effect=True)
    dependencies[1].side_effect = failure
    response = await client.post(URL, json=POST)
    assert response.status_code == 200
    assert response.json() == {"decision": "REJECTED", "issues": [], "error_code": "MODEL_CALL_FAILED", "image_errors": []}
    assert dependencies[1].await_count == 2


@pytest.mark.parametrize("failure", [ImageDownloadFailed("thumbnailUrl", "failed"), ImageInvalid("thumbnailUrl", "invalid")])
async def test_image_preparation_errors_are_200_without_model_call(client, dependencies, failure):
    dependencies[0].side_effect = None
    dependencies[0].return_value = PreparedImages(images=[], errors=[failure])
    response = await client.post(URL, json=POST)
    assert response.status_code == 200
    assert response.json() == {"decision": "REJECTED", "issues": [], "error_code": failure.code, "image_errors": [{"path": "thumbnailUrl", "error_code": failure.code}]}
    dependencies[1].assert_not_awaited()


async def test_unreadable_image_and_oversized_request_are_200(client, dependencies, monkeypatch):
    dependencies[1].return_value = output("REJECTED", [{"path": "thumbnailUrl", "rule": None, "reason": "판독 불가"}])
    response = await client.post(URL, json=POST)
    assert response.status_code == 200
    assert response.json()["error_code"] == "IMAGE_UNREADABLE"
    assert response.json()["issues"] == []
    from src.services.moderation import limits
    monkeypatch.setattr(limits, "MAX_REQUEST_BYTES", 1)
    dependencies[1].reset_mock()
    response = await client.post(URL, json=POST)
    assert response.status_code == 200
    assert response.json()["error_code"] == "IMAGE_INVALID"
    dependencies[1].assert_not_awaited()


async def test_file_download_and_model_image_errors_are_combined(client, dependencies, monkeypatch):
    post = deepcopy(POST)
    post["characters"].append({"name": "다른 인물", "images": [
        {"imageName": "다른 인물_기본", "imageUrl": "https://cdn.example.com/readable.png"},
    ]})
    seen = []
    reports = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/cover.png":
            return httpx.Response(200, content=b"broken file")
        if request.url.path == "/a.png":
            return httpx.Response(404)
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nimage")

    client_type = httpx.AsyncClient
    monkeypatch.setattr(settings, "image_parent_allowed_hosts", ["cdn.example.com"])
    monkeypatch.setattr(images.httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(service, "fetch_images", images.fetch_images)
    monkeypatch.setattr(service, "capture_ai_exception", lambda exc, **tags: reports.append(tags))
    dependencies[1].return_value = output("REJECTED", [{
        "path": "characters[1].images[0].imageUrl", "rule": None, "reason": "판독 불가",
    }])

    response = await client.post(URL, json=post)

    assert response.status_code == 200
    assert response.json() == {
        "decision": "REJECTED", "issues": [], "error_code": "IMAGE_INVALID",
        "image_errors": [
            {"path": "thumbnailUrl", "error_code": "IMAGE_INVALID"},
            {"path": "characters[0].images[0].imageUrl", "error_code": "IMAGE_DOWNLOAD_FAILED"},
            {"path": "characters[1].images[0].imageUrl", "error_code": "IMAGE_UNREADABLE"},
        ],
    }
    assert sorted(seen) == ["/a.png", "/cover.png", "/readable.png"]
    dependencies[1].assert_awaited_once()
    parts = dependencies[1].call_args.args[0].messages[1]["content"]
    assert [part["type"] for part in parts] == ["text", "text", "image_url"]
    assert parts[1]["text"] == "첨부 이미지 경로: characters[1].images[0].imageUrl"
    assert "/cover.png" not in parts[0]["text"] and "/a.png" not in parts[0]["text"]
    assert "인물_평상시" in parts[0]["text"]
    # 준비 실패는 요청당 한 건(대표 코드), 판독 실패는 모델 호출당 한 건.
    assert [report["error_code"] for report in reports] == ["IMAGE_INVALID", "IMAGE_UNREADABLE"]


@pytest.mark.parametrize("path,value", [
    (("storyId",), 1), (("title",), 123), (("description",), {}),
    (("genres",), [True]), (("storySettings",), []),
    (("storySettings", "worldSetting"), 123),
    (("startSettings", 0, "prologue"), 123),
    (("startSettings", 0, "endings", 0, "requirement"), []),
    (("characters", 0, "images", 0, "imageUrl"), {}), (("thumbnailUrl",), 123),
])
async def test_invalid_nested_input_is_422_before_any_work(client, dependencies, path, value):
    post = deepcopy(POST)
    target = post
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    response = await client.post(URL, json=post)
    assert response.status_code == 422
    assert any(error["loc"][:len(path) + 1] == ["body", *path] for error in response.json()["detail"])
    assert all(set(error) <= {"type", "loc", "msg"} for error in response.json()["detail"])
    for dependency in dependencies:
        dependency.assert_not_awaited()


@pytest.mark.parametrize("key", ["storyId"])
async def test_missing_required_fields_are_422_without_echoing_post(client, dependencies, key):
    post = deepcopy(POST)
    post.pop(key)
    response = await client.post(URL, json=post)
    assert response.status_code == 422
    assert "cdn.example.com" not in response.text
    for dependency in dependencies:
        dependency.assert_not_awaited()


async def test_invalid_json_and_server_bug_are_not_moderation_decisions(client, dependencies):
    response = await client.post(URL, content="{", headers={"content-type": "application/json"})
    assert response.status_code == 422
    dependencies[0].assert_not_awaited()
    dependencies[1].side_effect = TypeError("programming bug")
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as safe_client:
        response = await safe_client.post(URL, json=POST)
    assert response.status_code == 500


def test_openapi_declares_request_and_response():
    operation = app.openapi()["paths"][URL]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/StoryModerationRequest")
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/StoryModerationResponse")


@pytest.mark.parametrize("preparation_failure", [False, True])
@pytest.mark.parametrize("fallback", [False, True])
async def test_image_errors_preserve_all_content_violations(client, dependencies, preparation_failure, fallback):
    if preparation_failure:
        dependencies[0].side_effect = None
        dependencies[0].return_value = PreparedImages(
            images=[ModerationImage("characters[0].images[0].imageUrl", "image/png", b"image")],
            errors=[ImageInvalid("thumbnailUrl", "broken")],
        )
    text_paths = ["title", "oneLineIntro", "storySettings.worldSetting"]
    content_issues = [{"path": path, "rule": "DRUGS", "reason": "마약 사용을 권장합니다."} for path in text_paths]
    content_issues.append({
        "path": "characters[0].images[0].imageName", "rule": "DRUGS", "reason": "마약 사용을 권장합니다.",
    })
    model_result = output("REJECTED", [
        {"path": "characters[0].images[0].imageUrl", "rule": None, "reason": "판독 불가"},
        *content_issues,
    ])
    dependencies[1].side_effect = (
        [LlmUnavailable("failed", provider="openai", model="test"), model_result] if fallback else [model_result]
    )
    response = await client.post(URL, json=POST)
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "REJECTED"
    assert body["issues"] == [{**item, "type": "TEXT"} for item in content_issues]
    expected_errors = ([{"path": "thumbnailUrl", "error_code": "IMAGE_INVALID"}] if preparation_failure else [])
    expected_errors.append({"path": "characters[0].images[0].imageUrl", "error_code": "IMAGE_UNREADABLE"})
    assert body["image_errors"] == expected_errors
    assert body["error_code"] == ("IMAGE_INVALID" if preparation_failure else "IMAGE_UNREADABLE")
    assert dependencies[1].await_count == (2 if fallback else 1)
