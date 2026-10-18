Handling: Unclassified — public

# Automation lanes

An automation lane lets a scheduled feed update one folder of public registry data without a human merge. It does not remove any check. The pull request still has to pass the data-handling guard and the schema validator, and the `merge-gate` check still waits for every other check.

## A pull request is green (no owner approval needed) only if all of these are true

1. It is not marked `[RED]`.
2. It comes from a branch inside this repository (never a fork) whose name starts with the lane's `branch_prefix` (`feed/`).
3. Its author is one of the lane's named `authors`. A lane with no named author is inert.
4. Every changed file is a `.yaml` file under a lane folder, and every lane folder is under `data/`.
5. No file is deleted or renamed, there are at most `max_files` files, and at most `max_changed_lines` lines change.
6. The repository variable `FEEDS_PAUSED` is not set to `true`.

If anything cannot be read or does not match, the owner rules apply. `.github/` and `CODEOWNERS` can never be in a lane.

## Where the rules live

- `.github/automation-lanes.json` holds the lanes. The gate reads it from `main`, so a pull request cannot loosen its own lane. Changing it needs the owner.
- `CODEOWNERS` is unchanged. `/data/` stays owner-only for every other pull request.

## Pause, stop, undo

- **Pause every lane at once:** in GitHub, Settings, Secrets and variables, Actions, Variables, set `FEEDS_PAUSED` to `true`. No pull request needed.
- **Undo a bad feed run:** revert the squash commit. The revert is itself a pull request to `data/`, so the owner merges it.
- **Add or change a lane:** edit `.github/automation-lanes.json` in a `[RED]` pull request.
