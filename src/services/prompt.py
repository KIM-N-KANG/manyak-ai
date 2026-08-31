import re
from pathlib import Path

from src.schemas.story import CharacterInput
from src.schemas.story_compile import LorebookItem
from src.services.prompt_meta import read_version

from src.services.llm.base import PROVIDER_GOOGLE

_PROMPT_DIR = Path(__file__).parent.parent.parent / "prompt" / "story"
_STORYLINES_TEMPLATE_PATH = _PROMPT_DIR / "STORYLINES-TEMPLATE.md"
_COMPILE_TEMPLATE_PATH = _PROMPT_DIR / "COMPILE-TEMPLATE.md"
_COMPILE_GEMINI_TEMPLATE_PATH = _PROMPT_DIR / "COMPILE-TEMPLATE-gemini.md"

# 버전은 파일명이 아니라 frontmatter가 SSOT다(KNK-228). 로깅용으로 노출한다(KNK-243).
STORYLINES_VERSION = read_version(_STORYLINES_TEMPLATE_PATH)
COMPILE_VERSION = read_version(_COMPILE_TEMPLATE_PATH)
COMPILE_GEMINI_VERSION = read_version(_COMPILE_GEMINI_TEMPLATE_PATH)


def _load_template(path: Path) -> tuple[str, str]:
    """템플릿 파일을 `[SYSTEM]` / `[USER]` 두 블록으로 분할해 반환한다."""
    try:
        template = path.read_text(encoding="utf-8")
        _, after_system = template.split("## [SYSTEM]", 1)
        system_raw, user_raw = after_system.split("## [USER]", 1)
        return system_raw.strip().removesuffix("---").strip(), user_raw.strip()
    except (FileNotFoundError, ValueError) as e:
        raise RuntimeError(f"프롬프트 템플릿 로드 또는 파싱 실패: {path.name}: {e}")


_STORYLINES_SYSTEM, _STORYLINES_USER = _load_template(_STORYLINES_TEMPLATE_PATH)
_COMPILE_SYSTEM, _COMPILE_USER = _load_template(_COMPILE_TEMPLATE_PATH)
_COMPILE_GEMINI_SYSTEM, _COMPILE_GEMINI_USER = _load_template(_COMPILE_GEMINI_TEMPLATE_PATH)


def _render(template: str, mapping: dict[str, str]) -> str:
    """자리표시자를 단일 패스로 치환한다.

    순차 .replace()는 앞서 채워 넣은 값(사용자 입력)을 다음 치환이 다시 검사해,
    입력에 자리표시자 모양 문자열이 들어 있으면 프롬프트가 엉킨다. 한 번에 훑으면
    치환된 값은 재검사되지 않는다.
    """
    pattern = r"\{\{(" + "|".join(map(re.escape, mapping)) + r")\}\}"
    return re.sub(pattern, lambda m: mapping[m.group(1)], template)


# 성별은 계약 값("MALE"·"FEMALE")이 아니라 한국어로 프롬프트에 싣는다.
GENDER_KO = {"MALE": "남성", "FEMALE": "여성"}


def _format_character(c: CharacterInput) -> str:
    """인물 세트 한 명을 한 줄로 렌더한다. 비운 항목은 (미정)으로 표시해 LLM이 정하게 한다."""
    gender = GENDER_KO[c.gender] if c.gender else "(미정)"
    features = ", ".join(c.features) if c.features else "(미정)"
    return f"이름: {c.name or '(미정)'} / 성별: {gender} / 특징: {features}"


def _format_supporting_characters(characters: list[CharacterInput]) -> str:
    """주변 인물 블록. 0명이면 구성 전체를 LLM에 맡긴다(0명 허용 계약, KNK-833)."""
    if not characters:
        return "(미정 — 이야기에 어울리는 주변 인물을 직접 구성하라)"
    return "\n".join(f"{i}) {_format_character(c)}" for i, c in enumerate(characters, 1))


def _format_compile_supporting_characters(characters: list[CharacterInput]) -> str:
    """컴파일 입력 인물에 중간 JSON에서 되돌려 받을 내부 식별자를 붙인다."""
    if not characters:
        return "(미정 — 이야기에 어울리는 주변 인물을 직접 구성하라)"
    return "\n".join(
        f"[input_character_id: input-{i}] {_format_character(c)}"
        for i, c in enumerate(characters, 1)
    )


def build_storylines_prompt(
    genre_tags: list[str],
    protagonist: CharacterInput,
    supporting_characters: list[CharacterInput],
) -> tuple[str, str]:
    user_text = _render(
        _STORYLINES_USER,
        {
            "장르_태그": ", ".join(genre_tags),
            "주인공": _format_character(protagonist),
            "주변_인물": _format_supporting_characters(supporting_characters),
        },
    )
    return _STORYLINES_SYSTEM, user_text


