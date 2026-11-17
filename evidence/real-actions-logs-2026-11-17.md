# Agent on real GitHub Actions logs, 2026-11-17

The agent repaired S01–S05 starting from logs produced by real CI jobs, and escalated
S06. Real CI stops at the first failing step: for S04 and S05 the starting log shows
only the first defect, and the later ones came from the agent's own isolated runs.

- Workflow run: [Real CI logs #44134780212](https://github.com/JeanneBM/langgraph-ci-repair-agent/actions/runs/44134780212)
  on commit `fc57560` (pull request #4), GitHub-hosted `ubuntu-latest`, Python 3.12.
- Model: the scripted demo adapter, so this checks the pipeline (log adapter, policy,
  isolated runs, guarded export), not a model's diagnosis quality.
- Isolation: `venv` backend with a pre-fetched wheelhouse. The same scenarios also
  passed in the network-less container job of the same run.

## Starting logs

Each fixture job ran the synthetic repository's own CI commands in a fresh virtual
environment. The agent job downloaded the logs through `ci_repair/actions_logs.py`.

| Scenario | Fixture job | Raw log SHA-256 (first 16) | Error in the real log |
|---|---|---|---|
| S01 | [134221217086](https://github.com/JeanneBM/langgraph-ci-repair-agent/actions/runs/44134780212/job/134221217086) | `77640f57df3fb9b2` | `ERROR: Could not open requirements file: [Errno 2] No such file or directory: 'requirement.txt'` |
| S02 | [134221217284](https://github.com/JeanneBM/langgraph-ci-repair-agent/actions/runs/44134780212/job/134221217284) | `f2f11467a1e05a63` | `E   ModuleNotFoundError: No module named 'requests'` |
| S03 | [134221217704](https://github.com/JeanneBM/langgraph-ci-repair-agent/actions/runs/44134780212/job/134221217704) | `c2a8ddf0dd0fd5e5` | `.../.venv/bin/python: No module named pytests` |
| S04 | [134221217290](https://github.com/JeanneBM/langgraph-ci-repair-agent/actions/runs/44134780212/job/134221217290) | `3f22d27250309a5a` | Only the `requirement.txt` error; the missing `requests` is not visible yet |
| S05 | [134221217167](https://github.com/JeanneBM/langgraph-ci-repair-agent/actions/runs/44134780212/job/134221217167) | `e767c37e2363dc69` | Only the `requirement.txt` error; `pytests` and `requests` are not visible yet |
| S06 | [134221217276](https://github.com/JeanneBM/langgraph-ci-repair-agent/actions/runs/44134780212/job/134221217276) | `cf6104ee0aa1dcc2` | `FAILED tests/test_app.py::test_sum - assert (2 + 2) == 5` |

## Outcome

From the [agent job](https://github.com/JeanneBM/langgraph-ci-repair-agent/actions/runs/44134780212/job/134221381620):

| Scenario | Attempts | Isolated runs | Final status | Expected |
|---|---|---|---|---|
| S01 | 1 | passed | draft_prepared | yes |
| S02 | 1 | passed | draft_prepared | yes |
| S03 | 1 | passed | draft_prepared | yes |
| S04 | 2 | failed, passed | draft_prepared | yes |
| S05 | 3 | failed, failed, passed | draft_prepared | yes |
| S06 | 1 | - | escalated | yes |

Every export was allowed by rule `export-lab-verified`, which requires human approval,
static validation, an isolated pass of the same snapshot and no test changes. S06
stopped at `ci.accept_proposal`: no repair rule matches a failing assertion, so the
scripted model proposed no change and the policy denied it (`proposal-is-noop`). An edit
to the test itself would have been denied by `protected-tests`.

## Limits

The repositories are synthetic fixtures, and the approval was given by the script. The
report artifact (`real-logs-report`) is kept for the workflow's retention period; the
table above copies its summary from the job log.
