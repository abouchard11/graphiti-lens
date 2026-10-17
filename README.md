# Graphiti Lens

A read-only explorer for the Graphiti temporal knowledge graph in `neo4j-graphiti`.
Nothing off-the-shelf understands Graphiti's bitemporal edges, so this does.

```bash
./run.sh                            # opens a one-time link (valid 5 min)
./run.sh --port 8080 --no-token --no-open
lsof -ti tcp:7878 | xargs kill      # stop
```

`run.sh` uses the first Python it finds that already has the `neo4j` driver
(a local `.venv`, then the Graphiti MCP venv), falling back to `uv run --with neo4j`.

**Requirements:** a Neo4j database holding a [Graphiti](https://github.com/getzep/graphiti)
graph, and its credentials in `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` (or in the
Graphiti MCP server's `.env`, which the server reads as a fallback).

## Tests

The unit tests need the `neo4j` driver importable but no running database — the same
two steps CI runs (`.github/workflows/tests.yml`):

```bash
python3 -m venv .venv && ./.venv/bin/pip install 'neo4j>=5,<6'
./.venv/bin/python -m unittest discover -s tests -v
```

## Modes

| Mode | What it answers |
|---|---|
| **Explore** | *What do I know about X?* Full-text search over entity names, fact text, or episode content. Pick a result to open its neighbourhood. Double-click any node to expand one more hop. |
| **Audit** | *Is the graph right?* Thirteen checks (eleven graph-shape and operability checks, plus two on stale facts: anchored facts whose evidence still matches, and new observed writes carrying evidence), the fact-lifecycle split, 60-day ingest sparkline, quietest projects, duplicate names, hygiene counts. |
| **Bridges** | *What connects my projects?* Entities whose name appears in more than one `group_id`. Graphiti dedupes inside a group, never across them, so this is the only place portfolio overlap shows up. Nodes colour by project here. |
| **Episodes** | *Why did this episode produce these entities?* Timeline of ingests, each showing extracted entities, derived facts, `source_description`, and whether the content carries an `OBSERVED:`/`DERIVED:` tag. |
| **Cypher** | Free-form read-only Cypher with seven Graphiti-aware recipes. Results render as both a table and a graph when the query returns nodes or relationships. |
| **`/api/explain`** | The same questions, answered in prose instead of pixels — see below. No UI; it is an endpoint. |

## The text surface

Every other view here is a drawing, which is no use to a screen reader, to an agent with no
vision, or to anyone who does not think in pictures. `/api/explain` answers the Explore
question in plain text:

```bash
TOKEN=$(cat .lens-token)          # from the repo directory
curl -s -H "X-Lens-Token: $TOKEN" '127.0.0.1:7878/api/explain?entity=Alex&days=14'
curl -s -H "X-Lens-Token: $TOKEN" '127.0.0.1:7878/api/explain?entity=Alex&format=json'
```

It returns every node carrying that name and which group each sits in, the strongest facts
with their state spelled out as a word (`live`, `superseded`, `retracted` — never colour
alone), what was superseded inside the window, and what was written about it. `format=json`
returns the same payload as data. Output is plain text with no tabs and no markup, so it
diffs cleanly and can feed a check rather than only being looked at.

## The temporal controls

Graphiti keeps superseded facts instead of deleting them. Two controls make that legible:

- **As of** — a scrubber across the ingest window. Turn it on and drag: edges that were
  not live on that date drop to 10% opacity, and the HUD reports how many facts stood
  that day. Computed client-side, so it is instant.
- **Hide superseded** — drops non-live edges from the canvas *and* from the force
  simulation, so the layout reflects only what still stands.

Edges carry their state in colour **and** dash pattern: solid grey = live,
dashed amber = superseded (`invalid_at`), dotted red = retracted (`expired_at`).

## Wiring the audit into the Monday drift check

`/api/health` returns the whole audit as stable JSON — same numbers as the Audit tab.

```bash
./run.sh --no-token --no-open &   # or leave it running
curl -s 'http://127.0.0.1:7878/api/health' | jq '.checks[] | select(.status == "warning" or .status == "critical")'
curl -s 'http://127.0.0.1:7878/api/health?group=buylandfl' | jq .bitemporal
```

Shape: `totals`, `bitemporal`, `provenance`, `hygiene`, `checks[]` (each with
`value`, `status`, `detail`), `duplicates[]`, `ingest_daily[]`, `quiet_groups[]`.

`status` is `good`, `warning`, `critical`, or (from 2026-10-15) `not_measured`. A
`not_measured` check has `value: null`: it has nothing to score yet, and a consumer must
count it as neither healthy nor failing. Today that is `evidence_fresh` and
`anchor_coverage` on any database with no `(:Evidence)` node in scope. The old filter
`select(.status != "good")` will therefore list them; use
`select(.status == "warning" or .status == "critical")` to see only failures. (The memory
server's `get_status` lists them under `lens.checks_not_measured`, not `checks_not_good`.)

`evidence_fresh` is current ÷ (current + stale) over live anchored facts. Facts whose
evidence nobody has confirmed yet (`trust_state = unverified`: never checked, source
unreachable, or checkable only on the server) are counted in the detail line and kept out
of the percentage: they are evidence of neither freshness nor staleness.

`quiet_groups[]` (groups ordered by last write) was called `group_staleness[]` until
2026-10-15, when "stale" was reserved for facts whose evidence no longer matches. The old
key is still emitted with identical content for one release (remove after 2026-11); read
`quiet_groups`.

**The same audit is also a file.** While the Lens runs it writes
`~/.local/state/graphiti/lens-health.json` every 15 minutes (atomic write, so a reader
never sees half a file). Anything that only needs the numbers — `truth.py`, the drift
check, the memory daemon's `get_status` on the UpCloud host (the infra repo's state-pull
job pushes the file there) — reads the file and needs no token and no live Lens. Tune
with `LENS_HEALTH_FILE` / `LENS_HEALTH_INTERVAL_S`; `--no-health-file` turns it off.

## Security model

- Binds `127.0.0.1` only. The bearer token has exactly one home: `.lens-token`, mode 600,
  written by the server at startup. It is not in the LaunchAgent plist, not in the log, and
  never in a URL. A restart rotates it, which is why nothing may cache the old value.
- **Opening the UI uses a one-time nonce, not the token.** `POST /api/session` with the token
  in the `X-Lens-Token` header returns a nonce good for one request and 30 seconds; the app
  opens `/?t=<nonce>`, and the server trades it for an `HttpOnly; SameSite=Strict` session
  cookie. Anything a URL leaks into — browser history, this server's log, a screen share —
  therefore holds a credential that was already dead when it was written. `run.sh` prints a
  5-minute nonce for the same reason.
- Neo4j credentials are resolved server-side from the environment or
  `~/dev/graphiti/mcp_server/.env`. They never reach the browser.
- **Every** query runs in a Neo4j READ transaction. A write is refused by the database
  with `Neo.ClientError.Statement.AccessMode`, not merely by a string match. The lexical
  denylist on the Cypher console is a second layer that fails fast with a clear message.
- First-party endpoints use bound parameters only. Search terms are stripped of Lucene
  syntax before touching the full-text indexes.
- Embedding vectors (`name_embedding`, `fact_embedding`) are dropped server-side and
  never serialised.

## Layout

```
server.py            stdlib HTTP + neo4j driver; all queries and the audit logic
run.sh               interpreter picker
static/index.html    shell + styles (dark only, by design)
static/app.js        canvas force-graph, modes, inspector, audit dashboard
static/vendor/       d3 v7 UMD, vendored — no CDN, works offline
```

Renderer is a hand-written canvas layer over `d3-force`. WebGL (sigma) was the other
option and was dropped: it cannot draw dashed edges without a custom shader program,
and the dash pattern is how superseded facts stay readable in greyscale and for
colour-vision-deficient viewing.

## Extending

- New audit check: add a query in `api_health`, then push an entry onto `checks`
  with `good`/`warn` thresholds. The tile grid and the JSON both pick it up. A check
  that cannot be measured yet sets `value: None`; `check_status` turns that into
  `not_measured`, and the tile shows a dash instead of a score.
- New Cypher recipe: append to `SNIPPETS` in `app.js`.
- Node budget: `DEFAULT_NODE_BUDGET` in `server.py` (hard cap 8000). Above ~4000
  nodes on screen the force layout is the bottleneck, not the renderer.