def build_storylines_refill_prompt(
    original_user_prompt: str,
    current_stories_json: str,
    missing_ids: list[int],
) -> tuple[str, str]:
    """인물이 빠진 이야기를 고쳐 쓰게 하는 부분 재호출 프롬프트를 만든다(KNK-840).

    이야기 자체는 잘 나왔으니 통째로 버리지 않고, 빠진 인물만 이야기에 들어가게
    고치라고 한다. 직전 세 편을 맥락으로 주는 이유는 고칠 대상을 보여주면서 나머지
    편과 겹치지 않게 하기 위해서다.
    """
    ids = ", ".join(f"{i}번 이야기" for i in missing_ids)
    user_text = (
        f"{original_user_prompt}\n\n"
        f"--- 직전 생성 결과(JSON) ---\n{current_stories_json}\n\n"
        f"위 결과의 {ids}에 입력 주변 인물이 빠졌다. 이야기의 흐름은 그대로 유지하면서 "
        f"빠진 인물이 사건에 실제로 관여하도록 고쳐라. 이름만 한 줄 얹지 말고, "
        f"그 인물이 이야기 속에서 역할을 하게 해라. "
        f"나머지 이야기는 그대로 두므로 응답에 포함하지 마라.\n"
        f"고친 이야기만 담아 `{{\"stories\": [{{\"id\": <번호>, \"storyline\": \"...\", "
        f'"recommended_infos": ["...", "...", "..."]}}]}}` 형식으로, 설명·머리말·코드 펜스 없이 '
        f"JSON만 출력한다. 분량·문장 수·금지 규칙은 위 지시와 같다."
    )
    return _STORYLINES_SYSTEM, user_text


def _format_lorebooks(lorebooks: list[LorebookItem]) -> str:
    """로어북을 프롬프트 블록으로 만든다. 비어 있으면 `(없음)`(추가정보와 동일 관례)."""
    if not lorebooks:
        return "(없음)"
    # 이름·내용 앞뒤 공백·개행을 털어 ### 헤더·문단이 항상 깔끔히 렌더되게 한다.
    return "\n\n".join(f"### {lb.name.strip()}\n{lb.content.strip()}" for lb in lorebooks)


def build_compile_prompt(
    selected_storyline: str,
    additional_info: str,
    genre_tags: list[str],
    protagonist: CharacterInput,
    supporting_characters: list[CharacterInput],
    lorebooks: list[LorebookItem] | None = None,
    *,
    provider: str | None = None,
) -> tuple[str, str, str]:
    """스토리 컴파일(시점 A-1)용 프롬프트를 완성한다.

    provider가 Google이면 Gemini용 프롬프트를 쓴다(KNK-958). 반환값 세 번째는 프롬프트 버전 키다.
    """
    if provider == PROVIDER_GOOGLE:
        system, user_tmpl = _COMPILE_GEMINI_SYSTEM, _COMPILE_GEMINI_USER
        version_key = "COMPILE_GEMINI"
    else:
        system, user_tmpl = _COMPILE_SYSTEM, _COMPILE_USER
        version_key = "COMPILE"
    user_text = _render(
        user_tmpl,
        {
            "선택_스토리라인": selected_storyline,
            "추가정보": additional_info or "(없음)",
            "장르_태그": ", ".join(genre_tags),
            "주인공": _format_character(protagonist),
            "주변_인물": _format_compile_supporting_characters(supporting_characters),
            "로어북": _format_lorebooks(lorebooks or []),
        },
    )
    return system, user_text, version_key


def build_refill_prompt(
    original_user_prompt: str,
    current_data_json: str,
    missing_blocks: list[str],
    character_fields: dict[int, tuple[str, ...]] | None = None,
    *,
    provider: str | None = None,
) -> tuple[str, str]:
    """누락 블록과 인물의 빈 필드·중복 이름을 한 번에 고치는 프롬프트를 만든다.

    블록은 기존처럼 통째로 다시 받고, 이름·외형만 문제가 있는 인물 카드는 해당 필드만
    ``character_updates``로 받는다. 잘 나온 값은 서버가 보존한다.
    provider가 Google이면 Gemini용 system prompt를 쓴다(KNK-958).
    """
    system = _COMPILE_GEMINI_SYSTEM if provider == PROVIDER_GOOGLE else _COMPILE_SYSTEM
    instructions: list[str] = []
    if missing_blocks:
        blocks_str = ", ".join(missing_blocks)
        instructions.append(
            f"다음 블록이 비어 있거나 누락됐다: {blocks_str}. 이 블록들은 작성 규칙과 "
            f"스키마에 맞게 통째로 새로 채워서 같은 이름의 최상위 키로 반환하라."
        )
    if character_fields:
        targets = "; ".join(
            f"index {index}: {', '.join(fields)}"
            for index, fields in sorted(character_fields.items())
        )
        instructions.append(
            f"character_setting에서 다음 필드만 고쳐라(배열 index는 0부터 시작): {targets}. "
            f"`character_updates` 배열에 각 대상의 `index`와 지정된 필드만 넣어 반환하라. "
            f"name은 다른 모든 인물 이름과 겹치지 않는 이름으로 채워라."
        )
    requested = "\n".join(instructions)
    if missing_blocks and character_fields:
        output_keys = "요청한 블록과 `character_updates`만"
    elif missing_blocks:
        output_keys = "요청한 블록만"
    else:
        output_keys = "`character_updates`만"
    user_text = (
        f"{original_user_prompt}\n\n"
        f"--- 직전 생성 결과(JSON) ---\n{current_data_json}\n\n"
        f"위 결과의 문제를 한 번의 응답에서 모두 고쳐라.\n{requested}\n"
        f"{output_keys} 최상위 키로 갖는 JSON 객체를 반환하라. "
        f"요청하지 않은 블록과 인물 필드는 절대 포함하지 말 것. "
        f"설명·머리말·코드 펜스 없이 JSON만 출력한다."
    )
    return system, user_text
