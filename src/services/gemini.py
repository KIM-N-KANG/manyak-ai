import json

# from google import genai
# from google.genai import types
from fastapi import HTTPException, status
from openai import AsyncOpenAI, OpenAIError

from src.core.config import settings

# _client = genai.Client(api_key=settings.gemini_api_key)
_client = AsyncOpenAI(
    api_key=settings.upstage_api_key,
    base_url="https://api.upstage.ai/v1",
)


async def generate_storylines(system_prompt: str, user_prompt: str) -> dict:
    # response = await _client.aio.models.generate_content(
    #     model=settings.gemini_model,
    #     contents=user_prompt,
    #     config=types.GenerateContentConfig(
    #         system_instruction=system_prompt,
    #         response_mime_type="application/json",
    #     ),
    # )
    # return json.loads(response.text)
    try:
        response = await _client.chat.completions.create(
            model=settings.upstage_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        if not content:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="LLM이 빈 응답을 반환했습니다.",
            )
        return json.loads(content)
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
