#!/usr/bin/env python3
"""PreToolUse(Bash) 훅 — `git clean -x/-X`를 기계적으로 차단한다.

왜 지침이 아니라 훅인가:
    이 레포는 로컬 전용 자산이 많다(experiment/·scripts/·references/·스킬·.env
    — 2026-07-22 기준 삭제 대상 50개). 전부 gitignore 대상이라 `git clean -Xf`
    한 번에 사라지고, git에 없으니 되살릴 방법이 없다.
    "쓰지 마세요"는 부탁이고, 이 훅은 실제로 막는다.

통과시키는 것: -n / --dry-run — 무엇이 지워질지 보는 건 안전하고 오히려 권장한다.
차단 방법: PreToolUse에서 exit 2 = 도구 호출 차단, stderr가 Claude에게 전달된다.
"""

import json
import os
import re
import sys

# `git clean` / `git -C path clean` / `sudo git clean` 을 모두 잡는다.
# rest는 그 명령 안의 플래그만 보도록 구분자(; && || | 개행) 앞까지만 취한다.
GIT_CLEAN = re.compile(r"\bgit\b(?:\s+-\S+(?:\s+\S+)?)*\s+clean\b(?P<rest>[^;&|\n]*)")
FLAG = re.compile(r"(?<!\S)--?[A-Za-z][A-Za-z-]*")

MESSAGE = """차단됨: 이 레포에서 `git clean -x/-X`는 금지입니다.

무시 대상(experiment/·scripts/·references/·스킬·.env 등 약 50개)이 한 번에 지워지는데,
git에 없으니 복구 수단이 없습니다.

- 무엇이 지워질지 보려면: git clean -ndX      (dry-run은 허용됩니다)
- 빌드 찌꺼기만 치우려면: rm -rf .pytest_cache .coverage

정말 필요하면 사용자에게 알리고, 사용자가 직접 실행하게 하세요.
"""


def is_blocked(command: str) -> bool:
    for match in GIT_CLEAN.finditer(command):
        flags = FLAG.findall(match.group("rest"))
        # 묶음 플래그(-fdX)도 잡아야 하므로 글자 단위로 본다. 긴 옵션은 통째로 비교한다.
        short = [f[1:] for f in flags if not f.startswith("--")]
        has_xX = any("x" in s or "X" in s for s in short)
        has_dry = "n" in "".join(short) or "--dry-run" in flags
        if has_xX and not has_dry:
            return True
    return False


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        # 훅이 입력을 못 읽으면 통과시킨다 — 훅 고장으로 작업 전체가 막히면 안 된다.
        return 0

    command = (payload.get("tool_input") or {}).get("command") or ""
    if is_blocked(command):
        sys.stderr.write(MESSAGE)
        return 2
    return 0


if __name__ == "__main__":
    # 자체 점검: --selftest로 부르면 대표 사례를 검사하고 결과를 뱉는다(훅 등록과 무관).
    if "--selftest" in sys.argv:
        cases = [
            ("git clean -xdf", True), ("git clean -fdX", True), ("git clean -x", True),
            ("git clean --force -X", True), ("git -C /repo clean -xdf", True),
            ("sudo git clean -xdf", True), ("make build && git clean -xdf", True),
            ("git clean -ndX", False), ("git clean -n -x", False),
            ("git clean --dry-run -X", False), ("git clean -fd", False),
            ("git status", False), ("rm -rf .pytest_cache", False),
        ]
        bad = 0
        for cmd, want in cases:
            got = is_blocked(cmd)
            mark = "OK " if got == want else "실패"
            if got != want:
                bad += 1
            print(f"{mark} {cmd:<34} 차단={got} (기대={want})")
        print("자체 점검 " + ("통과" if bad == 0 else f"실패 {bad}건"))
        sys.exit(1 if bad else 0)
    sys.exit(main())
