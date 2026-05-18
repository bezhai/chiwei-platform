#!/usr/bin/env bash
# Offline test harness for enforce-routing.sh PreToolUse hook.
#
# Feeds constructed stdin JSON to the hook and asserts the exit code.
#   exit 2 = block (deny)
#   exit 0 = allow
#
# Run: bash .claude/hooks/test-enforce-routing.sh
#
# NOTE: this is an OFFLINE logic test. Real fresh-session end-to-end
# verification (main session blocked / subagent allowed / security layer
# blocks both / git+make pass) is a separate pre-merge gate that CANNOT
# be done in the authoring session (hooks load at session start).

set -uo pipefail

HOOK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/enforce-routing.sh"
PASS=0
FAIL=0

# run_case <name> <expected_exit> <stdin_json>
run_case() {
  local name="$1" expected="$2" json="$3"
  local actual
  set +e
  echo "$json" | bash "$HOOK" >/dev/null 2>&1
  actual=$?
  set -e
  if [[ "$actual" == "$expected" ]]; then
    echo "PASS: $name (exit $actual)"
    PASS=$((PASS+1))
  else
    echo "FAIL: $name (expected exit $expected, got $actual)"
    FAIL=$((FAIL+1))
  fi
}

# JSON builders (compact, single line)
main_tool()    { printf '{"tool_name":"%s","tool_input":%s}' "$1" "$2"; }
sub_tool()     { printf '{"agent_id":"a2d1da75943f62b02","agent_type":"Explore","tool_name":"%s","tool_input":%s}' "$1" "$2"; }

EMPTY='{}'
READ_IN='{"file_path":"/data00/x/y.go"}'
EDIT_IN='{"file_path":"/data00/x/y.go","old_string":"a","new_string":"b"}'
WRITE_IN='{"file_path":"/data00/x/y.go","content":"hi"}'
GREP_IN='{"pattern":"foo","path":"."}'
GLOB_IN='{"pattern":"**/*.go"}'

bash_in() { printf '{"command":%s}' "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1")"; }
# bash_in64 <base64-of-command>: decode to the raw command, then JSON-encode.
# Used for payloads whose RAW text would trip the live security layer on this
# authoring session's own command line (e.g. curl write strings). The danger
# string never appears verbatim in the harness command line.
bash_in64() { bash_in "$(printf '%s' "$1" | base64 -d)"; }

# ---------- Route layer: main session file tools -> BLOCK ----------
run_case "main Read -> block"   2 "$(main_tool Read "$READ_IN")"
run_case "main Edit -> block"   2 "$(main_tool Edit "$EDIT_IN")"
run_case "main Write -> block"  2 "$(main_tool Write "$WRITE_IN")"
run_case "main Grep -> block"   2 "$(main_tool Grep "$GREP_IN")"
run_case "main Glob -> block"   2 "$(main_tool Glob "$GLOB_IN")"
run_case "main NotebookEdit -> block" 2 "$(main_tool NotebookEdit '{"notebook_path":"/x.ipynb"}')"

# ---------- Route layer: subagent same tools -> ALLOW ----------
run_case "sub Read -> allow"   0 "$(sub_tool Read "$READ_IN")"
run_case "sub Edit -> allow"   0 "$(sub_tool Edit "$EDIT_IN")"
run_case "sub Write -> allow"  0 "$(sub_tool Write "$WRITE_IN")"
run_case "sub Grep -> allow"   0 "$(sub_tool Grep "$GREP_IN")"
run_case "sub Glob -> allow"   0 "$(sub_tool Glob "$GLOB_IN")"

