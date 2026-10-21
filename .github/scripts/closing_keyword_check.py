#!/usr/bin/env python3
# Handling: Unclassified — public
# SPDX-License-Identifier: Apache-2.0
"""Closing-keyword check (rule CK-001).

GitHub closes an issue when a pull request whose title, description or commit
messages say "Closes #N" (or close/closed, fix/fixes/fixed, resolve/resolves/
resolved) is merged into the default branch. On a squash merge the commit
messages become the merged commit's message, so a stale "Closes #3" in any
commit closes the issue even if the description was corrected (this closed
open call #3 when PR #33 merged).

Standing issues must never close this way. An issue is protected when:
  - its title contains "Open call" or starts with "[Programme]", or
  - it carries the label `keep-open`.
Slice issues and ordinary task issues are not protected: a pull request that
finishes one may close it.

The fix for a contributor is to write "Refs #N" instead, in the description
and in every commit message (amend or reword the commit, then force-push).

Inputs (environment): GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_EVENT_PATH.
Reads the pull request title and body from the event file and the commit
messages from the REST API. Never executes pull request content. Fails closed
if a referenced issue cannot be looked up.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

RULE = "CK-001"
KEEP_OPEN_LABEL = "keep-open"
KEYWORDS = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
# KEYWORD[:] then #N, owner/repo#N, or https://github.com/owner/repo/issues/N
REF = re.compile(
    rf"\b{KEYWORDS}\s*:?\s+"
    r"(?:(?P<repo>[\w.-]+/[\w.-]+)?#(?P<num>\d+)"
    r"|https://github\.com/(?P<urlrepo>[\w.-]+/[\w.-]+)/(?:issues|pull)/(?P<urlnum>\d+))",
    re.I,
)
PROTECTED_TITLE = re.compile(r"open call|^\s*\[programme\]", re.I)


class LookupError_(Exception):
    pass


def closing_refs(text, repo):
    """Issue numbers in `repo` that `text` would close. Other repos are ignored."""
    found = []
    for m in REF.finditer(text or ""):
        target = (m.group("repo") or m.group("urlrepo") or repo).lower()
        num = m.group("num") or m.group("urlnum")
        if target == repo.lower():
            found.append((int(num), m.group(0)))
    return found


def is_protected(issue):
    if issue.get("pull_request"):
        return False
    labels = {(l.get("name") or "").lower() for l in issue.get("labels") or []}
    return KEEP_OPEN_LABEL in labels or bool(PROTECTED_TITLE.search(issue.get("title") or ""))


class GitHub:
    def __init__(self, repo, token):
        self.repo, self.token = repo, token

    def _get(self, path):
        req = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}/{path}",
            headers={"Accept": "application/vnd.github+json",
                     "Authorization": f"Bearer {self.token}",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "openchokepoint-closing-keyword-check"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise LookupError_(f"GET {path}: HTTP {exc.code}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise LookupError_(f"GET {path}: {exc}")

    def issue(self, n):
        return self._get(f"issues/{n}")

    def commit_messages(self, pr):
        out, page = [], 1
        while True:
            batch = self._get(f"pulls/{pr}/commits?per_page=100&page={page}") or []
            out += [(c["sha"][:7], c["commit"]["message"]) for c in batch]
            if len(batch) < 100 or page >= 3:  # GitHub lists at most 250 commits
                return out
            page += 1


def find_violations(sources, repo, get_issue):
    """sources: list of (where, text). Returns list of violation dicts."""
    cache, violations = {}, []
    for where, text in sources:
        for num, phrase in closing_refs(text, repo):
            if num not in cache:
                cache[num] = get_issue(num)
            issue = cache[num]
            if issue and is_protected(issue):
                violations.append(dict(where=where, num=num, phrase=phrase.strip(),
                                       title=issue.get("title", "")))
    return violations


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as fh:
        pr = json.load(fh)["pull_request"]
    gh = GitHub(repo, os.environ.get("GITHUB_TOKEN", ""))
    sources = [("pull request title", pr.get("title") or ""),
               ("pull request description", pr.get("body") or "")]
    try:
        sources += [(f"commit {sha}", msg) for sha, msg in gh.commit_messages(pr["number"])]
        violations = find_violations(sources, repo, gh.issue)
    except LookupError_ as exc:
        print(f"::error title={RULE} lookup failed::{RULE} could not check closing keywords ({exc}). Re-run the job.")
        return 1
    for v in violations:
        print(f"::error title={RULE} closing keyword on a standing issue::{RULE} The {v['where']} says "
              f"\"{v['phrase']}\". Merging would close #{v['num']} ({v['title']}), which must stay open. "
              f"Write \"Refs #{v['num']}\" instead. For a commit message, reword the commit "
              f"(git commit --amend, or git rebase -i) and force-push.")
    if violations:
        print(f"Closing-keyword check failed: {len(violations)} reference(s) would close a standing issue.")
        return 1
    print("Closing-keyword check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
