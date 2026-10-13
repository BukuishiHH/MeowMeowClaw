"""GM2 核心只读工具端到端单测(diff/log/show/blame/file_at_revision, 临时仓库)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meowmeowclaw_mcp_git.app import GitMcpApp
from meowmeowclaw_mcp_git.config import ServerConfig


def make_app(repo: Path, **overrides) -> GitMcpApp:
    return GitMcpApp(ServerConfig(roots=(str(repo),), **overrides))


async def call(app: GitMcpApp, name: str, **arguments) -> dict:
    return json.loads(await app.call_tool(name, arguments))


class TestDiff:
    @pytest.mark.asyncio
    async def test_worktree_mode_returns_files_and_patch(self, history_repo: Path):
        (history_repo / "README.md").write_text("hello changed\n", encoding="utf-8")

        payload = await call(make_app(history_repo), "git_diff")

        data = payload["data"]
        assert data["mode"] == "worktree"
        assert [item["path"] for item in data["files"]] == ["README.md"]
        assert data["file_count"] == 1
        assert "diff --git" in data["patch"]
        assert "-hello repo" in data["patch"]
        assert data["truncated"] is False

    @pytest.mark.asyncio
    async def test_staged_mode(self, history_repo: Path, git):
        (history_repo / "README.md").write_text("staged change\n", encoding="utf-8")
        git(history_repo, "add", "README.md")

        staged = await call(make_app(history_repo), "git_diff", mode="staged")
        worktree = await call(make_app(history_repo), "git_diff", mode="worktree")

        assert [item["path"] for item in staged["data"]["files"]] == ["README.md"]
        assert worktree["data"]["files"] == []

    @pytest.mark.asyncio
    async def test_commit_and_range_modes(self, history_repo: Path):
        app = make_app(history_repo)

        since = await call(app, "git_diff", mode="commit", base="HEAD~1", stat_only=True)
        between = await call(app, "git_diff", mode="range", base="HEAD~1", head="HEAD", stat_only=True)

        assert [item["path"] for item in since["data"]["files"]] == ["app.py"]
        assert [item["path"] for item in between["data"]["files"]] == ["app.py"]
        assert since["data"]["base"] == "HEAD~1" and since["data"]["head"] is None
        assert between["data"]["base"] == "HEAD~1" and between["data"]["head"] == "HEAD"

    @pytest.mark.asyncio
    async def test_paths_filter(self, history_repo: Path):
        (history_repo / "README.md").write_text("changed\n", encoding="utf-8")
        (history_repo / "app.py").write_text("changed too\n", encoding="utf-8")

        payload = await call(make_app(history_repo), "git_diff", paths=["README.md"])

        assert [item["path"] for item in payload["data"]["files"]] == ["README.md"]

    @pytest.mark.asyncio
    async def test_stat_only_omits_patch(self, history_repo: Path):
        (history_repo / "README.md").write_text("changed\n", encoding="utf-8")

        payload = await call(make_app(history_repo), "git_diff", stat_only=True)

        assert payload["data"]["stat_only"] is True
        assert "patch" not in payload["data"]

    @pytest.mark.asyncio
    async def test_patch_is_clipped_by_max_bytes(self, history_repo: Path):
        (history_repo / "README.md").write_text("changed\n" * 50, encoding="utf-8")

        payload = await call(make_app(history_repo), "git_diff", max_bytes=20)

        data = payload["data"]
        assert data["patch_truncated"] is True
        assert data["truncated"] is True
        assert len(data["patch"].encode("utf-8")) <= 20
        assert payload["meta"]["truncated"] is True

    @pytest.mark.asyncio
    async def test_mode_validation(self, history_repo: Path):
        app = make_app(history_repo)

        bad_worktree = await call(app, "git_diff", mode="worktree", base="HEAD")
        bad_range = await call(app, "git_diff", mode="range", base="HEAD")
        bad_mode = await call(app, "git_diff", mode="everything")

        assert bad_worktree["error_code"] == "invalid_argument"
        assert bad_range["error_code"] == "invalid_argument"
        assert bad_mode["error_code"] == "invalid_argument"

    @pytest.mark.asyncio
    async def test_bad_ref_is_command_failed(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_diff", mode="commit", base="no-such-ref")

        assert payload["ok"] is False
        assert payload["error_code"] == "command_failed"

    @pytest.mark.asyncio
    async def test_option_like_ref_rejected(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_diff", mode="range", base="HEAD", head="--output=/tmp/x")

        assert payload["error_code"] == "invalid_argument"
        assert "不能以 - 开头" in payload["message"]

    @pytest.mark.asyncio
    async def test_path_escape_rejected(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_diff", paths=["../outside"])

        assert payload["error_code"] == "invalid_path"
        assert payload["meta"]["repo"] == history_repo.name

    @pytest.mark.asyncio
    async def test_meta_records_numstat_command(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_diff", stat_only=True)

        assert payload["meta"]["command"][4:7] == ["diff", "--no-color", "--numstat"]


class TestLog:
    @pytest.mark.asyncio
    async def test_default_history(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_log")

        # main 上有 2 个提交(feature/login 的提交不在 main 历史里)
        data = payload["data"]
        assert data["count"] == 2
        assert data["has_more"] is False
        assert data["commits"][0]["subject"] == "fix: update greeting"
        assert data["commits"][0]["author"] == "Alice"
        assert "hash" in data["commits"][0] and "short_hash" in data["commits"][0]

    @pytest.mark.asyncio
    async def test_max_count_and_has_more(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_log", max_count=1)

        assert payload["data"]["count"] == 1
        assert payload["data"]["has_more"] is True
        assert payload["meta"]["has_more"] is True

    @pytest.mark.asyncio
    async def test_path_filter(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_log", path="app.py")

        assert [item["subject"] for item in payload["data"]["commits"]] == [
            "fix: update greeting",
            "feat: initial commit",
        ]

    @pytest.mark.asyncio
    async def test_author_and_time_filters(self, history_repo: Path):
        app = make_app(history_repo)

        by_author = await call(app, "git_log", author="Alice")
        no_author = await call(app, "git_log", author="Nobody")
        # 注意: git 对 2039 之后的日期会溢出, 这里用 2027(相对测试机时间仍是未来)
        since_future = await call(app, "git_log", since="2027-01-01")
        until_future = await call(app, "git_log", until="2027-01-01")

        assert by_author["data"]["count"] == 2
        assert no_author["data"]["count"] == 0
        assert since_future["data"]["count"] == 0
        assert until_future["data"]["count"] == 2

    @pytest.mark.asyncio
    async def test_grep_filter(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_log", grep="greeting")

        assert payload["data"]["count"] == 1
        assert payload["data"]["commits"][0]["subject"] == "fix: update greeting"

    @pytest.mark.asyncio
    async def test_bounds_are_enforced_by_schema(self, history_repo: Path):
        app = make_app(history_repo)

        zero = await call(app, "git_log", max_count=0)
        too_many = await call(app, "git_log", max_count=5000)
        bad_type = await call(app, "git_log", max_count="ten")

        assert zero["error_code"] == "invalid_argument"
        assert too_many["error_code"] == "invalid_argument"
        assert bad_type["error_code"] == "invalid_argument"

    @pytest.mark.asyncio
    async def test_path_escape_rejected(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_log", path="../../etc/passwd")

        assert payload["error_code"] == "invalid_path"


class TestShow:
    @pytest.mark.asyncio
    async def test_commit_metadata_and_files(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_show", ref="HEAD", stat_only=True)

        data = payload["data"]
        assert data["commit"]["subject"] == "fix: update greeting"
        assert data["commit"]["author"] == "Alice"
        assert [item["path"] for item in data["files"]] == ["app.py"]
        assert data["stat_only"] is True and "patch" not in data

    @pytest.mark.asyncio
    async def test_patch_returned_by_default_and_clipped(self, history_repo: Path):
        app = make_app(history_repo)

        full = await call(app, "git_show", ref="HEAD")
        clipped = await call(app, "git_show", ref="HEAD", max_bytes=12)

        assert "diff --git" in full["data"]["patch"]
        assert clipped["data"]["patch_truncated"] is True
        assert len(clipped["data"]["patch"].encode("utf-8")) <= 12

    @pytest.mark.asyncio
    async def test_annotated_tag_dereferences_to_commit(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_show", ref="v0.2", stat_only=True)

        assert payload["data"]["commit"]["subject"] == "feat: initial commit"

    @pytest.mark.asyncio
    async def test_bad_ref_and_blob_ref_fail_stably(self, history_repo: Path):
        app = make_app(history_repo)

        missing = await call(app, "git_show", ref="no-such-ref")
        blob = await call(app, "git_show", ref="HEAD:app.py")

        assert missing["error_code"] == "command_failed"
        assert blob["error_code"] == "command_failed"

    @pytest.mark.asyncio
    async def test_missing_ref_argument(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_show")

        assert payload["error_code"] == "invalid_argument"
        assert "ref" in payload["message"]


class TestBlame:
    @pytest.mark.asyncio
    async def test_explicit_range(self, history_repo: Path):
        payload = await call(
            make_app(history_repo), "git_blame", file="app.py", start_line=1, end_line=2
        )

        lines = payload["data"]["lines"]
        assert [line["line"] for line in lines] == [1, 2]
        assert lines[0]["content"] == "def main():"
        assert lines[0]["author"] == "Alice"
        assert lines[0]["hash"]
        assert payload["data"]["truncated"] is False

    @pytest.mark.asyncio
    async def test_default_range_uses_max_lines_and_marks_truncated(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_blame", file="app.py", max_lines=1)

        data = payload["data"]
        assert data["count"] == 1
        assert data["truncated"] is True

    @pytest.mark.asyncio
    async def test_start_line(self, history_repo: Path):
        payload = await call(
            make_app(history_repo), "git_blame", file="app.py", start_line=2, end_line=2
        )

        assert payload["data"]["lines"][0]["content"].strip() == 'print("hello world")'
        assert payload["data"]["lines"][0]["line"] == 2

    @pytest.mark.asyncio
    async def test_invalid_range(self, history_repo: Path):
        payload = await call(
            make_app(history_repo), "git_blame", file="app.py", start_line=3, end_line=1
        )

        assert payload["error_code"] == "invalid_argument"

    @pytest.mark.asyncio
    async def test_path_escape_and_missing_file(self, history_repo: Path):
        app = make_app(history_repo)

        escape = await call(app, "git_blame", file="../../etc/passwd")
        missing = await call(app, "git_blame", file="no/such/file.py")

        assert escape["error_code"] == "invalid_path"
        assert missing["error_code"] == "command_failed"


class TestFileAtRevision:
    @pytest.mark.asyncio
    async def test_text_content_with_encoding_and_size(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_file_at_revision", ref="HEAD", path="app.py")

        data = payload["data"]
        assert data["encoding"] == "utf-8"
        assert data["binary"] is False
        assert "hello world" in data["text"]
        assert data["size"] == len(data["text"].encode("utf-8"))
        assert data["truncated"] is False

    @pytest.mark.asyncio
    async def test_previous_revision(self, history_repo: Path):
        payload = await call(
            make_app(history_repo), "git_file_at_revision", ref="HEAD~1", path="app.py"
        )

        assert "world" not in payload["data"]["text"]

    @pytest.mark.asyncio
    async def test_chinese_utf8_and_gb18030(self, history_repo: Path):
        app = make_app(history_repo)

        utf8 = await call(app, "git_file_at_revision", ref="HEAD", path="data/中文.txt")
        gbk = await call(app, "git_file_at_revision", ref="HEAD", path="data/gbk.txt")

        assert utf8["data"]["encoding"] == "utf-8"
        assert "你好" in utf8["data"]["text"]
        assert gbk["data"]["encoding"] == "gb18030"
        assert "你好" in gbk["data"]["text"]

    @pytest.mark.asyncio
    async def test_binary_file(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_file_at_revision", ref="HEAD", path="bin.dat")

        data = payload["data"]
        assert data["binary"] is True
        assert data["text"] is None
        assert data["encoding"] is None
        assert data["size"] == 15

    @pytest.mark.asyncio
    async def test_truncation(self, history_repo: Path):
        payload = await call(
            make_app(history_repo), "git_file_at_revision", ref="HEAD", path="app.py", max_bytes=5
        )

        data = payload["data"]
        assert data["truncated"] is True
        assert len(data["text"].encode("utf-8")) <= 5
        assert data["size"] > 5
        assert payload["meta"]["truncated"] is True

    @pytest.mark.asyncio
    async def test_missing_path_and_bad_ref(self, history_repo: Path):
        app = make_app(history_repo)

        missing = await call(app, "git_file_at_revision", ref="HEAD", path="nope.txt")
        bad_ref = await call(app, "git_file_at_revision", ref="nope", path="app.py")

        assert missing["error_code"] == "command_failed"
        assert bad_ref["error_code"] == "command_failed"

    @pytest.mark.asyncio
    async def test_ref_with_colon_and_option_like_rejected(self, history_repo: Path):
        app = make_app(history_repo)

        colon = await call(app, "git_file_at_revision", ref="HEAD:app.py", path="app.py")
        option = await call(app, "git_file_at_revision", ref="--output=/tmp/x", path="app.py")

        assert colon["error_code"] == "invalid_argument"
        assert option["error_code"] == "invalid_argument"

    @pytest.mark.asyncio
    async def test_path_escape_rejected(self, history_repo: Path):
        payload = await call(make_app(history_repo), "git_file_at_revision", ref="HEAD", path="../../etc/passwd")

        assert payload["error_code"] == "invalid_path"
        assert payload["meta"]["repo"] == history_repo.name
