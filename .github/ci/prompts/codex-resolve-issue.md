You are an autonomous agent tasked with resolving GitHub issue #${ISSUE_NUMBER} in ${REPO}.

ISSUE TITLE: ${ISSUE_TITLE}

YOUR TASK:
1. Fetch the full issue for context: gh api repos/${REPO}/issues/${ISSUE_NUMBER}
2. If needed, fetch issue comments: gh api repos/${REPO}/issues/${ISSUE_NUMBER}/comments
3. Analyze the issue and understand what needs to be done
4. Explore the codebase to find relevant files
5. Implement the fix or feature
6. Run `shellcheck scripts/*.sh` and `yamllint .github/workflows/*.yml` to verify your changes
7. Create a branch, commit your changes, and open a PR

IMPORTANT RULES:
- This is an OpenClaw agent project, not a compiled application
- Use `shellcheck scripts/*.sh` for shell script linting
- Use `yamllint .github/workflows/*.yml` for workflow validation
- Check documentation links are not broken
- Reference docs/architecture.md for system design context
- Never use `git push --no-verify`

BRANCH NAMING: Use `codex/issue-${ISSUE_NUMBER}` as the branch name.

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

If you cannot resolve the issue (unclear requirements, needs human decision, etc.),
comment on the issue explaining what clarification is needed:
gh issue comment ${ISSUE_NUMBER} --body 'YOUR_EXPLANATION'
