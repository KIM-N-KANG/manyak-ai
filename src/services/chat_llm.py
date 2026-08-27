"""채팅 본문 LLM 통로(ChatProvider) — 스트리밍 호출 (시점 B).

`assemble()`가 만든 messages를 DeepSeek(OpenAI 호환) `stream=True`로 호출하고,
토큰을 실시간으로 흘린다. 본문은 상황 묘사 + 인물 대사만 만든다 — **다음 행동 선택지는
이 호출이 만들지 않으며**, 본문 스트림이 끝난 뒤 별도 호출(`chat_choices`)이 전담한다
(그래서 `[다음 행동]` 마커 파싱·버퍼링이 사라졌다).

이벤트(dict)를 async generator로 낸다. SSE 와이어 변환·선택지 합치기는 엔드포인트(chat.py)가 맡는다.
- {"event": "token",     "text": ...}
- {"event": "character_image", "name": ..., "image_url": ...}  — 이미지 보유 인물의 `인물명:` 줄 직전(KNK-1005)
- {"event": "completed", "ai_output": ..., "character_images": [...], "model": ..., "provider": ..., "input_tokens": ..., "output_tokens": ...}
- {"event": "error",     "code": ..., "message": ...}
"""

import logging
import re
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import aclosing

from src.core.config import settings
from src.core.sentry import FEATURE_CHAT_RESPONSE, capture_ai_exception
from src.schemas.chat_turn import (
    EVENT_CHARACTER_IMAGE,
    EVENT_COMPLETED,
    EVENT_ERROR,
    EVENT_TOKEN,
    CharacterImageMapping,
)
from src.services import llm
from src.services.chat_assembler import LAYER_VERSIONS
from src.services.llm.base import LlmError, LlmRequest, TextDelta

logger = logging.getLogger(__name__)

# LLM 호출은 공통 통로(src.services.llm)를 통한다(KNK-673). 어느 회사 SDK로 어떤 인자를
# 보낼지는 모델 등록부와 어댑터가 정하므로, 여기서는 "무엇을 원하는지"만 넘긴다 —
# 클라이언트 생성·추론 모드(thinking) 같은 회사 문법은 이 파일에서 사라졌다.

# 이 호출의 제한 시간(초). 첫 토큰까지의 상한이고, 스트리밍이라 통상 빠르게 시작된다.
# **호출마다 반드시 넘긴다** — 비우면 상한이 SDK 기본값(10분)으로 늘어난다.
_TIMEOUT_SECONDS = 90.0

# 인물 이름 상한. 클라이언트·백엔드가 30자로 막는다(3-1-client.md §3-1-4).
_SPEAKER_NAME_MAX_CHARS = 30

# 라벨 안에서 허용하는 공백 수(볼드 기호와 이름 사이, 이름과 콜론 사이). 정규식과 스트리밍
# 상한이 같은 값을 써야 한다 — 한쪽만 더 너그러우면 실시간과 저장본이 다른 라벨을 인정한다.
_LABEL_INNER_WS_MAX = 2

# 줄머리에서 인물명 라벨인지 확정될 때까지 붙잡아 두는 글자 수 상한(들여쓰기 제외).
# 가장 긴 라벨은 `**` + 공백 + 이름(30) + 공백 + `**` + 공백 + `:` 이며, 그 길이와 정확히
# 같다. 이 길이를 넘기면 라벨이 아니라고 보고 원문을 그대로 낸다.
_LABEL_BUFFER_MAX_CHARS = 4 + _SPEAKER_NAME_MAX_CHARS + 3 * _LABEL_INNER_WS_MAX + 1

_WS = rf"[ \t]{{0,{_LABEL_INNER_WS_MAX}}}"
_NAME = rf"[^\n*]{{1,{_SPEAKER_NAME_MAX_CHARS}}}?"

