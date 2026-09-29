"""
Multi-agent code review for git changes, like a pull-request review bot.

WHAT IT DOES
  Takes some code changes, lets three "agents" review them at the same time, merges what they
  found, and gives a verdict. The exit code tells git hooks and CI whether to block.

                   +--> security agent    --+
    git diff  ---> +--> reliability agent --+--> aggregator --> ranked report + verdict
                   +--> style agent       --+
                   (run in parallel)           (remove duplicates, sort, decide)

  "Agent" here means a reviewer with ONE narrow job. Two kinds:
    offline (default)  regex rules. Fast, free, no API key. Good for the demo and the git hook.
    --llm              each agent is a Claude API call with a focused instruction.

WHY SEVERAL AGENTS INSTEAD OF ONE
  - a narrow job misses less than "review everything"
  - they run in parallel: total time = the slowest agent, not all of them added up
  - if one agent crashes, the others still report (and the verdict becomes INCOMPLETE, see below)

HOW TO RUN
  python code_review/review.py --diff main          only the lines your branch changed vs main
  python code_review/review.py --staged             only what you `git add`-ed (the pre-commit hook uses this)
  python code_review/review.py demo_app/payments.py a whole file
  Options:  --llm (Claude agents, needs ANTHROPIC_API_KEY)   --markdown (PR comment format)   --all (show style notes)

EXIT CODES (this is how git hooks and CI use a tool: 0 = OK, anything else = fail)
    0 = APPROVE
    1 = REQUEST CHANGES   at least one critical or high finding
    2 = INCOMPLETE        an agent failed. FAIL CLOSED: a review that didn't run is never an approval.

A "finding" is a plain dict:
  {"agent": "security", "file": "demo_app/payments.py", "line": 14,
   "severity": "critical", "title": "SQL injection ...", "fix": "Use parameterised queries"}
"""
import argparse
import json
import re                      # regular expressions, used by the offline rules
import subprocess              # runs git
import sys
import time
from concurrent.futures import ThreadPoolExecutor   # runs functions in parallel threads

MODEL = "claude-opus-5"
AGENTS = ["security", "reliability", "style"]
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}   # for sorting: critical first

# Don't review the reviewer. Its rule list contains the exact strings it searches for
# (os.system, verify=False, ...), so it flagged itself. A real false positive we hit.
SKIP_PATHS = ["code_review/"]


# ================================================================== 1. WHAT TO REVIEW
def run_git(*args):
    """Run a git command and return its output. Stop the program if git fails."""
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def added_lines_from_diff(diff_text):
    """
    Read `git diff --unified=0` output and keep only the ADDED lines, with their line numbers:
        {"demo_app/payments.py": [(31, "PSP_API_KEY = ..."), (32, "..."), ...]}

    What git diff output looks like:
        +++ b/demo_app/payments.py        <- the file the next changes belong to
        @@ -24,0 +25,26 @@                <- "hunk header": new lines start at line 25
        +import os                         <- an added line (starts with +)
        +import requests
    --unified=0 means "no context lines", so we only see what actually changed.
    """
    changes = {}
    current_file = None
    line_number = 0
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            path = line[4:]                                  # "b/demo_app/payments.py"
            if path.startswith("b/"):
                current_file = path[2:]                      # "demo_app/payments.py"
                changes.setdefault(current_file, [])
            else:
                current_file = None                          # "/dev/null" = the file was deleted
        elif line.startswith("@@"):
            new_part = line.split("+")[1].split(" ")[0]      # "25,26" or "25"
            line_number = int(new_part.split(",")[0])        # 25
        elif line.startswith("+") and current_file:
            changes[current_file].append((line_number, line[1:]))   # drop the leading "+"
            line_number += 1
    return changes


def whole_files(paths):
    """Review full files: every line counts as 'changed'. Same shape as added_lines_from_diff()."""
    changes = {}
    for path in paths:
        lines = open(path).read().splitlines()
        numbered = []
        for number, text in enumerate(lines, start=1):   # enumerate gives (1, first line), (2, second line), ...
            numbered.append((number, text))
        changes[path] = numbered
    return changes


