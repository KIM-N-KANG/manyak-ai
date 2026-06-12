import json

# from google import genai
# from google.genai import types
from openai import AsyncOpenAI

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
    response = await _client.chat.completions.create(
        model=settings.upstage_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)
