#!/usr/bin/env python3
"""Count the prompts a human actually typed in a Claude Code session.

The session transcript is the source of truth: it survives resumes, restarts,
and compaction, and it is written by Claude Code itself rather than inferred by
a hook. Counting it directly avoids the failure mode of a counter file, which
starts at whatever moment the hook was installed and cannot be reconciled.

Usage: prompt-count.py <session-id> [transcript-path]

Prints the count to stdout, or nothing if the transcript cannot be found.

What counts as a prompt: a `user` row carrying real typed text. Excluded are
tool results, meta/sidechain rows, system reminders, task notifications,
compaction continuations, and `!` shell commands with their output (those never
reach the model). Slash commands DO count -- the user typed them -- and are
normalized to `/name args` so they read like what was entered.

Fork/rewind branches leave the retried prompt in the file twice with different
uuids, so consecutive identical texts are collapsed. Walking the parentUuid
chain from the leaf would handle forks too, but compaction severs that chain
and the walk then sees only the final segment -- measured at 3 prompts for a
25-prompt session -- so a full scan plus collapse is the accurate approach.
"""

import itertools
import json
import os
import re
import sys
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

# realpath, not abspath: this file is reached through the
# ~/.claude/hooks/prompt-count.py symlink, and abspath would look for the
# shared module in ~/.claude/hooks, where it is not.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import worktime_common as wc  # noqa: E402

LOCAL = wc.local_tz()

SKIP_PREFIXES = (
    "<local-command-stdout>",
    "<local-command-caveat>",
    "<system-reminder>",
    "<task-notification>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "[Request interrupted by user]",
    "This session is being continued from a previous conversation",
    "Caveat: The messages below",
)

COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.S)
COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)

# Prompt previews get written to the Obsidian vault, so anything that looks
# like a credential is masked first -- a pasted token would otherwise be
# copied out of the transcript into a second file on disk.
SECRET = re.compile(
    r"\b(xox[baprs]-[\w-]+"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|sk-[A-Za-z0-9_-]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+)"
)


def redact(text: str) -> str:
    return SECRET.sub("<redacted-credential>", text)


# The worktime background-job profile runs under a separate Claude Code
# profile dir (~/.claude-personal) with its own ~/.claude-personal/projects
# transcripts and no hooks of its own. Its sessions are real interactive work
# -- entrypoint filtering below already excludes anything non-human-typed --
# so they must be walked alongside the normal profile or worktime background
# jobs are invisible to the tracker no matter how long they run.
PROJECT_ROOTS = wc.PROJECT_ROOTS


def find_transcript(session_id: str) -> str | None:
    for root, _dirs, files in itertools.chain.from_iterable(
        os.walk(p) for p in PROJECT_ROOTS
    ):
        if f"{session_id}.jsonl" in files:
            return os.path.join(root, f"{session_id}.jsonl")
    return None


