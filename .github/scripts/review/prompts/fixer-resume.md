You = ORIGINAL AUTHOR, resumed. PR #${PR_NUMBER} on ${REPO}. Reviewed SHA ${REVIEWED_SHA}, cycle ${REVIEW_CYCLE}.

You authored this PR earlier in the same conversation. The reviewer panel
has now examined what you produced. Their findings are below. Decide what
to do, then leave the working tree updated.

You stay in the conversation. Reviewers and judge are read-only.

## Current PR metadata

Decode current PR title/body before starting:
```bash
echo "$PR_TITLE_B64" | base64 -d
echo "$PR_BODY_B64" | base64 -d
```
Reflects current PR state, not what you opened.

## How this differs from a cold fixer

You have full context — every choice you made authoring the PR is in your
scrollback. A misreading by a reviewer (mistaking intent for a bug) is
something only you can spot. A real bug, you can revisit at the level you
made the original decision, not just patch the surface.

For each reviewer blocker, decide:
- **Real**: revisit your earlier choice. Apply the right fix at the right
  level. Add the blocker id to `addressed[]`.
- **Misread of intent**: do not change code. Sharpen the PR body so the
  reader sees the same intent the reviewer missed. Add to `addressed[]`
  only if the body edit closes the misread; otherwise add to `skipped[]`
  with a brief reason.
- **Out of scope**: leave it; add to `skipped[]` with reason "out of scope
  for this PR — file follow-up issue".

## Hard constraints

1. **Apply only what reviewers asked for.** Read every reviewer artifact
   under `${REVIEWER_ARTIFACTS_DIR}` (one subdir per role, each with
   `*-result.json`).
2. **No new scope.** No unrelated refactors, features, improvements.
   Unrelated bugs → leave for a future PR.
3. **Deterministic CI fixes NOT your job.** Lint/format/typecheck/test
   repair = upstream CI. Lint failure found → abort + report.
4. **Do NOT touch `.git/`.** No `git add`, `git commit`, `git push`,
   `git config`, `git rebase`. The host runner commits and pushes after
   you exit — `.git/` is bind-mounted under a different UID, and any
   git-write inside the container corrupts it (see PR #409).
5. **Read-only git fine.** `git diff`, `git log`, `git status`,
   `git show`, `git ls-files` all work. Container sets
   `safe.directory=/workspace` — use freely.
6. **One logical fix batch, unstaged.** Write files, edit text. No
   `git add`. Host step captures the working tree via `git add -A` and
   commits as one. Commit message is built from your `addressed[]` list.

## Procedure

1. List reviewer artifacts:
   ```bash
   find "${REVIEWER_ARTIFACTS_DIR}" -name '*-result.json'
   ```
2. No reviewer files or unparseable → no edits. Write a no-op fix result
   to `$OUTPUT_PATH` and stop.
3. Read every blocker. Categorize each as real / misread / out-of-scope.
4. Apply real-blocker fixes at the level you originally decided. If a
   group of files needs the same change, apply uniformly — no per-file
   improvisation.
5. For misreads: edit the PR body via `gh pr edit ${PR_NUMBER} --body
   "..."` to clarify intent. Preserve the existing
   `Author-Session: <agent>/<run-id>` trailer exactly — the next cycle
   reads it to resume you again.
6. Leave file changes unstaged. Host step commits.
7. Write fix-result JSON to `$OUTPUT_PATH`.

## Output contract

Write to `$OUTPUT_PATH`, conforming to
`.github/scripts/review/schema/fix-result-v1.json`:

```json
{
  "schema_version": "1",
  "input_sha": "${REVIEWED_SHA}",
  "new_sha": "${REVIEWED_SHA}",
  "addressed": ["security/sec-001", "docs/doc-001"],
  "skipped": [
    { "id": "design/d-002", "reason": "out of scope; opened follow-up #NNN" }
  ]
}
```

Always set `new_sha` to `${REVIEWED_SHA}`. Host commit step rewrites it
with the real post-commit SHA before judge reads.

`addressed[]` and `skipped[].id` MUST be namespaced as `<role>/<id>`.
Judge fails closed on bare ids — prevents two reviewers' colliding ids
from cross-clearing.

`input_sha` MUST equal `${REVIEWED_SHA}`. Judge fails closed on mismatch.
