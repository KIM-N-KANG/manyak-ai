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
    PreparedImages,
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

    [image] = (await fetch_images([_src("thumbnailUrl", "wrong.bmp")])).images

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

    assert [i.path for i in result.images] == [s.path for s in sources]
    assert [i.content_type for i in result.images] == ["image/png", "image/jpeg", "image/png"]
    assert len(seen) == 3
    assert all(req.method == "GET" for req in seen)


async def test_no_sources_returns_empty_without_network(monkeypatch) -> None:
    seen = _mock(monkeypatch, lambda req: httpx.Response(200, content=PNG))

    assert await fetch_images([]) == PreparedImages(images=[], errors=[])
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

    prepared = await fetch_images([ImageSource(path="thumbnailUrl", url=url)])
    [error] = prepared.errors
    assert isinstance(error, ImageDownloadFailed)

    assert error.code == ERROR_IMAGE_DOWNLOAD_FAILED
    assert error.path == "thumbnailUrl"
    assert seen == []


@pytest.mark.parametrize("status", [403, 404, 500])
async def test_non_2xx_is_download_failure(monkeypatch, status) -> None:
    _mock(monkeypatch, lambda req: httpx.Response(status, content=b"nope"))

    prepared = await fetch_images([_src("thumbnailUrl")])
    [error] = prepared.errors
    assert isinstance(error, ImageDownloadFailed)
    assert error.code == ERROR_IMAGE_DOWNLOAD_FAILED


async def test_redirect_is_not_followed(monkeypatch) -> None:
    """리다이렉트를 따라가지 않는다 — 허용 검사를 통과한 주소가 다른 곳으로 튀는 길을 막는다."""
    seen = _mock(
        monkeypatch,
        lambda req: httpx.Response(302, headers={"location": "https://evil.example.com/x"}),
    )

    prepared = await fetch_images([_src("thumbnailUrl")])
    [error] = prepared.errors
    assert isinstance(error, ImageDownloadFailed)
    assert len(seen) == 1


async def test_transport_error_is_download_failure(monkeypatch) -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    _mock(monkeypatch, boom)

    prepared = await fetch_images([_src("thumbnailUrl")])
    [error] = prepared.errors
    assert isinstance(error, ImageDownloadFailed)
    assert error.code == ERROR_IMAGE_DOWNLOAD_FAILED