# 줄머리 볼드 화자 라벨을 평문으로 정규화한다: `**설하:**`·`**설하**:` → `설하:`.
# 모델이 사전학습 편향으로 화자 이름을 볼드로 감싸는 경향이 강해(프롬프트로 못 막음 —
# KNK-194 검증에서 100건 중 약 절반 발생) 코드로 떼어낸다. 콜론을 동반한 줄머리 볼드만
# 손대므로 본문 강조 `**단어**`(콜론 없음)는 건드리지 않는다. 스트리밍 파서와 완료 본문이
# 같은 정규식을 쓴다 — 실시간 화면과 저장 본문이 같아야 한다(KNK-1005).
#
# 공백은 `\s*`가 아니라 줄 안(`[ \t]`)이고 개수도 제한한다. `\s*`는 줄바꿈을 건너 다음 줄의
# 콜론까지 한 라벨로 묶어(`**세린:**` 다음 줄이 `:`로 시작하면 `세린:: …`로 합쳐짐) 줄 안에서만
# 보는 스트리밍 파서와 어긋났고, 무제한 공백은 스트리밍 상한 밖의 라벨을 저장본만 인정하게
# 했다(KNK-1005 퍼징·리뷰에서 발견).
_SPEAKER_BOLD_RE = re.compile(
    rf"^([ \t]*)\*\*{_WS}({_NAME}){_WS}\*\*{_WS}:[ \t]*"  # **설하**:
    rf"|^([ \t]*)\*\*{_WS}({_NAME}){_WS}:{_WS}\*\*[ \t]*",  # **설하:**
    re.M,
)


def _strip_speaker_bold(text: str) -> str:
    """줄머리 볼드 화자 라벨(`**이름:**`·`**이름**:`)의 볼드를 떼 `이름: `로 정규화한다."""

    def _repl(m: "re.Match[str]") -> str:
        indent, name = _split_bold_label(m)
        return f"{indent}{name}: "

    return _SPEAKER_BOLD_RE.sub(_repl, text)


def _split_bold_label(m: "re.Match[str]") -> tuple[str, str]:
    """볼드 라벨 매치의 두 대안 중 매칭된 쪽의 (들여쓰기, 이름)을 고른다."""
    if m.group(2) is not None:
        return m.group(1), m.group(2).strip()
    return m.group(3), m.group(4).strip()


def _images_by_name(
    character_images: list[CharacterImageMapping],
) -> dict[str, CharacterImageMapping]:
    """이름-URL 표. 빈 이름은 어떤 줄과도 맞을 수 없으므로 뺀다(A19)."""
    return {image.name: image for image in character_images if image.name}


def _speaker_label_re(names: "Iterable[str]") -> "re.Pattern[str]":
    """이미지 보유 인물의 평문 인물명 라벨(`이름:`)을 줄머리에서 찾는 정규식.

    이름은 등록된 것과 정확히 같아야 한다. 긴 이름을 먼저 두어 한 이름이 다른 이름의
    앞부분일 때(`세린`·`세린아`) 긴 쪽이 이긴다.
    """
    alternation = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    return re.compile(rf"^([ \t]*)({alternation})[ \t]*:", re.M)


def _insert_storage_markers(
    text: str, character_images: list[CharacterImageMapping]
) -> tuple[str, list[dict]]:
    """볼드를 뗀 완료 본문에서 인물명 라벨 앞에 URL 마커를 붙이고 표시 순서 목록을 만든다."""
    images = _images_by_name(character_images)
    if not images:
        return text, []
    displayed: list[dict] = []

    def replace(match: "re.Match[str]") -> str:
        image = images[match.group(2)]
        displayed.append({"name": image.name, "image_url": image.image_url})
        # 마커는 대사 줄 위에 빈 줄을 두고 따로 둔다 — 프론트가 마커 줄을 통째로 이미지로
        # 바꾸기 쉽게(프론트 요청, KNK-1002). 원래 들여쓰기는 대사 줄에 그대로 남긴다.
        # 마커에는 URL만 담는다(KNK-1025) — 인물 이름은 URL 파일명과 completed의
        # character_images에 이미 있어 마커에 또 넣을 이유가 없다.
        return f"[[{image.image_url}]]\n\n{match.group(0)}"

    return _speaker_label_re(images).sub(replace, text), displayed


