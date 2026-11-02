#!/usr/bin/env bash
# Run the remaining implementation batches back to back, one fresh session each.
#
#   bash scripts/run_batches.sh                 # all batches A-F, guarded permissions
#   BATCHES="A" bash scripts/run_batches.sh     # just one batch
#   DRY_RUN=1 bash scripts/run_batches.sh       # assemble prompts, run nothing
#
# Unattended mode disables every permission check for the child sessions, which
# means they may run any command and push.  It must be requested twice, and it
# is meant for one developer's own machine on a scratch branch:
#
#   UNATTENDED=1 I_ACCEPT_UNATTENDED=1 bash scripts/run_batches.sh
#
# Each batch is a separate `claude -p` process, so each gets a fresh context.
# A batch must leave `.superpowers/batch-runs/report-<letter>.md`; its text is
# handed to the next batch verbatim (rulings and deferred minors carry forward).
# The script stops at the first batch that fails, so a broken batch is never
# followed by a push of the next one.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 2

DOC="docs/technical-design/one-shot-prompts.md"
RUN_DIR=".superpowers/batch-runs"
BATCHES="${BATCHES:-A B C D E F}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-2}"
RETRY_SLEEP="${RETRY_SLEEP:-60}"
DRY_RUN="${DRY_RUN:-0}"
UNATTENDED="${UNATTENDED:-0}"
ACCEPT_UNATTENDED="${I_ACCEPT_UNATTENDED:-0}"

if [[ ! -f "$DOC" ]]; then
  echo "missing $DOC" >&2
  exit 2
fi

# These values reach the shell as loop words, an arithmetic bound and a sleep
# argument; keep each one to a plain token so nothing else can be interpreted.
if [[ ! "$BATCHES" =~ ^[A-F[:space:]]+$ ]]; then
  echo "BATCHES must list batch letters A-F separated by spaces" >&2
  exit 2
fi
for value in "$MAX_ATTEMPTS" "$RETRY_SLEEP"; do
  if [[ ! "$value" =~ ^[0-9]+$ ]]; then
    echo "MAX_ATTEMPTS and RETRY_SLEEP must be plain non-negative integers" >&2
    exit 2
  fi
done
if ! (( MAX_ATTEMPTS >= 1 )); then
  echo "MAX_ATTEMPTS must be at least 1" >&2
  exit 2
fi

mkdir -p "$RUN_DIR"

# Extract the text between <!-- TAG-BEGIN --> and <!-- TAG-END -->, dropping
# markdown fence lines so the prompt is plain text.
section() {
  awk -v tag="$1" '
    index($0, "<!-- " tag "-BEGIN -->") { on = 1; next }
    index($0, "<!-- " tag "-END -->")   { on = 0; next }
    on && $0 !~ /^```/ { print }
  ' "$DOC"
}

MASTER_TEXT="$(section MASTER)"
if [[ -z "$MASTER_TEXT" ]]; then
  echo "the master prompt markers are missing from $DOC" >&2
  exit 2
fi

# One argument per array element, each quoted, so no word is re-split or
# expanded as a glob when the array is passed to the child process.
PERM_ARGS=(
  "--permission-mode" "acceptEdits"
  "--allowedTools" "Read Edit Write Glob Grep Agent Skill TodoWrite"
  "--allowedTools" "Bash(git *) Bash(py *) Bash(ls *) Bash(mkdir *)"
)
if [[ "$UNATTENDED" == "1" ]]; then
  if [[ "$ACCEPT_UNATTENDED" != "1" ]]; then
    cat >&2 <<'MSG'
UNATTENDED=1 disables every permission check for the child sessions: they may
run any command and push to the configured remote.  Refusing until that is
acknowledged explicitly.

Use a scratch branch, then repeat the request:

  git switch -c auto/remaining
  UNATTENDED=1 I_ACCEPT_UNATTENDED=1 bash scripts/run_batches.sh

Omit UNATTENDED to keep the guarded permission set instead.
MSG
    exit 2
  fi
  echo "WARNING: unattended mode disables permission checks for every batch." >&2
  PERM_ARGS=("--dangerously-skip-permissions")
fi

CARRY=""
for batch in $BATCHES; do
  BATCH_TEXT="$(section "BATCH-$batch")"
  if [[ -z "$BATCH_TEXT" ]]; then
    echo "no task list for batch $batch in $DOC" >&2
    exit 2
  fi

  PROMPT_FILE="$RUN_DIR/prompt-$batch.txt"
  {
    printf '%s\n\n' "$MASTER_TEXT"
    printf '## 本批次代号\n\n批次 %s。最终报告写入 `%s/report-%s.md`。\n\n' \
      "$batch" "$RUN_DIR" "$batch"
    if [[ -n "$CARRY" ]]; then
      printf '## 上一批次的最终报告（裁决与遗留项必须遵守，不要重复裁决）\n\n%s\n\n' "$CARRY"
    fi
    printf '## 9. 本批次任务清单\n\n%s\n' "$BATCH_TEXT"
  } > "$PROMPT_FILE"

  echo "==> batch $batch: prompt written to $PROMPT_FILE ($(wc -c < "$PROMPT_FILE") bytes)"
  if [[ "$DRY_RUN" == "1" ]]; then
    continue
  fi

  LOG="$RUN_DIR/batch-$batch.log"
  REPORT="$RUN_DIR/report-$batch.md"
  rm -f "$REPORT"

  ok=0
  attempt=0
  while (( attempt < MAX_ATTEMPTS )); do
    attempt=$((attempt + 1))
    echo "==> batch $batch: attempt $attempt, log $LOG"
    if claude -p --output-format stream-json --verbose "${PERM_ARGS[@]}" \
         < "$PROMPT_FILE" > "$LOG" 2>&1; then
      ok=1
      break
    fi
    echo "    attempt $attempt failed; see $LOG"
    if (( attempt < MAX_ATTEMPTS )); then sleep "$RETRY_SLEEP"; fi
  done

  if (( ok == 0 )); then
    echo "batch $batch did not finish; stopping without continuing or pushing." >&2
    echo "inspect $LOG, fix the cause, then re-run with BATCHES=\"$batch\"." >&2
    exit 1
  fi

  if [[ ! -s "$REPORT" ]]; then
    echo "batch $batch finished but wrote no $REPORT; refusing to continue blind." >&2
    exit 1
  fi

  CARRY="$(cat "$REPORT")"
  echo "==> batch $batch done"
done

echo "==> all requested batches finished"
