#!/usr/bin/env bash
# PreToolUse hook — two layers:
#
#   1. SECURITY layer (unconditional, main session AND subagent):
#      kubectl write / curl write / direct internal connection / DB client
#      -> hard deny. Migrated from the old validate-bash.sh, rewritten to
#      the official stdin-JSON contract. agent_id NEVER exempts this layer.
#
#   2. ROUTE layer (main session ONLY; subagent passes):
#      Any tool with repo-file read/write capability (Read/Edit/Write/Grep/
#      Glob/NotebookEdit/LSP/MultiEdit/...) -> block. Bash is dual-use:
#      main session only passes an orchestration allowlist (git VCS, make,
#      ghc, deploy, skill-driven script calls); everything else -> block.
#      `git show <path>` / `git cat-file` count as repo reading -> block.
#
# Main session vs subagent is decided by KEY PRESENCE of "agent_id" in the
# stdin JSON (Task 1 nailed-down contract): key absent => main session;
# key present => subagent. NEVER a value comparison.
#
# Exit codes: 0 = allow, 2 = deny (stderr fed back to Claude).

set -uo pipefail

PAYLOAD="$(cat 2>/dev/null || true)"

if [[ -z "${PAYLOAD//[[:space:]]/}" ]]; then
  exit 0
fi

# Parse fields with python3. Emits exactly 3 lines:
#   line1: "main" or "sub"   (agent_id key presence)
#   line2: tool_name
#   line3: command (only for Bash; else empty)
# Bad-JSON contract is unchanged: fail-OPEN as "main" with empty command.
PARSED="$(printf '%s' "$PAYLOAD" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print("main"); print(""); print(""); sys.exit(0)
ctx = "sub" if ("agent_id" in d) else "main"
tool = d.get("tool_name", "") or ""
ti = d.get("tool_input", {})
cmd = ti.get("command", "") if isinstance(ti, dict) else ""
print(ctx)
print(tool)
print(cmd if isinstance(cmd, str) else "")
' 2>/dev/null)" || PARSED=$'main\n\n'

CTX="$(printf '%s' "$PARSED" | sed -n '1p')"
TOOL="$(printf '%s' "$PARSED" | sed -n '2p')"
COMMAND="$(printf '%s' "$PARSED" | sed -n '3,$p')"

# ============================================================
# SECURITY LAYER — unconditional (main AND subagent)
# Only inspects Bash commands.
# ============================================================
if [[ -n "$COMMAND" ]]; then

  KUBECTL_WRITE_RE='kubectl\s+(apply|delete|edit|patch|create|exec|run|replace|scale|rollout|label|annotate|taint|drain|cordon|uncordon|cp|port-forward|proxy)\b'
  if echo "$COMMAND" | grep -qPi "$KUBECTL_WRITE_RE"; then
    echo "BLOCKED (security): kubectl write/bypass operation detected. Use make commands or \$PAAS_API instead." >&2
    exit 2
  fi

  # Cheap equivalence patch (T3): also catch `--request=POST` (equals form)
  # alongside the space form. Deliberately NOT chasing shell-token obfuscation
  # like kube""ctl — .claude/settings.json permission deny/ask is the
  # defense-in-depth backstop for that.
  CURL_WRITE_RE='curl\b.*(-X\s*(POST|PUT|DELETE|PATCH)\b|-X(POST|PUT|DELETE|PATCH)\b|--request[=\s]*(POST|PUT|DELETE|PATCH)\b)'
  if echo "$COMMAND" | grep -qPi "$CURL_WRITE_RE"; then
    echo "BLOCKED (security): curl write request detected. Use make commands instead of direct curl." >&2
    exit 2
  fi

  # `-d` glued straight to its value with no space (`-dfoo`, `-d@file`) is an
  # implicit POST just like `-d foo`; the old regex required a space after -d.
  CURL_DATA_RE='curl\b.*(\s-d\s|\s-d$|\s-d\S|\s--data\s|\s--data-raw\s|\s--data-binary\s|\s--data-urlencode\s|\s-F\s|\s--form\s)'
  if echo "$COMMAND" | grep -qPi "$CURL_DATA_RE"; then
    if ! echo "$COMMAND" | grep -qPi 'curl\b.*(-X\s*GET\b|-XGET\b|--request\s*GET\b)'; then
      echo "BLOCKED (security): curl with data payload (implicit POST) detected. Use make commands instead of direct curl." >&2
      exit 2
    fi
  fi

  INTERNAL_RE='(curl|wget|http)\b.*(\.svc\.cluster\.local|\.prod\.svc|localhost:[0-9]|127\.0\.0\.1:[0-9]|10\.[0-9]+\.[0-9]+\.[0-9]+:[0-9])'
  if echo "$COMMAND" | grep -qPi "$INTERNAL_RE"; then
    echo "BLOCKED (security): direct connection to internal service detected. All API access must go through \$PAAS_API." >&2
    exit 2
  fi

  DB_CLIENT_RE='\b(psql|mysql|mongosh|redis-cli)\b'
  if echo "$COMMAND" | grep -qPi "$DB_CLIENT_RE"; then
    echo "BLOCKED (security): direct database client connection detected. Use 'make ops-query' instead." >&2
    exit 2
  fi
