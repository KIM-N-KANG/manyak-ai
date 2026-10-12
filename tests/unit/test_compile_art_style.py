"""컴파일 인물 이미지 화풍 선택 테스트(KNK-1484).

요청 계약(art_style 기본값·null·허용값), 화풍별 프롬프트 조립, "없음"(NONE)일 때 인물 이미지
호출 생략과 표지 유지, 템플릿 화풍 블록 누락 감지를 검증한다.
실제 그림체 품질은 유닛으로 검증되지 않는다 — 라이브 실측은 KNK-1524.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.schemas.story_compile import (
    ART_STYLE_ANIME,
    ART_STYLE_NONE,
    ART_STYLE_LIGHT_NOVEL,
    ART_STYLE_SEMI_REAL,
    ART_STYLE_CINEMATIC,
    ART_STYLE_OIL_PAINTING,
    ART_STYLE_RETRO,
    ART_STYLE_INK_WASH,
    ART_STYLE_MOE,
    ART_STYLE_SHOJO,
    ART_STYLE_WEBTOON,
    IMAGE_ART_STYLES,
    CharacterSetting,
    StoryCompileRequest,
    ThumbnailImageOut,
)
from src.services import story_llm
# conftest의 autouse 픽스처가 story_llm의 두 함수를 가짜로 바꾸므로, 실제 함수는 임포트 시점에 잡아 둔다.
from src.services.story_llm import _generate_character_images_safe, _generate_thumbnail_image_safe
from src.services.image import generate_characters as gen_mod
from src.services.image import generate_thumbnail as thumb_mod
from src.services.image.base import ImageResult
from src.services.image.prompt import _load_character_template, build_image_prompt

_FIXTURES = Path(__file__).parent / "fixtures"
_FAKE_WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 50
_GENRE = ["로맨스 판타지"]


def _char(**overrides) -> CharacterSetting:
    defaults = {
        "name": "레이",
        "gender": "남성",
        "personality": "충직한 원칙주의자.",
        "tone": "직설적인 말투.",
        "motivation": "진실 규명.",
        "attitude_to_user": "신뢰하는 전우.",
        "description": "원칙을 지키며 주인공과 함께 진실을 좇는 전우.",
        "age": "20대 후반",
        "body": "건장한",
        "face": "각진 턱선, 굳은 표정",
        "hair": "짧은 단발",
        "outfit": "은색 판금 흉갑",
        "visual_identity": "왼쪽 관자놀이의 칼자국",
    }
    defaults.update(overrides)
    return CharacterSetting(**defaults)


def _request(**extra) -> StoryCompileRequest:
    return StoryCompileRequest(
        selected_storyline="x",
        genre_tags=_GENRE,
        protagonist={"name": "카일", "gender": "MALE", "features": []},
        supporting_characters=[{"name": "레이", "gender": "MALE", "features": []}, {}, {}],
        **extra,
    )


# ── 요청 계약 ─────────────────────────────────────────────────────────────────


def test_art_style_defaults_to_anime_for_old_requests() -> None:
    """필드를 안 보내는 기존 컴파일 요청은 종전과 같은 ANIME이다(하위호환)."""
    assert _request().art_style == ART_STYLE_ANIME


def test_art_style_explicit_null_is_default() -> None:
    """백엔드가 "선택 안 함"을 null로 보내도 기본 화풍으로 받는다(lorebooks와 같은 관례)."""
    assert _request(art_style=None).art_style == ART_STYLE_ANIME


@pytest.mark.parametrize("style", [ART_STYLE_ANIME, ART_STYLE_SEMI_REAL, ART_STYLE_LIGHT_NOVEL, ART_STYLE_CINEMATIC,
    ART_STYLE_OIL_PAINTING, ART_STYLE_RETRO, ART_STYLE_INK_WASH, ART_STYLE_MOE, ART_STYLE_SHOJO, ART_STYLE_WEBTOON,
    ART_STYLE_NONE,
])
def test_art_style_accepts_contract_values(style: str) -> None:
    assert _request(art_style=style).art_style == style


@pytest.mark.parametrize("style", ["anime", "ZETA", "", "REALISTIC", 1])
def test_art_style_rejects_values_outside_contract(style: object) -> None:
    """계약 밖 값은 조용히 기본값으로 바꾸지 않고 422로 드러낸다(백엔드 오타 노출)."""
    with pytest.raises(ValidationError):
        _request(art_style=style)


# ── 화풍별 프롬프트 조립 ──────────────────────────────────────────────────────


def test_prompt_anime_uses_anime_block_only() -> None:
    prompt = build_image_prompt(_char(), _GENRE, ART_STYLE_ANIME)
    assert "화풍: 깔끔한 현대 일본 애니메이션 게임 일러스트" in prompt
    assert "반실사" not in prompt


def test_prompt_semi_real_uses_semi_real_block_only() -> None:
    prompt = build_image_prompt(_char(), _GENRE, ART_STYLE_SEMI_REAL)
    assert "화풍: 한국 웹툰·로맨스 일러스트 계열의 반실사 스타일" in prompt
    assert "셀 셰이딩을 기본으로" not in prompt
    # 화풍이 피부색을 정하면 인물 설정(구릿빛 피부·주근깨)을 덮는다(KNK-1524 실측). 색은 설정을 따른다.
    assert "피부색과 주근깨·점·흉터 같은 피부 특징은 인물 설정을 그대로 따르고" in prompt
    assert "밝은 톤" not in prompt


def test_prompt_light_novel_uses_light_novel_block_only() -> None:
    prompt = build_image_prompt(_char(), _GENRE, ART_STYLE_LIGHT_NOVEL)
    assert "화풍: 요즘 일본 라이트노벨 삽화와 픽시브 일러스트 스타일" in prompt
    assert "셀 셰이딩을 기본으로" not in prompt and "반실사" not in prompt
    # 화풍 문단도 피부색을 정하지 않고 인물 설정을 따른다(KNK-1524 실측).
    assert "피부색과 주근깨·점·흉터 같은 피부 특징은 인물 설정을 그대로 따르고" in prompt


def test_prompt_cinematic_uses_cinematic_block_only() -> None:
    prompt = build_image_prompt(_char(), _GENRE, ART_STYLE_CINEMATIC)
    assert "화풍: 한국 웹소설 플랫폼 표지 일러스트 스타일의 디지털 페인팅" in prompt
    assert "셀 셰이딩을 기본으로" not in prompt and "라이트노벨" not in prompt
    assert "피부색과 주근깨·점·흉터 같은 피부 특징은 인물 설정을 그대로 따르고" in prompt


@pytest.mark.parametrize(
    ("style", "marker"),
    [(ART_STYLE_OIL_PAINTING, "화풍: 고전 유화 초상화 스타일"), (ART_STYLE_RETRO, "화풍: 1990년대 일본 TV 애니메이션 셀화 스타일")],
)
def test_prompt_uses_requested_style_block(style, marker) -> None:
    prompt = build_image_prompt(_char(), _GENRE, style)
    assert marker in prompt
    assert "피부색과 주근깨·점·흉터 같은 피부 특징은 인물 설정을 그대로 따른다" in prompt


@pytest.mark.parametrize(
    ("style", "marker"),
    [
        (ART_STYLE_INK_WASH, "화풍: 전통 동양 수묵화 스타일"),
        (ART_STYLE_MOE, "화풍: 파스텔 데코 모에 일러스트 스타일"),
        (ART_STYLE_SHOJO, "화풍: 2000년대 일본 순정만화의 컬러 원고처럼"),
        (ART_STYLE_WEBTOON, "화풍: 한국 로맨스 웹툰 그림체"),
    ],
)
def test_prompt_uses_added_style_block(style, marker) -> None:
    prompt = build_image_prompt(_char(), _GENRE, style)
    assert marker in prompt
    assert prompt.count("화풍: ") == 1


def test_moe_keeps_age_from_settings() -> None:
    """모에가 얼굴을 귀엽게 그려도 체격·비율은 설정 나이를 따라야 성인 인물이 미성년자로 보이지 않는다."""
    prompt = build_image_prompt(_char(), _GENRE, ART_STYLE_MOE)
    assert "체격·신체 비율·얼굴 골격은 인물 설정의 나이를 그대로 따르고" in prompt
    assert "성인은 키와 체형에서 분명히 성인으로 읽히게" in prompt


def test_moe_does_not_fix_pink_palette() -> None:
    """모에가 분홍·라벤더 팔레트를 고정하면 배경·머리색까지 분홍이 된다(KNK-1524 실측). 색은 설정·장소를 따른다."""
    prompt = build_image_prompt(_char(), _GENRE, ART_STYLE_MOE)
    assert "분홍·라벤더" not in prompt and "분홍·보라" not in prompt
    assert "인물 설정의 색은 색상은 지키되" in prompt


@pytest.mark.parametrize(
    ("style", "female_only"),
    [
        (ART_STYLE_SEMI_REAL, "입술은 도톰하고"),
        (ART_STYLE_LIGHT_NOVEL, "속눈썹을 길고 진하게"),
        (ART_STYLE_RETRO, "턱이 뾰족하고 갸름하며"),
        (ART_STYLE_SHOJO, "아래 속눈썹을 짧은 선 몇 개로"),
        (ART_STYLE_WEBTOON, "입술은 연한 분홍 면으로"),
    ],
)
def test_female_face_traits_stay_in_female_sentence(style, female_only) -> None:
    """여성형 얼굴 묘사가 성별 구분 없는 문장에 있으면 남성 인물도 그렇게 그려진다(KNK-1524 실측).

    여성 묘사는 "여성 인물은" 문장 안에만 두고, 남성 묘사 문장을 따로 둔다.
    """
    prompt = build_image_prompt(_char(), _GENRE, style)
    female, male = prompt.index("여성 인물은"), prompt.index("남성 인물은")
    assert female < prompt.index(female_only) < male
    assert prompt.count(female_only) == 1


def test_retro_does_not_fix_background_color() -> None:
    """레트로가 배경색(노을빛 보라)을 고정하면 장소와 무관하게 모든 배경이 같은 색이 된다(KNK-1524 실측)."""
    prompt = build_image_prompt(_char(), _GENRE, ART_STYLE_RETRO)
    assert "노을빛" not in prompt
    assert "장소와 시간대에 어울리는 색" in prompt


@pytest.mark.parametrize("style", IMAGE_ART_STYLES)
def test_prompt_shares_body_across_styles(style: str) -> None:
    """무표정·정면 응시와 인물 설정은 화풍과 무관하게 같은 본문에서 나온다."""
    prompt = build_image_prompt(_char(), _GENRE, style)
    assert "무표정으로 카메라를 똑바로 바라본다" in prompt
    assert "- 얼굴: 각진 턱선, 굳은 표정" in prompt
    assert "{{" not in prompt and "<" not in prompt


def test_prompt_rejects_none_style() -> None:
    """NONE은 그림을 만드는 화풍이 아니다 — 호출부 실수는 조용히 ANIME로 가지 않고 터진다."""
    with pytest.raises(KeyError):
        build_image_prompt(_char(), _GENRE, ART_STYLE_NONE)


def test_character_template_requires_every_image_style_block(tmp_path) -> None:
    """화풍 블록이 하나라도 빠진 템플릿은 모듈 로드 시점에 실패한다."""
    path = tmp_path / "T.md"
    path.write_text(
        "---\nversion: 9\n---\n# 제목\n\n## 본문\n\n{{genre}} {{art_style}}\n\n## 화풍: ANIME\n\n애니\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="SEMI_REAL"):
        _load_character_template(path)


def test_character_template_requires_body_placeholder(tmp_path) -> None:
    path = tmp_path / "T.md"
    path.write_text(
        "---\nversion: 9\n---\n\n## 본문\n\n{{genre}}\n\n## 화풍: ANIME\n\n애니\n\n## 화풍: SEMI_REAL\n\n반실사\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="art_style"):
        _load_character_template(path)


def test_character_template_splits_body_and_styles(tmp_path) -> None:
    # 블록 목록은 계약(IMAGE_ART_STYLES)에서 만든다 — 화풍이 늘어도 이 테스트를 손으로 고치지 않게.
    blocks = "".join(f"## 화풍: {style}\n\n{style} 블록\n\n" for style in IMAGE_ART_STYLES)
    path = tmp_path / "T.md"
    path.write_text(
        "---\nversion: 9\n---\n# 제목\n설명\n\n## 본문\n\n{{genre}}\n화풍: {{art_style}}\n\n" + blocks,
        encoding="utf-8",
    )
    body, styles = _load_character_template(path)
    assert body == "{{genre}}\n화풍: {{art_style}}"
    assert styles == {style: f"{style} 블록" for style in IMAGE_ART_STYLES}


# ── compile_story 연결 ────────────────────────────────────────────────────────


def _spec() -> dict:
    return json.loads((_FIXTURES / "spec_valid.json").read_text(encoding="utf-8"))


async def test_compile_none_skips_character_images_and_thumbnail(monkeypatch) -> None:
    """"없음"이면 인물 이미지와 표지 호출이 한 번도 나가지 않는다. 표지 객체는 error=skipped로 내려간다."""
    spec = _spec()
    character_calls: list[str] = []

    async def fake_complete(system, user, **kwargs):
        return spec, story_llm.LlmUsage("test", 100, 200, provider="openai")

    async def fake_character_generate(prompt, **_kwargs):
        character_calls.append(prompt)
        return ImageResult(image_bytes=_FAKE_WEBP, model="test", provider="openai")

    thumb_styles: list[str] = []

    async def fake_thumb(characters, genre_tags, art_style):
        thumb_styles.append(art_style)
        return ThumbnailImageOut(image_name="썸네일_기본", image_base64="QQ==")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    # conftest가 인물 생성을 가짜로 바꿔 두므로 실제 함수를 되돌려 호출 생략을 본다.
    monkeypatch.setattr(story_llm, "_generate_character_images_safe", _generate_character_images_safe)
    monkeypatch.setattr(gen_mod, "generate_image", fake_character_generate)
    monkeypatch.setattr(story_llm, "_generate_thumbnail_image_safe", fake_thumb)

    response = await story_llm.compile_story(_request(art_style=ART_STYLE_NONE))

    assert character_calls == []
    assert response.character_images == []
    # "없음"이면 표지도 만들지 않는다. 표지는 필수 필드라 객체는 내려가되 error=skipped다.
    assert thumb_styles == []
    assert response.thumbnail_image.image_base64 is None
    assert response.thumbnail_image.error == "skipped"
    assert response.thumbnail_image.image_name == "썸네일_기본"
    # 인물 외형·소개는 화풍과 무관하게 그대로 내려간다 — 백엔드 저장 흐름이 바뀌지 않는다.
    assert len(response.character_appearances) == 3
    assert response.meta.prompt_versions["CHARACTER_IMAGE"] >= 2


async def test_compile_semi_real_reaches_image_prompt(monkeypatch) -> None:
    """요청의 화풍이 인물 이미지와 표지 프롬프트 모두에 전달된다(표지도 같은 그림체여야 한다)."""
    spec = _spec()
    character_prompts: list[str] = []
    thumbnail_prompts: list[str] = []

    async def fake_complete(system, user, **kwargs):
        return spec, story_llm.LlmUsage("test", 100, 200, provider="openai")

    async def fake_character_generate(prompt, **_kwargs):
        character_prompts.append(prompt)
        return ImageResult(image_bytes=_FAKE_WEBP, model="test", provider="openai")

    async def fake_thumbnail_generate(prompt, **_kwargs):
        thumbnail_prompts.append(prompt)
        return ImageResult(image_bytes=_FAKE_WEBP, model="test", provider="openai")

    monkeypatch.setattr(story_llm, "_complete_json", fake_complete)
    monkeypatch.setattr(story_llm, "_generate_character_images_safe", _generate_character_images_safe)
    monkeypatch.setattr(story_llm, "_generate_thumbnail_image_safe", _generate_thumbnail_image_safe)
    monkeypatch.setattr(gen_mod, "generate_image", fake_character_generate)
    monkeypatch.setattr(thumb_mod, "generate_image", fake_thumbnail_generate)

    response = await story_llm.compile_story(_request(art_style=ART_STYLE_SEMI_REAL))

    assert len(character_prompts) == 3
    assert all("반실사 스타일" in p for p in character_prompts)
    assert len(thumbnail_prompts) == 1
    assert "한국 웹툰·로맨스 일러스트 계열의 반실사 스타일" in thumbnail_prompts[0]
    assert "셀 셰이딩을 기본으로" not in thumbnail_prompts[0]
    assert all(img.image_base64 for img in response.character_images)
