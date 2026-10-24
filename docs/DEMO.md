# Demo script (about 8 minutes)

## Before the demo

1. Run `.venv/bin/pytest`. All tests should pass.
2. Start the server: `.venv/bin/uvicorn researchai.server:app --port 8765`, then open http://localhost:8765.
3. Check that the **Sessions** dropdown lists at least one finished session. It's your backup if the network or APIs fail. Saved sessions replay their whole action log.
4. Keep `README.md` open on the architecture diagram.

## 1. You choose the direction, then the plan (about 1.5 min)

Type a topic, e.g. **`Impact of the EU AI Act on European AI startups`**, and press Research.

- A dialog asks **which direction the research should take**, with tabs such as **Focus** (pick several) and **Depth**. The agent proposed these options for this topic and marked its recommendations.
- Click two focus areas, pick a depth, and on the **Your direction** tab type something like "Prioritise German and French startups". Click **Start research**.
- The **plan board** fills, and the sub-questions follow your choices, including what you typed.
- *Talking point:* "Like Claude before a big task, the agent proposes options instead of guessing. It's enforced in code: no plan until you've chosen. For an ambiguous topic such as 'Mercury impact', the first question resolves the meaning."

## 2. Autonomous research and transparency (about 1.5 min)

- Researchers work on every sub-question **in parallel**. Open the **Action log**: searches, sources kept with reasons ("recent, official source"), and drop summaries ("6 lower-ranked, 2 outdated").
- *Talking point:* "Every keep or drop decision is made in code and has a visible reason. Outdated sources are filtered when the topic is time-sensitive."

## 3. The headline behaviour: the gap loop (about 2 min)

- The conversation shows **"Evidence review"**, then orange **Gap** cards. Each one names the problem (thin, conflicting, missing angle), **why it matters**, and the follow-up queries.
- **Round 2** starts and only the weak sub-questions are researched again.
- If a **"Quality gate · draft rejected"** message appears, point at it: *"The agent tried to finish early. The code refused because a gap was still open."*
- When the report arrives, scroll to **"How this report was strengthened"**: gap → why it mattered → before → after (e.g. 1 source from 1 site → 3 sources from 3 sites).
- *Talking point:* "The LLM drives, code guards. Evidence quality is measured, not guessed. The agent has to justify extra research, and it stops after 2 rounds and lists what is still missing."

## 4. Human oversight (about 1.5 min)

Use a second topic, or do this during step 2:

- **Pause.** The status changes and the log shows the user action, labelled as yours. **Resume.**
- **Skip** a sub-question on the plan board. It's marked skipped, no longer researched, and listed in the report as skipped.
- After the report is ready: on the **Sources** tab, **Exclude** a source that the report cites (click a citation chip to jump to it). The agent **revises the report** (version 2 banner) and the source is no longer cited.
- *Talking point:* "Every override is an event in the same log, so you can always tell what the agent decided and what the user decided."

## 5. Memory (about 1 min)

- Click one of the **"Next · related topics"** suggestions, or type a related topic, e.g. *EU AI Act enforcement and fines for AI startups* after an EU AI Act session.
- A **Memory** note appears: *"You've researched related topics before…"*. The earlier takeaways are passed to the agent as context.
- *Talking point:* "Memory is deliberately simple: SQLite and keyword overlap, transparent and good enough at personal scale. Embeddings would be the next step."

## 6. The summary (about 30 s)

- When the run finishes, the **Summary** tab opens: your direction, the findings, evidence per sub-question, gaps with before → after, drafts the checks sent back, source statistics, your actions and next topics.
- Click **Download summary (.md)**: that's the hand-in version of the whole session.
- *Talking point:* "The summary is assembled from the session's records, not written by the model, so it can't misreport what happened."

## If something goes wrong

- **Network or API failure:** open a saved session from the **Sessions** dropdown. The report, sources and full action log replay exactly as they happened.
- **A slow run:** narrate the action log while it works. A run takes about 2–3 minutes in total.

## Likely questions

- *Why Deep Agents?* Planning, sub-agents, streaming and interrupts come built in, so the effort went into the gap loop and the guardrails.
- *How do you know it doesn't make up citations?* Only search results get IDs, the report gate checks every `[Sn]`, and quotes are verified word for word against the source text.
- *What stops it looping forever?* A limit of 2 rounds. After that, gaps are reported rather than chased.
- *How is it tested without spending money?* A scripted fake model drives the real agent graph against a fake search.
