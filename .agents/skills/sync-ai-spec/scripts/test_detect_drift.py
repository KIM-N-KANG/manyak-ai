"""Run with: python3 .agents/skills/sync-ai-spec/scripts/test_detect_drift.py"""

import shutil
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        repo = root / "manyak-ai"
        repo.mkdir()

        def git(*args: str) -> str:
            return subprocess.check_output(
                ["git", "-C", str(repo), *args], stderr=subprocess.STDOUT, text=True
            ).strip()

        git("init", "-b", "dev")
        git("config", "user.name", "Drift test")
        git("config", "user.email", "drift@example.invalid")
        git("-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "baseline")
        sha = git("rev-parse", "HEAD")
        # A local origin keeps the check independent of credentials and network access.
        git("clone", "--bare", str(repo), str(root / "origin.git"))
        git("remote", "add", "origin", str(root / "origin.git"))
        script = repo / ".agents/skills/sync-ai-spec/scripts/detect-drift.sh"
        script.parent.mkdir(parents=True)
        shutil.copyfile(Path(__file__).with_name("detect-drift.sh"), script)
        docs = [root / "knk-harness/docs" / name for name in (
            "spec/5-ai-server-spec.md", "design/3-ai-server-design.md", "adr/3-ai-server-adr.md"
        )]
        metadata = f"# 5-ai-server-spec\n\n## 문서 정보\n\n| 기준 코드 | dev 브랜치 `{sha[:12]}` |\n"
        for doc in docs:
            doc.parent.mkdir(parents=True, exist_ok=True)
            doc.write_text(metadata, encoding="utf-8")

        def check(expected: int, *args: str) -> None:
            result = subprocess.run(
                ["bash", str(script), *args], cwd=root, capture_output=True, text=True
            )
            assert result.returncode == expected, (expected, result.stdout, result.stderr)

        check(0)
        check(2, "unexpected")
        for doc in docs:
            doc.unlink()
            check(1)
            doc.write_text(metadata, encoding="utf-8")
        docs[0].write_text("# Missing metadata\n", encoding="utf-8")
        check(3)
        docs[0].write_text(metadata.replace(sha[:12], "0" * 12), encoding="utf-8")
        check(4)
        docs[0].write_text(metadata, encoding="utf-8")
        for filename, expected in (("README.md", 11), ("src/change.py", 10)):
            file = repo / filename
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("# change\n", encoding="utf-8")
            git("add", "--", filename)
            git("-c", "commit.gpgsign=false", "commit", "-m", filename)
            git("push", "origin", "dev")
            check(expected)
        git("remote", "set-url", "origin", str(root / "missing.git"))
        check(5)
    print("PASS: new document paths, metadata, change detection, and failure exit codes")


if __name__ == "__main__":
    main()