# ================================================================== 2. OFFLINE AGENTS (regex rules)
# Each rule: (regex to search for on a line, severity, title, how to fix)
# A regex is a text pattern. Examples:  \s* = any spaces,  \w+ = a word,  (a|b) = a or b.
RULES = {
    "security": [
        (r"""(api_key|secret|password|token)\s*=\s*["'][^"']{8,}""", "critical", "Hardcoded secret", "Load from Vault/env; rotate the leaked key"),
        (r"""execute\(.*(%|\bf["'])""", "critical", "SQL injection via string formatting", "Use parameterised queries (?, %s placeholders)"),
        (r"pickle\.loads?\(", "critical", "Unsafe deserialisation (pickle)", "Use JSON; never unpickle untrusted data"),
        (r"os\.system\(|shell=True", "high", "Shell command injection", "subprocess.run([...]) with an argument list"),
        (r"verify=False", "high", "TLS verification disabled", "Remove verify=False; fix the CA bundle instead"),
    ],
    "reliability": [
        # (?!.*timeout) = "not followed by the word timeout anywhere on the line"
        (r"requests\.(get|post|put|delete)\((?!.*timeout)", "high", "HTTP call without timeout", "Add timeout=(3, 10); a hung call blocks the worker forever"),
        (r"if\s+\w+\s*>=\s*\w+:", "high", "Check-then-act race on balance", "One atomic UPDATE ... WHERE balance >= ?, or SELECT ... FOR UPDATE"),
        (r"except\s*:", "medium", "Bare except swallows all errors", "Catch specific exceptions and log them"),
        (r"requests\.post\(", "medium", "Payment call has no idempotency key", "Send an Idempotency-Key so retries can't double-charge"),
        (r"fetchone\(\)\[0\]", "medium", "Crashes when the row is missing", "Handle None"),
    ],
    "style": [
        (r"^\s*print\(", "low", "print() instead of logging", "Use logging, with ids as fields"),
        (r"^def \w+\([^):]*\)\s*:", "low", "Missing type hints", "Annotate parameters and return type"),
    ],
}


def offline_agent(agent, changes):
    """One offline agent: check every changed line against this agent's regex rules."""
    findings = []
    for path, lines in changes.items():
        if not path.endswith(".py"):                       # only review Python files
            continue
        skip = False
        for skip_path in SKIP_PATHS:
            if path.startswith(skip_path):
                skip = True
        if skip:
            continue
        for line_number, text in lines:
            code = text.split("  #")[0]                    # ignore trailing comments
            for pattern, severity, title, fix in RULES[agent]:
                if re.search(pattern, code, re.IGNORECASE):
                    findings.append({"agent": agent, "file": path, "line": line_number,
                                     "severity": severity, "title": title, "fix": fix})
    time.sleep(0.05)   # pretend to think, so you can see the parallel speed-up in the timings
    return findings


# ================================================================== 3. LLM AGENTS (Claude API)
# Each agent gets the same code but a different focus in its instructions.
FOCUS = {
    "security": "security vulnerabilities: injection, secrets, authn/authz, unsafe deserialisation, TLS, SSRF",
    "reliability": "production reliability: timeouts, retries, idempotency, race conditions, error handling, resource leaks",
    "style": "maintainability: naming, typing, logging, dead code, readability. Ignore security and reliability.",
}

# STRUCTURED OUTPUT: the exact JSON shape every agent must return (a JSON Schema).
# The API enforces it, so we can json.loads() the answer and merge it with plain code,
# instead of trying to parse free text.
FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                    "title": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["file", "line", "severity", "title", "fix"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}