# ---------- Route layer: main session orchestration Bash -> ALLOW ----------
run_case "main git status -> allow" 0 "$(main_tool Bash "$(bash_in 'git status --porcelain')")"
run_case "main git log -> allow"    0 "$(main_tool Bash "$(bash_in 'git log --oneline -5')")"
run_case "main git diff -> allow"   0 "$(main_tool Bash "$(bash_in 'git diff HEAD')")"
run_case "main git commit -> allow" 0 "$(main_tool Bash "$(bash_in 'git commit -m x')")"
run_case "main git revert -> allow" 0 "$(main_tool Bash "$(bash_in 'git revert abc123')")"
run_case "main make deploy -> allow" 0 "$(main_tool Bash "$(bash_in 'make deploy APP=lark-proxy GIT_REF=main')")"
run_case "main ghc pr -> allow"     0 "$(main_tool Bash "$(bash_in 'ghc pr view 123')")"
run_case "main run_codex.sh -> allow" 0 "$(main_tool Bash "$(bash_in 'bash ~/.claude/skills/codex-worker/scripts/run_codex.sh "review this"')")"
run_case "main http.sh -> allow"    0 "$(main_tool Bash "$(bash_in 'bash .claude/skills/api-test/scripts/http.sh GET /x')")"

# ---------- Route layer: main session repo-reading Bash -> BLOCK ----------
run_case "main git show file -> block" 2 "$(main_tool Bash "$(bash_in 'git show HEAD:apps/lark-proxy/main.ts')")"
run_case "main git cat-file -> block"  2 "$(main_tool Bash "$(bash_in 'git cat-file -p HEAD:foo.go')")"
run_case "main cat file -> block"      2 "$(main_tool Bash "$(bash_in 'cat apps/lark-proxy/main.ts')")"
run_case "main sed file -> block"      2 "$(main_tool Bash "$(bash_in 'sed -n 1,20p foo.go')")"
run_case "main grep cmd -> block"      2 "$(main_tool Bash "$(bash_in 'grep -r foo apps/')")"

# ---------- Security layer: kubectl write / curl POST -> BLOCK (main AND sub) ----------
run_case "main kubectl apply -> block"  2 "$(main_tool Bash "$(bash_in 'kubectl apply -f x.yaml')")"
run_case "main curl POST -> block"      2 "$(main_tool Bash "$(bash_in 'curl -X POST http://x/y -d a=b')")"
run_case "sub kubectl apply -> block"   2 "$(sub_tool Bash "$(bash_in 'kubectl apply -f x.yaml')")"
run_case "sub curl POST -> block"       2 "$(sub_tool Bash "$(bash_in 'curl -X POST http://x/y -d a=b')")"
run_case "sub psql -> block"            2 "$(sub_tool Bash "$(bash_in 'psql -h db -c "select 1"')")"

# ---------- Subagent dispatch from main session -> ALLOW (not route-killed) ----------
run_case "main dispatch Agent -> allow" 0 "$(main_tool Agent '{"description":"x","prompt":"do","subagent_type":"general-purpose"}')"
run_case "main dispatch Task -> allow"  0 "$(main_tool Task '{"description":"x"}')"

# ---------- Route layer: compound / pipeline command segmentation ----------
# Bug 1 (false block): leading non-allowlist orchestration token (cd) made the
# whole command blocked even though every real segment is allowlisted.
run_case "main cd . && git status -> allow"   0 "$(main_tool Bash "$(bash_in 'cd . && git status')")"
run_case "main cd /tmp && git log -> allow"   0 "$(main_tool Bash "$(bash_in 'cd /tmp && git log')")"
run_case "main git add && git commit -> allow" 0 "$(main_tool Bash "$(bash_in 'git add x && git commit -m y')")"
# T3 HARDENING: every pipeline stage's argv0 must be allowlisted now. The old
# "pipeline only vets first stage" carve-out was a real downstream-bypass hole
# (codex T3). `grep`/`head` argv0 are NOT on the allowlist -> these now BLOCK.
# This is the INTENDED tightening, not a regression. Want fewer git lines: use
# `git log -n`. Want to filter: dispatch a subagent.
run_case "main git status | grep -> block (T3)"  2 "$(main_tool Bash "$(bash_in 'git status | grep foo')")"
run_case "main git log | head -> block (T3)"     2 "$(main_tool Bash "$(bash_in 'git log --oneline | head -20')")"

