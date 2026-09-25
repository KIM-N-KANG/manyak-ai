"""저장된 게시물에서 계약에 있는 검수 필드와 이미지 경로만 추출한다."""

from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from src.services.moderation.images import ImageSource

# None은 텍스트, dict는 객체, 원소 하나의 list는 배열의 구조다.
_FIELDS = {
    "title": None, "oneLineIntro": None, "description": None, "genres": [None],
    "storySettings": {
        "worldSetting": None, "characterSetting": None, "userRoleSetting": None, "ruleSetting": None,
    },
    "startSettings": [{
        "name": None, "prologue": None, "startSituation": None, "suggestedInputs": [None],
        "endings": [{"name": None, "requirement": {"achievementCondition": None}, "epilogue": None}],
    }],
    "mainEvents": [{"name": None, "description": None, "keySentence": None}],
    "thumbnailUrl": None,
    "characters": [{"name": None, "images": [{"imageName": None, "imageUrl": None}]}],
}


@dataclass(frozen=True)
class ModerationInput:
    post: dict[str, JsonValue]
    paths: dict[str, Literal["TEXT", "IMAGE"]]
    images: list[ImageSource]


def prepare_input(post: dict[str, JsonValue]) -> ModerationInput:
    """storyId・공개 설정・알 수 없는 필드는 모델 입력과 유효 경로에서 제외한다."""
    paths: dict[str, Literal["TEXT", "IMAGE"]] = {}
    images: list[ImageSource] = []

    def select(value: JsonValue, shape: object, path: str) -> JsonValue:
        if isinstance(shape, dict):
            if not isinstance(value, dict):
                raise ValueError(f"{path}: 객체가 필요합니다")
            return {
                key: select(value[key], child, f"{path}.{key}" if path else key)
                for key, child in shape.items() if key in value and value[key] is not None
            }
        if isinstance(shape, list):
            if not isinstance(value, list):
                raise ValueError(f"{path}: 배열이 필요합니다")
            return [select(item, shape[0], f"{path}[{i}]") for i, item in enumerate(value)]
        if not isinstance(value, str):
            raise ValueError(f"{path}: 문자열이 필요합니다")
        kind = "IMAGE" if path == "thumbnailUrl" or path.endswith(".imageUrl") else "TEXT"
        paths[path] = kind
        if kind == "IMAGE":
            images.append(ImageSource(path=path, url=value))
        return value

    selected = select(post, _FIELDS, "")
    assert isinstance(selected, dict)
    return ModerationInput(post=selected, paths=paths, images=images)
