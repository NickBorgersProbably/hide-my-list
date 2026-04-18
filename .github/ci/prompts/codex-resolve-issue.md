You are autonomous agent. Resolve GitHub issue #${ISSUE_NUMBER} in ${REPO}.

ISSUE TITLE: ${ISSUE_TITLE}

YOUR TASK:
1. Fetch full issue: `gh api repos/${REPO}/issues/${ISSUE_NUMBER}`
2. If needed, fetch comments: `gh api repos/${REPO}/issues/${ISSUE_NUMBER}/comments`
3. Analyze issue, understand what to do
4. Explore codebase, find relevant files
5. Implement fix or feature
6. Run `shellcheck scripts/*.sh` and `yamllint .github/workflows/*.yml` to verify
7. Create branch, commit, open PR

IMPORTANT RULES:
- OpenClaw agent project, not compiled application
- Use `shellcheck scripts/*.sh` for shell linting
- Use `yamllint .github/workflows/*.yml` for workflow validation
- Check docs links not broken
- Reference `docs/architecture.md` for system design
- Never use `git push --no-verify`

BRANCH NAMING: Use `codex/issue-${ISSUE_NUMBER}`.

PR CREATION:
gh pr create --title '<brief description of what this PR accomplishes>' \
  --assignee NickBorgers \
  --body 'Resolves #${ISSUE_NUMBER}

## Summary
<describe what you changed>

## Test Plan
<how to verify the fix>

Generated with Codex' \
  --head codex/issue-${ISSUE_NUMBER}

Can't resolve? Comment explaining what clarification needed:
`gh issue comment ${ISSUE_NUMBER} --body 'YOUR_EXPLANATION'`