fi

# ============================================================
# ROUTE LAYER — main session ONLY. Subagent passes everything
# the security layer didn't already block.
# ============================================================
if [[ "$CTX" == "sub" ]]; then
  exit 0
fi

# Block message: design-intercept wording (anti-livelock load-bearing).
route_block() {
  echo "BLOCKED (subagent-routing): this is a DESIGN interception, not a transient error. The main session must NOT touch repo files directly (investigate or modify). $1 Immediately dispatch a subagent (Explore for read/investigate, general-purpose for modify) to perform this. Do NOT retry this same operation in the main session — retrying will be blocked again. Relay only the conclusion + diff + evidence back." >&2
  exit 2
}

# --- Repo-file-capable tools: blanket block in main session ---
# Enumerated by capability, not by example. Canonical Claude Code harness
# file tools that can read or write repo files.
case "$TOOL" in
  Read|Edit|MultiEdit|Write|Grep|Glob|NotebookEdit|NotebookRead|LSP)
    route_block "Tool '$TOOL' reads/writes repo files."
    ;;
esac

# --- Non-Bash, non-file tools (Agent/Task dispatch, WebFetch, Skill, ...) ---
# Not a file tool -> allow. Subagent dispatch itself MUST pass.
if [[ "$TOOL" != "Bash" ]]; then
  exit 0
fi

# --- Bash in main session: orchestration allowlist only ---
if [[ -z "$COMMAND" ]]; then
  exit 0
fi

# A single command line can be a chain of independent commands joined by
# `;`, `&&`, `||`. Each independent command may itself be a pipeline
# `A | B | C`. Bug-fix rationale:
#   * Bug 1 (false block): the old code matched only the FIRST token of the
#     whole line, so `cd . && git status` was blocked because it starts with
#     `cd` even though every real segment is fine.
#   * Bug 2 (false allow / security hole): an allowlisted leading token like
#     `git add` let trailing segments (`&& rm <repo file>`) through totally
#     unchecked — the security layer only catches kubectl/curl/db, not rm/cat.
# Fix: split into independent commands; EVERY one must pass; for pipelines we
# only vet the FIRST stage (later stages just consume stdout: head/grep/jq/
# wc/awk/sed/cut/sort/uniq — they don't touch repo files). Decision-4
# (`git show <rev>:<path>` / `git cat-file`) is enforced PER SEGMENT so
# segmentation cannot smuggle a repo read past it.

