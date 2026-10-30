"""compact 입력 계약과 원문 보존을 검증한다. 실제 모델은 호출하지 않는다."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.schemas.chat_compact import ChatCompactRequest, ChatCompactResponse
from src.services import chat_compact_prompt
from src.services.prompt_meta import read_version


@pytest.mark.parametrize("payload", [
    {}, {"summary": ""}, {"history": []},
    {"summary": None, "history": []},
    {"summary": 1, "history": []},
    {"summary": "", "history": None},
    {"summary": "", "history": "invalid"},
    {"summary": "", "history": [{"role": "SYSTEM", "content": "text"}]},
    {"summary": "", "history": [{"role": "USER", "content": None}]},
    {"summary": "", "history": [{"role": "USER"}]},
])
def test_invalid_request(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ChatCompactRequest.model_validate(payload)


@pytest.mark.parametrize("summary", ["", "기존 사건 [[https://example.invalid/old.webp]]"])
def test_build_messages_preserves_complete_history_and_original(summary: str) -> None:
    request = ChatCompactRequest(summary=summary, history=[
        {"role": "ASSISTANT", "content": "이전 장면"},
        {"role": "USER", "content": '현재 입력 {compact_json} {{summary}} "인용"\n다음 줄'},
        {"role": "ASSISTANT", "content": "[[https://example.invalid/new.webp]]\n\n현재 응답"},
    ])
    before = request.model_dump()
    messages = chat_compact_prompt.build_messages(request)
    assert [message["role"] for message in messages] == ["system", "user"]
    user = messages[1]["content"]
    assert isinstance(user, str)
    payload = json.loads(user[user.index("{"):])
    assert payload["summary"] == ("기존 사건 " if summary else "")
    assert payload["history"] == [
        {"role": "ASSISTANT", "content": "이전 장면"},
        before["history"][1],
        {"role": "ASSISTANT", "content": "현재 응답"},
    ]
    assert request.model_dump() == before
    assert "https://example.invalid" not in user
    assert "version:" not in messages[0]["content"]
    assert chat_compact_prompt.COMPACT_VERSION == read_version(chat_compact_prompt._PATH)


def test_empty_history_and_response_contract() -> None:
    request = ChatCompactRequest(summary="기존 요약", history=[])
    assert len(chat_compact_prompt.build_messages(request)) == 2
    # 응답도 최대 글자 수를 넘기지 못한다.
    for summary in ("짧은 요약", "가" * 3000):
        assert ChatCompactResponse(summary=summary).model_dump() == {"summary": summary}
    with pytest.raises(ValidationError):
        ChatCompactResponse(summary="가" * 3001)


@pytest.mark.parametrize("template", [
    "", "## [SYSTEM]\n지시", "## [USER]\n{compact_json}",
    "## [SYSTEM]\n\n## [USER]\n{compact_json}",
    "## [SYSTEM]\n지시\n## [USER]\n입력 없음",
    "## [SYSTEM]\n지시\n## [USER]\n{compact_json}{compact_json}",
])
def test_invalid_template_fails_explicitly(tmp_path: Path, template: str) -> None:
    path = tmp_path / "template.md"
    path.write_text(template, encoding="utf-8")
    with pytest.raises(RuntimeError, match="요약 템플릿"):
        chat_compact_prompt._load_template(path)
