"""Answer the store's ancestor question from a local object graph.

`GitHubStore.head()` proves the fixed store anchor is reachable from the branch
head. It does that through `GET /compare/{anchor}...{head}`, which makes GitHub
enumerate every commit in between. On 2026-10-23 that range held 1127 commits:
measured at 7 s and at 47.5 s on the same link, against a 30 s socket timeout,
which is why a read that costs GitHub a few milliseconds in us-east can fail
outright on a workstation.

The question has an exact local answer. A commit's ancestor set is fixed by the
commit itself, because the parent ids are part of the hashed content, so once the
remote has named the head id, any object graph containing that id answers "does
the anchor reach it" without asking GitHub to count anything.

This module is an accelerator, not a new trust root. It stays inside three limits:

1. It answers only for commit ids the remote already returned. The caller keeps
   fetching the live ref first, so a moved head still changes the question.
2. It never guesses. A commit missing from the local graph makes it refuse, and
   a refused question falls back to the remote comparison.
3. It does not verify its own object store. Rewriting an object in `.git/objects`
   to keep a sha while changing its parents is therefore out of scope, exactly as
   rewriting this module's source is: both assume the executing machine is trusted.
   The sha-addressed answers above are the part that does not need that assumption.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from decision_kernel.domain.records import require

SCHEMA = "local-git-ancestry-v1"
DEFAULT_TIMEOUT_SECONDS = 120
_SHA = re.compile(r"[0-9a-f]{40}")


class AncestryUnavailable(Exception):
    """The local graph cannot answer this question; ask the remote instead.

    Never means "not an ancestor". A caller that treated it as a negative answer
    would turn a stale or partial mirror into a false accusation.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class LocalGitAncestry:
    """Read-only ancestry questions against one existing Git object graph."""

    def __init__(self, path, *, timeout: int = DEFAULT_TIMEOUT_SECONDS):
        require(isinstance(timeout, int) and timeout > 0, "LOCAL_ANCESTRY_INVALID_TIMEOUT")
        self.path = str(Path(path).expanduser().resolve())
        self._timeout = timeout
        self._answered = 0
        self._declined = 0
        self._env = {
            **os.environ,
            # Never block on a credential prompt or take an optional lock: this
            # reader must be safe inside a query process that cannot talk to a tty.
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_ASKPASS": "",
        }

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", "-C", self.path, *args],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=self._timeout, check=False, env=self._env, shell=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AncestryUnavailable("LOCAL_ANCESTRY_GIT_UNAVAILABLE") from exc

    @staticmethod
    def _commit(sha) -> str:
        require(isinstance(sha, str) and bool(_SHA.fullmatch(sha)),
                "LOCAL_ANCESTRY_INVALID_COMMIT")
        return sha

    def available(self) -> bool:
        """Whether `path` is a readable Git object graph. Never raises."""
        try:
            done = self._git("rev-parse", "--git-dir")
        except AncestryUnavailable:
            return False
        return done.returncode == 0

    def has_commit(self, sha) -> bool:
        """Whether this exact commit object is present, not merely nameable.

        `cat-file -e` on the bare id is the only form that reports absence as
        exit 1: `-t` and any `^<type>` peel report it as 128, the same code git
        uses when it is itself broken. Keeping those apart decides between
        "fetch this mirror" and "do not use this mirror at all".
        """
        self._commit(sha)
        present = self._git("cat-file", "-e", sha)
        if present.returncode == 1:
            return False
        if present.returncode != 0:
            raise AncestryUnavailable("LOCAL_ANCESTRY_GIT_UNAVAILABLE")
        kind = self._git("cat-file", "-t", sha)
        if kind.returncode != 0:
            raise AncestryUnavailable("LOCAL_ANCESTRY_GIT_UNAVAILABLE")
        return kind.stdout.strip() == "commit"

    def ref_commit(self, ref) -> str | None:
        """Resolve a local ref to a commit id, or None when it does not resolve.

        Not used to decide anything on its own: a local ref states where this
        mirror stopped reading, never where the remote branch is.
        """
        require(isinstance(ref, str) and bool(ref) and not ref.startswith("-"),
                "LOCAL_ANCESTRY_INVALID_REF")
        done = self._git("rev-parse", "--verify", "--quiet", ref + "^{commit}")
        if done.returncode == 0:
            value = done.stdout.strip()
            return value if _SHA.fullmatch(value) else None
        if done.returncode == 1:
            return None
        raise AncestryUnavailable("LOCAL_ANCESTRY_GIT_UNAVAILABLE")

    def is_ancestor(self, base, head) -> bool:
        """Whether `base` is a strict-or-equal ancestor of `head`.

        Raises `AncestryUnavailable` when the graph cannot answer, so the caller
        can fall back. Git's own exit codes are the only positive/negative signal
        used: 0 means yes, 1 means no, anything else means the question failed.
        """
        try:
            answer = self._is_ancestor(base, head)
        except AncestryUnavailable:
            self._declined += 1
            raise
        self._answered += 1
        return answer

    def _is_ancestor(self, base, head) -> bool:
        base, head = self._commit(base), self._commit(head)
        if base == head:
            return True
        if not self.has_commit(base) or not self.has_commit(head):
            raise AncestryUnavailable("LOCAL_ANCESTRY_OBJECT_MISSING")
        done = self._git("merge-base", "--is-ancestor", base, head)
        if done.returncode == 0:
            return True
        if done.returncode == 1:
            return False
        raise AncestryUnavailable("LOCAL_ANCESTRY_GIT_UNAVAILABLE")

    def stats(self) -> dict:
        """How often this graph answered versus declined. Counts only, no ids."""
        return {"schema": SCHEMA, "answered": self._answered, "declined": self._declined}
