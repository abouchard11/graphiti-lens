# Graphiti Lens

A read-only explorer for the Graphiti temporal knowledge graph in `neo4j-graphiti`.
Nothing off-the-shelf understands Graphiti's bitemporal edges, so this does.

```bash
~/dev/graphiti-lens/run.sh          # opens http://127.0.0.1:7878/?t=<token>
~/dev/graphiti-lens/run.sh --port 8080 --no-token --no-open
lsof -ti tcp:7878 | xargs kill     # stop
```

`run.sh` uses the first Python it finds that already has the `neo4j` driver
(the Graphiti MCP venv), falling back to `uv run --with neo4j`. Nothing to install.

## Modes

| Mode | What it answers |
|---|---|
| **Explore** | *What do I know about X?* Full-text search over entity names, fact text, or episode content. Pick a result to open its neighbourhood. Double-click any node to expand one more hop. |
| **Audit** | *Is the graph right?* Seven scored checks, the fact-lifecycle split, 60-day ingest sparkline, quietest projects, duplicate names, hygiene counts. |
| **Bridges** | *What connects my projects?* Entities whose name appears in more than one `group_id`. Graphiti dedupes inside a group, never across them, so this is the only place portfolio overlap shows up. Nodes colour by project here. |
| **Episodes** | *Why did this episode produce these entities?* Timeline of ingests, each showing extracted entities, derived facts, `source_description`, and whether the content carries an `OBSERVED:`/`DERIVED:` tag. |
| **Cypher** | Free-form read-only Cypher with seven Graphiti-aware recipes. Results render as both a table and a graph when the query returns nodes or relationships. |

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
~/dev/graphiti-lens/run.sh --no-token --no-open &   # or leave it running
curl -s 'http://127.0.0.1:7878/api/health' | jq '.checks[] | select(.status != "good")'
curl -s 'http://127.0.0.1:7878/api/health?group=buylandfl' | jq .bitemporal
```

Shape: `totals`, `bitemporal`, `provenance`, `hygiene`, `checks[]` (each with
`value`, `status`, `detail`), `duplicates[]`, `ingest_daily[]`, `group_staleness[]`.

## Security model

- Binds `127.0.0.1` only. Per-run bearer token in the URL and the `X-Lens-Token` header.
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
  with `good`/`warn` thresholds. The tile grid and the JSON both pick it up.
- New Cypher recipe: append to `SNIPPETS` in `app.js`.
- Node budget: `DEFAULT_NODE_BUDGET` in `server.py` (hard cap 8000). Above ~4000
  nodes on screen the force layout is the bottleneck, not the renderer.