async def test_per_request_timeout_is_download_failure(monkeypatch) -> None:
    def slow(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    _mock(monkeypatch, slow)

    prepared = await fetch_images([_src("characters[0].images[0].imageUrl")])
    [error] = prepared.errors
    assert isinstance(error, ImageDownloadFailed)
    assert error.path == "characters[0].images[0].imageUrl"


async def test_overall_timeout_preserves_success_and_names_unfinished_image(monkeypatch) -> None:
    """전체 제한 시간을 넘겨도 완료된 정상 이미지와 끝나지 않은 이미지 오류를 함께 반환한다."""

    async def hang(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("fast.png"):
            return httpx.Response(200, content=PNG)
        await asyncio.sleep(5)
        return httpx.Response(200, content=PNG)

    _mock(monkeypatch, hang)
    monkeypatch.setattr(settings, "moderation_image_timeout", 0.05)

    prepared = await fetch_images([_src("thumbnailUrl", "fast.png"), _src("characters[0].images[0].imageUrl", "slow.png")])
    [error] = prepared.errors
    assert isinstance(error, ImageDownloadFailed)
    assert error.path == "characters[0].images[0].imageUrl"
    assert [image.path for image in prepared.images] == ["thumbnailUrl"]


# ── 사용자가 고칠 문제: IMAGE_INVALID ────────────────────────────────────────
@pytest.mark.parametrize(
    "data",
    [b"", b"<html>not an image</html>", b"BM\x00\x00bitmap", b"<svg xmlns='x'/>"],
)
async def test_non_image_or_unsupported_format_is_invalid(monkeypatch, data) -> None:
    _mock(monkeypatch, lambda req: httpx.Response(200, content=data))

    prepared = await fetch_images([_src("thumbnailUrl")])
    [error] = prepared.errors
    assert isinstance(error, ImageInvalid)
    assert error.code == ERROR_IMAGE_INVALID
    assert error.path == "thumbnailUrl"


async def test_oversized_body_is_invalid(monkeypatch) -> None:
    monkeypatch.setattr(settings, "moderation_image_max_bytes", 16)
    _mock(monkeypatch, lambda req: httpx.Response(200, content=PNG + b"x" * 32))

    prepared = await fetch_images([_src("thumbnailUrl")])
    [error] = prepared.errors
    assert isinstance(error, ImageInvalid)


async def test_declared_oversize_is_rejected_before_reading_body(monkeypatch) -> None:
    """Content-Length가 상한을 넘으면 본문을 받지 않고 끊는다."""
    monkeypatch.setattr(settings, "moderation_image_max_bytes", 16)
    _mock(
        monkeypatch,
        lambda req: httpx.Response(200, headers={"content-length": "1000000"}, content=PNG),
    )

    prepared = await fetch_images([_src("thumbnailUrl")])
    [error] = prepared.errors
    assert isinstance(error, ImageInvalid)


async def test_exactly_max_bytes_is_allowed(monkeypatch) -> None:
    data = PNG + b"x" * (32 - len(PNG))
    monkeypatch.setattr(settings, "moderation_image_max_bytes", 32)
    _mock(monkeypatch, lambda req: httpx.Response(200, content=data))

    [image] = (await fetch_images([_src("thumbnailUrl")])).images
    assert image.data == data


# ── 여러 장 실패 시 오류 전체 수집 ────────────────────────────────────────────────
async def test_all_failures_are_returned_in_input_order(monkeypatch) -> None:
    """파일 불량과 다운로드 실패를 모두 입력 순서대로 반환한다."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("bad.png"):
            return httpx.Response(200, content=b"not image")
        return httpx.Response(500)

    _mock(monkeypatch, handler)

    prepared = await fetch_images([_src("thumbnailUrl", "bad.png"), _src("characters[0].images[0].imageUrl", "err.png")])
    assert prepared.images == []
    assert [(error.path, error.code) for error in prepared.errors] == [
        ("thumbnailUrl", "IMAGE_INVALID"), ("characters[0].images[0].imageUrl", "IMAGE_DOWNLOAD_FAILED"),
    ]



async def test_error_message_does_not_contain_url(monkeypatch) -> None:
    """예외 문자열은 로그·Sentry로 흘러가므로 서빙 URL을 담지 않는다."""
    _mock(monkeypatch, lambda req: httpx.Response(404))

    prepared = await fetch_images([_src("thumbnailUrl", "secret-object-key.png")])
    [error] = prepared.errors
    assert isinstance(error, ImageDownloadFailed)
    assert "secret-object-key" not in str(error)
    assert HOST not in str(error)


@pytest.mark.parametrize("bad_first", [True, False])
@pytest.mark.parametrize("slow", [True, False])
async def test_invalid_and_download_failures_survive_timeouts_in_either_order(monkeypatch, bad_first, slow) -> None:
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

    prepared = await fetch_images(sources)
    assert prepared.images == []
    assert [error.path for error in prepared.errors] == [source.path for source in sources]
    assert {error.path: error.code for error in prepared.errors} == {
        "thumbnailUrl": "IMAGE_INVALID", "characters[0].images[0].imageUrl": "IMAGE_DOWNLOAD_FAILED",
    }



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
    assert [image.path for image in result.images] == [source.path for source in sources]
    assert active == 0


@pytest.mark.parametrize("limit, rejected", [(39, True), (40, False)])
async def test_total_image_limit_counts_base64_expansion(monkeypatch, limit, rejected) -> None:
    # PNG는 13바이트이므로 두 장의 원본 26바이트가 base64에서는 40바이트가 된다.
    monkeypatch.setattr(images, "MAX_REQUEST_BYTES", limit)
    _mock(monkeypatch, lambda req: httpx.Response(200, content=PNG))
    sources = [_src("thumbnailUrl"), _src("characters[0].images[0].imageUrl")]
    prepared = await fetch_images(sources)
    if rejected:
        assert prepared.images == []
        assert [error.path for error in prepared.errors] == [source.path for source in sources]
        assert all(isinstance(error, ImageInvalid) and "전체 전송 용량" in str(error) for error in prepared.errors)
    else:
        assert len(prepared.images) == 2
        assert prepared.errors == []


async def test_total_limit_stops_active_and_queued_downloads_before_reading_full_files(monkeypatch) -> None:
    monkeypatch.setattr(images, "MAX_REQUEST_BYTES", 80)
    monkeypatch.setattr(images, "_CHUNK_SIZE", 12)
    started = 0
    chunks = 0
    closed = 0
    all_started = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal started, chunks
            started += 1
            if started == 5:
                all_started.set()
            await all_started.wait()
            for _ in range(100):
                chunks += 1
                yield b"\x89PNG\r\n\x1a\nxxxx"
                await asyncio.sleep(0)

        async def aclose(self):
            nonlocal closed
            closed += 1

    _mock(monkeypatch, lambda req: httpx.Response(200, stream=Stream()))
    sources = [_src(f"characters[{i}].images[0].imageUrl") for i in range(12)]
    prepared = await asyncio.wait_for(fetch_images(sources), timeout=2)

    # 원본 12바이트는 base64 16바이트. 다섯 청크까지만 보관하고 여섯 번째에서 중단한다.
    assert chunks == 6
    assert started == closed == 5
    assert prepared.images == []
    assert [error.path for error in prepared.errors] == [source.path for source in sources]
    assert all(isinstance(error, ImageInvalid) for error in prepared.errors)


@pytest.mark.parametrize("broken_stream", [False, True])
async def test_failed_download_releases_budget_for_valid_images(monkeypatch, broken_stream) -> None:
    monkeypatch.setattr(images, "MAX_REQUEST_BYTES", 20)
    monkeypatch.setattr(images, "_MAX_CONCURRENT_DOWNLOADS", 1)
    monkeypatch.setattr(images, "_CHUNK_SIZE", 12)

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 12
            raise httpx.ReadError("synthetic")

    def handler(req):
        if req.url.path == "/bad.png":
            return httpx.Response(200, stream=BrokenStream()) if broken_stream else httpx.Response(200, content=b"x" * 12)
        return httpx.Response(200, content=PNG)

    _mock(monkeypatch, handler)
    sources = [_src("thumbnailUrl", "bad.png"), _src("characters[0].images[0].imageUrl")]
    prepared = await fetch_images(sources)

    assert [image.path for image in prepared.images] == [sources[1].path]
    assert [(error.path, error.code) for error in prepared.errors] == [
        (sources[0].path, "IMAGE_DOWNLOAD_FAILED" if broken_stream else "IMAGE_INVALID"),
    ]


async def test_total_limit_preserves_previously_detected_download_error(monkeypatch) -> None:
    monkeypatch.setattr(images, "MAX_REQUEST_BYTES", 39)
    monkeypatch.setattr(images, "_MAX_CONCURRENT_DOWNLOADS", 1)
    _mock(monkeypatch, lambda req: httpx.Response(404) if req.url.path == "/missing.png" else httpx.Response(200, content=PNG))
    sources = [_src("thumbnailUrl", "missing.png"), *[_src(f"characters[{i}].images[0].imageUrl") for i in range(3)]]

    prepared = await fetch_images(sources)

    assert prepared.images == []
    assert [error.path for error in prepared.errors] == [source.path for source in sources]
    assert [error.code for error in prepared.errors] == ["IMAGE_DOWNLOAD_FAILED", *["IMAGE_INVALID"] * 3]


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
    prepared = await fetch_images([_src(f"characters[{i}].images[0].imageUrl") for i in range(12)])
    assert len(prepared.errors) == 12
    assert all(isinstance(error, ImageDownloadFailed) for error in prepared.errors)
    assert prepared.images == []
    assert started == 5
    assert active == 0


async def test_success_and_all_errors_are_kept_despite_completion_order(monkeypatch) -> None:
    last_completed = asyncio.Event()

    async def handler(req):
        if req.url.path == "/first.png":
            await last_completed.wait()
            return httpx.Response(200, content=b"not an image")
        if req.url.path == "/last.png":
            last_completed.set()
            return httpx.Response(404)
        return httpx.Response(200, content=PNG)

    _mock(monkeypatch, handler)
    sources = [_src("thumbnailUrl", "first.png"), _src("characters[0].images[0].imageUrl", "good.png"),
               _src("characters[1].images[0].imageUrl", "last.png")]
    prepared = await fetch_images(sources)
    assert [image.path for image in prepared.images] == [sources[1].path]
    assert [(error.path, error.code) for error in prepared.errors] == [
        (sources[0].path, "IMAGE_INVALID"), (sources[2].path, "IMAGE_DOWNLOAD_FAILED"),
    ]
    assert all(error.__traceback__ is None for error in prepared.errors)


async def test_request_cancellation_cleans_up_downloads(monkeypatch) -> None:
    started = asyncio.Event()
    active = 0

    async def handler(req):
        nonlocal active
        active += 1
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    _mock(monkeypatch, handler)
    task = asyncio.create_task(fetch_images([_src(f"characters[{i}].images[0].imageUrl") for i in range(12)]))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert active == 0


async def test_programming_error_is_not_hidden_by_image_failure(monkeypatch) -> None:
    def handler(req):
        if req.url.path == "/bug.png":
            raise TypeError("bug")
        return httpx.Response(200, content=b"invalid")

    _mock(monkeypatch, handler)
    with pytest.raises(TypeError, match="bug"):
        await fetch_images([_src("thumbnailUrl", "bad.png"), _src("characters[0].images[0].imageUrl", "bug.png")])
