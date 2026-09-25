"""검수 이미지 다운로드·검사 테스트(KNK-1359).

실제 네트워크 대신 httpx.MockTransport를 끼운다(자식 이미지 부모 다운로드 테스트와 같은 방식).
실패 코드가 계약(하네스 Spec §5-9-6)의 두 가지 — 다시 시도할 문제(`IMAGE_DOWNLOAD_FAILED`)와
사용자가 고칠 문제(`IMAGE_INVALID`) — 로 정확히 갈리는지를 본다.
"""

import asyncio

import httpx
import pytest

from src.core.config import settings
from src.services.moderation import images
from src.services.moderation.images import (
    ERROR_IMAGE_DOWNLOAD_FAILED,
    ERROR_IMAGE_INVALID,
    ImageDownloadFailed,
    ImageInvalid,
    ImageSource,
    fetch_images,
)

PNG = b"\x89PNG\r\n\x1a\ncover"
JPEG = b"\xff\xd8\xff\xe0portrait"
GIF = b"GIF89aanimated"
WEBP = b"RIFF\x00\x00\x00\x00WEBPvp8"
HOST = "cdn.example.com"


def _mock(monkeypatch, handler) -> list[httpx.Request]:
    """허용 호스트를 테스트용으로 바꾸고 AsyncClient를 가짜 전송으로 만든다."""
    seen: list[httpx.Request] = []
    client_type = httpx.AsyncClient

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(settings, "image_parent_allowed_hosts", [HOST])
    monkeypatch.setattr(
        images.httpx,
        "AsyncClient",
        lambda **kw: client_type(transport=httpx.MockTransport(record), **kw),
    )
    return seen


def _src(path: str, name: str = "a.png") -> ImageSource:
    return ImageSource(path=path, url=f"https://{HOST}/{name}")


# ── 정상 경로 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("data", "content_type"),
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (GIF, "image/gif"), (WEBP, "image/webp")],
)
async def test_detects_format_from_bytes_not_extension(monkeypatch, data, content_type) -> None:
    """형식은 실제 바이트로 판별한다 — 확장자가 거짓이어도 결과가 같다."""
    _mock(monkeypatch, lambda req: httpx.Response(200, content=data))

    [image] = await fetch_images([_src("thumbnailUrl", "wrong.bmp")])

    assert (image.path, image.content_type, image.data) == ("thumbnailUrl", content_type, data)
    part = image.content_part()
    assert part["type"] == "image_url"
    assert part["image_url"]["url"].startswith(f"data:{content_type};base64,")


async def test_keeps_input_order_and_downloads_all(monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PNG if req.url.path.endswith("a.png") else JPEG)

    seen = _mock(monkeypatch, handler)
    sources = [
        _src("thumbnailUrl", "a.png"),
        _src("characters[0].images[0].imageUrl", "b.jpg"),
        _src("characters[1].images[0].imageUrl", "a.png"),
    ]

    result = await fetch_images(sources)

    assert [i.path for i in result] == [s.path for s in sources]
    assert [i.content_type for i in result] == ["image/png", "image/jpeg", "image/png"]
    assert len(seen) == 3
    assert all(req.method == "GET" for req in seen)


async def test_no_sources_returns_empty_without_network(monkeypatch) -> None:
    seen = _mock(monkeypatch, lambda req: httpx.Response(200, content=PNG))

    assert await fetch_images([]) == []
    assert seen == []


# ── 다시 시도할 문제: IMAGE_DOWNLOAD_FAILED ─────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.example.com/a.png",  # https가 아님
        "https://evil.example.com/a.png",  # 허용 목록 밖
        "https://cdn.example.com:8443/a.png",  # 비표준 포트
        "https://user:pw@cdn.example.com/a.png",  # 자격증명 포함
        "not a url",
    ],
)
async def test_disallowed_url_is_download_failure_without_request(monkeypatch, url) -> None:
    """허용되지 않은 주소는 요청을 보내지 않고 접근 실패로 분류한다 — 파일 문제가 아니다."""
    seen = _mock(monkeypatch, lambda req: httpx.Response(200, content=PNG))

    with pytest.raises(ImageDownloadFailed) as info:
        await fetch_images([ImageSource(path="thumbnailUrl", url=url)])

    assert info.value.code == ERROR_IMAGE_DOWNLOAD_FAILED
    assert info.value.path == "thumbnailUrl"
    assert seen == []


