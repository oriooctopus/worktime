---
name: indicator-dot
description: >-
  Show what the menu bar dot would be showing right now — green or amber, what
  is covering the current moment, and how fresh the data is. Use when someone
  says "/indicator-dot", "what does the dot say", "is it showing me as working",
  "what colour is the dot", "why is the dot amber", "why isn't the dot green",
  or asks what the time tracker currently thinks they are doing.
model: haiku
user-invocable: true
argument-hint: "[optional HH:MM to check a past moment instead of now]"
---

# Indicator dot status

Run the script and relay its output. That is the whole skill.

`haiku` because the script does all the logic and formatting — this is a run-
and-relay with no reasoning step.

```
python3 ~/coding/worktime/skills/indicator-dot/indicator-dot.py
```

Pass `--at HH:MM` if the user asks about a specific earlier moment today.

Exit codes: `0` green, `1` amber, `2` unusable data. Report what it prints; do
not re-derive or second-guess it.

## The one caveat to pass on if it disagrees with the real dot

This reads the same file the probe reads and applies the rule the probe is
*believed* to apply: a `work`-tagged row covering now means working. The probe
itself lives on the Mac and is not yet in this repo, so it has never been read.

If this says green and the real dot is amber, the mismatch is this script's
assumption being wrong — not the dot being broken. Say that plainly rather than
telling the user their dot is faulty.