def text_of(row: dict) -> str | None:
    content = row.get("message", {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(b.get("type") == "tool_result" for b in content if isinstance(b, dict)):
            return None
        return " ".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return None


def prompts_with_time(path: str) -> list[tuple[str, str, str]]:
    """Return [(text, iso_utc_timestamp, session_title)] per typed prompt.

    `session_title` is the conversation's name as of that prompt. Claude Code
    appends a `custom-title` row on every `/rename` and an `ai-title` row when
    it auto-names a session, so a session can carry several names over its
    life; tracking the running value means a day's entry shows what the
    conversation was called *then*, matching how every other number here is
    attributed per-prompt rather than per-session. A user rename outranks an
    auto-title. Empty when the session was never named -- callers fall back to
    the opening prompt.

    `entrypoint` separates a human at a keyboard from a programmatic driver:
    `cli` is the interactive binary, while `sdk-ts` / `sdk-cli` / `sdk-py` are
    SDK-driven runs (eval harnesses, agent task runners) whose "user" rows are
    generated prompts. Without this, an eval sweep adds hundreds of phantom
    prompts -- one day measured 260, of which 95 were grader invocations.
    `sessionKind` is NOT a discriminator: real interactive sessions run as
    background jobs and are marked `bg`.
    """
    found: list[tuple[str, str, str]] = []
    custom_title = ""
    ai_title = ""
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            kind = row.get("type")
            if kind == "custom-title":
                custom_title = (row.get("customTitle") or "").strip()
                continue
            if kind == "ai-title":
                ai_title = (row.get("aiTitle") or "").strip()
                continue
            if kind != "user" or row.get("isMeta") or row.get("isSidechain"):
                continue
            if row.get("entrypoint") not in (None, "cli"):
                continue
            text = text_of(row)
            if not text or not text.strip():
                continue
            text = text.strip()
            if text.startswith(SKIP_PREFIXES):
                continue
            name = COMMAND_NAME.search(text)
            if name:
                args = COMMAND_ARGS.search(text)
                text = f"{name.group(1).strip()} {args.group(1).strip() if args else ''}".strip()
            # Fork/rewind replays the same prompt under a new uuid.
            if found and found[-1][0] == text:
                continue
            found.append((text, row.get("timestamp") or "", custom_title or ai_title))
    return found


def cached_prompts_with_time(path: str) -> list[tuple[str, str, str]]:
    """prompts_with_time(), reusing the last parse while the file is unchanged.

    Same size+mtime key as cached_count(), for the same reason and with the
    same guarantee: transcripts are append-only, so any new prompt moves both.

    This one matters more than the count cache. A day's walk re-parses every
    transcript touched that day -- 66 MB by mid-afternoon -- and the worktime
    probe calls it on a timer. Uncached, a 5-second menu bar poll re-derives
    the entire day twelve times a minute; cached, only the session actually
    being typed into is re-read.
    """
    stat = os.stat(path)
    key = f"{stat.st_size}:{int(stat.st_mtime)}"
    cache_path = os.path.join(
        os.path.expanduser("~/.claude/stats"),
        f"promptrows-{os.path.basename(path)}.json",
    )
    try:
        with open(cache_path) as fh:
            blob = json.load(fh)
        if blob.get("key") == key:
            return [tuple(r) for r in blob["rows"]]
    except (OSError, ValueError, KeyError):
        pass

    rows = prompts_with_time(path)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    tmp = f"{cache_path}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump({"key": key, "rows": rows}, fh)
    os.replace(tmp, cache_path)
    return rows


def prompts(path: str) -> list[str]:
    return [text for text, _ts, _title in prompts_with_time(path)]


def cached_count(path: str) -> int:
    """Count prompts, reusing the last result while the transcript is unchanged.

    The statusline re-renders on every turn and on a refresh timer, and a long
    session's transcript runs to thousands of lines, so a re-parse each time is
    wasted work. Size plus mtime is a sufficient cache key: the transcript is
    append-only, so any new prompt changes both.
    """
    stat = os.stat(path)
    key = f"{stat.st_size}:{int(stat.st_mtime)}"
    cache_path = os.path.join(
        os.path.expanduser("~/.claude/stats"),
        f"promptcount-{os.path.basename(path)}.cache",
    )
    try:
        with open(cache_path) as fh:
            cached_key, cached_value = fh.read().split(" ", 1)
        if cached_key == key:
            return int(cached_value)
    except (OSError, ValueError):
        pass

    count = len(prompts(path))
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    tmp = f"{cache_path}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        fh.write(f"{key} {count}")
    os.replace(tmp, cache_path)
    return count


def load_api_key() -> str | None:
    # Prefer env var (already set by shell profile / Claude Code env)
    if key := os.environ.get("ANTHROPIC_API_KEY"):
        return key
    path = os.path.expanduser("~/.claude/tokens.env")
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("ANTHROPIC_API_KEY="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return None


def _summarize_one(label: str, prompt_texts: list[str], api_key: str) -> dict:
    """Call Haiku to generate a title + body for one session. Returns {} on error."""
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    model = os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", "claude-haiku-4-5-20251001")
    msgs_block = "\n".join(f"- {t[:200]}" for t in prompt_texts[:30])
    payload = json.dumps({
        "model": model,
        "max_tokens": 300,
        "messages": [{
            "role": "user",
            "content": (
                f'Session name: "{label}"\n\n'
                f"User messages:\n{msgs_block}\n\n"
                "Return JSON with exactly two keys:\n"
                '- "title": one sentence ≤80 chars stating what was accomplished\n'
                '- "body": 1-2 sentences with more detail on what was done\n\n'
                "Rules:\n"
                "- Never reference a PR, issue, or commit by number (e.g. #249769, PR 123). "
                "Describe what the PR/change was about using its subject matter instead.\n"
                "JSON only, no other text."
            ),
        }],
    }).encode()
    req = urllib.request.Request(
        f"{base_url}/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        text = data["content"][0]["text"].strip()
        # Strip markdown code fences the model sometimes wraps around JSON.
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(text)
    except Exception:
        return {}


def summarize_sessions(sessions_with_texts: list[tuple[dict, list[str]]], api_key: str) -> None:
    """Add 'summary_title' and 'summary_body' to each session dict in-place, in parallel."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_summarize_one, s["label"], texts, api_key): s
            for s, texts in sessions_with_texts
        }
        for future in as_completed(futures):
            session = futures[future]
            try:
                result = future.result()
                session["summary_title"] = result.get("title", "")
                session["summary_body"] = result.get("body", "")
            except Exception:
                session["summary_title"] = ""
                session["summary_body"] = ""


def count_for_day(day: str, summarize: bool = False, full: bool = False) -> dict:
    """Per-session prompt counts for one LOCAL calendar day.

    Each prompt is attributed by its own timestamp, not by the session's start
    date -- a session that runs past midnight or is resumed days later splits
    across days correctly. Transcript timestamps are UTC, so they are converted
    to local time before bucketing; skipping that would misfile every prompt
    sent after 6pm local (UTC-6) into the following day.

    Only transcripts whose mtime is at or after the start of the day are read.
    Transcripts are append-only, so an older file cannot contain a prompt from
    that day -- this is a sound prune, not a sampling heuristic.

    The zone is named explicitly rather than taken from the ambient locale.
    Under the sandbox some callers run in, /var/db/timezone/zoneinfo is denied
    and the C library answers by reporting UTC -- with no error -- so a bare
    .astimezone() silently shifts every prompt by the UTC offset and moves the
    whole evening onto the next day. ZoneInfo reads bundled tzdata and is not
    affected, so the same day boundary holds however this is invoked.
    """
    start_local = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL)
    end_local = start_local + timedelta(days=1)
    cutoff = start_local.timestamp()

    sessions = []
    sessions_hits: list[tuple[dict, list[str]]] = []  # for optional summarization
    total = 0
    for root, _dirs, files in itertools.chain.from_iterable(
        os.walk(p) for p in PROJECT_ROOTS
    ):
        for fname in files:
            if not fname.endswith(".jsonl"):
                continue
            path = os.path.join(root, fname)
            try:
                if os.stat(path).st_mtime < cutoff:
                    continue
            except OSError:
                continue
            hits = []
            for text, ts, title in cached_prompts_with_time(path):
                if not ts:
                    continue
                try:
                    when = datetime.fromisoformat(
                        ts.replace("Z", "+00:00")
                    ).astimezone(LOCAL)
                except ValueError:
                    continue
                if start_local <= when < end_local:
                    hits.append((text, when, title))
            if not hits:
                continue
            total += len(hits)
            # Title as of the day's last prompt: a session renamed mid-day
            # should read as whatever it had become by the end of that day.
            title = hits[-1][2]
            raw_texts = [redact(t) for t, _, _ in hits]
            session: dict = {
                "session_id": fname[:-6],
                "project": os.path.basename(root),
                "count": len(hits),
                "first": hits[0][1].strftime("%H:%M"),
                "last": hits[-1][1].strftime("%H:%M"),
                "title": redact(title)[:80],
                "named": bool(title),
                "opening_prompt": redact(hits[0][0])[:120],
                # What a renderer should show: the name when there is one,
                # else the opening prompt as a stand-in.
                "label": redact(title or hits[0][0])[:100],
                # First 20 prompts, each truncated to 150 chars. Stored in
                # the note so the dashboard can show the full list on click.
                # Capped at 20 by default because these carry prompt TEXT into
                # the Obsidian note. `full` lifts the cap for callers that need
                # to know what was said across a whole day rather than render a
                # readable list -- a summariser cannot describe an afternoon it
                # was only shown the morning of.
                "prompts": [{"text": redact(t)[:150], "ts": w.strftime("%H:%M")}
                            for t, w, _ in (hits if full else hits[:20])],
                # EVERY timestamp, uncapped. The 20-cap above exists to keep
                # prompt text out of the Obsidian note, but it was silently
                # truncating the timing signal too: a 46-prompt session running
                # to 17:56 exposed only its first 20, so an entire afternoon of
                # work read as absence and the gap detector invented a
                # two-and-a-half-hour hole that never happened. Bare "HH:MM"
                # strings cost five bytes each, so there is no reason to cap
                # them alongside the text.
                #
                # Seconds are kept. At minute resolution a burst of four prompts
                # inside one minute collapsed to four identical stamps, so the
                # work period they formed measured last-minus-first = zero and
                # rendered as "23:52-23:52 0m". Consumers that want minutes can
                # truncate; nothing can recover a second that was never written.
                "times": [w.strftime("%H:%M:%S") for _, w, _ in hits],
            }
            sessions.append(session)
            sessions_hits.append((session, raw_texts))

    sessions.sort(key=lambda s: s["count"], reverse=True)

    if summarize:
        api_key = load_api_key()
        if api_key:
            summarize_sessions(sessions_hits, api_key)

    return {"date": day, "total": total, "sessions": sessions}


def main() -> None:
    args = sys.argv[1:]
    summarize = "--summarize" in args
    args = [a for a in args if a != "--summarize"]
    full = "--full" in args
    args = [a for a in args if a != "--full"]
    if args and args[0] == "--day" and len(args) >= 2:
        print(json.dumps(count_for_day(args[1], summarize=summarize, full=full), indent=2))
        return
    if len(sys.argv) < 2:
        return
    # argv[2] may be present but empty when the caller had no transcript_path.
    path = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
    if not path or not os.path.exists(path):
        path = find_transcript(sys.argv[1])
    if not path or not os.path.exists(path):
        return
    print(cached_count(path))


if __name__ == "__main__":
    main()
