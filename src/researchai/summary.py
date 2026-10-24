"""One summary of a whole research session: what was asked, how the agent planned and
adapted, what it found, how strong the evidence is, and what the user did.

Pure functions over a session snapshot and its event log, so live and saved sessions are
summarised identically. No LLM call: everything is derived from what actually happened.
"""

import re
from collections import Counter
from datetime import datetime

from researchai.sources import reason_bucket

CITATION_GROUP = re.compile(r"\[(S\d+(?:\s*,\s*S\d+)*)\]")


# ---------------------------------------------------------------- report parsing


def report_section(report: str | None, title: str) -> str:
    """Body of a '## <title>' section (case-insensitive), or ''."""
    if not report:
        return ""
    match = re.search(rf"^## {re.escape(title)}\s*$(.*?)(?=^## |\Z)", report, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else ""


def bullets(text: str) -> list[str]:
    return [line[2:].strip() for line in text.splitlines() if line.startswith("- ")]


def citation_counts(report: str | None) -> Counter:
    counts: Counter = Counter()
    body = (report or "").split("\n## Sources", 1)[0]  # don't count the reference list
    for group in CITATION_GROUP.findall(body):
        counts.update(re.split(r"\s*,\s*", group))
    return counts


def measure(status: dict | None) -> str:
    """'3 sources from 2 sites, newest 2026-10-06, 7/8 quotes verified' (or '—')."""
    if not status:
        return "—"
    plural = lambda n, w: f"{n} {w}" if n == 1 else f"{n} {w}s"
    newest = status.get("newest") or "n/a"
    return (f"{plural(status['sources'], 'source')} from {plural(status['sites'], 'site')}, "
            f"newest {newest}, {status['quotes_verified']}/{status['quotes_total']} quotes verified")


def paused_seconds(events: list[dict]) -> float:
    """Time spent paused by the user (pause → resume, or pause → last event)."""
    total, since = 0.0, None
    for e in events:
        action = e.get("data", {}).get("action") if e["kind"] == "control" else None
        if action == "pause" and since is None:
            since = datetime.fromisoformat(e["ts"])
        elif action == "resume" and since is not None:
            total += (datetime.fromisoformat(e["ts"]) - since).total_seconds()
            since = None
    if since is not None and events:
        total += (datetime.fromisoformat(events[-1]["ts"]) - since).total_seconds()
    return total


# ---------------------------------------------------------------- summary


def build_summary(snapshot: dict, events: list[dict]) -> dict:
    report = snapshot.get("report")
    questions = snapshot.get("questions") or []
    by_number = {q["number"]: q for q in questions}
    todos = snapshot.get("todos") or []

    def todo_status(n: int) -> str:
        todo = next((t for t in todos if re.match(rf"Q{n}\b", t.get("content", ""))), None)
        return todo["status"] if todo else "pending"

    times = [datetime.fromisoformat(e["ts"]) for e in events if e.get("ts")]
    duration = int((max(times) - min(times)).total_seconds() - paused_seconds(events)) \
        if len(times) > 1 else 0

    # -- sources
    sources = snapshot.get("sources") or []
    titles = {s["id"]: s for s in sources}
    dropped_reasons: Counter = Counter()
    for e in events:
        if e["kind"] == "dropped":
            for s in e.get("data", {}).get("sources", []):
                dropped_reasons[reason_bucket(s["reasons"][-1]) if s.get("reasons") else "other"] += 1
    cites = citation_counts(report)
    top_cited = [
        {"id": sid, "count": n, "title": titles[sid]["title"], "domain": titles[sid]["domain"],
         "url": titles[sid]["url"]}
        for sid, n in cites.most_common(5) if sid in titles
    ]

    # -- what the user did (and the revisions it caused)
    actions = []
    for e in events:
        if e.get("agent") == "user" or (e["kind"] == "session"
                                        and e.get("data", {}).get("status") == "revising"):
            actions.append({"time": e["ts"], "text": e["message"]})

    return {
        "overview": {
            "topic": snapshot.get("topic"), "status": snapshot.get("status"),
            "started": snapshot.get("created"), "duration_s": duration,
            "rounds_used": snapshot.get("round", 1), "max_rounds": snapshot.get("max_rounds"),
            "report_version": snapshot.get("report_version", 0),
            "sub_questions": len(questions),
            "sources_used": sum(1 for s in sources if s.get("evidence")),
            "time_sensitive": bool((snapshot.get("plan") or {}).get("time_sensitive")),
        },
        "direction": {"answers": snapshot.get("direction"),
                      "assumed": bool(snapshot.get("direction_assumed"))},
        "findings": {
            "executive_summary": report_section(report, "Executive summary"),
            "takeaways": bullets(report_section(report, "Key takeaways")),
            "disagreements": report_section(report, "Where sources disagree"),
            "open_questions": report_section(report, "Open questions"),
        },
        "questions": [
            {"number": q["number"], "question": q["question"],
             "status": "skipped" if q.get("skipped") else todo_status(q["number"]),
             "measure": measure(q), "flags": q.get("flags", []), "skipped": q.get("skipped", False)}
            for q in questions
        ],
        "adaptation": {
            "gaps": [
                {"question_number": g["question_number"],
                 "question": by_number.get(g["question_number"], {}).get("question", ""),
                 "problem": g["problem"], "why_it_matters": g["why_it_matters"],
                 "follow_up_queries": g.get("follow_up_queries", []),
                 "before": measure(g.get("before")),
                 "after": measure(by_number.get(g["question_number"])),
                 "still_open": g["problem"] in by_number.get(g["question_number"], {}).get("flags", [])}
                for g in snapshot.get("gaps") or []
            ],
            "rejected_drafts": [
                {"time": e["ts"], "problems": e.get("data", {}).get("problems", [])}
                for e in events if e["kind"] == "report_rejected"
            ],
        },
        "sources": {
            "used": sum(1 for s in sources if s.get("evidence")),
            "candidates": sum(1 for s in sources if not s.get("evidence")),
            "dropped": snapshot.get("dropped", 0),
            "dropped_by_reason": dict(dropped_reasons.most_common()),
            "excluded": [{"id": s["id"], "title": s["title"], "domain": s["domain"]}
                         for s in sources if s.get("excluded")],
            "top_cited": top_cited,
        },
        "actions": actions,
        "context": {
            "assumptions": snapshot.get("assumptions") or [],
            "related": snapshot.get("related") or [],
            "suggestions": snapshot.get("suggestions") or [],
        },
    }


# ---------------------------------------------------------------- markdown export


def _duration(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    return f"{minutes} min {secs:02d} s" if minutes else f"{secs} s"


def summary_markdown(s: dict) -> str:
    o, d, f, a, src = (s["overview"], s["direction"], s["findings"], s["adaptation"],
                       s["sources"])
    out = [f"# Research summary: {o['topic']}", ""]
    revised = f", revised (v{o['report_version']})" if o["report_version"] > 1 else ""
    out += [(f"Started {(o['started'] or '')[:16].replace('T', ' ')} · took "
            f"{_duration(o['duration_s'])} · {o['rounds_used']} of {o['max_rounds']} research "
            f"rounds · {o['sub_questions']} sub-questions · {o['sources_used']} sources used as "
            f"evidence · status: {o['status']}{revised}"), ""]

    out += ["## Your direction", ""]
    if d["answers"]:
        if d["assumed"]:
            out += ["Nobody answered, so the agent used its recommended options:", ""]
        for header, value in d["answers"].items():
            out.append(f"- **{header}:** {value if isinstance(value, str) else ', '.join(value)}")
    else:
        out.append("Not chosen.")
    out.append("")

    out += ["## Findings", ""]
    if f["executive_summary"]:
        out += [f["executive_summary"], ""]
    if f["takeaways"]:
        out += ["**Key takeaways**", ""] + [f"- {t}" for t in f["takeaways"]] + [""]
    if f["disagreements"]:
        out += ["**Where sources disagree**", "", f["disagreements"], ""]
    if f["open_questions"]:
        out += ["**Open questions**", "", f["open_questions"], ""]
    if not any(f.values()):
        out += ["The report isn't ready yet.", ""]

    out += ["## Plan and evidence", "", "| | Sub-question | Evidence | Status |", "|---|---|---|---|"]
    for q in s["questions"]:
        flags = f" ({', '.join(q['flags'])})" if q["flags"] else ""
        out.append(f"| Q{q['number']} | {q['question']} | {q['measure']}{flags} | "
                   f"{q['status'].replace('_', ' ')} |")
    out.append("")

    out += ["## How the agent adapted", ""]
    if a["gaps"]:
        out += ["| Gap | Why it mattered | Before | After |", "|---|---|---|---|"]
        for g in a["gaps"]:
            state = " (still open)" if g["still_open"] else ""
            out.append(f"| Q{g['question_number']} {g['problem'].replace('_', ' ')}{state} | "
                       f"{g['why_it_matters']} | {g['before']} | {g['after']} |")
        out.append("")
    else:
        out += ["No evidence gaps needed follow-up research.", ""]
    n = len(a["rejected_drafts"])
    out.append(f"The quality checks sent back {n} draft{'s' if n != 1 else ''} before accepting "
               "the report." if n else "The first draft passed the quality checks.")
    for r in a["rejected_drafts"]:
        out += [f"  - {p}" for p in r["problems"]]
    out.append("")

    out += ["## Sources", "",
            f"{src['used']} used as evidence · {src['candidates']} other candidates kept · "
            f"{src['dropped']} dropped"
            + (" (" + ", ".join(f"{v} {k}" for k, v in src["dropped_by_reason"].items()) + ")"
               if src["dropped_by_reason"] else ""), ""]
    if src["top_cited"]:
        out += ["Most cited:", ""] + [f"- [{t['id']}] [{t['title']}]({t['url']}) — "
                                      f"{t['domain']}, cited {t['count']}×"
                                      for t in src["top_cited"]] + [""]
    if src["excluded"]:
        out += ["Excluded by you: " + ", ".join(f"{x['id']} ({x['domain']})"
                                                for x in src["excluded"]), ""]

    out += ["## Your actions", ""]
    out += [f"- {x['time'][11:16]} — {x['text']}" for x in s["actions"]] or ["None."]
    out.append("")

    c = s["context"]
    if c["assumptions"]:
        out += ["## Assumptions", ""] + [f"- {x}" for x in c["assumptions"]] + [""]
    if c["related"]:
        out += ["## Built on earlier research", ""]
        out += [f"- {r['topic']} ({r['created'][:10]})" for r in c["related"]] + [""]
    if c["suggestions"]:
        out += ["## What to research next", ""]
        out += [f"- **{x['topic']}** — {x['why']}" for x in c["suggestions"]] + [""]
    return "\n".join(out)
