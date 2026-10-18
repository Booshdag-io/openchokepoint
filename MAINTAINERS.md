Handling: Unclassified — public

# Maintainers

| Name | Role | GitHub |
|---|---|---|
| T. Leroy Smith | Lead maintainer | @TLS-sys |

Additional maintainers are added by the lead maintainer.

## Technical Steering Committee

The TSC described in [GOVERNANCE.md](GOVERNANCE.md) section 2 is appointed and operating. Its composition is published here by seat; members are named with their consent. Working records are kept in EV's private repositories.

## Approving red-zone pull requests

A pull request is **red** when it changes a path `.github/CODEOWNERS` assigns to the owner, anything under `.github/`, any `CODEOWNERS` file, or has `[RED]` in its title or description. Everything else is **green**. A pull request that exactly matches an automation lane in `.github/automation-lanes.json` (see `docs/automation-lanes.md`) is green, as before; setting the repository variable `FEEDS_PAUSED` to `true` turns every lane off.

### What you see

| Situation | `owner-approval` check | Label | Merges? |
| :-- | :-- | :-- | :-- |
| Green pull request | success | `zone:green` | On its own (squash auto-merge) once the required checks pass |
| Red pull request, not approved | **pending** (yellow, not red) | `zone:red` | No. Auto-merge is switched off |
| Red pull request, you added `owner-approved` | success, for that exact commit | `zone:red`, `owner-approved` | Yes, squash auto-merge once the other checks pass |
| New commits pushed after you approved | back to pending | `owner-approved` removed | No, until you approve again |

Each pull request gets **one** "Owner approval" comment that is edited in place as things change. It lists the red paths and the commit you are being asked to approve.

### How to approve (plain steps)

1. Open the pull request and read the "Owner approval" comment. It names the commit (a short code like `a1b2c3d`) and the red paths. *Why: you approve one exact version; anything pushed later needs a fresh look.*
2. Review the changes in the **Files changed** tab.
3. Add the label `owner-approved` (right-hand side, **Labels**). *Why: the label is the approval. Only your account can make it count.*
4. Within a minute the comment says **approved**, `owner-approval` turns green and auto-merge is on. *Why: it then merges by itself when the other checks pass; you do not need the ruleset bypass.*
5. To withdraw an approval before it merges, remove the label.

Pull requests from forks are approved the same way but never auto-merge: press **Squash and merge** yourself once the checks are green.

### What the automation checks

- **Who added the label.** It must be `TLS-sys` (matched by login *and* numeric account id) with **admin** permission on this repository, checked with the REST collaborator-permission API. Anyone else's label is removed and the comment says the approval was refused.
- **Which commit.** The label must be added while the pull request's latest commit is the one you saw. If a push lands in between, the approval is refused.
- **New pushes.** Every push removes `owner-approved` and resets `owner-approval` to pending.
- **Real errors only fail.** The red zone gate and merge-gate fail only when something is actually broken (for example GitHub's API or `CODEOWNERS` cannot be read, or `CODEOWNERS` names no owner paths). Red is never a failure.

### How it is built

| Piece | Runs on | Does |
| :-- | :-- | :-- |
| `.github/workflows/red-zone.yml` | PR opened, pushed, reopened, edited, ready for review | Classifies the PR, sets `zone:red`/`zone:green`, posts `owner-approval` (success for green, pending for red), clears `owner-approved` on a push, turns auto-merge off on an unapproved red PR, updates the one comment |
| `.github/workflows/owner-approval.yml` | `owner-approved` label added or removed | Verifies the approver and commit, turns on squash auto-merge, sets `owner-approval` to success; removal withdraws it |
| `.github/workflows/merge-gate.yml` | Same events as the red zone gate | Turns on auto-merge for green PRs from this repository, then waits for all other checks (not `owner-approval`) |
| `.github/scripts/red_zone_gate.py` | Called by all three | Zone rules, approval checks, the comment; tested by `.github/scripts/tests/test_red_zone_gate.py` |

Security rules these workflows follow: they run on `pull_request_target` from `main` and check out `main` only, never the pull request's code; every action is pinned to a full commit SHA; each job asks for only the permissions it uses (`statuses` for the check, `pull-requests` for labels and the comment, `contents` only to switch auto-merge on or off). Untrusted text (file paths, titles) is read through the API and escaped before it goes into the comment.

Known limit: anyone with **write** access to this repository could, in principle, post a fake `owner-approval` status from their own workflow. The required check is pinned to the GitHub Actions app, the gate ignores statuses not posted by `github-actions[bot]`, and write access is held by the owner only. Keep it that way. Claude Code sessions act under the owner's account, so they must never add the `owner-approved` label.

### One-time ruleset setup

The organization ruleset requires `merge-gate`. A repository ruleset on `main` requires `owner-approval`, with no bypass, so a red pull request merges only through the label. The command that creates it is in the pull request that introduced this flow. Until that ruleset exists, `owner-approval` does not block a merge.
