"""KNK-1770: 승인·반려 라벨이 붙은 이미지 데이터셋으로 실제 검수 모델의 판정을 확인한다.

레포가 공개라 이미지는 커밋하지 않고 `evaluation/moderation/datasets/images/{approve,reject}/`에
로컬로 둔다. 다운로드 단계만 그 파일을 읽도록 바꾸고 프롬프트·모델 호출·응답 해석은 실제 경로를 탄다.
이미지 한 장이 검수 호출 1회 이상이라 RUN_MODERATION_DATASET=1일 때만 돈다 —
릴리스 QA(qa.sh)는 운영(main) 대비 모더레이션이 바뀐 릴리스에서만 이 스위치를 켠다.
"""

import asyncio
import os
from pathlib import Path

import pytest

from src.services.moderation import service
from src.services.moderation.images import ImageInvalid, ModerationImage, PreparedImages, detect_content_type
from src.services.moderation.models import ModerationResult

DATASET = Path(__file__).resolve().parents[2] / "evaluation" / "moderation" / "datasets" / "images"
EXPECTED = {"approve": "APPROVED", "reject": "REJECTED"}
# 반려 이미지 통과는 한 장도 허용하지 않는다. 승인 이미지 오반려 허용 수는 첫 실측 결과로 정한다.
MAX_FALSE_REJECTS = 0
CONCURRENCY = 5


@pytest.fixture(autouse=True)
def require_dataset_env() -> None:
    if os.getenv("RUN_LIVE_TESTS") != "1":
        pytest.skip("RUN_LIVE_TESTS=1이 아니면 라이브 통합 테스트를 건너뜁니다")
    if os.getenv("RUN_MODERATION_DATASET") != "1":
        pytest.skip("RUN_MODERATION_DATASET=1이 아니면 검수 데이터셋 테스트를 건너뜁니다")


def load_dataset() -> list[tuple[Path, str]]:
    return [
        (path, expected)
        for label, expected in EXPECTED.items()
        for path in sorted((DATASET / label).glob("*"))
        if path.is_file() and not path.name.startswith(".")
    ]


async def test_moderation_dataset_live(monkeypatch) -> None:
    samples = load_dataset()
    # 스위치를 켰는데 데이터가 없으면 건너뛰지 않고 실패한다 — 건너뛰면 QA가 검수 확인 없이 통과한다.
    assert {expected for _, expected in samples} == set(EXPECTED.values()), (
        f"검수 데이터셋이 없습니다: {DATASET}/approve, reject 폴더에 이미지를 넣으세요"
    )

    # 게시물 JSON이 프롬프트에 그대로 들어가므로 파일명(라벨 힌트)이 아닌 번호 주소를 쓴다.
    files = {f"https://moderation-dataset.test/{index}": path for index, (path, _) in enumerate(samples)}

    async def fetch_local(sources, **kwargs):
        images, errors = [], []
        for source in sources:
            data = files[source.url].read_bytes()
            content_type = detect_content_type(data)
            if content_type is None:
                errors.append(ImageInvalid(source.path, "이미지가 아니거나 지원하지 않는 형식"))
            else:
                images.append(ModerationImage(source.path, content_type, data))
        return PreparedImages(images=images, errors=errors)

    monkeypatch.setattr(service, "fetch_images", fetch_local)
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def moderate(url: str) -> ModerationResult:
        # 검수 제한 시간은 moderate_story 진입부터 재므로 세마포어를 얻은 뒤에 부른다.
        async with semaphore:
            return await service.moderate_story({"thumbnailUrl": url})

    results = await asyncio.gather(*(moderate(url) for url in files))

    errors, missed, false_rejects = [], [], []
    for (path, expected), result in zip(samples, results):
        name = f"{path.parent.name}/{path.name}"
        if result.error_code is not None:
            errors.append(f"{name}: {result.error_code}")
        elif expected == "REJECTED" and result.decision == "APPROVED":
            missed.append(name)
        elif expected == "APPROVED" and result.decision == "REJECTED":
            reasons = "; ".join(f"{issue.rule} {issue.reason}" for issue in result.issues)
            false_rejects.append(f"{name}: {reasons}")

    report = "\n".join([
        f"검수 데이터셋 {len(samples)}장 — 판정 불가 {len(errors)} · 반려 누락 {len(missed)} · "
        f"승인 오반려 {len(false_rejects)}(허용 {MAX_FALSE_REJECTS})",
        *(f"  [판정 불가] {line}" for line in errors),
        *(f"  [반려 누락] {line}" for line in missed),
        *(f"  [승인 오반려] {line}" for line in false_rejects),
    ])
    assert not errors and not missed and len(false_rejects) <= MAX_FALSE_REJECTS, report
