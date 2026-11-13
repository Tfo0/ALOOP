<div align="center">

# aloop

`while (!done()) agent()`

**The loop is all you need. Everything is a file.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue)

</div>

Claude Code pursues a goal as **loop + LLM**: one context accumulates until it has to be compacted.

aloop turns that into **loop + agent**: every iteration spawns a fresh, zero-context agent (a brand-new claude session), and the disk is the only memory. No conversation history, no compaction, no context rot — the context-management problem flips from "what do we do when it's full" to "**what should each fresh agent see when it wakes up**".

Everything else follows from that inversion.

## Skeleton: ReAct, with the call unit upgraded from LLM to agent

```
planner = Thought      when the queue runs dry, reads the whole board and writes new todos
worker  = Act          claims one card and works: findings land in cache/, deliverables in goal/
disk    = Observation  observations don't go into a transcript — they go into the filesystem
```

When observations live on disk instead of in a transcript, **fidelity** becomes the core problem: if records are false, messy, or unfindable, every plan is built on sand. Hence the write discipline (filename = one-sentence conclusion) and the watchdog (periodic curation; it fixes records, never picks directions).

The classic death of such loops is task-list drift. Three mechanisms address it directly: **dedup discipline** against repetition, **goal/ acceptance** against drift, **watchdog** against record rot.

## Mapping to Claude Code

| Claude Code | aloop |
|---|---|
| system prompt | three prompts in `prompts.py` (worker / planner / watchdog) |
| CLAUDE.md + memory | `cache/`: filename = one-sentence conclusion, `ls` is the index |
| conversation history ("what just happened") | `cache/done/` archive: incrementing IDs + a result/artifact footer on every card |
| compaction | the watchdog agent, on a timer |
| subagents | workers: zero context in, only what hit the disk comes back |
| human-in-the-loop corrections | human edits files: deleting from `goal/` = reject; any `人工*`-prefixed dir in `cache/` = veto list |

The split of labor: **deterministic things belong to code** (acceptance, dispatch, sequencing, reaping), **judgment belongs to the model** (direction, truth, curation).

## Usage

```bash
pip install claude-agent-sdk   # also requires a working claude CLI (or compatible gateway)

python loop.py "research a topic and produce 3 reports" --count 3
python loop.py "goal..." --verify "your-shell-check-command"
```

**The model produces; the code judges.** Acceptance is executed by code: `--count N` counts first-level subdirectories under `goal/` (one directory = one deliverable, attachments included), or `--verify` runs any shell command. Only two ways to stop: acceptance passes, or Ctrl-C.

## The loop

```
while not accepted:
    todos exist     → dispatch workers (concurrent, free slots refill)
    todos exhausted → dispatch the planner to read the board and write new todos
    every hour      → dispatch the watchdog to curate memory
```

The queue flows one way: `todo → doing → done`. Nothing is ever mechanically retried.

All the "orchestration logic" is the three prompts in `prompts.py`. To change behavior, edit that file — don't add code.

## Intervening

No UI. Intervening = editing files:

- write `queue/todo/xxx.md` → executed next tick
- delete a deliverable from `goal/` → sent back for a redo
- move it into any `人工*`-prefixed dir under `cache/` → permanently vetoed, never touched or proposed again
- Ctrl-C → stop (all agent processes are killed with it); rerun the same command to resume from disk

## Parameters

| flag | default | meaning |
|---|---|---|
| `--workspace` | `./workspace` | working directory |
| `--parallel` | 6 | max concurrent agents |
| `--model` / `--plan-model` | `deepseek-v4.1-flash` | model for workers / planner; planning deserves a stronger one |
| `--timeout` | 25 min | per-agent timeout; fresh sessions are cheap, long ones rot |
| `--max-agents` | unlimited | budget fuse: stop after N total dispatches |

## Limitations

- `bypassPermissions`: agents hold full file permissions inside the workspace — **do not run it in directories containing important data**
- `--count` counts directories, not quality; use `--verify` when it matters
- does not make the model smarter: the ceiling is the model, and the precision of your goal

## Files

6 Python files, ~580 lines, sole dependency `claude-agent-sdk`:

`loop.py` main loop · `prompts.py` the three prompts · `agent.py` SDK layer · `workspace.py` directory layout · `goal.py` acceptance · `log.py` logging

## Credits

- artex — the subject of this analysis: it proved the capability ceiling of this shape; this project merely explains where the ceiling comes from
- [karpathy/llama2.c](https://github.com/karpathy/llama2.c) — the reference for "explain one thing with a minimal implementation"
- built on [claude-agent-sdk](https://github.com/anthropics/claude-agent-sdk-python)

## License

MIT
