"""
Multi-agent code review for git changes, like a pull-request review bot.

Review what changed on your branch compared to main (only the new/changed lines):
    python code_review/review.py --diff main
Review what you have staged for the next commit (used by the pre-commit hook):
    python code_review/review.py --staged
Review whole files:
    python code_review/review.py demo_app/payments.py

Add --llm to use Claude agents instead of the offline regex rules (needs ANTHROPIC_API_KEY).
Add --markdown to print a report you can post as a PR comment (the GitHub Action does this).

How it works:

                   +--> security agent    --+
    git diff  ---> +--> reliability agent --+--> aggregator --> ranked report + verdict
                   +--> style agent       --+
                   (run in parallel)           (remove duplicates, sort, decide)

Exit code (so CI and git hooks can block a merge or commit):
    0 = APPROVE     1 = REQUEST CHANGES (critical/high found)     2 = INCOMPLETE (an agent failed)

A "finding" is a plain dict:
  {"agent": "security", "file": "demo_app/payments.py", "line": 14, "severity": "critical", "title": "...", "fix": "..."}
"""
import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

MODEL = "claude-opus-5"
AGENTS = ["security", "reliability", "style"]
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# Don't review the reviewer: its rule list contains the exact strings it searches for
# (os.system, verify=False, ...), so it would flag itself. A real false positive we hit.
SKIP_PATHS = ["code_review/"]


# ------------------------------------------------------------------ 1. what to review
def run_git(*args):
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def added_lines_from_diff(diff_text):
    """
    Read `git diff --unified=0` output and return only the ADDED lines:
        {"demo_app/payments.py": [(14, "code on line 14"), (15, "..."), ...]}
    A hunk header looks like:  @@ -10,0 +14,3 @@   meaning "new lines start at line 14".
    """
    changes = {}
    current_file = None
    line_number = 0
    for line in diff_text.splitlines():
        if line.startswith("+++ "):                        # which file the next hunks belong to
            path = line[4:]
            current_file = path[2:] if path.startswith("b/") else None   # "/dev/null" = file deleted
            if current_file:
                changes.setdefault(current_file, [])
        elif line.startswith("@@"):
            new_part = line.split("+")[1].split(" ")[0]    # "14,3" or "14"
            line_number = int(new_part.split(",")[0])
        elif line.startswith("+") and current_file:
            changes[current_file].append((line_number, line[1:]))
            line_number += 1
    return changes


def whole_files(paths):
    changes = {}
    for path in paths:
        lines = open(path).read().splitlines()
        changes[path] = [(number, text) for number, text in enumerate(lines, start=1)]
    return changes


