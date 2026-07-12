"""요청 바디 검증(422) 테스트 (KNK-574 감사 1-4).

모든 API 테스트가 올바른 요청만 보내, 필수 필드 누락·Literal 위반 같은 계약 완화
회귀(스키마에 기본값이 붙어 필수가 선택이 되는 등)를 잡을 장치가 없었다. FastAPI가
Pydantic 검증 실패를 422로 돌려주는 계약을 엔드포인트별로 고정한다. 검증은 핸들러
진입 전 단계라 LLM은 호출되지 않는다(모킹 불필요·무과금).
"""


async def test_compile_missing_required_field_422(client) -> None:
    # genre_tags(필수) 누락 → 422.
    payload = {
        "selected_storyline": "x",
        "protagonist_tags": ["신중한"],
        "supporting_tags": ["거친"],
    }
    resp = await client.post("/api/v1/story/compile", json=payload)
    assert resp.status_code == 422


async def test_storylines_missing_required_field_422(client) -> None:
    # genre_tags(필수) 누락 → 422.
    payload = {"protagonist_tags": ["신중한"], "supporting_tags": ["거친"]}
    resp = await client.post("/api/v1/story/storylines", json=payload)
    assert resp.status_code == 422


def _chat_payload() -> dict:
    return {
        "genre": "판타지",
        "story_settings": {
            "world_setting": "w",
            "character_setting": "c",
            "user_role_setting": "u",
            "rule_setting": "r",
        },
        "start_settings": {"name": "n", "prologue": "p", "start_situation": "s"},
        "history": [],
        "user_input": "입력",
        "summary": "",
    }


async def test_chat_missing_required_field_422(client) -> None:
    # user_input(필수) 누락 → 422.
    payload = _chat_payload()
    del payload["user_input"]
    resp = await client.post("/api/v1/chat/turns", json=payload)
    assert resp.status_code == 422


async def test_chat_invalid_history_role_422(client) -> None:
    # history role은 Literal["USER","ASSISTANT"] — "SYSTEM"은 계약 위반 → 422.
    payload = _chat_payload()
    payload["history"] = [{"role": "SYSTEM", "content": "x"}]
    resp = await client.post("/api/v1/chat/turns", json=payload)
    assert resp.status_code == 422
