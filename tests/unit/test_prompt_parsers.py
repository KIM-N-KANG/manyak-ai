"""프롬프트 파서 3종의 유닛 테스트 (KNK-574 감사 1-2 보강).

파서 계층은 이 레포에서 가장 자주 바뀌는 자산인 프롬프트 파일을 읽는 입구인데
그동안 테스트가 없었다. tmp_path에 픽스처 파일을 만들어 파서를 직접 때리고,
특히 CRLF(윈도우식)로 저장된 파일에서 frontmatter가 본문에 새지 않음을 고정한다.

- read_version   : frontmatter의 version을 읽고, 없으면 기동 실패(RuntimeError).
- _load_template : `## [SYSTEM]`/`## [USER]` 마커로 분할, 누락 시 기동 실패.
- _body_only     : frontmatter를 떼고 본문만 — LF/CRLF 모두 허용해야 한다.
"""

import pytest

from src.services import chat_choices, prompt
from src.services.chat_assembler import _body_only
from src.services.prompt_meta import read_version


def _write(tmp_path, name: str, content: str):
    """줄바꿈(LF/CRLF)·BOM을 그대로 보존하려고 bytes로 쓴다(text 모드 변환 회피)."""
    path = tmp_path / name
    path.write_bytes(content.encode("utf-8"))
    return path


# ── read_version ─────────────────────────────────────────────────────────────
def test_read_version_lf(tmp_path) -> None:
    path = _write(tmp_path, "lf.md", "---\nlayer: CORE\nversion: 3\n---\n본문")
    assert read_version(path) == 3


def test_read_version_crlf(tmp_path) -> None:
    """윈도우식 줄바꿈으로 저장돼도 version을 읽어야 한다."""
    path = _write(tmp_path, "crlf.md", "---\r\nlayer: CORE\r\nversion: 5\r\n---\r\n본문")
    assert read_version(path) == 5


def test_read_version_bom(tmp_path) -> None:
    """맨 앞 BOM이 있어도 frontmatter를 인식해야 한다(utf-8-sig)."""
    path = _write(tmp_path, "bom.md", "﻿---\nversion: 7\n---\n본문")
    assert read_version(path) == 7


def test_read_version_missing_version_raises(tmp_path) -> None:
    """frontmatter는 있으나 version 줄이 없으면 기동 시점에 드러낸다."""
    path = _write(tmp_path, "no_version.md", "---\nlayer: CORE\n---\n본문")
    with pytest.raises(RuntimeError):
        read_version(path)


def test_read_version_missing_frontmatter_raises(tmp_path) -> None:
    """frontmatter 블록 자체가 없으면 RuntimeError."""
    path = _write(tmp_path, "no_front.md", "version: 1\n본문만 있음")
    with pytest.raises(RuntimeError):
        read_version(path)


# ── _load_template ───────────────────────────────────────────────────────────
_TEMPLATE_OK = "머리말\n## [SYSTEM]\n너는 작가다.\n## [USER]\n장르: {{장르}}"


@pytest.mark.parametrize("load", [prompt._load_template, chat_choices._load_template])
def test_load_template_splits_system_and_user(load, tmp_path) -> None:
    path = _write(tmp_path, "tmpl.md", _TEMPLATE_OK)
    system, user = load(path)
    assert system == "너는 작가다."
    assert user == "장르: {{장르}}"


@pytest.mark.parametrize("load", [prompt._load_template, chat_choices._load_template])
def test_load_template_missing_user_marker_raises(load, tmp_path) -> None:
    path = _write(tmp_path, "no_user.md", "## [SYSTEM]\n너는 작가다.")
    with pytest.raises(RuntimeError):
        load(path)


@pytest.mark.parametrize("load", [prompt._load_template, chat_choices._load_template])
def test_load_template_missing_system_marker_raises(load, tmp_path) -> None:
    path = _write(tmp_path, "no_system.md", "## [USER]\n장르: {{장르}}")
    with pytest.raises(RuntimeError):
        load(path)


# ── _body_only ───────────────────────────────────────────────────────────────
def test_body_only_strips_frontmatter_lf() -> None:
    assert _body_only("---\nlayer: CORE\nversion: 1\n---\n본문 시작") == "본문 시작"


def test_body_only_strips_frontmatter_crlf() -> None:
    """CRLF로 저장된 템플릿도 frontmatter가 잘려 본문만 남아야 한다(누출 방지)."""
    body = _body_only("---\r\nlayer: CORE\r\nversion: 1\r\n---\r\n본문 시작")
    assert body == "본문 시작"
    assert "layer" not in body  # frontmatter가 시스템 프롬프트에 새지 않음


def test_body_only_no_frontmatter_passthrough() -> None:
    """frontmatter가 없으면 원문(공백만 정리)을 그대로 반환한다."""
    assert _body_only("frontmatter 없는 본문") == "frontmatter 없는 본문"
