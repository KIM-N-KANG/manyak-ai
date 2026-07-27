"""통로 경계 가드 — 호출부가 공통 통로를 우회하지 않는지 확인한다(KNK-672·673).

LLM 호출 개편의 목적은 "모델을 바꿀 때 호출부를 고치지 않는 것"이다. 그 목적은 호출부가
회사 SDK나 통로 내부(어댑터·등록부)를 직접 만지는 순간 조용히 무너진다 — **기능은 멀쩡히
돌기 때문에** 테스트도 리뷰도 놓치기 쉽다. 그래서 import 관계만 보고 구조를 고정한다.

이 파일이 막는 실제 상황: 급할 때 "통로 거치기 귀찮은데 여기서 그냥 직접 부르자"는 한 줄이
초록불로 들어오고, 나중에 모델을 바꿀 때 "왜 여기만 안 바뀌지?"로 되돌아오는 것.

**검사 대상을 손으로 적지 않는다.** `src/services/`의 모듈을 그때그때 훑는다 — 목록을 적어두면
새로 만든 호출부가 검사에서 통째로 빠지는데, 정작 새 코드가 옛 습관을 따라가기 쉽다.

**알려진 한계**: import 문만 본다. `importlib.import_module("...openai_sdk")`처럼 문자열로
가져오거나, 통로를 가져온 뒤 `llm.openai_sdk`로 속성을 타고 들어가면 잡지 못한다. 이 가드가
막으려는 것은 무심코 쓰는 한 줄이고 그건 평범한 import이므로, 문자열까지 쫓아 오탐을 늘리지
않는다(KNK-673 리뷰).
"""

import ast
from pathlib import Path

import pytest

import src.services

# 회사 SDK 패키지들. 새 공급자를 붙이면 여기에 더한다.
_COMPANY_SDKS = frozenset({"openai", "anthropic"})
# 통로 묶음(`src.services.llm`) 중 호출부가 써도 되는 것 — 공용 타입뿐이다.
# 어댑터(`openai_sdk` 등)와 등록부는 통로 내부라, 호출부가 직접 만지면 통로가 무의미해진다.
_ALLOWED_LLM_MEMBERS = frozenset({"base"})
_LLM_PACKAGE = "src.services.llm"

_SERVICES_PACKAGE = "src.services"
_SERVICES_DIR = Path(src.services.__file__).parent
# `src/services/`의 모듈 전부. 통로 자신(`llm/`)은 하위 디렉터리라 glob에 걸리지 않는다.
_CALL_SITE_FILES = sorted(p for p in _SERVICES_DIR.glob("*.py") if p.name != "__init__.py")


def _absolute_module(node: ast.ImportFrom, package: str) -> str | None:
    """`from ... import`의 모듈 이름을 절대 경로로 편다.

    상대 import(`from .llm import openai_sdk`)를 안 펴면 모듈 이름이 `llm`으로만 보여
    `src.services.llm.` 검사에 걸리지 않는다 — **점 하나 붙였다고 통과하는 구멍**이 된다
    (Codex 리뷰에서 변이로 확인).
    """
    if node.level == 0:
        return node.module
    parts = package.split(".")
    if node.level > len(parts):
        return None  # 패키지 밖으로 올라가는 import — 이 레포에는 없다
    base = ".".join(parts[: len(parts) - node.level + 1])
    return f"{base}.{node.module}" if node.module else base


def _imported_paths(module_file: Path, package: str) -> set[str]:
    """모듈이 가져오는 이름을 절대 점 경로로 모은다.

    `from A import B`는 A와 `A.B`를 **둘 다** 넣는다. 안 그러면
    `from src.services.llm import openai_sdk`가 "src.services.llm을 가져왔다"로만 보여
    어댑터를 직접 집어온 것을 놓친다(Codex 2차 리뷰에서 변이로 확인된 구멍).
    """
    tree = ast.parse(module_file.read_text(encoding="utf-8"))
    paths: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            paths.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = _absolute_module(node, package)
            if not module:
                continue
            paths.add(module)
            paths.update(f"{module}.{alias.name}" for alias in node.names)
    return paths


def _gateway_offenders(module_file: Path, package: str) -> list[str]:
    """이 모듈이 가져온 것 중 통로 우회에 해당하는 경로를 모은다(없으면 빈 목록)."""
    offenders = []
    for path in _imported_paths(module_file, package):
        if path.split(".")[0] in _COMPANY_SDKS:
            offenders.append(path)
        elif path.startswith(f"{_LLM_PACKAGE}."):
            member = path[len(_LLM_PACKAGE) + 1 :].split(".")[0]
            if member not in _ALLOWED_LLM_MEMBERS:
                offenders.append(path)
    return sorted(offenders)


def test_call_site_discovery_is_not_empty() -> None:
    """훑기가 0개를 집어오면 아래 검사가 전부 무의미하게 통과한다 — 그것부터 막는다."""
    names = {p.name for p in _CALL_SITE_FILES}
    assert {"chat_llm.py", "chat_choices.py", "chat_judgement.py", "story_llm.py"} <= names


@pytest.mark.parametrize("module_file", _CALL_SITE_FILES, ids=lambda p: p.stem)
def test_call_sites_use_only_the_gateway(module_file: Path) -> None:
    """호출부는 회사 SDK도, 통로 내부(어댑터·등록부)도 직접 쓰지 않는다.

    쓸 수 있는 것은 통로 자체(`src.services.llm`)와 공용 타입(`...llm.base`)뿐이다.
    """
    offenders = _gateway_offenders(module_file, _SERVICES_PACKAGE)
    assert not offenders, f"{module_file.name}이 통로를 우회한다: {offenders}"


def test_guard_actually_detects_a_bypass(tmp_path: Path) -> None:
    """가드 자체가 동작하는지 확인한다 — 통과만 보면 검사식이 망가져도 초록불이다.

    우회 네 종류(회사 SDK·어댑터·등록부·상대 경로 어댑터)를 모두 잡고, 허용된 두 가지는
    통과시켜야 한다.
    """
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from src.services import llm\n"  # 허용 — 통로
        "from src.services.llm.base import LlmRequest\n"  # 허용 — 공용 타입
        "from openai import AsyncOpenAI\n"  # 우회 — 회사 SDK
        "from src.services.llm import openai_sdk\n"  # 우회 — 어댑터
        "import src.services.llm.registry\n"  # 우회 — 등록부
        "from .llm import anthropic_sdk\n",  # 우회 — 상대 경로로 적은 어댑터
        encoding="utf-8",
    )

    assert _gateway_offenders(sample, _SERVICES_PACKAGE) == [
        "openai",
        "openai.AsyncOpenAI",
        f"{_LLM_PACKAGE}.anthropic_sdk",
        f"{_LLM_PACKAGE}.openai_sdk",
        f"{_LLM_PACKAGE}.registry",
    ]
