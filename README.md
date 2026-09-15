# DinoAI PR Review — GitHub Action

Review pull requests with [Paradime](https://www.paradime.io)'s DinoAI background agent and get the findings back as an inline PR review.

The agent does not run in your CI runner. It runs in your Paradime workspace, with the repository checked out at the PR's head commit **and** everything else the workspace already has: your warehouse connection, dbt, the catalog, column-level lineage and Bolt run history. So it can answer the questions a diff can't — does this join fan out, what breaks three models downstream, is the new column mostly null in production — and it posts the answer where the code is.

## Quick start

1. In Paradime, create a workspace API key with the `dinoai:agent:trigger` and `dinoai:agent:read` capabilities. Note the API endpoint shown next to it. (A company-level key works too — then also pass `workspace_uid`.)
2. Add `PARADIME_API_ENDPOINT` and `PARADIME_API_KEY` as repository secrets.
3. Add a workflow:

```yaml
name: DinoAI PR review
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
permissions:
  contents: read
  pull-requests: write
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: paradime-io/dinoai-action@v1
        with:
          api_endpoint: ${{ secrets.PARADIME_API_ENDPOINT }}
          api_key: ${{ secrets.PARADIME_API_KEY }}
```

Open a PR that touches your dbt project. A review appears when the agent finishes.

See [`examples/`](examples/) for the mention-driven variant (`@dinoai <question>` in a PR comment).

## How it works

```
pull_request event ──▶ action builds a context block ──▶ triggerDinoaiAgentRun(base_branch = head SHA)
                        (coordinates + diffstat,                     │
                         PR comments, prior findings)                ▼
                                                        agent pod: full clone, checks out head SHA,
                                                        git diff base...head, dbt, warehouse, lineage
                                                                     │
action polls dinoaiAgentRun ◀────────────────────────────────────────┘
        │
        ▼
parses the findings block ──▶ POST /pulls/{n}/reviews with inline comments (your GITHUB_TOKEN)
```

Three things worth knowing:

- **The diff is never uploaded.** The action sends the base and head SHAs and a diffstat; the pod computes the diff itself with `git diff base...head` (three-dot — what GitHub shows). The payload is the same size for a 3-file PR and a 300-file PR.
- **Re-runs are incremental.** On a new push the agent gets the exact changes since its last review (from GitHub's compare API), the list of what it already reported, and an instruction to re-read those files at head — so it reviews the delta and says what's fixed instead of repeating itself. Set `incremental: "false"` to always review the whole PR.
- **The tree is verified.** The agent reports `git rev-parse HEAD` in its findings block. If that isn't the PR head, the review is posted with a warning and no inline findings rather than passing off a review of the wrong commit.
- **The review is posted with your workflow token**, as `github-actions[bot]`. Paradime's GitHub App needs no extra permissions for this action to work.

## Customising the reviewer

The action runs the agent named by `agent` (default `pr-reviewer`), defined in `.dinoai/agents/pr-reviewer.yml` **on your default branch**. If that file doesn't exist the action says so in the log and runs an unnamed session with the built-in review prompt, so nothing breaks before you've written one. The action checks for the file on the default branch on purpose: a PR cannot supply the reviewer that reviews it.

The agent also honours rule files it finds in the checkout — `.dinorules`, `CLAUDE.md`, `AGENTS.md`, `.cursorrules` and `.cursor/rules/**`.

Use `instructions` for per-workflow focus without touching the agent definition.

## Posting as "DinoAI" instead of github-actions[bot]

The review is authored by whichever token posts it. The default `${{ github.token }}` shows as **github-actions[bot]**. To have reviews appear under a DinoAI identity with its own name and avatar, install a GitHub App for it and mint a token in the workflow — no user account or seat needed:

```yaml
      - uses: actions/create-github-app-token@v1
        id: app
        with:
          app-id: ${{ vars.DINOAI_APP_ID }}
          private-key: ${{ secrets.DINOAI_APP_PRIVATE_KEY }}
      - uses: paradime-io/dinoai-action@v1
        with:
          api_endpoint: ${{ secrets.PARADIME_API_ENDPOINT }}
          api_key: ${{ secrets.PARADIME_API_KEY }}
          github_token: ${{ steps.app.outputs.token }}
```

The App needs only `Pull requests: Read & write` and `Contents: Read`, and no webhooks. Incremental re-review keys on a marker in the review body, not on the author, so switching identities mid-PR is safe.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `api_endpoint` | — | Paradime API endpoint (GraphQL URL). Required. |
| `api_key` | — | Workspace API key (`prdm_wsp_…`). Required. |
| `api_secret` | `""` | Only for legacy key/secret pairs. |
| `workspace_uid` | `""` | Required with a company key (`prdm_cmp_…`). Workspace keys already know their workspace. |
| `github_token` | `${{ github.token }}` | Needs `pull-requests: write` to post. |
| `agent` | `pr-reviewer` | Agent definition name. |
| `instructions` | `""` | Extra instructions appended to the prompt. |
| `model_family` | `""` | A model family enabled in your workspace. |
| `mode` | `review` | `review` for `pull_request` events, `mention` for comment events. |
| `trigger_phrase` | `@dinoai` | Phrase that triggers a run in `mention` mode. |
| `post_review` | `true` | Set `false` to only expose outputs. |
| `fail_on_findings` | `false` | Posts `REQUEST_CHANGES` and fails the step when findings exist. |
| `max_findings` | `25` | Inline comments per review; the rest go in the summary. |
| `incremental` | `true` | Review only what changed since the last review. |
| `review_drafts` | `false` | Review draft PRs. |
| `timeout_minutes` | `30` | Stop the run and fail after this long. |
| `poll_interval_seconds` | `10` | Polling cadence. |
| `session_url_template` | `""` | Footer link; `{agent_session_id}` is substituted. |

## Outputs

`agent_session_id`, `status` (`completed`, `failed`, `expired`, `stopped`), `findings_count`, `structured` (whether a findings block was parsed), `reviewed_head` (the commit the agent reported reviewing), `review_body`.

## Merge gating

Set `fail_on_findings: "true"` and make the job a required status check. The review is then posted as `REQUEST_CHANGES` when there are findings. The action never posts `APPROVE` — a bot approval satisfying a required-reviewer rule would be a policy hole.

## Limitations

- **Fork PRs are not reviewed.** The agent reviews the base repository's clone, where a fork's commits don't exist — and GitHub doesn't expose secrets to fork PRs anyway. The action skips with a warning (or fails, if `fail_on_findings` is set).
- **Cancellation.** Cancelling the workflow sends the step a signal; the action stops the agent session on the way out. If the runner itself dies, the session is released by Paradime's inactivity timeout.
- **GitHub Enterprise Server** is supported through the runner's `GITHUB_API_URL`.
- Only lines that are part of the diff can carry an inline comment (a GitHub rule). Findings on other lines are listed in the review summary instead.

## The findings contract

The agent is asked to end its final message with a fenced `dinoai-findings` JSON block:

```json
{"summary": "…", "findings": [{"path": "models/marts/orders.sql", "line": 42, "severity": "high", "title": "…", "body": "…", "suggestion": "…"}]}
```

`suggestion` becomes a GitHub suggestion block (one-click apply). The contract opens the prompt, and if the agent still finishes without the block the action sends one follow-up on the same session asking for just the JSON before posting. If that also yields nothing, the message is posted as the summary and `structured` is `false`.

## Development

Stdlib-only Python; no build step, nothing to bundle.

```
python -m unittest discover -s tests -v
```

## License

MIT