@pytest.mark.parametrize("status", [403, 404, 500])
async def test_non_2xx_is_download_failure(monkeypatch, status) -> None:
    _mock(monkeypatch, lambda req: httpx.Response(status, content=b"nope"))

    with pytest.raises(ImageDownloadFailed) as info:
        await fetch_images([_src("thumbnailUrl")])
    assert info.value.code == ERROR_IMAGE_DOWNLOAD_FAILED


async def test_redirect_is_not_followed(monkeypatch) -> None:
    """리다이렉트를 따라가지 않는다 — 허용 검사를 통과한 주소가 다른 곳으로 튀는 길을 막는다."""
    seen = _mock(
        monkeypatch,
        lambda req: httpx.Response(302, headers={"location": "https://evil.example.com/x"}),
    )

    with pytest.raises(ImageDownloadFailed):
        await fetch_images([_src("thumbnailUrl")])
    assert len(seen) == 1


async def test_transport_error_is_download_failure(monkeypatch) -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    _mock(monkeypatch, boom)

    with pytest.raises(ImageDownloadFailed) as info:
        await fetch_images([_src("thumbnailUrl")])
    assert info.value.code == ERROR_IMAGE_DOWNLOAD_FAILED


async def test_per_request_timeout_is_download_failure(monkeypatch) -> None:
    def slow(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    _mock(monkeypatch, slow)

    with pytest.raises(ImageDownloadFailed) as info:
        await fetch_images([_src("characters[0].images[0].imageUrl")])
    assert info.value.path == "characters[0].images[0].imageUrl"


async def test_overall_timeout_names_first_unfinished_image(monkeypatch) -> None:
    """전체 제한 시간을 넘기면 끝나지 않은 첫 장의 경로로 실패한다."""

    async def hang(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("fast.png"):
            return httpx.Response(200, content=PNG)
        await asyncio.sleep(5)
        return httpx.Response(200, content=PNG)

    _mock(monkeypatch, hang)
    monkeypatch.setattr(settings, "moderation_image_timeout", 0.05)

    with pytest.raises(ImageDownloadFailed) as info:
        await fetch_images([_src("thumbnailUrl", "fast.png"), _src("characters[0].images[0].imageUrl", "slow.png")])
    assert info.value.path == "characters[0].images[0].imageUrl"


# ── 사용자가 고칠 문제: IMAGE_INVALID ────────────────────────────────────────
@pytest.mark.parametrize(
    "data",
    [b"", b"<html>not an image</html>", b"BM\x00\x00bitmap", b"<svg xmlns='x'/>"],
)
async def test_non_image_or_unsupported_format_is_invalid(monkeypatch, data) -> None:
    _mock(monkeypatch, lambda req: httpx.Response(200, content=data))

    with pytest.raises(ImageInvalid) as info:
        await fetch_images([_src("thumbnailUrl")])
    assert info.value.code == ERROR_IMAGE_INVALID
    assert info.value.path == "thumbnailUrl"


async def test_oversized_body_is_invalid(monkeypatch) -> None:
    monkeypatch.setattr(settings, "moderation_image_max_bytes", 16)
    _mock(monkeypatch, lambda req: httpx.Response(200, content=PNG + b"x" * 32))

    with pytest.raises(ImageInvalid):
        await fetch_images([_src("thumbnailUrl")])


async def test_declared_oversize_is_rejected_before_reading_body(monkeypatch) -> None:
    """Content-Length가 상한을 넘으면 본문을 받지 않고 끊는다."""
    monkeypatch.setattr(settings, "moderation_image_max_bytes", 16)
    _mock(
        monkeypatch,
        lambda req: httpx.Response(200, headers={"content-length": "1000000"}, content=PNG),
    )

    with pytest.raises(ImageInvalid):
        await fetch_images([_src("thumbnailUrl")])


async def test_exactly_max_bytes_is_allowed(monkeypatch) -> None:
    data = PNG + b"x" * (32 - len(PNG))
    monkeypatch.setattr(settings, "moderation_image_max_bytes", 32)
    _mock(monkeypatch, lambda req: httpx.Response(200, content=data))

    [image] = await fetch_images([_src("thumbnailUrl")])
    assert image.data == data


# ── 여러 장 실패 시 첫 번째만 ────────────────────────────────────────────────
async def test_first_failure_in_input_order_wins(monkeypatch) -> None:
    """뒤 장이 다운로드 실패, 앞 장이 파일 불량이면 앞 장의 오류를 낸다."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("bad.png"):
            return httpx.Response(200, content=b"not image")
        return httpx.Response(500)

    _mock(monkeypatch, handler)

    with pytest.raises(ImageInvalid) as info:
        await fetch_images([_src("thumbnailUrl", "bad.png"), _src("characters[0].images[0].imageUrl", "err.png")])
    assert info.value.path == "thumbnailUrl"


async def test_error_message_does_not_contain_url(monkeypatch) -> None:
    """예외 문자열은 로그·Sentry로 흘러가므로 서빙 URL을 담지 않는다."""
    _mock(monkeypatch, lambda req: httpx.Response(404))

    with pytest.raises(ImageDownloadFailed) as info:
        await fetch_images([_src("thumbnailUrl", "secret-object-key.png")])
    assert "secret-object-key" not in str(info.value)
    assert HOST not in str(info.value)


@pytest.mark.parametrize("bad_first", [True, False])
@pytest.mark.parametrize("slow", [True, False])
async def test_invalid_image_wins_over_download_failure(monkeypatch, bad_first, slow) -> None:
    async def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("bad.png"):
            return httpx.Response(200, content=b"not image")
        if slow:
            await asyncio.sleep(5)
        return httpx.Response(500)

    _mock(monkeypatch, handler)
    monkeypatch.setattr(settings, "moderation_image_timeout", 0.05)
    sources = [_src("thumbnailUrl", "bad.png"), _src("characters[0].images[0].imageUrl", "other.png")]
    if not bad_first:
        sources.reverse()

    with pytest.raises(ImageInvalid) as info:
        await fetch_images(sources)

    assert info.value.path == "thumbnailUrl"


async def test_downloads_at_most_five_images_at_once(monkeypatch) -> None:
    active = 0
    peak = 0
    first_five_started = asyncio.Event()
    release = asyncio.Event()

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 5:
            first_five_started.set()
        try:
            await release.wait()
            return httpx.Response(200, content=PNG)
        finally:
            active -= 1

    _mock(monkeypatch, handler)
    sources = [_src(f"characters[{i}].images[0].imageUrl") for i in range(12)]
    task = asyncio.create_task(fetch_images(sources))
    try:
        await asyncio.wait_for(first_five_started.wait(), timeout=1)
        assert peak == 5
    finally:
        release.set()
        result = await task

    assert peak == 5
    assert [image.path for image in result] == [source.path for source in sources]
    assert active == 0


@pytest.mark.parametrize("limit, rejected", [(39, True), (40, False)])
async def test_total_image_limit_counts_base64_expansion(monkeypatch, limit, rejected) -> None:
    # PNG는 13바이트이므로 두 장의 원본 26바이트가 base64에서는 40바이트가 된다.
    monkeypatch.setattr(images, "MAX_REQUEST_BYTES", limit)
    _mock(monkeypatch, lambda req: httpx.Response(200, content=PNG))
    sources = [_src("thumbnailUrl"), _src("characters[0].images[0].imageUrl")]
    if rejected:
        with pytest.raises(ImageInvalid, match="전체 전송 용량"):
            await fetch_images(sources)
    else:
        assert len(await fetch_images(sources)) == 2


async def test_timeout_cancels_queued_and_active_downloads(monkeypatch) -> None:
    active = 0
    started = 0

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal active, started
        active += 1
        started += 1
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    _mock(monkeypatch, handler)
    monkeypatch.setattr(settings, "moderation_image_timeout", 0.05)
    with pytest.raises(ImageDownloadFailed):
        await fetch_images([_src(f"characters[{i}].images[0].imageUrl") for i in range(12)])
    assert started == 5
    assert active == 0