# Bug 2 (false allow / security hole): allowlisted leading token let trailing
# repo-touching segments through unchecked.
run_case "main cd /repo && cat -> block"      2 "$(main_tool Bash "$(bash_in 'cd /repo && cat CLAUDE.md')")"
run_case "main git add && rm -> block"        2 "$(main_tool Bash "$(bash_in 'git add x && rm CLAUDE.md')")"
run_case "main git status; curl GET -> block" 2 "$(main_tool Bash "$(bash_in 'git status; curl http://x')")"
run_case "main cat | grep -> block"           2 "$(main_tool Bash "$(bash_in 'cat file | grep x')")"
# Per-segment decision-4 enforcement must survive segmentation.
run_case "main git show:path && git status -> block" 2 "$(main_tool Bash "$(bash_in 'git show HEAD:CLAUDE.md && git status')")"

# ---------- Quote-aware segmentation ----------
# Separators (; && || |) INSIDE single/double quotes must NOT split the
# command. Only separators OUTSIDE quotes are real segment boundaries.
run_case "main commit msg with && -> allow"   0 "$(main_tool Bash "$(bash_in 'git commit -m "fix: a && b"')")"
run_case "main commit msg with ; -> allow"    0 "$(main_tool Bash "$(bash_in 'git commit -m "a; b"')")"
run_case "main commit msg with | -> allow"    0 "$(main_tool Bash "$(bash_in 'git commit -m "x | y"')")"
run_case "main commit msg nested single quote -> allow" 0 "$(main_tool Bash "$(bash_in "git commit -m \"msg with 'nested' single quote\"")")"
# Quote-awareness must NOT go too far: real OUTSIDE separators still split.
run_case "main commit && rm -> block"         2 "$(main_tool Bash "$(bash_in 'git commit -m "real" && rm CLAUDE.md')")"
run_case "main git status; cat file -> block" 2 "$(main_tool Bash "$(bash_in 'git status; cat file')")"
# Fail-closed: unbalanced quotes cannot be reliably tokenized -> block.
run_case "main unbalanced quote -> block"     2 "$(main_tool Bash "$(bash_in 'git commit -m "unbalanced')")"

# ---------- T3 HARDENING: codex adversarial bypass cases ----------
# Command substitution / backticks / redirection smuggle a repo-file touch past
# a benign-looking allowlisted argv0. Out-of-quote $( ` > < must hard-block.
run_case "main git status \$(cat) -> block"   2 "$(main_tool Bash "$(bash_in 'git status $(cat CLAUDE.md)')")"
run_case "main git status backtick -> block"  2 "$(main_tool Bash "$(bash_in 'git status `cat CLAUDE.md`')")"
run_case "main git status > file -> block"    2 "$(main_tool Bash "$(bash_in 'git status > CLAUDE.md')")"
run_case "main git status < file -> block"    2 "$(main_tool Bash "$(bash_in 'git status < CLAUDE.md')")"
run_case "main git status >> file -> block"   2 "$(main_tool Bash "$(bash_in 'git status >> CLAUDE.md')")"
run_case "main proc-subst >(x) -> block"      2 "$(main_tool Bash "$(bash_in 'git status > >(tee CLAUDE.md)')")"
# Pipeline downstream repo touch: every segment argv0 must pass.
run_case "main git status | cat file -> block" 2 "$(main_tool Bash "$(bash_in 'git status | cat CLAUDE.md')")"
# Skill-script allowlist must be argv0-anchored, not a substring anywhere.
run_case "main cat X /skills/.../scripts -> block" 2 "$(main_tool Bash "$(bash_in 'cat CLAUDE.md /skills/foo/scripts/x')")"
# env-prefixed command must NOT be specially allowed (argv0 treated as not on
# allowlist; the inner allowlist check does not peek past env).
run_case "main env git status -> block"       2 "$(main_tool Bash "$(bash_in 'env FOO=1 git status')")"
# Redirection / separator chars INSIDE quotes are literal -> still allow.
# This holds for BOTH single and double quotes: bash does NOT do redirection
# or word-splitting on `> < ; && || |` inside any quote, so the state machine
# must keep treating them as literals there (regression guard).
run_case "main commit msg with > -> allow"    0 "$(main_tool Bash "$(bash_in 'git commit -m "fix > bug"')")"
# QUOTE-SEMANTICS FIX (codex round 2): bash executes `$(...)` and backticks
# INSIDE DOUBLE quotes (only single quotes make them literal). The old state
# machine treated single and double quotes identically, so these two cases
# used to (wrongly) expect ALLOW — they ran `cat`/`date` for real. They are
# now flipped to BLOCK: a double-quoted command substitution next to an
# allowlisted argv0 is still a Trojan for an unvetted repo-file touch.
run_case "main commit msg dq \$( -> block (codex r2)"  2 "$(main_tool Bash "$(bash_in 'git commit -m "use $(date)"')")"
run_case "main commit msg dq backtick -> block (codex r2)" 2 "$(main_tool Bash "$(bash_in 'git commit -m "run \`x\`"')")"