class _SpeakerLabelStreamParser:
    """LLM 델타를 줄 단위로 살펴 인물명 라벨을 찾고, 이미지 이벤트를 그 앞에 끼운다.

    이미지의 근거는 LLM이 따로 쓰는 태그가 아니라 대사 형식 자체(`인물명:`)다 — 형식은
    프롬프트가 아니라 코드가 담보한다(D7). 줄이 시작될 때마다 라벨인지 확정될 때까지
    글자를 잠시 모으고, 볼드 라벨은 평문으로 바꿔 내보낸다. 이미지 보유 인물의 라벨이면
    character_image 이벤트를 라벨 글자보다 먼저 낸다.
    """

    def __init__(self, character_images: list[CharacterImageMapping]) -> None:
        self._images = _images_by_name(character_images)
        self._label_re = _speaker_label_re(self._images) if self._images else None
        self._buffer = ""          # 줄머리에서 모으는 중인 글자
        self._collecting = True    # 본문 첫 줄부터 줄머리다
        self._skip_ws = False      # 볼드 라벨을 `이름: `로 바꾼 직후, 원문의 뒤따르는 공백을 버린다

    def feed(self, text: str) -> list[dict]:
        """델타 하나를 처리한다. 일반 글은 token, 이미지 보유 인물의 라벨 앞엔 character_image다."""
        events: list[dict] = []
        visible: list[str] = []

        def emit_visible() -> None:
            if visible:
                events.append({"event": EVENT_TOKEN, "text": "".join(visible)})
                visible.clear()

        for char in text:
            if self._skip_ws:
                if char in " \t":
                    continue
                self._skip_ws = False

            if not self._collecting:
                visible.append(char)
                if char == "\n":
                    self._collecting = True
                continue

            self._buffer += char
            resolved = self._resolve_label()
            if resolved is not None:
                name, label_text = resolved
                image = self._images.get(name)
                if image is not None:
                    emit_visible()
                    events.append(
                        {
                            "event": EVENT_CHARACTER_IMAGE,
                            "name": image.name,
                            "image_url": image.image_url,
                        }
                    )
                visible.append(label_text)
                self._buffer = ""
                self._collecting = False
            elif not self._can_still_be_label():
                visible.append(self._buffer)
                self._buffer = ""
                # 모은 글자가 줄바꿈으로 끝났으면 다음 줄머리를 다시 모은다.
                self._collecting = char == "\n"

        emit_visible()
        return events

    def _resolve_label(self) -> tuple[str, str] | None:
        """모은 글자가 라벨로 확정되면 (이름, 내보낼 글자)를 돌려준다."""
        bold = _SPEAKER_BOLD_RE.match(self._buffer)
        if bold is not None and bold.end() == len(self._buffer):
            indent, name = _split_bold_label(bold)
            self._skip_ws = True
            return name, f"{indent}{name}: "
        if self._label_re is not None:
            plain = self._label_re.match(self._buffer)
            if plain is not None and plain.end() == len(self._buffer):
                return plain.group(2), self._buffer  # 평문 라벨은 원문 그대로
        return None

    def _can_still_be_label(self) -> bool:
        """더 모으면 라벨이 될 가능성이 남아 있는가. 아니면 바로 내보내 지연을 없앤다."""
        head = self._buffer.lstrip(" \t")
        if not head:
            return True
        # 상한은 들여쓰기를 뺀 길이로 센다 — 저장 쪽 정규식도 들여쓰기엔 제한이 없다.
        if "\n" in head or len(head) > _LABEL_BUFFER_MAX_CHARS:
            return False
        if head.startswith("**") or head == "*":
            return True  # 볼드 라벨 후보. `*지문*`은 둘째 글자에서 여기 못 와 바로 나간다
        if self._label_re is None:
            return False
        stem = head.rstrip(" \t")
        return any(name.startswith(stem) for name in self._images)

    def flush(self) -> list[dict]:
        """정상 종료나 오류 직전에 아직 확정되지 않은 줄머리 글자를 원문으로 내보낸다."""
        if not self._buffer:
            return []
        text = self._buffer
        self._buffer = ""
        return [{"event": EVENT_TOKEN, "text": text}]


