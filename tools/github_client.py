from __future__ import annotations

import logging
import re
import difflib
from typing import List

from pydantic import BaseModel, Field
from github import Auth, Github, GithubException

from agents.base import FileDiff as AgentFileDiff
from config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

_EXT_TO_LANG: dict[str, str] = {
    "py": "python", "js": "javascript", "ts": "typescript",
    "jsx": "javascript", "tsx": "typescript", "java": "java",
    "go": "go", "rb": "ruby", "rs": "rust", "cpp": "cpp",
    "cc": "cpp", "cxx": "cpp", "c": "c", "h": "c",
    "cs": "csharp", "php": "php", "swift": "swift",
    "kt": "kotlin", "scala": "scala", "sh": "bash",
    "bash": "bash", "zsh": "bash", "sql": "sql",
    "html": "html", "css": "css", "scss": "scss",
    "json": "json", "yaml": "yaml", "yml": "yaml",
    "toml": "toml", "md": "markdown",
}


def _detect_language(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _EXT_TO_LANG.get(ext, "unknown")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class FileDiff(AgentFileDiff):
    patch: str


class PRDiff(BaseModel):
    files: List[FileDiff] = []
    pr_title: str = ""
    pr_description: str = ""
    author: str = ""
    base_sha: str = ""
    head_sha: str = ""
    merge_base_sha: str = ""
    metadata: dict = Field(default_factory=dict)
    skipped_files: dict[str, str] = Field(default_factory=dict)

# ---------------------------------------------------------------------------
# Patch parsing
# ---------------------------------------------------------------------------

def _parse_patch(patch: str) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Extract added and removed lines with their line numbers from a unified diff patch."""
    added: list[tuple[int, str]] = []
    removed: list[tuple[int, str]] = []
    if not patch:
        return added, removed

    new_lineno = 0
    old_lineno = 0
    in_hunk = False

    for line in patch.splitlines():
        if line.startswith("@@"):
            # e.g. @@ -10,7 +10,8 @@
            m = re.search(r"@@ -(?P<old>\d+)(?:,\d+)? \+(?P<new>\d+)(?:,\d+)? @@", line)
            if m:
                in_hunk = True
                old_lineno = int(m.group("old")) - 1
                new_lineno = int(m.group("new")) - 1
        elif not in_hunk:
            continue
        elif line.startswith("\\"):
            # The no-newline marker does not consume a source line.
            continue
        elif line.startswith("+"):
            new_lineno += 1
            added.append((new_lineno, line[1:]))
        elif line.startswith("-"):
            old_lineno += 1
            removed.append((old_lineno, line[1:]))
        else:
            old_lineno += 1
            new_lineno += 1

    return added, removed


# ---------------------------------------------------------------------------
# PR URL parsing
# ---------------------------------------------------------------------------

_PR_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)


def _parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    m = _PR_URL_RE.match(pr_url.strip())
    if not m:
        raise ValueError(f"Invalid GitHub PR URL: {pr_url!r}")
    return m.group("owner"), m.group("repo"), int(m.group("number"))


# ---------------------------------------------------------------------------
# GitHubClient
# ---------------------------------------------------------------------------

class GitHubClient:
    """Thin wrapper around PyGithub for PR-related operations."""

    def __init__(self, token: str | None = None) -> None:
        self._token = token or settings.GITHUB_TOKEN
        self._gh = Github(auth=Auth.Token(self._token)) if self._token else Github()

    def _get_pr(self, pr_url: str):
        owner, repo, number = _parse_pr_url(pr_url)
        return self._gh.get_repo(f"{owner}/{repo}").get_pull(number)

    # ------------------------------------------------------------------
    def get_pr_diff(self, pr_url: str) -> PRDiff:
        """Fetch PR metadata and per-file diffs.

        Raises:
            GithubException: If GitHub rejects the request.
            ValueError: If *pr_url* is invalid.
        """
        pr = self._get_pr(pr_url)
        base_sha, head_sha = pr.base.sha, pr.head.sha
        repo = pr.base.repo
        merge_base_sha = repo.compare(base_sha, head_sha).merge_base_commit.sha
        files: list[FileDiff] = []
        skipped: dict[str, str] = {}
        changed_files = list(pr.get_files())
        if len(changed_files) != pr.changed_files:
            raise ValueError("GitHub returned an incomplete PR file list")
        for f in changed_files:
            if f.status == "removed":
                skipped[f.filename] = "deleted file; no head-side lines to review"
                continue
            language = _detect_language(f.filename)
            if language in {"unknown", "markdown", "json", "yaml", "toml", "html", "css", "scss"}:
                skipped[f.filename] = "unsupported language"
                continue
            source = self._read_source(repo, head_sha, f.filename)
            if source is None:
                skipped[f.filename] = "binary, non-UTF-8, or file larger than 1 MiB"
                continue
            patch = f.patch or ""
            added, removed = _parse_patch(patch)
            if len(added) != f.additions or len(removed) != f.deletions:
                old_path = f.previous_filename or f.filename
                old_source = "" if f.status == "added" else self._read_source(repo, merge_base_sha, old_path)
                if old_source is None:
                    skipped[f.filename] = "base file cannot be decoded for diff reconstruction"
                    continue
                patch = "\n".join(difflib.unified_diff(
                    old_source.splitlines(), source.splitlines(),
                    fromfile=old_path, tofile=f.filename, lineterm="",
                ))
                # The patch parser consumes hunks, not the two file headers.
                patch = "\n".join(patch.splitlines()[2:])
                added, removed = _parse_patch(patch)
            files.append(FileDiff(
                filename=f.filename,
                language=language,
                patch=patch,
                raw_diff=patch,
                added_lines=added,
                removed_lines=removed,
                full_source=source,
                status=f.status,
                previous_filename=f.previous_filename,
            ))
        current = self._get_pr(pr_url)
        if (current.base.sha, current.head.sha) != (base_sha, head_sha):
            raise ValueError("PR changed while fetching its diff; retry the review")
        metadata = self._metadata(pr)
        metadata.update(base_sha=base_sha, head_sha=head_sha, merge_base_sha=merge_base_sha, skipped_files=skipped)
        return PRDiff(
            files=files,
            pr_title=pr.title or "",
            pr_description=pr.body or "",
            author=pr.user.login if pr.user else "",
            base_sha=base_sha,
            head_sha=head_sha,
            merge_base_sha=merge_base_sha,
            metadata=metadata,
            skipped_files=skipped,
        )

    @staticmethod
    def _read_source(repo, sha: str, path: str) -> str | None:
        """Read an immutable blob; transport failures must not look like clean code."""
        content = repo.get_contents(path, ref=sha)
        if content.type != "file" or content.size > 1_048_576:
            return None
        try:
            data = content.decoded_content
            return None if b"\x00" in data else data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    @staticmethod
    def _metadata(pr) -> dict:
        return {
            "number": pr.number, "title": pr.title, "body": pr.body or "",
            "author": pr.user.login if pr.user else "", "state": pr.state,
            "base_branch": pr.base.ref, "head_branch": pr.head.ref,
            "base_sha": pr.base.sha, "head_sha": pr.head.sha,
            "additions": pr.additions, "deletions": pr.deletions,
            "changed_files": pr.changed_files, "url": pr.html_url,
            "created_at": pr.created_at.isoformat() if pr.created_at else "",
            "updated_at": pr.updated_at.isoformat() if pr.updated_at else "",
            "merged": pr.merged,
        }

    # ------------------------------------------------------------------
    def get_pr_metadata(self, pr_url: str) -> dict:
        """Return a flat dict of PR metadata fields."""
        try:
            pr = self._get_pr(pr_url)
            return {
                "number": pr.number,
                "title": pr.title,
                "body": pr.body or "",
                "author": pr.user.login if pr.user else "",
                "state": pr.state,
                "created_at": pr.created_at.isoformat() if pr.created_at else "",
                "updated_at": pr.updated_at.isoformat() if pr.updated_at else "",
                "merged": pr.merged,
                "base_branch": pr.base.ref,
                "head_branch": pr.head.ref,
                "additions": pr.additions,
                "deletions": pr.deletions,
                "changed_files": pr.changed_files,
                "url": pr.html_url,
            }
        except (GithubException, ValueError, Exception) as exc:
            logger.warning("Failed to fetch PR metadata for %s: %s", pr_url, exc)
            return {}

    # ------------------------------------------------------------------
    def post_review_comment(self, pr_url: str, body: str) -> bool:
        """Post a top-level review comment on the PR. Returns True on success."""
        try:
            pr = self._get_pr(pr_url)
            pr.create_issue_comment(body)
            return True
        except (GithubException, ValueError, Exception):
            return False

    # ------------------------------------------------------------------
    def post_inline_review(
        self,
        pr_url: str,
        findings: list[dict],
        summary_body: str = "",
        commit_sha: str | None = None,
        file_diffs: list[AgentFileDiff] | None = None,
    ) -> bool:
        """Create a GitHub Pull Request Review with per-finding inline comments.

        Each entry in *findings* must have keys:
            file, line_start, severity, category, description, suggestion, confidence

        A single review object is created (``COMMENT`` event) so all inline
        comments appear in one batch. Returns True on success.
        """
        try:
            pr = self._get_pr(pr_url)
            if not commit_sha or file_diffs is None:
                logger.warning("Inline review requires a pinned SHA and reviewed diff")
                return False
            if pr.head.sha != commit_sha:
                logger.info("Skipping stale inline review for %s", commit_sha)
                return False
            commit = pr.base.repo.get_commit(commit_sha)
            allowed = {fd.filename: {line for line, _ in fd.added_lines} for fd in file_diffs}

            comments = []
            for f in findings:
                if f["line_start"] not in allowed.get(f["file"], set()):
                    continue
                body = (
                    f"**[{f['severity']}] {f['category']}**\n\n"
                    f"{f['description']}\n\n"
                    f"**Suggestion:** {f['suggestion']}\n\n"
                    f"*Confidence: {f['confidence']:.0%} "
                    f"| Sources: {', '.join(f.get('source_agents', []))}*"
                )
                comments.append(
                    {
                        "path": f["file"],
                        "line": f["line_start"],
                        "side": "RIGHT",
                        "body": body,
                    }
                )

            if not comments:
                return True

            pr.create_review(
                commit=commit,
                body=summary_body,
                event="COMMENT",
                comments=comments,
            )
            return True
        except (GithubException, ValueError, Exception):
            return False

    # ------------------------------------------------------------------
    def get_head_commit_sha(self, pr_url: str) -> str | None:
        """Return the HEAD commit SHA of the PR, or None on error."""
        try:
            pr = self._get_pr(pr_url)
            return pr.head.sha
        except (GithubException, ValueError, Exception):
            return None