# seg_is_allowed <pipeline-stage> -> exit 0 if this stage's argv0 is on the
# orchestration allowlist (decision-4 repo reads are NOT "allowed" here; they
# route_block). EVERY pipeline stage of EVERY independent command is fed here
# (T3 hardening removed the old "only vet first stage" downstream-bypass hole).
# argv0 is anchored: leading `bash `/`sh `/`zsh ` wrappers are stripped first;
# `env ` is NOT stripped (an env-prefixed command is treated as not-allowlisted
# on purpose — no special-casing). The skill-script allowlist matches argv0
# being the script path, never a substring anywhere in the args.
seg_is_allowed() {
  local first="$1"
  # Trim leading/trailing whitespace.
  first="$(printf '%s' "$first" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"

  [[ -z "$first" ]] && return 0   # empty segment (e.g. trailing ;) is inert

  # Strip leading interpreter wrappers so argv0 is the real command word.
  # `env` is intentionally NOT stripped: `env FOO=1 git status` keeps argv0
  # = `env`, which is not on the allowlist -> blocks (no special allowance).
  while [[ "$first" =~ ^(bash|sh|zsh)[[:space:]]+ ]]; do
    first="$(printf '%s' "$first" | sed -E 's/^(bash|sh|zsh)[[:space:]]+//')"
    first="$(printf '%s' "$first" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  done

  [[ -z "$first" ]] && return 0

  # Decision 4 — repo reading via git, enforced on THIS segment's first stage.
  if echo "$first" | grep -qPi '^git\s+show\s+\S*:\S'; then
    route_block "'git show <rev>:<path>' reads a repo file (decision 4)."
  fi
  if echo "$first" | grep -qPi '^git\s+cat-file\b'; then
    route_block "'git cat-file' reads repo file content (decision 4)."
  fi

  # git VCS / orchestration subcommands
  if echo "$first" | grep -qPi '^git\s+(status|log|diff|branch|rev-parse|show-ref|ls-files|add|commit|push|fetch|pull|remote|merge|rebase|tag|reset|cherry-pick|revert|switch|checkout|worktree|stash|config)\b'; then
    return 0
  fi
  # bare `git show` (no rev:path) e.g. `git show HEAD --stat` is VCS-ish; allow.
  if echo "$first" | grep -qPi '^git\s+show\b' && ! echo "$first" | grep -qPi '^git\s+show\s+\S*:\S'; then
    return 0
  fi
  # make / ghc / deploy orchestration
  if echo "$first" | grep -qPi '^(make|ghc)\b'; then
    return 0
  fi
  # cd only changes directory, never touches repo file content -> allowed
  # segment. Segments AFTER cd are still each vetted, so this opens no hole.
  if echo "$first" | grep -qPi '^cd\b'; then
    return 0
  fi
  # skill-driven script calls (decision 5) — codex-worker / api-test / ops etc.
  # These ARE the collaboration chain; cannot be delegated to a subagent.
  # argv0-ANCHORED (^...): the FIRST word must itself BE the skill script path.
  # `bash `/`sh `/`zsh ` wrappers are already stripped above, so a legit call
  # `bash ~/.claude/skills/codex-worker/scripts/run_codex.sh ...` arrives here
  # as `~/.claude/.../run_codex.sh ...`. A command merely MENTIONING such a
  # path as a later arg (`cat X /skills/foo/scripts/x`, argv0=`cat`) does NOT
  # match -> blocks (closes the codex substring-anywhere hole).
  if echo "$first" | grep -qPi '^\S*(run_codex\.sh|codex-worker/scripts/|api-test/scripts/http\.sh|/skills/[^[:space:]]*/scripts/)'; then
    return 0
  fi
  # direct codex CLI invocation is part of the collaboration chain
  if echo "$first" | grep -qPi '^codex\b'; then
    return 0
  fi

  return 1
}

# --- Quote-aware command segmentation + smuggling-metachar guard ---
# The old sed split on literal ; && || | regardless of quoting, so a legit
# `git commit -m "fix: a && b"` got shredded into bogus segments and blocked.
# Fix: a quote-aware scanner (single AND double quotes; quotes nested inside
# the other kind are literal). Only separators OUTSIDE quotes split.
#
# T3 hardening — robust-by-default, NOT a whack-a-mole shell parser:
#   * SMUGGLING GUARD: if any of  $(  `  >  <  (covers >> << >( <( )  appears
#     OUTSIDE quotes, an allowlisted argv0 (`git status $(cat CLAUDE.md)`,
#     `git status > CLAUDE.md`) is a Trojan for a repo-file touch -> hard
#     route_block. exit(4) signals this.
#   * SINGLE vs DOUBLE quote semantics (codex round 2 fix): bash makes
#     `$(...)` and backtick command substitution LITERAL only inside SINGLE
#     quotes. Inside DOUBLE quotes they are still EXECUTED. So:
#       - inside '...'  : everything literal ($( ` > < ; && || | all inert)
#       - inside "..."  : $( and backtick STILL hard-block (they run a real
#                         command); but > < ; && || | stay literal (bash does
#                         no redirection / word-splitting inside quotes), so
#                         `git commit -m "fix > bug"` / `"a && b"` stay allowed
#       - outside quotes: everything triggers as before
#     The OLD machine treated both quote kinds identically, letting
#     `git status "$(cat CLAUDE.md)"` smuggle a `cat` past the allowlist.
#   * Emit EVERY pipeline stage of EVERY independent command (not just the
#     first). The old "first stage only" carve-out let `git status | cat
#     CLAUDE.md` smuggle a downstream repo read past the allowlist.
#
# fail-CLOSED contract: if the command cannot be reliably tokenized (e.g.
# unbalanced quotes), python exits non-zero and we route_block. A command we
# can't parse is treated as a deny, NEVER fail-open — the main session can
# re-dispatch via subagent, but smuggling an unparseable command through is a
# hole. (This is distinct from the bad-JSON path above, which stays fail-open
# and is intentionally NOT changed here.)
SEGMENTS="$(printf '%s' "$COMMAND" | python3 -c '
import sys