def ask_claude(system_prompt, user_prompt, effort):
    """One Claude API call that must answer with JSON matching FINDINGS_SCHEMA."""
    import anthropic                      # imported here so offline mode works without the package
    client = anthropic.Anthropic()        # reads ANTHROPIC_API_KEY from the environment
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,                 # upper limit on the answer's length
        system=system_prompt,             # the agent's role and focus
        messages=[{"role": "user", "content": user_prompt}],   # the code to review
        thinking={"type": "adaptive"},    # let the model decide how much to reason first
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": FINDINGS_SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",              # if the model declines a request, the API retries on a fallback model
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"model declined: {response.stop_details}")
    for block in response.content:        # the answer is a list of blocks; the JSON is in the text block
        if block.type == "text":
            return json.loads(block.text)
    raise RuntimeError("no text in response")


def changes_as_text(changes):
    """Turn {file: [(line, text)]} into readable text with line numbers, for the model to read."""
    text = ""
    for path, lines in changes.items():
        text += f"\n=== {path} (only new or changed lines) ===\n"
        for line_number, line in lines:
            text += f"{line_number:>5}  {line}\n"
    return text


def llm_agent(agent, changes):
    """One Claude agent: same code, focused instructions, structured JSON back."""
    answer = ask_claude(
        system_prompt=f"You are a senior code reviewer. Focus ONLY on {FOCUS[agent]}. "
                      "Report real issues in the lines shown, with exact file and line numbers and a specific fix. "
                      "Return an empty list if you find nothing in your area.",
        user_prompt=f"Review these changes:\n{changes_as_text(changes)}",
        effort="medium",
    )
    findings = []
    for item in answer["findings"]:
        item["agent"] = agent             # remember which agent found it
        findings.append(item)
    return findings


# ================================================================== 4. RUN AGENTS, MERGE, REPORT
def run_agent(agent, changes, use_llm):
    """
    Run one agent. It NEVER raises an error: it returns (findings, error or None, seconds taken).
    That way one broken agent (bad API key, timeout) can't crash the whole review.
    """
    start = time.perf_counter()
    try:
        if use_llm:
            findings = llm_agent(agent, changes)
        else:
            findings = offline_agent(agent, changes)
        return findings, None, time.perf_counter() - start
    except Exception as error:
        return [], error, time.perf_counter() - start


def sort_key(finding):
    """How to sort findings: by severity (critical first), then file, then line."""
    return (SEVERITY_ORDER[finding["severity"]], finding["file"], finding["line"])


def aggregate(findings, use_llm):
    """The aggregator: remove duplicates and sort, critical first."""
    if use_llm and findings:
        # LLM mode: a 4th Claude call merges the findings, drops duplicates and false positives.
        answer = ask_claude(
            system_prompt="You merge code review findings from several reviewers. Remove duplicates "
                          "(same file and line, same root cause), drop false positives, keep the clearest wording.",
            user_prompt=json.dumps(findings, indent=1),
            effort="high",
        )
        merged = answer["findings"]
        for item in merged:
            item["agent"] = "aggregator"
        return sorted(merged, key=sort_key)

    # Offline mode: a simple duplicate check (same file + same line + same first word of the title).
    seen = set()
    merged = []
    for finding in sorted(findings, key=sort_key):
        key = (finding["file"], finding["line"], finding["title"].split()[0].lower())
        if key not in seen:
            seen.add(key)
            merged.append(finding)
    return merged


def print_text_report(final, what, mode, seconds_per_agent, wall_seconds, raw_count, show_all):
    """The terminal report."""
    print(f"\n# Review of {what}  ({mode})\n")
    hidden = 0
    for f in final:
        if f["severity"] == "low" and not show_all:
            hidden += 1                   # low = style notes; hide them so real problems stand out
            continue
        print(f"  [{f['severity'].upper():8}] {f['file']}:{f['line']}  {f['title']}  ({f['agent']})")
        print(f"{'':13}fix: {f['fix']}")
    if hidden:
        print(f"  (+ {hidden} low-severity style notes, show them with --all)")
    print(f"\nraw findings: {raw_count}   after removing duplicates: {len(final)}")
    timing_parts = []
    for agent, seconds in seconds_per_agent.items():
        timing_parts.append(f"{agent}={seconds:.2f}s")
    print(f"agent timings: {', '.join(timing_parts)}")
    print(f"parallel wall time = {wall_seconds:.2f}s   (one after another would be {sum(seconds_per_agent.values()):.2f}s)")


