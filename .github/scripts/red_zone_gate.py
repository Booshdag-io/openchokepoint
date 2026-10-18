# Handling: Unclassified — working
"""Red zone gate and owner approval.

A pull request that changes a red-zone path waits for the repository owner.
It does not fail. The gate reports a commit status called `owner-approval`:

  green PR                         -> success
  red PR, not yet approved         -> pending (and the zone:red label)
  red PR, owner added the label    -> success for that exact commit
  new commits pushed               -> label removed, back to pending

The ruleset on main requires `owner-approval`, so a red pull request merges
only after the owner approves the commit that is actually being merged.

Zones come from .github/CODEOWNERS on the BASE branch: a path whose last
matching CODEOWNERS line names an owner is red; every other path is green
(this repository's CODEOWNERS lists only the owner's paths). Anything under
.github/ and any CODEOWNERS file is always red. A title or description
containing [RED] is red too. A CODEOWNERS file with no owner rules is an
error (fail closed).

Automation lanes (.github/automation-lanes.json on the BASE branch, if the
file exists) keep working as before: a pull request that matches a lane
exactly (author, feed branch inside this repository, allowed files under
data/ only, size limits) is green. A [RED] pull request is never in a lane,
and setting the repository variable FEEDS_PAUSED to true turns every lane off.

Commands (one per workflow event):
  gate      Red zone workflow: opened, synchronize, reopened, edited, ...
  approve   Owner approval workflow: the owner-approved label was added
  revoke    Owner approval workflow: the owner-approved label was removed
  classify  merge-gate: print red=true|false to GITHUB_OUTPUT, change nothing

Each command posts or updates one summary comment; it never posts a second.
Exit code 1 means a real error (API failure, missing CODEOWNERS), never
"red". Uses only the GitHub REST/GraphQL API and the base-branch checkout.
Never checks out or runs pull request code.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CODEOWNERS = Path(".github/CODEOWNERS")
LANES = Path(".github/automation-lanes.json")

OWNER_LOGIN = "TLS-sys"
OWNER_ID = 330289023  # numeric account id; a renamed or re-registered login does not match
CONTEXT = "owner-approval"
APPROVED_LABEL = "owner-approved"
RED_LABEL = "zone:red"
GREEN_LABEL = "zone:green"
BOT = "github-actions[bot]"
MARKER = "<!-- owner-approval-gate -->"
APPROVED_PREFIX = f"Approved by {OWNER_LOGIN} at "
ALWAYS_RED = re.compile(r"^\.github/|(^|/)CODEOWNERS$")
MAX_LISTED = 50


# ---------------------------------------------------------------- zones

def pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a CODEOWNERS (gitignore-style) pattern into a regex."""
    anchored = pattern.startswith("/") or "/" in pattern.strip("/")
    dir_only = pattern.endswith("/")
    body = pattern.strip("/")
    out, i = [], 0
    while i < len(body):
        if body.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif body.startswith("/**", i) and i + 3 == len(body):
            out.append("/.*")
            i += 3
        elif body.startswith("**", i):
            out.append(".*")
            i += 2
        elif body[i] == "*":
            out.append("[^/]*")
            i += 1
        elif body[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(body[i]))
            i += 1
    prefix = "^" if anchored else "^(?:.*/)?"
    suffix = "/.*$" if dir_only else "(?:/.*)?$"
    return re.compile(prefix + "".join(out) + suffix)


def parse_codeowners(text: str) -> list[tuple[re.Pattern[str], bool]]:
    """Return (regex, has_owner) per rule, in file order."""
    rules = []
    for raw in text.splitlines():
        parts = raw.split("#", 1)[0].split()
        if not parts:
            continue
        rules.append((pattern_to_regex(parts[0]), len(parts) > 1))
    return rules


def is_red(path: str, rules: list[tuple[re.Pattern[str], bool]]) -> bool:
    verdict = False  # only paths CODEOWNERS gives an owner are red
    for regex, has_owner in rules:
        if regex.match(path):
            verdict = has_owner  # last match wins
    return verdict


def red_paths(paths: list[str], rules: list[tuple[re.Pattern[str], bool]]) -> list[str]:
    return sorted({p for p in paths if ALWAYS_RED.search(p) or is_red(p, rules)})