async def stream_chat_turn(
    messages: list[dict], *, character_images: list[CharacterImageMapping] | None = None
) -> AsyncIterator[dict]:
    """messages를 LLM에 스트리밍 호출하고 token·character_image→completed(또는 error)를 낸다.

    줄머리 볼드 화자 라벨은 실시간 token과 완료 본문 양쪽에서 평문 `이름: `으로 바꾼다.
    인물 이미지 매핑이 있으면 이미지 보유 인물의 인물명 라벨(`이름:`) 앞에 이미지 이벤트를
    끼우고, 완료 시 같은 자리에 URL 저장 마커를 붙여 `ai_output`과 표시 순서의
    `character_images`를 낸다(KNK-1005). 선택지는 여기서 만들지 않으며,
    엔드포인트가 본문 종료 후 별도 호출로 받아 completed에 합친다.
    """
    full = ""                              # 전체 누적 — 완료 시 ai_output
    model: str | None = None               # 응답이 돌려준 실제 모델(로깅 메타)
    input_tokens: int | None = None        # 종료 이벤트에서 취득(없으면 None)
    output_tokens: int | None = None
    label_parser = _SpeakerLabelStreamParser(character_images or [])
    # 이 호출이 어느 공급자로 갈지는 부르기 전에 정해진다 — 스트림이 오류로 끝나면 종료
    # 이벤트가 아예 없어서, 결과에서 읽는 방식으로는 실패 태그를 채울 수 없다(KNK-674).
    provider = llm.provider_of(settings.chat_model)
    start = time.monotonic()

    try:
        # 조각을 흘리고 마지막에 종료 이벤트(모델·토큰)를 준다. 빈 청크 가드·usage 수집·
        # 회사별 청크 모양 해석은 통로가 맡는다.
        #
        # `aclosing`으로 감싸는 이유: 사용자가 채팅 도중 창을 닫으면 이 제너레이터가 중간에
        # 닫히는데, `async for`는 중도 이탈 때 **안쪽 제너레이터를 닫아주지 않는다.** 그러면
        # 어댑터가 커넥션을 반납하려고 넣어둔 정리 코드(`openai_sdk.stream`의 finally)가
        # 쓰레기 수집 시점까지 밀린다 — 어댑터에 있는 결정적 정리가 실제 운영 경로에서만
        # 작동하지 않는 상태가 된다(KNK-673 리뷰에서 탐침으로 확인).
        async with aclosing(
            llm.stream(
                LlmRequest(
                    model=settings.chat_model,
                    messages=messages,
                    timeout=_TIMEOUT_SECONDS,
                )
            )
        ) as events:
            async for event in events:
                if isinstance(event, TextDelta):
                    full += event.text
                    for parsed in label_parser.feed(event.text):
                        yield parsed
                else:  # StreamCompleted — 정상 종료일 때만 온다
                    model = event.model
                    input_tokens = event.usage.input_tokens
                    output_tokens = event.usage.output_tokens

        for parsed in label_parser.flush():
            yield parsed

        # 스트리밍 파서와 같은 순서 — 볼드를 먼저 떼야 라벨 정규식이 평문 `이름:`을 찾는다.
        normalized = _strip_speaker_bold(full.strip())
        ai_output, completed_images = _insert_storage_markers(
            normalized, character_images or []
        )
        # 로깅 메타 재료(model·토큰)를 함께 넘긴다 — 엔드포인트가 판정 호출 메타를 합산하고
        # prompt_versions·provider를 더해 completed의 meta로 조립한다(KNK-243).
        # 선택지 몫 메타는 KNK-625로 /chat/choices 응답 meta에 분리됐다.
        yield {
            "event": EVENT_COMPLETED,
            "ai_output": ai_output,
            "character_images": completed_images,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "provider": provider,
        }
    except LlmError as e:
        # 전송 오류(타임아웃·429·요청거부·연결실패)를 통로가 공급자 중립 예외로 접어 준 것.
        # 일부 토큰을 이미 흘려보낸 뒤여도 여기로 와서 error 이벤트로 끝난다.
        # SSE는 HTTP 200이라 미들웨어가 못 잡는다 — 여기서 직접 Sentry로 보고한다(AN-4).
        # logger.exception보다 먼저 보낸다. Sentry는 같은 예외의 두 번째 이벤트를 중복으로
        # 버리므로 로그가 먼저면 feature·provider·error_code가 없는 자동 캡처만 남는다.
        for parsed in label_parser.flush():
            yield parsed

        capture_ai_exception(
            e,
            feature=FEATURE_CHAT_RESPONSE,
            provider=provider,
            model=settings.chat_model,
            prompt_versions=LAYER_VERSIONS,
            retry_count=0,  # 본문은 재호출 없음
            latency_ms=int((time.monotonic() - start) * 1000),
        )
        logger.exception("채팅 본문 LLM 스트리밍 실패")  # 스택트레이스 포함 서버 로그
        # 내부 상세(str(e))는 Sentry·로그로만 남기고 클라이언트엔 정제 메시지를 보낸다(AN-4-10).
        yield {
            "event": EVENT_ERROR,
            "code": "LLM_ERROR",
            "message": "채팅 연동 중 오류가 발생했습니다.",
        }
