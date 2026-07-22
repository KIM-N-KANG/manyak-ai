#!/usr/bin/env python3
"""PreToolUse(Bash) 훅 — `git clean -x/-X`를 기계적으로 차단한다.

왜 지침이 아니라 훅인가:
    이 레포는 로컬 전용 자산이 많다(experiment/·scripts/·references/·스킬·.env
    — 2026-07-22 기준 삭제 대상 50개). 전부 gitignore 대상이라 `git clean -Xf`
    한 번에 사라지고, git에 없으니 되살릴 방법이 없다.
    "쓰지 마세요"는 부탁이고, 이 훅은 실제로 막는다.

왜 정규식이 아니라 토큰화인가:
    정규식판은 `"git" clean -fx`(따옴표)·`git -C "/경로 띄어쓰기" clean -fx`·
    `git clean -fx -- -n`(`--` 뒤 -n은 옵션이 아니라 파일 이름)을 놓쳤고,
    반대로 `echo git clean -fx`와 주석을 막았다. 그래서 셸이 하는 것처럼
    토큰으로 쪼개 **명령 자리에 있는 git만** 본다.

통과시키는 것: -n / --dry-run — 무엇이 지워질지 보는 건 안전하고 오히려 권장한다.
차단 방법: PreToolUse에서 exit 2 = 도구 호출 차단, stderr가 Claude에게 전달된다.
"""

import json
import os
import re
import shlex
import sys

# 명령 앞에 붙어도 실제 실행 대상이 뒤에 오는 것들.
WRAPPERS = {"sudo", "doas", "command", "env", "nohup", "time", "builtin", "exec"}
# 값을 따로 받는 git 전역 옵션 — 뒤 토큰까지 건너뛰어야 서브커맨드를 제대로 만난다.
GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                       "--exec-path", "--config-env", "--super-prefix"}
ENV_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z_0-9]*=")

MESSAGE = """차단됨: 이 레포에서 `git clean -x/-X`는 금지입니다.

무시 대상(experiment/·scripts/·references/·스킬·.env 등 약 50개)이 한 번에 지워지는데,
git에 없으니 복구 수단이 없습니다.

- 무엇이 지워질지 보려면: git clean -ndX      (dry-run은 허용됩니다)
- 빌드 찌꺼기만 치우려면: rm -rf .pytest_cache .coverage

정말 필요하면 사용자에게 알리고, 사용자가 직접 실행하게 하세요.
"""


def _segments(line: str):
    """한 줄을 셸 연산자로 나눠 '명령 하나 = 토큰 목록' 단위로 돌려준다."""
    lex = shlex.shlex(line, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = "#"          # 주석은 통째로 버린다(오탐 방지)
    segments, current = [], []
    for token in lex:             # 따옴표 불균형이면 ValueError — 호출부에서 받는다
        if token and all(ch in lex.punctuation_chars for ch in token):
            segments.append(current)   # ; && || | & ( ) < > 는 명령 경계로 본다
            current = []
        else:
            current.append(token)
    segments.append(current)
    return segments


def _is_destructive_clean(tokens) -> bool:
    """이 토큰 목록이 '되돌릴 수 없는 git clean'인지 판단한다."""
    i = 0
    while i < len(tokens):                       # 앞의 환경변수 대입·래퍼 건너뛰기
        token = tokens[i]
        if ENV_ASSIGN.match(token) or os.path.basename(token) in WRAPPERS:
            i += 1
            continue
        break
    if i >= len(tokens) or os.path.basename(tokens[i]) != "git":
        return False                             # 명령 자리에 git이 없다 = 우리 일이 아니다
    i += 1

    while i < len(tokens):                       # git 전역 옵션 건너뛰기
        token = tokens[i]
        if token in GIT_OPTS_WITH_VALUE:
            i += 2                               # 값(-C <경로>)까지 함께 넘긴다
        elif token.startswith("-"):
            i += 1
        else:
            break
    if i >= len(tokens) or tokens[i] != "clean":
        return False
    i += 1

    has_xX = has_dry = False
    for token in tokens[i:]:
        if token == "--":
            break                                # 이후는 전부 pathspec — 옵션으로 읽지 않는다
        if token.startswith("--"):
            if token == "--dry-run":
                has_dry = True
        elif token.startswith("-") and len(token) > 1:
            body = token[1:]                     # 묶음 플래그(-fdX)라 글자 단위로 본다
            has_xX = has_xX or "x" in body or "X" in body
            has_dry = has_dry or "n" in body
    return has_xX and not has_dry


def _fallback_blocked(command: str) -> bool:
    """토큰화가 실패했을 때(따옴표 불균형 등) 쓰는 보수적 판정 — 애매하면 막는다."""
    if "clean" not in command:
        return False
    if "--dry-run" in command:
        return False
    return re.search(r"(?<!\S)-[A-Za-z]*[xX]", command) is not None


def is_blocked(command: str) -> bool:
    for line in command.splitlines():            # 개행도 명령 경계다
        line = line.replace("`", " ")            # 백틱 치환은 명령 자리를 가리므로 벗겨 본다
        try:
            segments = _segments(line)
        except ValueError:
            if _fallback_blocked(line):
                return True
            continue
        for tokens in segments:
            if tokens and _is_destructive_clean(tokens):
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
            # 막아야 하는 것
            ("git clean -xdf", True), ("git clean -fdX", True), ("git clean -x", True),
            ("git clean --force -X", True), ("git -C /repo clean -xdf", True),
            ("sudo git clean -xdf", True), ("make build && git clean -xdf", True),
            ('"git" clean -fx', True), ('git -C "/tmp/repo with spaces" clean -fx', True),
            ("git clean -fx -- -n", True), ("git clean -fx -- --dry-run", True),
            ("/usr/bin/git clean -fX", True), ("GIT_DIR=/x git clean -fx", True),
            ("cd /repo\ngit clean -fx", True), ("$(git clean -fx)", True),
            ("git -c core.x=1 clean -fx", True),
            # 통과시켜야 하는 것
            ("git clean -ndX", False), ("git clean -n -x", False),
            ("git clean --dry-run -X", False), ("git clean -fd", False),
            ("git status", False), ("rm -rf .pytest_cache", False),
            ("echo git clean -fx", False), ("# git clean -fx 하지 말 것", False),
            ("grep -x clean file", False), ("git stash clean", False),
        ]
        bad = 0
        for cmd, want in cases:
            got = is_blocked(cmd)
            mark = "OK " if got == want else "실패"
            if got != want:
                bad += 1
            print(f"{mark} {cmd!r:<42} 차단={got} (기대={want})")
        print("자체 점검 " + ("통과" if bad == 0 else f"실패 {bad}건"))
        sys.exit(1 if bad else 0)
    sys.exit(main())
