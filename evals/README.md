# Local Eval Suite

This directory contains small, deterministic benchmark tasks for Simple Coding Agent.

The suite is intentionally offline. It does not call an LLM. Instead, it defines fixture projects and checks whether a completed candidate workspace satisfies the expected outcome.

## Tasks

The first five tasks are:

- `fix_failing_pytest` - fix a failing unit test without changing the test.
- `add_type_hints` - add public function annotations while preserving behavior.
- `refactor_small_module` - refactor a small module and keep tests green.
- `add_cli_argument` - add a `--shout` CLI flag.
- `update_readme_and_test` - update README documentation and its synchronized test.

Each task checks:

- only allowed files changed
- required content exists
- pytest passes in the candidate workspace
- `.sca/final_report.md` exists and includes `tests`, `files`, and `risk`

## Usage

Copy initial fixtures into a working directory:

```bash
python -m evals.run_evals --candidate-root tmp/eval-runs --copy-fixtures-to tmp/eval-runs
```

Run an agent manually against each copied task directory. Then evaluate:

```bash
python -m evals.run_evals --candidate-root tmp/eval-runs
```

The expected candidate layout is:

```text
tmp/eval-runs/
  fix_failing_pytest/
  add_type_hints/
  refactor_small_module/
  add_cli_argument/
  update_readme_and_test/
```

Each candidate task directory should contain `.sca/final_report.md`.