# ------------------------------------------------------------------ 2. offline agents (regex rules)
# Each rule: (regex to find on a line, severity, title, how to fix)
RULES = {
    "security": [
        (r"""(api_key|secret|password|token)\s*=\s*["'][^"']{8,}""", "critical", "Hardcoded secret", "Load from Vault/env; rotate the leaked key"),
        (r"""execute\(.*(%|\bf["'])""", "critical", "SQL injection via string formatting", "Use parameterised queries (?, %s placeholders)"),
        (r"pickle\.loads?\(", "critical", "Unsafe deserialisation (pickle)", "Use JSON; never unpickle untrusted data"),
        (r"os\.system\(|shell=True", "high", "Shell command injection", "subprocess.run([...]) with an argument list"),
        (r"verify=False", "high", "TLS verification disabled", "Remove verify=False; fix the CA bundle instead"),
    ],
    "reliability": [
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
    findings = []
    for path, lines in changes.items():
        if not path.endswith(".py"):
            continue
        if any(path.startswith(skip) for skip in SKIP_PATHS):
            continue
        for line_number, text in lines:
            code = text.split("  #")[0]              # ignore trailing comments
            for pattern, severity, title, fix in RULES[agent]:
                if re.search(pattern, code, re.IGNORECASE):
                    findings.append({"agent": agent, "file": path, "line": line_number,
                                     "severity": severity, "title": title, "fix": fix})
    time.sleep(0.05)   # pretend to think, so the parallel speed-up is visible
    return findings


# ------------------------------------------------------------------ 3. LLM agents (Claude)
FOCUS = {
    "security": "security vulnerabilities: injection, secrets, authn/authz, unsafe deserialisation, TLS, SSRF",
    "reliability": "production reliability: timeouts, retries, idempotency, race conditions, error handling, resource leaks",
    "style": "maintainability: naming, typing, logging, dead code, readability. Ignore security and reliability.",
}

# The JSON shape every agent must return. The API enforces it, so json.loads() is safe.
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
    import anthropic                      # only needed in --llm mode
    client = anthropic.Anthropic()        # reads ANTHROPIC_API_KEY from the environment
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
        thinking={"type": "adaptive"},
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": FINDINGS_SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",              # if the model declines, the API retries on a fallback model
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"model declined: {response.stop_details}")
    for block in response.content:
        if block.type == "text":
            return json.loads(block.text)
    raise RuntimeError("no text in response")


def changes_as_text(changes):
    """Turn {file: [(line, text)]} into readable text with line numbers for the model."""
    text = ""
    for path, lines in changes.items():
        text += f"\n=== {path} (only new or changed lines) ===\n"
        for line_number, line in lines:
            text += f"{line_number:>5}  {line}\n"
    return text


def llm_agent(agent, changes):
    answer = ask_claude(
        system_prompt=f"You are a senior code reviewer. Focus ONLY on {FOCUS[agent]}. "
                      "Report real issues in the lines shown, with exact file and line numbers and a specific fix. "
                      "Return an empty list if you find nothing in your area.",
        user_prompt=f"Review these changes:\n{changes_as_text(changes)}",
        effort="medium",
    )
    findings = []
    for item in answer["findings"]:
        item["agent"] = agent
        findings.append(item)
    return findings


# ------------------------------------------------------------------ 4. run agents, merge, report
def run_agent(agent, changes, use_llm):
    """Run one agent. Never raises: returns (findings, error or None, seconds taken)."""
    start = time.perf_counter()
    try:
        if use_llm:
            findings = llm_agent(agent, changes)
        else:
            findings = offline_agent(agent, changes)
        return findings, None, time.perf_counter() - start
    except Exception as error:            # one broken agent must not kill the whole review
        return [], error, time.perf_counter() - start


def sort_key(finding):
    return (SEVERITY_ORDER[finding["severity"]], finding["file"], finding["line"])


def aggregate(findings, use_llm):
    """Remove duplicates and sort: critical first."""
    if use_llm and findings:
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

    seen = set()
    merged = []
    for finding in sorted(findings, key=sort_key):
        key = (finding["file"], finding["line"], finding["title"].split()[0].lower())
        if key not in seen:
            seen.add(key)
            merged.append(finding)
    return merged


def print_text_report(final, what, mode, seconds_per_agent, wall_seconds, raw_count, show_all):
    print(f"\n# Review of {what}  ({mode})\n")
    hidden = 0
    for f in final:
        if f["severity"] == "low" and not show_all:
            hidden += 1           # low = style notes; hide them so real problems stand out
            continue
        print(f"  [{f['severity'].upper():8}] {f['file']}:{f['line']}  {f['title']}  ({f['agent']})")
        print(f"{'':13}fix: {f['fix']}")
    if hidden:
        print(f"  (+ {hidden} low-severity style notes, show them with --all)")
    print(f"\nraw findings: {raw_count}   after removing duplicates: {len(final)}")
    timing_text = ", ".join(f"{agent}={sec:.2f}s" for agent, sec in seconds_per_agent.items())
    print(f"agent timings: {timing_text}")
    print(f"parallel wall time = {wall_seconds:.2f}s   (one after another would be {sum(seconds_per_agent.values()):.2f}s)")


def print_markdown_report(final, what, mode):
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
    parser = argparse.ArgumentParser(description="Multi-agent code review")
    parser.add_argument("files", nargs="*", help="files to review in full")
    parser.add_argument("--diff", metavar="BASE", help="review changes on this branch vs BASE (e.g. main)")
    parser.add_argument("--staged", action="store_true", help="review staged changes (pre-commit)")
    parser.add_argument("--llm", action="store_true", help="use Claude instead of regex rules")
    parser.add_argument("--markdown", action="store_true", help="print a markdown report (for PR comments)")
    parser.add_argument("--all", action="store_true", help="also show low-severity style notes")
    args = parser.parse_args()

    # 1. Decide what to review
    if args.diff:
        changes = added_lines_from_diff(run_git("diff", "--unified=0", f"{args.diff}...HEAD"))
        what = f"changes since {args.diff}"
    elif args.staged:
        changes = added_lines_from_diff(run_git("diff", "--unified=0", "--cached"))
        what = "staged changes"
    elif args.files:
        changes = whole_files(args.files)
        what = ", ".join(args.files)
    else:
        parser.error("give files, --diff BASE, or --staged")

    if not any(changes.values()):
        print("Nothing to review.")
        return 0

    # 2. Run the three agents in parallel (3 threads)
    all_findings = []
    failed_agents = []
    seconds_per_agent = {}
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {}
        for agent in AGENTS:
            jobs[agent] = pool.submit(run_agent, agent, changes, args.llm)
        for agent, job in jobs.items():
            findings, error, seconds = job.result()
            seconds_per_agent[agent] = seconds
            if error:
                print(f"! {agent} agent failed: {error}", file=sys.stderr)
                failed_agents.append(agent)
            all_findings.extend(findings)
    wall_seconds = time.perf_counter() - start

    # 3. Merge and report
    final = aggregate(all_findings, args.llm)
    mode = f"Claude {MODEL}" if args.llm else "offline rules"
    if args.markdown:
        print_markdown_report(final, what, mode)
    else:
        print_text_report(final, what, mode, seconds_per_agent, wall_seconds, len(all_findings), args.all)

    # 4. Verdict. FAIL CLOSED: if an agent didn't run, we can't say the code is fine.
    if failed_agents:
        print(f"\n**VERDICT: INCOMPLETE** - these agents failed: {', '.join(failed_agents)}. Do not merge.")
        return 2
    blocking = [f for f in final if f["severity"] in ("critical", "high")]
    if blocking:
        print(f"\n**VERDICT: REQUEST CHANGES** ({len(blocking)} blocking)")
        return 1
    print("\n**VERDICT: APPROVE**")
    return 0


if __name__ == "__main__":
    sys.exit(main())