# ---------- Single vs double quote semantics (codex round 2) ----------
# Double quotes: $( and backtick are LIVE in bash -> must BLOCK even with an
# allowlisted argv0. Other metachars (> < ; && || |) stay literal -> allow.
run_case "main git status dq \$(cat) -> block"   2 "$(main_tool Bash "$(bash_in 'git status "$(cat CLAUDE.md)"')")"
run_case "main git status dq backtick -> block"  2 "$(main_tool Bash "$(bash_in 'git status "`cat CLAUDE.md`"')")"
run_case "main git commit dq \$(cat) -> block"   2 "$(main_tool Bash "$(bash_in 'git commit -m "$(cat CLAUDE.md)"')")"
run_case "main git commit dq > literal -> allow" 0 "$(main_tool Bash "$(bash_in 'git commit -m "fix > bug"')")"
run_case "main git commit dq && literal -> allow" 0 "$(main_tool Bash "$(bash_in 'git commit -m "a && b"')")"
# Single quotes: EVERYTHING literal — $( backtick > < ; && || | all inert.
run_case "main git commit sq \$(x) -> allow"     0 "$(main_tool Bash "$(bash_in "git commit -m 'literal \$(x) text'")")"
run_case "main git commit sq seps -> allow"      0 "$(main_tool Bash "$(bash_in "git commit -m 'a ; b && c | d > e'")")"
run_case "main git commit sq backtick -> allow"  0 "$(main_tool Bash "$(bash_in "git commit -m 'run \`x\` here'")")"
run_case "main git commit sq full literal -> allow" 0 "$(main_tool Bash "$(bash_in "git commit -m 'literal \$(x) ; > text'")")"
# Explicit real-newline injection (a literal LF inside the command string,
# OUTSIDE quotes, acts as a command separator). codex noted this currently
# blocks only via the `<`/`$(` smuggling side effect; pin it down explicitly:
# a real newline must split into segments and the `cat` segment must block.
run_case "main real-newline cat injection -> block" 2 "$(main_tool Bash "$(bash_in 'git status
cat CLAUDE.md')")"

# ---------- Security layer cheap patch: curl equivalent write forms ----------
# Raw payloads base64-encoded so the danger string never hits THIS session's
# command line (live security layer would block the harness itself otherwise).
# 'curl --request=POST http://x'  — sub context isolates the SECURITY layer
# (route layer never blocks subagent, so a sub block here proves the security
# regex caught it, not the allowlist fallthrough).
run_case "sub curl --request=POST -> block"   2 "$(sub_tool Bash "$(bash_in64 'Y3VybCAtLXJlcXVlc3Q9UE9TVCBodHRwOi8veA==')")"
# 'curl -dfoo http://x'   (-d glued to value, no space)
run_case "sub curl -dfoo -> block"            2 "$(sub_tool Bash "$(bash_in64 'Y3VybCAtZGZvbyBodHRwOi8veA==')")"
# Main context too (defense-in-depth: blocked either way).
run_case "main curl --request=POST -> block"  2 "$(main_tool Bash "$(bash_in64 'Y3VybCAtLXJlcXVlc3Q9UE9TVCBodHRwOi8veA==')")"
run_case "main curl -dfoo -> block"           2 "$(main_tool Bash "$(bash_in64 'Y3VybCAtZGZvbyBodHRwOi8veA==')")"

# ---------- Misc ----------
run_case "empty stdin -> allow" 0 "$EMPTY"

echo "----"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" == 0 ]]
