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
        docs = [
            root / "knk-harness/docs/domain" / domain / "AI" / filename
            for domain in ("STORY", "CHAT", "MODERATION")
            for filename in ("AGENTS.md", "CONTRACT.md")
        ]
        for doc in docs:
            doc.parent.mkdir(parents=True, exist_ok=True)
            doc.write_text("# AI document\n", encoding="utf-8")

        def check(expected: int, *args: str) -> None:
            result = subprocess.run(
                ["bash", str(script), *args], cwd=root, capture_output=True, text=True
            )
            assert result.returncode == expected, (expected, result.stdout, result.stderr)

        check(0, "--base", sha)
        check(3)
        check(2, "unexpected")
        check(2, "--base", "origin/dev")
        check(2, "--base")
        for doc in docs:
            doc.unlink()
            check(1, "--base", sha)
            doc.write_text("# AI document\n", encoding="utf-8")
        check(4, "--base", "0" * 12)
        for filename, expected in (("README.md", 11), ("src/change.py", 10)):
            file = repo / filename
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("# change\n", encoding="utf-8")
            git("add", "--", filename)
            git("-c", "commit.gpgsign=false", "commit", "-m", filename)
            git("push", "origin", "dev")
            check(expected, "--base", sha)
        git("remote", "set-url", "origin", str(root / "missing.git"))
        check(5, "--base", sha)
    print("PASS: new document paths, explicit baseline, change detection, and failure exit codes")


if __name__ == "__main__":
    main()
