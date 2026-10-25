# CI Notes — Exam Prep

Study notes on using Claude Code as a CI worker with GitHub.

## 1. Get the `gh` CLI

- Install: `brew install gh` (macOS), `sudo apt install gh` (Debian/Ubuntu), or `winget install GitHub.cli` (Windows).
- Authenticate: `gh auth login` (choose GitHub.com, HTTPS, browser login). Verify with `gh auth status`.
- Common commands:
  - `gh repo view`, `gh repo clone <owner>/<repo>`
  - `gh issue list`, `gh issue view <n>`, `gh issue create`
  - `gh pr create`, `gh pr list`, `gh pr checks`, `gh pr view --comments`
  - `gh run list`, `gh run view <id> --log`, `gh run watch`
  - `gh secret set <NAME>` — store secrets (e.g. tokens) for Actions
- Claude Code can use `gh` itself (e.g. `--allowed-tools "Bash(gh pr *)"`).

## 2. CI concepts using a GitHub project

- **CI (continuous integration):** automatically build/test/lint every change (push or PR) so problems surface early.
- **GitHub Actions building blocks:**
  - *Workflow* — YAML file in `.github/workflows/`.
  - *Event/trigger* — `push`, `pull_request`, `issue_comment`, `schedule`, ...
  - *Job* — set of steps run on one runner (`runs-on: ubuntu-latest`).
  - *Step* — a shell command (`run:`) or a reusable action (`uses:`).
  - *Secrets* — encrypted values via `${{ secrets.NAME }}`.
  - *Permissions* — least-privilege `GITHUB_TOKEN` scopes per workflow/job.
- **In this repo:**
  - `.github/workflows/claude.yml` — responds to `@claude` mentions in issues/PRs/comments.
  - `.github/workflows/claude-code-review.yml` — automated Claude review of pull requests.
- **Typical flow:** branch -> push -> PR -> workflows run -> checks pass -> review -> merge.

## 3. Live demo: Claude as a CI worker

Demo steps:
1. Open an issue describing a task.
2. Comment `@claude work on this issue and create a PR`.
3. The `claude.yml` workflow triggers; Claude posts a progress comment with a checklist.
4. Claude commits to a branch (`claude/issue-<n>-...`) and gives a link to open a PR.
5. The review workflow can then comment on the PR; iterate with further `@claude` comments.

This very document was produced that way (issue #4).

## 4. In-depth configuration of Claude Code as a CI worker

Uses `anthropics/claude-code-action@v1`.

- **Authentication:** `claude_code_oauth_token` (from a Claude subscription) or `anthropic_api_key`, stored as repo secrets. Install the Claude GitHub app (`/install-github-app` in Claude Code).
- **Triggers:** `issue_comment`, `pull_request_review_comment`, `issues`, `pull_request_review`; gate with an `if:` on `contains(..., '@claude')`.
- **Permissions:** `contents`, `pull-requests`, `issues` (read at minimum), `id-token: write`, and `actions: read` (via `additional_permissions`) so Claude can read CI results.
- **`prompt`:** optional fixed instruction; if omitted, Claude follows the triggering comment. Useful for automation (e.g. auto-review every PR).
- **`claude_args`:** passes CLI flags, e.g.
  - `--allowed-tools "Bash(gh pr *),Bash(npm test)"` — restrict/allow tools
  - `--max-turns 10` — cap iterations
  - `--model <model-id>` — choose model
  - `--mcp-config` — add MCP servers
- **`CLAUDE.md`:** repo-level instructions Claude reads on every run (build/test commands, conventions).
- **Limitations:** cannot modify `.github/workflows/` (app permissions), cannot approve PRs, only edits its single tracking comment.
- **Security:** keep secrets in GitHub Secrets, grant least privilege, restrict allowed tools, review Claude's PRs before merging.
- **Docs:** https://github.com/anthropics/claude-code-action/blob/main/docs/usage.md