def print_markdown_report(final, what, mode):
    """A markdown table, for posting as a pull-request comment (the GitHub Action uses this)."""
    print(f"### Multi-agent review of {what}\n")
    print(f"_Mode: {mode}_\n")
    if not final:
        print("No findings.")
        return
    print("| Severity | Location | Issue | Fix | Agent |")
    print("|---|---|---|---|---|")
    for f in final:
        print(f"| **{f['severity']}** | `{f['file']}:{f['line']}` | {f['title']} | {f['fix']} | {f['agent']} |")


def main():
    # ---- command-line options
    parser = argparse.ArgumentParser(description="Multi-agent code review")
    parser.add_argument("files", nargs="*", help="files to review in full")
    parser.add_argument("--diff", metavar="BASE", help="review changes on this branch vs BASE (e.g. main)")
    parser.add_argument("--staged", action="store_true", help="review staged changes (pre-commit)")
    parser.add_argument("--llm", action="store_true", help="use Claude instead of regex rules")
    parser.add_argument("--markdown", action="store_true", help="print a markdown report (for PR comments)")
    parser.add_argument("--all", action="store_true", help="also show low-severity style notes")
    args = parser.parse_args()

    # ---- 1. Decide what to review
    if args.diff:
        # BASE...HEAD (three dots) = changes on this branch since it split off BASE.
        # So new commits on main don't show up as "your" changes.
        changes = added_lines_from_diff(run_git("diff", "--unified=0", f"{args.diff}...HEAD"))
        what = f"changes since {args.diff}"
    elif args.staged:
        changes = added_lines_from_diff(run_git("diff", "--unified=0", "--cached"))   # --cached = staged
        what = "staged changes"
    elif args.files:
        changes = whole_files(args.files)
        what = ", ".join(args.files)
    else:
        parser.error("give files, --diff BASE, or --staged")

    total_lines = 0
    for lines in changes.values():
        total_lines += len(lines)
    if total_lines == 0:
        print("Nothing to review.")
        return 0

    # ---- 2. Run the three agents in parallel (one thread each)
    all_findings = []
    failed_agents = []
    seconds_per_agent = {}
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {}
        for agent in AGENTS:
            jobs[agent] = pool.submit(run_agent, agent, changes, args.llm)   # start it; don't wait yet
        for agent, job in jobs.items():
            findings, error, seconds = job.result()                         # now wait for each result
            seconds_per_agent[agent] = seconds
            if error:
                print(f"! {agent} agent failed: {error}", file=sys.stderr)
                failed_agents.append(agent)
            all_findings.extend(findings)
    wall_seconds = time.perf_counter() - start

    # ---- 3. Merge and report
    final = aggregate(all_findings, args.llm)
    mode = f"Claude {MODEL}" if args.llm else "offline rules"
    if args.markdown:
        print_markdown_report(final, what, mode)
    else:
        print_text_report(final, what, mode, seconds_per_agent, wall_seconds, len(all_findings), args.all)

    # ---- 4. Verdict
    # FAIL CLOSED: if an agent didn't run, we can't say the code is fine.
    # (The first version said APPROVE when every agent crashed, because 0 findings looked like 0 bugs.)
    if failed_agents:
        print(f"\n**VERDICT: INCOMPLETE** - these agents failed: {', '.join(failed_agents)}. Do not merge.")
        return 2
    blocking = []
    for f in final:
        if f["severity"] in ("critical", "high"):
            blocking.append(f)
    if blocking:
        print(f"\n**VERDICT: REQUEST CHANGES** ({len(blocking)} blocking)")
        return 1
    print("\n**VERDICT: APPROVE**")
    return 0


if __name__ == "__main__":
    sys.exit(main())      # the number main() returns becomes the process exit code (0, 1 or 2)