s = sys.stdin.read()
segs = []          # list of independent commands (each: list of pipeline stages)
cur = ""           # current pipeline-stage buffer
stages = []        # pipeline stages of the current independent command
quote = None       # active quote char, or None
i = 0
n = len(s)
while i < n:
    c = s[i]
    if quote:
        # Inside a DOUBLE quote, bash STILL executes command substitution:
        # `"$(cat F)"` and "`cat F`" run `cat` for real. So $( and backtick
        # must still hard-block here. Everything else inside any quote (and
        # EVERYTHING inside a single quote) is a literal: > < ; && || | are
        # inert because bash does no redirection / word-splitting in quotes.
        if quote == "\x22":   # double quote: substitution is live
            if c == "$" and i + 1 < n and s[i + 1] == "(":
                sys.exit(4)
            if c == "`":
                sys.exit(4)
        cur += c
        if c == quote:
            quote = None
        i += 1
        continue
    if c == "\\" and i + 1 < n:
        # backslash escape OUTSIDE quotes: keep both chars, no split
        cur += c + s[i + 1]
        i += 2
        continue
    if c == "\x27" or c == "\x22":   # single / double quote opens
        quote = c
        cur += c
        i += 1
        continue
    # SMUGGLING GUARD (out of quotes): command substitution / redirection /
    # process substitution. Any one of these alongside an allowlisted argv0
    # is a Trojan -> deny the whole command. Covers `$(`, backtick, > >> >(
    # < << <( by matching the single chars $( ` > < .
    if c == "$" and i + 1 < n and s[i + 1] == "(":
        sys.exit(4)
    if c == "`" or c == ">" or c == "<":
        sys.exit(4)
    if c == ";" or c == "\n":
        # A real newline OUTSIDE quotes is a command separator just like `;`
        # (`git status\ncat CLAUDE.md` is two commands). Split explicitly so
        # the `cat` segment is vetted on its own merits, not blocked only as
        # an incidental side effect of some other guard (codex round 2).
        stages.append(cur); segs.append(stages); cur = ""; stages = []
        i += 1
        continue
    if c == "&" and i + 1 < n and s[i + 1] == "&":
        stages.append(cur); segs.append(stages); cur = ""; stages = []
        i += 2
        continue
    if c == "|" and i + 1 < n and s[i + 1] == "|":
        stages.append(cur); segs.append(stages); cur = ""; stages = []
        i += 2
        continue
    if c == "|":
        stages.append(cur); cur = ""
        i += 1
        continue
    cur += c
    i += 1

if quote is not None:
    # unbalanced quote -> cannot tokenize reliably -> fail-closed
    sys.exit(3)

stages.append(cur)
segs.append(stages)

# Emit EVERY pipeline stage of EVERY independent command, one per line, so
# seg_is_allowed vets each argv0. Empty stages (trailing ;) emit a blank line
# which seg_is_allowed treats as inert.
out = []
for st in segs:
    if st:
        out.extend(st)
    else:
        out.append("")
sys.stdout.write("\n".join(out))
'
)"
RC=$?
if [[ "$RC" == 4 ]]; then
  route_block "Bash command uses command substitution / redirection / process substitution (\$( \` > < >> << >( <( ) outside quotes; an allowlisted command word here is a Trojan for an unvetted repo-file touch. Treated as a deny (T3 hardening)."
elif [[ "$RC" != 0 ]]; then
  route_block "Bash command could not be reliably tokenized (likely unbalanced quotes); treated as a deny (fail-closed)."
fi

while IFS= read -r seg; do
  if ! seg_is_allowed "$seg"; then
    bad="$(printf '%s' "$seg" | sed -e 's/^[[:space:]]*//' | cut -c1-60)"
    route_block "Bash segment '$bad' is not on the orchestration allowlist (git VCS / make / ghc / cd / deploy / skill scripts). Every pipeline stage's command word must be allowlisted (T3); reading or modifying repo files via Bash must go through a subagent."
  fi
done <<< "$SEGMENTS"

exit 0
