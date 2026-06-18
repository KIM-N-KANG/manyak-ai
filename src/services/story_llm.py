import json

# from google import genai
# from google.genai import types
from fastapi import HTTPException, status
from openai import AsyncOpenAI, OpenAIError

from src.core.config import settings
from src.schemas.story_compile import StoryCompileRequest, StorySpec
from src.services.prompt import build_compile_prompt

# _client = genai.Client(api_key=settings.gemini_api_key)
_client = AsyncOpenAI(
    api_key=settings.upstage_api_key,
    base_url="https://api.upstage.ai/v1",
    timeout=90.0,  # 무한 대기 방지 — 정상 컴파일은 ~10초, 초과 시 APITimeoutError → 502
)

# 출력이 무한정 길어지지 않도록 상한. 컴파일 명세 JSON(인물 최대 3명)도 충분히 담긴다.
_MAX_TOKENS = 4096


def _strip_code_fence(text: str) -> str:
    """LLM이 JSON을 ```json ... ``` 코드 펜스로 감싼 경우 제거한다."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # 첫 줄(``` 또는 ```json)과 마지막 ``` 라인을 제거
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


async def _complete_json(system_prompt: str, user_prompt: str) -> dict:
    """LLM을 호출해 JSON 응답을 dict로 파싱한다. 호출·빈응답·파싱 오류를 502로 변환한다."""
    try:
        response = await _client.chat.completions.create(
            model=settings.upstage_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=_MAX_TOKENS,
        )
        content = response.choices[0].message.content
        if not content:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="LLM이 빈 응답을 반환했습니다.",
            )
        parsed = json.loads(_strip_code_fence(content))
        if not isinstance(parsed, dict):
            # json_object를 무시하고 배열·스칼라를 반환한 경우. dict 가정이 깨지면
            # 호출부(compile_story 등)에서 AttributeError로 500이 나므로 여기서 502로 막는다.
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="LLM이 JSON 객체를 반환하지 않았습니다.",
            )
        return parsed
    except OpenAIError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM 플랫폼 연동 중 오류가 발생했습니다: {str(e)}",
        )
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="LLM이 올바른 JSON 형식을 반환하지 않았습니다.",
        )


async def generate_storylines(system_prompt: str, user_prompt: str) -> dict:
    return await _complete_json(system_prompt, user_prompt)


async def compile_story(request: StoryCompileRequest) -> StorySpec:
    """시점 A-1: 희소 입력을 스토리 명세(StorySpec)로 컴파일한다.

    PromptCompiler 추상 경계(4-SERVICE-IMPLEMENTATION.md 6절). HTTP 노출은 KNK-135.
    """
    system_prompt, user_prompt = build_compile_prompt(
        request.selected_storyline,
        request.extra_info,
        request.genre_tags,
        request.protagonist_tags,
        request.supporting_tags,
    )
    data = await _complete_json(system_prompt, user_prompt)

    # genre는 예외 경로 — LLM 출력이 아니라 입력 태그를 정본으로 삼아 덮어쓴다(3.3·3.4).
    if isinstance(data.get("meta"), dict):
        data["meta"]["genre"] = ", ".join(request.genre_tags)

    try:
        return StorySpec(**data)
    except (TypeError, ValueError) as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"컴파일 결과가 스토리 명세 형식과 맞지 않습니다: {e}",
        )
