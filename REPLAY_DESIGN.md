# Session recorder + replay — design

A recorder that logs everything a bot does (and what was around it) to disk, plus a
dashboard replay mode to scrub through a session. For watching a bot's run for fun and,
mainly, for debugging strange behaviour after the fact.

## Locked decisions (2026-07-14)

- **Fidelity: Tier 2** — per-tick position/status/vitals, the CREATURES around the bot,
  the ACTIONS it took (walk / autowalk / use / escape / descend), and the DECISIONS
  behind them (chose frontier X, declined descent — landing crowded, blocked tile Y,
  boxed-in → break out). The "why" trace is what makes replay a debugging tool, not a
  toy.
- **Scope: flight-recorder by default + explicit whole-run record.**
  - *Flight recorder* (default): keep the last N minutes in memory (a time-bounded ring);
    auto-persist the segment when something bad happens (freeze / death). Always-on but
    bounded — you keep the footage around every rare bug without drowning in data.
  - *Full record* (opt-in, e.g. `--record full`): write every record straight to disk for
    a run you just want to watch end-to-end.
- **Replay UX: standard scrubber + jump-to-event.** Play/pause, speed, seek; plus click a
  logged event (stuck / boxed-in / freeze) to jump the replay to that moment.
- **Storage: JSONL** (append-only, greppable, streamable; gzip on close). Move to SQLite
  only if seek/filter-at-scale gets painful. HARD RULE: writes are buffered/async so a
  slow disk can never stall the navigation loop.

## Data model (JSONL, one object per line)

First line is a header; the rest are timestamped records. `t` is wall-clock epoch seconds
(so multiple bots' files share one timeline for a future swarm replay).

```
{"type":"header","bot":"Druid 1","character":"Druid 1","start":<t>,"mode":"flight|full"}
{"t":<t>,"type":"snapshot","x":..,"y":..,"z":..,"status":"scouting","hp":..,"maxhp":..,
         "mana":..,"maxmana":..,"level":..,"creatures":[{"id":..,"name":"Rat","x":..,"y":..,"z":..,"hp":100}]}
{"t":<t>,"type":"action","action":"autowalk","dirs":["north","north","east"]}
{"t":<t>,"type":"action","action":"use","category":"ladder","source":[x,y,z]}
{"t":<t>,"type":"decision","what":"declined_descent","reason":"crowded","dest":[x,y,z]}
{"t":<t>,"type":"decision","what":"chose_frontier","target":[x,y]}
{"t":<t>,"type":"event","level":"warning","category":"boxed-in","msg":".."}
```

Replay: the `snapshot` records are the timeline spine (binary-search the latest one ≤ T to
draw the bot + creatures + vitals); `action` / `decision` / `event` records are point
annotations shown in a side log and used for jump-to-event. Simple to write, simple to
render — no delta-fold needed for v1 (gzip absorbs the repetition; the flight ring keeps
memory bounded).

## Capture points (low-touch, central)

- `GameSession._report_to_colony` (every step) → `recorder.snapshot(...)` (pulls creatures
  from `state.nearby_creatures()`).
- `GameSession.walk / autowalk / use_item / use_item_with` → `recorder.action(...)`.
- `GameSession.log_event` → `recorder.event(...)` (events come for free).
- A handful of `recorder.decision(...)` calls at scout decision points for the "why".

## Phasing

1. **Phase 1 (DONE 2026-07-14)** — `recorder.py` (flight + full modes, async buffered
   writer), capture wiring, `--record` farm option, replay endpoints + single-bot replay
   in the dashboard (scrubber, playback, creatures, side log). Verified end to end in the
   browser: session picker, floor backdrop, bot + creature dots, seek, play, and the log.
2. **Phase 2 (DONE 2026-07-14, with Phase 1)** — decision trace (chose_frontier /
   declined_descent / take_shortcut / escaped) + events, all in the timeline log with
   click-to-jump. STILL OPEN from Phase 2: whole-SWARM timeline (load every recording
   overlapping a time range, render all bots on one wall clock) — the single-bot viewer
   is done; the multi-bot merge is the remaining piece.

### Refinements noticed while building (nice-to-have)
- **Zoom-to-bot option.** Replay currently frames the whole explored floor, so on a huge
  floor the bot shrinks to ~1px and adjacent creatures hide under the bot dot. A "follow
  the bot at fixed zoom" toggle (a window around the bot instead of whole-floor) would
  read much better. The data + render are fine; it's a framing choice.
- **Gzip recordings on close** (design said so; v1 keeps plain .jsonl for grep/serve ease).
- **Filter the log** to just events / just decisions when a session has thousands of
  chose_frontier entries.

## Future work (documented, not built)

- **Tier 3 — full sensor**: record the bot's view-window tiles each tick (exactly what it
  could see), for perfect perception replay. Heavier storage.
- **Tier 4 — deterministic re-simulation**: record raw server frames + inputs, then replay
  them through UPDATED bot code to test a fix against real history ("would the new
  pathfinder have avoided this freeze?"). The holy grail for debugging; heavy.
- **Live DVR**: pause/rewind a *running* session without stopping it.
- **Diff two runs side-by-side**: watch before/after a fix on one screen.
- **Aggregate heatmaps**: across ALL sessions — where bots get stuck, which links carry
  traffic, coverage over time. Recordings → swarm analytics.
- **Export a clip / GIF** to share a moment.
- **SQLite backend** for seek/filter when sessions get large.