def marked_red(title: str | None, body: str | None) -> bool:
    return "[RED]" in (title or "") or "[RED]" in (body or "")


def load_lanes(path: Path = LANES) -> dict:
    """The lane config from the base branch; missing or unreadable means no lanes."""
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def lane_match(cfg: dict, author: str, head_ref: str, same_repo: bool,
               entries: list[dict], paused: bool) -> str | None:
    """Name of the automation lane this pull request matches exactly, else None.
    Any doubt or malformed config returns None, so the owner rules apply."""
    try:
        if paused or not same_repo or cfg.get("enabled") is not True or not entries:
            return None
        author = (author or "").strip().lower()
        for lane in cfg.get("lanes") or []:
            authors = [a.strip().lower() for a in (lane.get("authors") or []) if a.strip()]
            prefix = lane.get("branch_prefix") or ""
            paths = lane.get("paths") or []
            exts = [e.lower() for e in (lane.get("extensions") or [])]
            # A lane must name an author, a branch prefix, data/ folders and file types.
            if not authors or not prefix or not paths or not exts:
                continue
            # Hard limit: a lane can only ever cover folders under data/.
            if any((not p.startswith("data/")) or (not p.endswith("/")) or ".." in p for p in paths):
                continue
            if author not in authors or not (head_ref or "").startswith(prefix):
                continue
            max_files = int(lane.get("max_files", 0))
            max_lines = int(lane.get("max_changed_lines", 0))
            if max_files < 1 or max_lines < 1 or len(entries) > max_files:
                continue
            lines, ok = 0, True
            for e in entries:
                name = e.get("filename") or ""
                if e.get("previous_filename") or e.get("status") not in ("added", "modified"):
                    ok = False
                if ".." in name.split("/") or not any(name.startswith(p) for p in paths):
                    ok = False
                if not any(name.lower().endswith(x) for x in exts):
                    ok = False
                lines += int(e.get("additions", 0)) + int(e.get("deletions", 0))
            if ok and lines <= max_lines:
                return str(lane.get("name", "?"))
        return None
    except (AttributeError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------- comment

def safe(text: str, limit: int = 200) -> str:
    """Untrusted text (file paths, logins) rendered inert inside the comment."""
    text = re.sub(r"[\x00-\x1f\x7f]", "?", str(text))[:limit]
    return html.escape(text, quote=True).replace("`", "&#96;")


def render_comment(state: str, red: list[str], marked: bool, sha: str,
                   run_url: str = "", detail: str = "") -> str:
    """One comment body. state: green|pending|approved|reset|refused|revoked|stale."""
    short = safe(sha[:7])
    lines = [MARKER, "## Owner approval", ""]
    if state == "green":
        lines.append("**Zone: GREEN.** No red-zone paths changed. `owner-approval` is "
                     "success; this pull request merges on its own once the required checks pass.")
    else:
        lines.append("**Zone: RED.** This pull request needs the repository owner's approval "
                     "before it can merge.")
        if marked:
            lines.append("")
            lines.append("The title or description is marked `[RED]`.")
        lines.append("")
        if state == "approved":
            lines.append(f"**Status: approved.** {safe(detail)} Squash auto-merge merges it once "
                         "the required checks pass. Any new commit clears this approval.")
        else:
            reason = {
                "pending": "",
                "reset": "New commits were pushed, so the earlier approval was cleared. ",
                "refused": f"An approval was refused: {safe(detail)} ",
                "revoked": "The `owner-approved` label was removed, so the approval was withdrawn. ",
                "stale": "The `owner-approved` label is on this pull request but no approval is "
                         "recorded for the latest commit. Remove the label and add it again. ",
            }[state]
            lines.append(f"**Status: waiting for the owner.** {reason}To approve, {OWNER_LOGIN} "
                         f"reviews commit `{short}` and adds the `{APPROVED_LABEL}` label.")
    if red:
        listed = red[:MAX_LISTED]
        lines += ["", f"<details><summary>Red paths ({len(red)})</summary>", ""]
        lines += [f"- <code>{safe(p)}</code>" for p in listed]
        if len(red) > len(listed):
            lines.append(f"- … and {len(red) - len(listed)} more")
        lines += ["", "</details>"]
    footer = f"_Updated for commit `{short}`"
    if run_url.startswith("https://"):
        footer += f" · [workflow run]({run_url})"
    lines += ["", footer + "_"]
    return "\n".join(lines) + "\n"


def approver_problem(login: str, user_id: str | int, permission: dict | None) -> str | None:
    """Why this person may not approve, or None if they may."""
    if (login or "").lower() != OWNER_LOGIN.lower():
        return f"the label was added by {login or 'an unknown account'}, not {OWNER_LOGIN}."
    if str(user_id) != str(OWNER_ID):
        return f"the account that added the label is not {OWNER_LOGIN}'s registered account."
    if not permission or permission.get("permission") != "admin":
        return f"{OWNER_LOGIN} does not have admin permission on this repository."
    if str((permission.get("user") or {}).get("id")) != str(OWNER_ID):
        return "the permission check returned a different account."
    return None


# ---------------------------------------------------------------- GitHub API

class ApiError(Exception):
    pass


class GitHub:
    """The few REST/GraphQL calls the gate needs. Tests replace this class."""

    def __init__(self, repo: str, token: str, api: str = "https://api.github.com"):
        self.repo, self.token, self.api = repo, token, api

    def _call(self, method: str, path: str, body=None, ok404: bool = False):
        url = path if path.startswith("https://") else f"{self.api}/{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if ok404 and exc.code == 404:
                return None
            raise ApiError(f"{method} {path}: HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise ApiError(f"{method} {path}: {exc.reason}") from None
        return json.loads(raw) if raw else None

    def _pages(self, path: str) -> list:
        out, page = [], 1
        sep = "&" if "?" in path else "?"
        while True:
            batch = self._call("GET", f"{path}{sep}per_page=100&page={page}") or []
            out += batch
            if len(batch) < 100:
                return out
            page += 1

    def _r(self, rest: str) -> str:
        return f"repos/{self.repo}/{rest}"

    def get_pr(self, n):
        return self._call("GET", self._r(f"pulls/{n}"))

    def pr_file_entries(self, n) -> list[dict]:
        return self._pages(self._r(f"pulls/{n}/files"))

    def add_label(self, n, name):
        self._call("POST", self._r(f"issues/{n}/labels"), {"labels": [name]})

    def remove_label(self, n, name):
        self._call("DELETE", self._r(f"issues/{n}/labels/{urllib.parse.quote(name, safe='')}"),
                   ok404=True)

    def ensure_label(self, name, color, description):
        if self._call("GET", self._r(f"labels/{urllib.parse.quote(name, safe='')}"), ok404=True):
            return
        self._call("POST", self._r("labels"),
                   {"name": name, "color": color, "description": description})

    def statuses(self, sha) -> list[dict]:
        return self._call("GET", self._r(f"commits/{sha}/statuses?per_page=100")) or []

    def set_status(self, sha, state, description, target_url=""):
        body = {"state": state, "context": CONTEXT, "description": description[:140]}
        if target_url.startswith("https://"):
            body["target_url"] = target_url
        self._call("POST", self._r(f"statuses/{sha}"), body)

    def comments(self, n) -> list[dict]:
        return self._pages(self._r(f"issues/{n}/comments"))

    def create_comment(self, n, body):
        self._call("POST", self._r(f"issues/{n}/comments"), {"body": body})

    def update_comment(self, comment_id, body):
        self._call("PATCH", self._r(f"issues/comments/{comment_id}"), {"body": body})

    def permission(self, login) -> dict | None:
        return self._call("GET", self._r(
            f"collaborators/{urllib.parse.quote(login, safe='')}/permission"), ok404=True)

    def set_auto_merge(self, node_id, enable: bool):
        if enable:
            query = ("mutation($id:ID!){enablePullRequestAutoMerge(input:{pullRequestId:$id,"
                     "mergeMethod:SQUASH}){clientMutationId}}")
        else:
            query = ("mutation($id:ID!){disablePullRequestAutoMerge(input:{pullRequestId:$id})"
                     "{clientMutationId}}")
        result = self._call("POST", "graphql", {"query": query, "variables": {"id": node_id}})
        if result and result.get("errors"):
            raise ApiError("auto-merge: " + "; ".join(e.get("message", "?") for e in result["errors"]))


# ---------------------------------------------------------------- actions

def upsert_comment(gh, n, body):
    """Edit the gate's one comment in place; create it only if it does not exist."""
    for c in gh.comments(n):
        if (c.get("user") or {}).get("login") == BOT and MARKER in (c.get("body") or ""):
            if c.get("body") != body:
                gh.update_comment(c["id"], body)
            return
    gh.create_comment(n, body)


def set_zone_labels(gh, n, labels, red):
    want, drop = (RED_LABEL, GREEN_LABEL) if red else (GREEN_LABEL, RED_LABEL)
    if want not in labels:
        gh.add_label(n, want)
    if drop in labels:
        gh.remove_label(n, drop)


def recorded_approval(gh, sha) -> dict | None:
    """The latest owner-approval status on this commit, if it is an approval."""
    for s in gh.statuses(sha):  # newest first
        if s.get("context") == CONTEXT and (s.get("creator") or {}).get("login") == BOT:
            ok = s.get("state") == "success" and (s.get("description") or "").startswith(APPROVED_PREFIX)
            return s if ok else None
    return None


def approval_detail(sha, when) -> str:
    return f"{OWNER_LOGIN} approved commit {sha} on {when}."


def file_paths(entries: list[dict]) -> list[str]:
    paths = []
    for f in entries:
        paths.append(f["filename"])
        if f.get("previous_filename"):
            paths.append(f["previous_filename"])  # moving a file out of red is red
    return paths


def classify(gh, n, rules, lanes=None):
    pr = gh.get_pr(n)
    entries = gh.pr_file_entries(n)
    if not entries:
        raise ApiError("could not read the list of changed files")
    red = red_paths(file_paths(entries), rules)
    marked = marked_red(pr.get("title"), pr.get("body"))
    if red and not marked and lanes:
        paused = os.environ.get("FEEDS_PAUSED", "").strip().lower() in ("1", "true", "yes")
        same_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name") == gh.repo
        lane = lane_match(lanes, (pr.get("user") or {}).get("login", ""),
                          (pr.get("head") or {}).get("ref", ""), same_repo, entries, paused)
        if lane:
            print(f"Automation lane '{lane}' matched {len(entries)} file(s): green. "
                  "Required checks still apply.")
            red = []
    return pr, red, marked, bool(red or marked)


def cmd_gate(gh, n, rules, action, run_url, lanes=None):
    pr, red, marked, is_red_pr = classify(gh, n, rules, lanes)
    sha = pr["head"]["sha"]
    labels = {l["name"] for l in pr.get("labels", [])}
    try:
        gh.ensure_label(APPROVED_LABEL, "0e8a16", "Owner approved the latest commit of this red PR")
    except ApiError as exc:
        print(f"::warning::could not create the {APPROVED_LABEL} label: {exc}")

    reset = False
    if action == "synchronize" and APPROVED_LABEL in labels:
        gh.remove_label(n, APPROVED_LABEL)  # an approval covers one commit only
        labels.discard(APPROVED_LABEL)
        reset = True
    set_zone_labels(gh, n, labels, is_red_pr)

    if not is_red_pr:
        gh.set_status(sha, "success", "Green: no red-zone paths changed", run_url)
        upsert_comment(gh, n, render_comment("green", [], False, sha, run_url))
        print("GREEN: no red-zone paths changed.")
        return 0

    approval = recorded_approval(gh, sha) if APPROVED_LABEL in labels else None
    if approval:
        detail = approval_detail(sha, approval.get("created_at", ""))
        upsert_comment(gh, n, render_comment("approved", red, marked, sha, run_url, detail))
        print(f"RED, approved for {sha}; status left as success.")
        return 0

    gh.set_status(sha, "pending", f"Waiting for {OWNER_LOGIN}: add the {APPROVED_LABEL} label",
                  run_url)
    if pr.get("auto_merge"):
        try:
            gh.set_auto_merge(pr["node_id"], False)  # nothing red merges unapproved
        except ApiError as exc:
            print(f"::warning::could not turn off auto-merge: {exc}")
    state = "reset" if reset else ("stale" if APPROVED_LABEL in labels else "pending")
    upsert_comment(gh, n, render_comment(state, red, marked, sha, run_url))
    print(f"RED: owner-approval pending for {sha}. Red paths: {len(red)}; marked [RED]: {marked}.")
    return 0


def cmd_approve(gh, n, rules, run_url, sender_login, sender_id, event_sha, now=None,
                lanes=None):
    pr, red, marked, is_red_pr = classify(gh, n, rules, lanes)
    sha = pr["head"]["sha"]
    if pr.get("state") != "open":
        print("Pull request is not open; nothing to approve.")
        return 0

    problem = approver_problem(sender_login, sender_id,
                               gh.permission(sender_login) if sender_login else None)
    if not problem and event_sha != sha:
        problem = (f"the label was added while commit {event_sha[:7]} was the latest, but "
                   f"{sha[:7]} has been pushed since. Review the latest commit and add the label again.")
    if problem:
        gh.remove_label(n, APPROVED_LABEL)
        if is_red_pr:
            gh.set_status(sha, "pending", "Approval refused; waiting for the owner", run_url)
        upsert_comment(gh, n, render_comment("refused" if is_red_pr else "green",
                                             red, marked, sha, run_url, problem))
        print(f"::warning::approval refused: {problem}")
        return 0

    same_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name") == gh.repo
    note = ""
    if not same_repo:
        note = " Pull requests from forks never auto-merge: the owner merges this one by hand."
    elif pr.get("draft"):
        note = " It is still a draft, so auto-merge is off until it is marked ready."
    else:
        # Before the success status: GitHub refuses auto-merge on a PR that is
        # already mergeable, and this order also means nothing merges if this fails.
        try:
            gh.set_auto_merge(pr["node_id"], True)
        except ApiError as exc:
            note = " Auto-merge could not be turned on; the owner can merge it by hand."
            print(f"::warning::could not turn on auto-merge: {exc}")

    when = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    gh.set_status(sha, "success", f"{APPROVED_PREFIX}{sha[:7]}", run_url)
    detail = approval_detail(sha, when)
    upsert_comment(gh, n, render_comment("approved" if is_red_pr else "green",
                                         red, marked, sha, run_url, detail + note))
    print(f"Approved {sha} for {OWNER_LOGIN}.")
    return 0


def cmd_revoke(gh, n, rules, run_url, lanes=None):
    pr, red, marked, is_red_pr = classify(gh, n, rules, lanes)
    if pr.get("state") != "open" or not is_red_pr:
        return 0
    sha = pr["head"]["sha"]
    gh.set_status(sha, "pending", f"Approval withdrawn; waiting for {OWNER_LOGIN}", run_url)
    if pr.get("auto_merge"):
        try:
            gh.set_auto_merge(pr["node_id"], False)
        except ApiError as exc:
            print(f"::warning::could not turn off auto-merge: {exc}")
    upsert_comment(gh, n, render_comment("revoked", red, marked, sha, run_url))
    print(f"Approval withdrawn for {sha}.")
    return 0


def cmd_classify(gh, n, rules, lanes=None):
    _, red, marked, is_red_pr = classify(gh, n, rules, lanes)
    print(f"red={'true' if is_red_pr else 'false'} ({len(red)} red path(s); marked [RED]: {marked})")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"red={'true' if is_red_pr else 'false'}\n")
    return 0


def main(argv=None, gh=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else "gate"
    env = os.environ
    try:
        if not CODEOWNERS.is_file():
            print("::error::.github/CODEOWNERS missing on base branch; failing closed")
            return 1
        rules = parse_codeowners(CODEOWNERS.read_text(encoding="utf-8"))
        if not any(has_owner for _, has_owner in rules):
            print("::error::.github/CODEOWNERS on base branch has no owner rules; failing closed")
            return 1
        lanes = load_lanes()
        gh = gh or GitHub(env["REPO"], env["GITHUB_TOKEN"])
        n, run_url = env["PR_NUMBER"], env.get("RUN_URL", "")
        if command == "gate":
            return cmd_gate(gh, n, rules, env.get("EVENT_ACTION", ""), run_url, lanes)
        if command == "approve":
            return cmd_approve(gh, n, rules, run_url, env.get("SENDER_LOGIN", ""),
                               env.get("SENDER_ID", ""), env.get("EVENT_HEAD_SHA", ""),
                               lanes=lanes)
        if command == "revoke":
            return cmd_revoke(gh, n, rules, run_url, lanes)
        if command == "classify":
            return cmd_classify(gh, n, rules, lanes)
        print(f"::error::unknown command {command!r}")
        return 1
    except (ApiError, KeyError) as exc:
        print(f"::error::owner-approval gate error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
