#!/usr/bin/env python3
"""
Graphiti Lens - a read-only local explorer for a Graphiti temporal knowledge graph
stored in Neo4j.

Design notes:
  * Loopback-only bind + per-run bearer token. Credentials never reach the browser.
  * EVERY query runs inside an explicit Neo4j READ transaction, so the server
    rejects writes even if a query slips past the lexical guard.
  * All first-party endpoints use bound parameters. The only place raw Cypher is
    accepted is /api/cypher, which is guarded twice (lexical denylist + read txn).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import pathlib
import secrets
import time
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

try:
    from neo4j import GraphDatabase
    from neo4j.exceptions import Neo4jError
except ImportError:  # pragma: no cover
    sys.exit(
        "neo4j driver not found.\n"
        "Run via ./run.sh, or: uv run --with 'neo4j>=5,<6' python3 server.py"
    )

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

# Where to look for Neo4j credentials if the environment does not supply them.
ENV_CANDIDATES = [
    HERE / ".env",
    Path.home() / "dev" / "graphiti" / "mcp_server" / ".env",
]

MAX_ROWS = 5000
DEFAULT_NODE_BUDGET = 1200
QUERY_TIMEOUT_S = 30

# Lexical guard for the Cypher console. The read transaction is the real
# enforcement; this exists to fail fast with a clear message.
WRITE_TOKENS = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|FOREACH|LOAD\s+CSV|"
    r"CALL\s*\{[^}]*\b(CREATE|MERGE|DELETE|SET)\b)\b",
    re.IGNORECASE,
)
WRITE_PROCS = re.compile(
    r"\b(apoc\.(create|merge|refactor|periodic|trigger|import|load)|"
    r"db\.(create|awaitIndex|index\.fulltext\.(create|drop))|dbms\.(security|killQuery))",
    re.IGNORECASE,
)
LUCENE_SPECIALS = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')


def load_env_file(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip("'").strip('"')
    except OSError:
        pass
    return out


def resolve_credentials() -> tuple[str, str, str]:
    uri = os.environ.get("NEO4J_URI")
    user = os.environ.get("NEO4J_USER")
    pwd = os.environ.get("NEO4J_PASSWORD")
    if not (uri and user and pwd):
        for cand in ENV_CANDIDATES:
            if not cand.exists():
                continue
            env = load_env_file(cand)
            uri = uri or env.get("NEO4J_URI")
            user = user or env.get("NEO4J_USER")
            pwd = pwd or env.get("NEO4J_PASSWORD")
            if uri and user and pwd:
                break
    if not (uri and user and pwd):
        sys.exit(
            "Could not resolve Neo4j credentials.\n"
            "Set NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD, or place them in "
            + " or ".join(str(p) for p in ENV_CANDIDATES)
        )
    return uri, user, pwd


# --------------------------------------------------------------------------- #
# Neo4j access
# --------------------------------------------------------------------------- #

class Graph:
    def __init__(self, uri: str, user: str, pwd: str):
        self.driver = GraphDatabase.driver(uri, auth=(user, pwd))
        self.driver.verify_connectivity()

    def close(self):
        self.driver.close()

    def read(self, cypher: str, params: dict | None = None, timeout: int = QUERY_TIMEOUT_S):
        """
        Run cypher inside a READ transaction and materialise the rows before the
        transaction closes. Node / Relationship objects are preserved so callers
        can read labels; jsonable() flattens them at serialisation time.
        Writes are refused by the server, not just by the lexical guard.
        """
        params = params or {}
        # default_access_mode="READ" makes the driver open the transaction with
        # Bolt mode 'r'; the server then refuses write clauses inside it.
        with self.driver.session(default_access_mode="READ") as session:
            with session.begin_transaction(timeout=timeout) as tx:
                res = tx.run(cypher, **params)
                return [{k: rec[k] for k in rec.keys()} for rec in res]

    def one(self, cypher: str, params: dict | None = None, default=None):
        rows = self.read(cypher, params)
        return rows[0] if rows else (default if default is not None else {})


def jsonable(v):
    """Coerce neo4j temporal / spatial / graph types into JSON-safe values."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    for attr in ("iso_format", "isoformat"):
        fn = getattr(v, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    # neo4j Node / Relationship
    if hasattr(v, "labels") and hasattr(v, "items"):
        d = {k: jsonable(x) for k, x in v.items() if k not in DROP_PROPS}
        d["_labels"] = sorted(v.labels)
        return d
    if hasattr(v, "type") and hasattr(v, "items"):
        d = {k: jsonable(x) for k, x in v.items() if k not in DROP_PROPS}
        d["_type"] = v.type
        return d
    return str(v)


# Embeddings are huge and useless in a UI; never ship them to the browser.
DROP_PROPS = {"name_embedding", "fact_embedding", "summary_embedding"}

# An edge is live at :asof when it had started, had not been superseded by a
# newer fact (invalid_at), and had not been retracted from the record (expired_at).
LIVE_PREDICATE = (
    "(coalesce(r.valid_at, r.created_at) <= datetime($asof)) "
    "AND (r.invalid_at IS NULL OR r.invalid_at > datetime($asof)) "
    "AND (r.expired_at IS NULL OR r.expired_at > datetime($asof))"
)

EDGE_STATE = (
    "CASE WHEN r.expired_at IS NOT NULL THEN 'expired' "
    "WHEN r.invalid_at IS NOT NULL THEN 'invalidated' "
    "ELSE 'live' END"
)


def lucene_safe(q: str) -> str:
    """Neutralise Lucene syntax, then prefix-match the trailing token."""
    cleaned = LUCENE_SPECIALS.sub(" ", q or "").strip()
    if not cleaned:
        return ""
    parts = [p for p in cleaned.split() if p]
    if not parts:
        return ""
    parts[-1] = parts[-1] + "*"
    return " ".join(parts)


def guard_cypher(q: str):
    stripped = re.sub(r"//[^\n]*", " ", q or "")
    stripped = re.sub(r"/\*.*?\*/", " ", stripped, flags=re.S)
    if WRITE_TOKENS.search(stripped):
        raise ValueError("Write clauses are refused. Graphiti Lens is read-only.")
    if WRITE_PROCS.search(stripped):
        raise ValueError("That procedure can mutate the graph. Refused.")


# --------------------------------------------------------------------------- #
# Subgraph assembly
# --------------------------------------------------------------------------- #

NODE_FIELDS = "n.uuid AS uuid, n.name AS name, n.group_id AS group_id, n.summary AS summary, n.created_at AS created_at, labels(n) AS node_labels, n.labels AS entity_types"

INDUCED = """
MATCH (n:Entity) WHERE n.uuid IN $uuids
WITH collect(n) AS ns
CALL {
  WITH ns
  UNWIND ns AS a
  MATCH (a)-[r:RELATES_TO]->(b:Entity) WHERE b IN ns
  RETURN collect(DISTINCT r) AS rels
}
RETURN ns AS nodes, rels
"""


def pack_node(n) -> dict:
    p = {k: jsonable(v) for k, v in n.items() if k not in DROP_PROPS}
    labels = sorted(n.labels)
    kind = "episodic" if "Episodic" in labels else ("community" if "Community" in labels else "entity")
    return {
        "id": p.get("uuid"),
        "name": p.get("name") or "(unnamed)",
        "kind": kind,
        "group": p.get("group_id"),
        "types": p.get("labels") or [],
        "summary": p.get("summary") or p.get("content") or "",
        "created_at": p.get("created_at"),
        "source_description": p.get("source_description"),
        "valid_at": p.get("valid_at"),
    }


def pack_edge(r, asof: str | None) -> dict:
    p = {k: jsonable(v) for k, v in r.items() if k not in DROP_PROPS}
    # invalid_at wins over expired_at: Graphiti commonly stamps BOTH when a fact
    # is superseded, and "the world changed" is the reading that drives as-of
    # queries. A bare expired_at (no invalid_at) is a true record retraction.
    if p.get("invalid_at"):
        state = "invalidated"
    elif p.get("expired_at"):
        state = "expired"
    else:
        state = "live"
    live = True
    if asof:
        start = p.get("valid_at") or p.get("created_at")
        live = (
            (start is None or start <= asof)
            and (not p.get("invalid_at") or p["invalid_at"] > asof)
            and (not p.get("expired_at") or p["expired_at"] > asof)
        )
    return {
        "id": p.get("uuid"),
        "source": p.get("source_node_uuid"),
        "target": p.get("target_node_uuid"),
        "name": p.get("name") or "RELATES_TO",
        "fact": p.get("fact") or "",
        "group": p.get("group_id"),
        "state": state,
        "live": live,
        "created_at": p.get("created_at"),
        "valid_at": p.get("valid_at"),
        "invalid_at": p.get("invalid_at"),
        "expired_at": p.get("expired_at"),
        "episodes": p.get("episodes") or [],
    }


def induced_subgraph(g: Graph, uuids: list[str], asof: str | None) -> dict:
    if not uuids:
        return {"nodes": [], "edges": []}
    rows = g.read(INDUCED, {"uuids": uuids})
    if not rows:
        return {"nodes": [], "edges": []}
    nodes = [pack_node(n) for n in rows[0]["nodes"]]
    present = {n["id"] for n in nodes}
    edges = [pack_edge(r, asof) for r in (rows[0]["rels"] or [])]
    edges = [e for e in edges if e["source"] in present and e["target"] in present]
    deg = {}
    for e in edges:
        deg[e["source"]] = deg.get(e["source"], 0) + 1
        deg[e["target"]] = deg.get(e["target"], 0) + 1
    for n in nodes:
        n["degree"] = deg.get(n["id"], 0)
    return {"nodes": nodes, "edges": edges}


# --------------------------------------------------------------------------- #
# API endpoints
# --------------------------------------------------------------------------- #

def api_meta(g: Graph, q) -> dict:
    groups = g.read(
        "MATCH (n:Entity) WITH n.group_id AS g, count(*) AS entities "
        "RETURN g AS group, entities ORDER BY entities DESC"
    )
    eps = {r["group"]: r for r in g.read(
        "MATCH (e:Episodic) WITH e.group_id AS g, count(*) AS episodes, max(e.created_at) AS last "
        "RETURN g AS group, episodes, last"
    )}
    edges = {r["group"]: r for r in g.read(
        "MATCH ()-[r:RELATES_TO]->() WITH r.group_id AS g, count(r) AS edges "
        "RETURN g AS group, edges"
    )}
    out = []
    for row in groups:
        gid = row["group"]
        e = eps.get(gid, {})
        out.append({
            "group": gid,
            "entities": row["entities"],
            "episodes": e.get("episodes", 0),
            "edges": edges.get(gid, {}).get("edges", 0),
            "last_episode": jsonable(e.get("last")),
        })
    totals = g.one(
        "MATCH (n:Entity) WITH count(n) AS entities "
        "MATCH (e:Episodic) WITH entities, count(e) AS episodes "
        "OPTIONAL MATCH (c:Community) WITH entities, episodes, count(c) AS communities "
        "MATCH ()-[r:RELATES_TO]->() RETURN entities, episodes, communities, count(r) AS edges"
    )
    types = g.read(
        "MATCH (n:Entity) WHERE n.labels IS NOT NULL UNWIND n.labels AS t "
        "RETURN t AS type, count(*) AS c ORDER BY c DESC LIMIT 40"
    )
    span = g.one(
        "MATCH (e:Episodic) RETURN min(e.created_at) AS first, max(e.created_at) AS last"
    )
    return {
        "groups": out,
        "totals": {k: jsonable(v) for k, v in totals.items()},
        "entity_types": [{"type": r["type"], "count": r["c"]} for r in types],
        "span": {k: jsonable(v) for k, v in span.items()},
        "server_now": datetime.now(timezone.utc).isoformat(),
    }


def api_search(g: Graph, q) -> dict:
    term = (q.get("q", [""])[0] or "").strip()
    group = q.get("group", [None])[0] or None
    kind = (q.get("kind", ["entity"])[0] or "entity").lower()
    limit = clamp_int(q.get("limit", ["40"])[0], 1, 300, 40)
    lq = lucene_safe(term)
    if not lq:
        return {"results": [], "note": "empty query"}

    idx, call, yields = {
        "entity": ("node_name_and_summary", "queryNodes", "node"),
        "episode": ("episode_content", "queryNodes", "node"),
        "fact": ("edge_name_and_fact", "queryRelationships", "relationship"),
    }.get(kind, ("node_name_and_summary", "queryNodes", "node"))

    cypher = (
        f"CALL db.index.fulltext.{call}('{idx}', $q) YIELD {yields} AS x, score "
        "WHERE ($group IS NULL OR x.group_id = $group) "
        "RETURN x, score LIMIT $limit"
    )
    try:
        rows = g.read(cypher, {"q": lq, "group": group, "limit": limit})
    except Neo4jError:
        # Fulltext unavailable or malformed - fall back to a bounded scan.
        label = "Episodic" if kind == "episode" else "Entity"
        rows = g.read(
            f"MATCH (x:{label}) WHERE ($group IS NULL OR x.group_id = $group) "
            "AND toLower(coalesce(x.name,'') + ' ' + coalesce(x.summary, x.content, '')) "
            "CONTAINS toLower($t) RETURN x, 0.0 AS score LIMIT $limit",
            {"t": term, "group": group, "limit": limit},
        )
    results = []
    for r in rows:
        x = r["x"]
        if kind == "fact":
            item = pack_edge(x, None)
            item["label"] = item["fact"] or item["name"]
        else:
            item = pack_node(x)
            item["label"] = item["name"]
        item["score"] = round(float(r.get("score") or 0), 4)
        results.append(item)
    return {"results": results, "kind": kind, "query": term}


def api_ego(g: Graph, q) -> dict:
    uuid = q.get("uuid", [""])[0]
    if not uuid:
        raise ValueError("uuid is required")
    depth = clamp_int(q.get("depth", ["1"])[0], 1, 3, 1)
    limit = clamp_int(q.get("limit", [str(DEFAULT_NODE_BUDGET)])[0], 10, 8000, DEFAULT_NODE_BUDGET)
    asof = q.get("asof", [None])[0] or None
    rows = g.read(
        f"MATCH (a:Entity {{uuid: $uuid}}) "
        f"OPTIONAL MATCH (a)-[:RELATES_TO*1..{depth}]-(b:Entity) "
        "WITH a, collect(DISTINCT b.uuid)[0..$limit] AS nbrs "
        "RETURN [a.uuid] + nbrs AS uuids",
        {"uuid": uuid, "limit": limit},
    )
    uuids = rows[0]["uuids"] if rows else [uuid]
    sub = induced_subgraph(g, [u for u in uuids if u], asof)
    sub["focus"] = uuid
    return sub


def api_group(g: Graph, q) -> dict:
    group = q.get("group", [None])[0] or None
    limit = clamp_int(q.get("limit", [str(DEFAULT_NODE_BUDGET)])[0], 10, 8000, DEFAULT_NODE_BUDGET)
    asof = q.get("asof", [None])[0] or None
    order = "n.created_at DESC" if q.get("order", ["degree"])[0] == "recent" else "deg DESC"
    rows = g.read(
        "MATCH (n:Entity) WHERE ($group IS NULL OR n.group_id = $group) "
        "OPTIONAL MATCH (n)-[r:RELATES_TO]-() "
        "WITH n, count(r) AS deg "
        f"RETURN n.uuid AS uuid ORDER BY {order} LIMIT $limit",
        {"group": group, "limit": limit},
    )
    sub = induced_subgraph(g, [r["uuid"] for r in rows], asof)
    sub["truncated"] = len(rows) >= limit
    return sub


def api_node(g: Graph, q) -> dict:
    uuid = q.get("uuid", [""])[0]
    if not uuid:
        raise ValueError("uuid is required")
    node = g.one("MATCH (n) WHERE n.uuid = $uuid RETURN n", {"uuid": uuid})
    if not node:
        raise KeyError("node not found")
    packed = pack_node(node["n"])
    facts = g.read(
        "MATCH (a:Entity {uuid:$uuid})-[r:RELATES_TO]-(b:Entity) "
        "RETURN r, b.uuid AS other_id, b.name AS other, "
        "(startNode(r).uuid = $uuid) AS outgoing "
        "ORDER BY coalesce(r.valid_at, r.created_at) DESC LIMIT 400",
        {"uuid": uuid},
    )
    packed["facts"] = [
        dict(pack_edge(r["r"], None), other=r["other"], other_id=r["other_id"], outgoing=r["outgoing"])
        for r in facts
    ]
    eps = g.read(
        "MATCH (e:Episodic)-[:MENTIONS]->(n:Entity {uuid:$uuid}) "
        "RETURN e.uuid AS uuid, e.name AS name, e.created_at AS created_at, "
        "e.source_description AS source_description, left(coalesce(e.content,''), 400) AS excerpt "
        "ORDER BY e.created_at DESC LIMIT 50",
        {"uuid": uuid},
    )
    packed["episodes"] = [{k: jsonable(v) for k, v in r.items()} for r in eps]
    return packed


# The provenance tier of an Episodic node `e`. Mirrors TIER_CYPHER in the graphiti MCP server
# (mcp_server/src/services/scope.py): since 2026-09-15 the server stores the tier on the node
# right after a write lands; the body prefix is only the fallback for older episodes. Counting
# by prefix alone read 7.7% here when 98.9% of the week's writes carried a tier.
TIER_EXPR = (
    "coalesce(e.tier, CASE "
    "WHEN coalesce(e.content,'') STARTS WITH 'OBSERVED:' THEN 'observed' "
    "WHEN coalesce(e.content,'') STARTS WITH 'DERIVED:' THEN 'derived' "
    "WHEN coalesce(e.content,'') STARTS WITH 'VERIFIED:' THEN 'verified' "
    "WHEN coalesce(e.content,'') STARTS WITH 'DECIDED:' THEN 'decided' END)"
)


def api_episodes(g: Graph, q) -> dict:
    group = q.get("group", [None])[0] or None
    limit = clamp_int(q.get("limit", ["100"])[0], 1, 500, 100)
    rows = g.read(
        "MATCH (e:Episodic) WHERE ($group IS NULL OR e.group_id = $group) "
        "OPTIONAL MATCH (e)-[:MENTIONS]->(n:Entity) "
        "WITH e, count(n) AS mentions "
        "RETURN e.uuid AS uuid, e.name AS name, e.group_id AS group, e.created_at AS created_at, "
        "e.valid_at AS valid_at, e.source AS source, e.source_description AS source_description, "
        "mentions, size(coalesce(e.entity_edges, [])) AS edge_count, "
        "left(coalesce(e.content,''), 240) AS excerpt, "
        f"{TIER_EXPR} AS tier, {TIER_EXPR} IS NOT NULL AS tagged "
        "ORDER BY e.created_at DESC LIMIT $limit",
        {"group": group, "limit": limit},
    )
    return {"episodes": [{k: jsonable(v) for k, v in r.items()} for r in rows]}


def api_episode(g: Graph, q) -> dict:
    uuid = q.get("uuid", [""])[0]
    if not uuid:
        raise ValueError("uuid is required")
    row = g.one("MATCH (e:Episodic {uuid:$uuid}) RETURN e", {"uuid": uuid})
    if not row:
        raise KeyError("episode not found")
    ep = {k: jsonable(v) for k, v in row["e"].items() if k not in DROP_PROPS}
    ents = g.read(
        "MATCH (e:Episodic {uuid:$uuid})-[:MENTIONS]->(n:Entity) RETURN n.uuid AS uuid",
        {"uuid": uuid},
    )
    uuids = [r["uuid"] for r in ents]
    sub = induced_subgraph(g, uuids, None)
    derived = g.read(
        "MATCH ()-[r:RELATES_TO]->() WHERE $uuid IN coalesce(r.episodes, []) RETURN r LIMIT 500",
        {"uuid": uuid},
    )
    ep["derived_edges"] = [pack_edge(r["r"], None) for r in derived]
    ep["extracted_count"] = len(uuids)
    ep["graph"] = sub
    ep["provenance_ok"] = bool(
        (ep.get("content") or "").startswith("OBSERVED:")
        or (ep.get("content") or "").startswith("DERIVED:")
    )
    return ep


def api_bridges(g: Graph, q) -> dict:
    min_groups = clamp_int(q.get("min_groups", ["2"])[0], 2, 10, 2)
    limit = clamp_int(q.get("limit", ["150"])[0], 1, 500, 150)
    rows = g.read(
        "MATCH (n:Entity) WHERE n.name IS NOT NULL "
        "WITH toLower(trim(n.name)) AS key, collect(DISTINCT n.group_id) AS groups, "
        "     collect(n.uuid) AS uuids, collect(n.name)[0] AS display "
        "WHERE size(groups) >= $min_groups "
        "RETURN key, display, groups, size(groups) AS group_count, uuids, size(uuids) AS node_count "
        "ORDER BY group_count DESC, node_count DESC LIMIT $limit",
        {"min_groups": min_groups, "limit": limit},
    )
    return {"bridges": [{k: jsonable(v) for k, v in r.items()} for r in rows]}


def api_bridge_graph(g: Graph, q) -> dict:
    key = (q.get("key", [""])[0] or "").strip().lower()
    if not key:
        raise ValueError("key is required")
    hops = clamp_int(q.get("hops", ["1"])[0], 0, 2, 1)
    limit = clamp_int(q.get("limit", ["600"])[0], 10, 4000, 600)
    asof = q.get("asof", [None])[0] or None
    if hops == 0:
        rows = g.read(
            "MATCH (n:Entity) WHERE toLower(trim(n.name)) = $key RETURN collect(n.uuid) AS uuids",
            {"key": key},
        )
    else:
        rows = g.read(
            f"MATCH (a:Entity) WHERE toLower(trim(a.name)) = $key "
            f"OPTIONAL MATCH (a)-[:RELATES_TO*1..{hops}]-(b:Entity) "
            "WITH collect(DISTINCT a.uuid) AS anchors, collect(DISTINCT b.uuid) AS nbrs "
            "RETURN anchors + nbrs[0..$limit] AS uuids",
            {"key": key, "limit": limit},
        )
    uuids = [u for u in (rows[0]["uuids"] if rows else []) if u]
    sub = induced_subgraph(g, uuids, asof)
    sub["anchor_key"] = key
    return sub


def pct(num, den):
    return round(100.0 * num / den, 1) if den else 0.0


def check_status(c: dict) -> str:
    """good / warning / critical from value against good and warn. A check whose value is
    None has nothing to measure yet and is 'not_measured': not good, not bad, and not
    counted either way by anything that rolls statuses up."""
    if c["value"] is None:
        return "not_measured"
    return "good" if c["value"] >= c["good"] else ("warning" if c["value"] >= c["warn"] else "critical")


def evidence_checks(g: Graph, p: dict) -> list:
    """The two stale-facts checks (2026-10-15). They read the (:Evidence) layer, which may not
    exist in a given database yet, so with no Evidence node in scope both report value None
    (not measured) instead of a false critical. `r.trust_state` is absent on a fact with no
    evidence link: absent is never counted as stale, it is simply outside the denominator."""
    unmeasured = "no evidence links recorded yet - measured once the first anchored write lands"
    n_evidence = g.one(
        "MATCH (v:Evidence) WHERE ($group IS NULL OR v.group_id = $group) RETURN count(v) AS c", p
    ).get("c", 0) or 0
    fresh = {"id": "evidence_fresh", "label": "Anchored facts whose evidence still matches",
             "value": None, "unit": "%", "good": 95, "warn": 80, "detail": unmeasured}
    cover = {"id": "anchor_coverage", "label": "New observed writes carrying evidence",
             "value": None, "unit": "%", "good": 50, "warn": 20, "detail": unmeasured}
    if n_evidence == 0:
        return [fresh, cover]

    # Same liveness as the Explore view (LIVE_PREDICATE), evaluated now.
    asof = datetime.now(timezone.utc).isoformat()
    row = g.one(
        "MATCH ()-[r:RELATES_TO]->() WHERE ($group IS NULL OR r.group_id = $group) "
        f"AND {LIVE_PREDICATE} AND r.trust_state IN ['current', 'stale', 'unverified'] "
        "RETURN sum(CASE WHEN r.trust_state = 'current' THEN 1 ELSE 0 END) AS current, "
        "sum(CASE WHEN r.trust_state = 'stale' THEN 1 ELSE 0 END) AS stale, "
        "sum(CASE WHEN r.trust_state = 'unverified' THEN 1 ELSE 0 END) AS unverified",
        {**p, "asof": asof},
    )
    current, stale = row.get("current", 0) or 0, row.get("stale", 0) or 0
    # 'unverified' = evidence exists but nothing confirmed it yet (never checked, repo
    # unreachable, or checkable only on the server). Outside the % either way: it is not
    # evidence of staleness, and not of freshness.
    unverified = row.get("unverified", 0) or 0
    tail = f"; {unverified:,} unverified (not yet checked or source unreachable)" if unverified else ""
    if current + stale == 0:
        fresh["detail"] = "evidence is recorded but no live fact has been checked against it yet" + tail
    else:
        fresh["value"] = pct(current, current + stale)
        if stale:
            reasons = g.read(
                "MATCH ()-[r:RELATES_TO]->() WHERE ($group IS NULL OR r.group_id = $group) "
                f"AND {LIVE_PREDICATE} AND r.trust_state = 'stale' "
                "UNWIND coalesce(r.stale_reasons, []) AS reason "
                "WITH reason, max(r.stale_since) AS since RETURN reason ORDER BY since DESC LIMIT 3",
                {**p, "asof": asof},
            )
            shown = "; ".join(str(r["reason"]) for r in reasons)
            fresh["detail"] = f"{stale:,} stale" + (f": {shown}" if shown else "") + \
                              f" ({current:,} current)" + tail
        else:
            fresh["detail"] = f"{current:,} anchored facts, none stale" + tail

    obs = g.one(
        "MATCH (e:Episodic) WHERE ($group IS NULL OR e.group_id = $group) "
        "AND e.created_at >= datetime() - duration({days: 14}) "
        f"AND {TIER_EXPR} = 'observed' "
        "RETURN count(e) AS total, "
        "sum(CASE WHEN EXISTS { (e)-[:CITES]->(:Evidence) } THEN 1 ELSE 0 END) AS cited", p
    )
    total, cited = obs.get("total", 0) or 0, obs.get("cited", 0) or 0
    if total == 0:
        cover["detail"] = "no observed writes in the last 14 days"
    else:
        cover["value"] = pct(cited, total)
        cover["detail"] = f"{cited} of {total} observed episodes in the last 14 days cite evidence"
    return [fresh, cover]


def api_health(g: Graph, q) -> dict:
    """
    Audit metrics for the graph, scoped to one group_id or the whole store.
    Stable JSON shape - safe to consume from a scheduled drift check:
        curl -s -H "X-Lens-Token: $TOK" 'http://127.0.0.1:7878/api/health'
    """
    group = q.get("group", [None])[0] or None
    p = {"group": group}

    edges = g.one(
        "MATCH ()-[r:RELATES_TO]->() WHERE ($group IS NULL OR r.group_id = $group) "
        "RETURN count(r) AS total, count(r.valid_at) AS with_valid_at, "
        "count(r.invalid_at) AS invalidated, count(r.expired_at) AS expired, "
        "sum(CASE WHEN r.invalid_at IS NOT NULL AND r.expired_at IS NULL THEN 1 ELSE 0 END) AS invalid_only, "
        "sum(CASE WHEN r.expired_at IS NOT NULL AND r.invalid_at IS NULL THEN 1 ELSE 0 END) AS expired_only, "
        "sum(CASE WHEN r.expired_at IS NOT NULL AND r.invalid_at IS NOT NULL THEN 1 ELSE 0 END) AS both_stamped, "
        "sum(CASE WHEN r.valid_at IS NOT NULL AND r.invalid_at IS NOT NULL AND r.invalid_at < r.valid_at THEN 1 ELSE 0 END) AS incoherent, "
        "sum(CASE WHEN r.fact IS NULL OR trim(r.fact) = '' THEN 1 ELSE 0 END) AS missing_fact, "
        "sum(CASE WHEN size(coalesce(r.episodes, [])) = 0 THEN 1 ELSE 0 END) AS unsourced_edges",
        p,
    )
    episodes = g.one(
        "MATCH (e:Episodic) WHERE ($group IS NULL OR e.group_id = $group) "
        "RETURN count(e) AS total, "
        f"sum(CASE WHEN {TIER_EXPR} = 'observed' THEN 1 ELSE 0 END) AS observed, "
        f"sum(CASE WHEN {TIER_EXPR} = 'derived' THEN 1 ELSE 0 END) AS derived, "
        f"sum(CASE WHEN {TIER_EXPR} = 'verified' THEN 1 ELSE 0 END) AS verified, "
        f"sum(CASE WHEN {TIER_EXPR} = 'decided' THEN 1 ELSE 0 END) AS decided, "
        f"sum(CASE WHEN {TIER_EXPR} IS NOT NULL THEN 1 ELSE 0 END) AS tagged, "
        "sum(CASE WHEN e.source_description IS NULL OR trim(e.source_description) = '' THEN 1 ELSE 0 END) AS no_source_desc, "
        "max(e.created_at) AS last_ingest",
        p,
    )
    ents = g.one(
        "MATCH (n:Entity) WHERE ($group IS NULL OR n.group_id = $group) "
        "RETURN count(n) AS total, "
        "sum(CASE WHEN n.summary IS NULL OR trim(n.summary) = '' THEN 1 ELSE 0 END) AS no_summary",
        p,
    )
    orphans = g.one(
        "MATCH (n:Entity) WHERE ($group IS NULL OR n.group_id = $group) "
        "AND NOT (n)-[:RELATES_TO]-() RETURN count(n) AS c", p
    ).get("c", 0)
    unmentioned = g.one(
        "MATCH (n:Entity) WHERE ($group IS NULL OR n.group_id = $group) "
        "AND NOT (:Episodic)-[:MENTIONS]->(n) RETURN count(n) AS c", p
    ).get("c", 0)
    barren = g.one(
        "MATCH (e:Episodic) WHERE ($group IS NULL OR e.group_id = $group) "
        "AND NOT (e)-[:MENTIONS]->() RETURN count(e) AS c", p
    ).get("c", 0)
    dupes = g.one(
        "MATCH (n:Entity) WHERE ($group IS NULL OR n.group_id = $group) AND n.name IS NOT NULL "
        "WITH n.group_id AS g, toLower(trim(n.name)) AS k, count(*) AS c WHERE c > 1 "
        "RETURN count(*) AS clusters, sum(c - 1) AS redundant", p
    )
    dupe_rows = g.read(
        "MATCH (n:Entity) WHERE ($group IS NULL OR n.group_id = $group) AND n.name IS NOT NULL "
        "WITH n.group_id AS g, toLower(trim(n.name)) AS k, count(*) AS c, collect(n.name)[0] AS display "
        "WHERE c > 1 RETURN g AS group, display, c AS copies ORDER BY c DESC LIMIT 40", p
    )
    daily = g.read(
        "MATCH (e:Episodic) WHERE ($group IS NULL OR e.group_id = $group) "
        "AND e.created_at >= datetime() - duration({days: 60}) "
        "RETURN date(e.created_at) AS day, count(*) AS c ORDER BY day", p
    )
    # Renamed 2026-10-15 (was `stale`): "stale" now means one thing only, a fact whose
    # evidence no longer matches. A group that has simply stopped writing is quiet.
    quiet_groups = g.read(
        "MATCH (e:Episodic) WITH e.group_id AS g, max(e.created_at) AS last, count(*) AS episodes "
        "RETURN g AS group, last, episodes ORDER BY last ASC LIMIT 40"
    )

    # --- operability probes (added 2026-08-29) ------------------------------
    # These answer "is the memory system still actually working", which the
    # graph-shape checks above cannot see. Two have a remediation path and are
    # self-healing in the real sense; the third is a detector only, and says so.
    recent7 = g.one(
        "MATCH (e:Episodic) WHERE ($group IS NULL OR e.group_id = $group) "
        "AND e.created_at >= datetime() - duration({days: 7}) RETURN count(e) AS c", p
    ).get("c", 0) or 0
    emb_n = g.one(
        "MATCH (n:Entity) WHERE ($group IS NULL OR n.group_id = $group) "
        "RETURN count(n) AS total, "
        "sum(CASE WHEN n.name_embedding IS NULL THEN 1 ELSE 0 END) AS missing", p
    )
    emb_e = g.one(
        "MATCH ()-[r:RELATES_TO]->() WHERE ($group IS NULL OR r.group_id = $group) "
        "RETURN count(r) AS total, "
        "sum(CASE WHEN r.fact_embedding IS NULL THEN 1 ELSE 0 END) AS missing", p
    )
    emb_total = (emb_n.get("total", 0) or 0) + (emb_e.get("total", 0) or 0)
    emb_missing = (emb_n.get("missing", 0) or 0) + (emb_e.get("missing", 0) or 0)
    # Filesystem, not Neo4j. The MCP server spools an episode before ingesting and
    # clears it only on a confirmed write, so anything left behind is a replayable
    # loss. Only meaningful when Lens runs on the MCP server's host, which it does.
    spool_dir = Path(
        os.environ.get("GRAPHITI_SPOOL_DIR", Path.home() / ".local" / "state" / "graphiti-spool")
    )
    try:
        spooled = len(list(spool_dir.glob("*.json"))) if spool_dir.is_dir() else 0
    except OSError:
        spooled = 0
    expected7 = (sum((r["c"] or 0) for r in daily) / 60.0) * 7 if daily else 0.0
    ingest_ratio = 100.0 if expected7 <= 0 else min(100.0, round(recent7 / expected7 * 100, 1))

    et, ep_t = edges.get("total", 0) or 0, episodes.get("total", 0) or 0
    en_t = ents.get("total", 0) or 0
    tagged = episodes.get("tagged", 0) or 0

    checks = [
        {"id": "provenance_tag", "label": "Episodes carrying a provenance tier",
         "value": pct(tagged, ep_t), "unit": "%", "good": 95, "warn": 70,
         "detail": f"{tagged} of {ep_t} episodes"},
        {"id": "source_desc", "label": "Episodes with a source_description",
         "value": pct(ep_t - (episodes.get("no_source_desc", 0) or 0), ep_t), "unit": "%",
         "good": 99, "warn": 90,
         "detail": f"{episodes.get('no_source_desc', 0)} missing"},
        {"id": "valid_at", "label": "Edges carrying valid_at",
         "value": pct(edges.get("with_valid_at", 0) or 0, et), "unit": "%", "good": 90, "warn": 60,
         "detail": f"{et - (edges.get('with_valid_at', 0) or 0)} edges have no start time"},
        {"id": "edge_sourced", "label": "Edges linked to a source episode",
         "value": pct(et - (edges.get("unsourced_edges", 0) or 0), et), "unit": "%",
         "good": 99, "warn": 95,
         "detail": f"{edges.get('unsourced_edges', 0)} unsourced"},
        {"id": "entity_connected", "label": "Entities with at least one fact",
         "value": pct(en_t - orphans, en_t), "unit": "%", "good": 90, "warn": 75,
         "detail": f"{orphans} orphan entities"},
        # CORRECTION 2026-08-28. The previous check here rewarded edges that carried
        # only ONE of invalid_at / expired_at, on the theory that stamping both was a
        # conflation. That was wrong. They are different axes: invalid_at is valid time
        # (when the fact stopped being true in the world) and expired_at is transaction
        # time (when the record stopped being current). A superseded fact SHOULD carry
        # both. Measured on this graph: 19,161 edges carry both and ZERO hold the same
        # value - mean separation 15 days, max 38 years. The model is correct; the old
        # check reported a permanent false critical.
        {"id": "supersession_complete", "label": "Superseded facts recording both valid- and transaction-time",
         "value": pct(edges.get("both_stamped", 0) or 0, max(1, edges.get("invalidated", 0) or 0)),
         "unit": "%", "good": 95, "warn": 80,
         "detail": f"{edges.get('both_stamped', 0):,} of {edges.get('invalidated', 0):,} superseded edges "
                   f"carry invalid_at (world time) and expired_at (record time)"},
        {"id": "temporal_coherence", "label": "Facts whose end date follows their start date",
         "value": pct(et - (edges.get("incoherent", 0) or 0), et), "unit": "%", "good": 100, "warn": 99,
         "detail": f"{edges.get('incoherent', 0)} edges claim invalid_at earlier than valid_at, "
                   f"which is impossible"},
        {"id": "dedup", "label": "Entities free of same-name duplicates",
         "value": pct(en_t - (dupes.get("redundant", 0) or 0), en_t), "unit": "%",
         "good": 99, "warn": 95,
         "detail": f"{dupes.get('redundant', 0)} redundant across {dupes.get('clusters', 0)} name clusters"},
    ]
    checks += [
        # SELF-HEALING: a non-empty spool is a recoverable loss with a known fix.
        {"id": "spool_drained", "label": "Episode write-ahead spool drained",
         "value": 100.0 if spooled == 0 else 0.0, "unit": "%", "good": 100, "warn": 100,
         "detail": "spool empty" if spooled == 0 else
                   f"{spooled} unreplayed episode(s) in {spool_dir} - "
                   f"recover with mcp_server/scripts/replay_spool.py"},
        # SELF-HEALING: a missing embedding is invisible to semantic search; re-embed.
        {"id": "embedding_coverage", "label": "Entities and facts carrying an embedding",
         "value": pct(emb_total - emb_missing, emb_total), "unit": "%", "good": 100, "warn": 99,
         "detail": f"{emb_missing:,} of {emb_total:,} carry no embedding and cannot be "
                   f"reached by semantic search"},
        # DETECTOR ONLY - no automatic remediation, because writing to memory is a
        # behaviour, not a service. On 2026-08-28 this graph took 1 episode against a
        # ~18/day baseline and nothing surfaced it: the sparkline carries no threshold.
        {"id": "ingest_live", "label": "Last 7 days' ingest vs the 60-day rate",
         "value": ingest_ratio, "unit": "%", "good": 50, "warn": 25,
         "detail": f"{recent7} episodes in 7 days against an expected ~{expected7:.0f}"},
    ]

    checks += evidence_checks(g, p)
    for c in checks:
        c["status"] = check_status(c)
    quiet_groups_json = [{k: jsonable(v) for k, v in r.items()} for r in quiet_groups]

    return {
        "group": group,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": {"entities": en_t, "episodes": ep_t, "edges": et},
        "bitemporal": {
            "total": et,
            "with_valid_at": edges.get("with_valid_at", 0),
            "invalidated": edges.get("invalidated", 0),
            "expired": edges.get("expired", 0),
            "invalid_only": edges.get("invalid_only", 0),
            "expired_only": edges.get("expired_only", 0),
            "both_stamped": edges.get("both_stamped", 0),
            "incoherent": edges.get("incoherent", 0),
            "live": et - (edges.get("invalidated", 0) or 0) - (edges.get("expired_only", 0) or 0),
            "missing_fact": edges.get("missing_fact", 0),
        },
        "provenance": {
            "observed": episodes.get("observed", 0),
            "derived": episodes.get("derived", 0),
            "verified": episodes.get("verified", 0),
            "decided": episodes.get("decided", 0),
            "untagged": ep_t - tagged,
            "no_source_description": episodes.get("no_source_desc", 0),
            "last_ingest": jsonable(episodes.get("last_ingest")),
        },
        "hygiene": {
            "orphan_entities": orphans,
            "unmentioned_entities": unmentioned,
            "episodes_with_no_entities": barren,
            "entities_without_summary": ents.get("no_summary", 0),
            "duplicate_clusters": dupes.get("clusters", 0),
            "redundant_entities": dupes.get("redundant", 0),
        },
        "operability": {
            "spooled_episodes": spooled,
            "spool_dir": str(spool_dir),
            "missing_embeddings": emb_missing,
            "episodes_last_7d": recent7,
            "expected_7d": round(expected7, 1),
        },
        "checks": checks,
        "duplicates": [{k: jsonable(v) for k, v in r.items()} for r in dupe_rows],
        "ingest_daily": [{"day": jsonable(r["day"]), "count": r["c"]} for r in daily],
        "quiet_groups": quiet_groups_json,
        # Deprecated alias of quiet_groups, kept for one release: README documents this key,
        # so the drift check / truth.py may read it from lens-health.json. Remove after 2026-11.
        "group_staleness": quiet_groups_json,
    }


def api_cypher(g: Graph, body: dict) -> dict:
    query = (body.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    guard_cypher(query)
    limit = clamp_int(str(body.get("limit", 500)), 1, MAX_ROWS, 500)
    rows = g.read(query, {}, timeout=QUERY_TIMEOUT_S)
    truncated = len(rows) > limit
    rows = rows[:limit]
    columns = list(rows[0].keys()) if rows else []

    # If the result carries graph elements, assemble a renderable subgraph too.
    nodes, edges, seen_n, seen_e = [], [], set(), set()
    for row in rows:
        for v in row.values():
            items = v if isinstance(v, (list, tuple)) else [v]
            for it in items:
                if hasattr(it, "labels") and hasattr(it, "items"):
                    n = pack_node(it)
                    if n["id"] and n["id"] not in seen_n:
                        seen_n.add(n["id"]); nodes.append(n)
                elif hasattr(it, "type") and hasattr(it, "items"):
                    e = pack_edge(it, None)
                    if e["id"] and e["id"] not in seen_e:
                        seen_e.add(e["id"]); edges.append(e)
    edges = [e for e in edges if e["source"] in seen_n and e["target"] in seen_n]
    deg = {}
    for e in edges:
        deg[e["source"]] = deg.get(e["source"], 0) + 1
        deg[e["target"]] = deg.get(e["target"], 0) + 1
    for n in nodes:
        n["degree"] = deg.get(n["id"], 0)

    return {
        "columns": columns,
        "rows": [{k: jsonable(v) for k, v in r.items()} for r in rows],
        "row_count": len(rows),
        "truncated": truncated,
        "graph": {"nodes": nodes, "edges": edges} if nodes else None,
    }


def clamp_int(raw, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(str(raw).strip())))
    except (TypeError, ValueError):
        return default



# --- one-time nonces -------------------------------------------------------- #
# The launcher must hand the browser something to authenticate with, and anything
# in a URL survives in browser history and in this server's own log. So it hands
# over a nonce instead: single-use, seconds-scale, and dead by the time anyone
# reads it back. The long-lived token never leaves its 0600 file.

NONCE_TTL = 30
TOKEN_FILE = HERE / ".lens-token"


def write_token_file(path, token: str) -> None:
    """The token's single home. Readable only by this user; gone in --no-token mode,
    so a stale file can never authenticate against a server that has moved on."""
    path = pathlib.Path(path) if not hasattr(path, "write_text") else path
    if not token:
        path.unlink(missing_ok=True)
        return
    path.write_text(token)
    path.chmod(0o600)


# D-mem-010: the health audit also lives as a file on disk, so every reader (truth.py,
# get_status on the UpCloud host once the file is pushed there, the Monday drift check)
# needs neither the token nor a live Lens. 15 minutes matches the audit's own cadence.
HEALTH_FILE = pathlib.Path(
    os.environ.get("LENS_HEALTH_FILE")
    or (pathlib.Path.home() / ".local" / "state" / "graphiti" / "lens-health.json")
)
HEALTH_FILE_INTERVAL_S = int(os.environ.get("LENS_HEALTH_INTERVAL_S", "900"))


def write_health_file(path, payload: dict) -> pathlib.Path:
    """Write the audit JSON atomically (tmp + rename): a reader never sees half a file,
    and a crash mid-write leaves the previous good copy in place."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(jsonable(payload), default=str, separators=(",", ":")))
    os.replace(tmp, path)
    return path


def health_file_loop(graph, path, interval_s: float, stop: threading.Event, log=print) -> None:
    """Audit, write, sleep, repeat until `stop` is set. A failed audit (Neo4j down, tunnel
    asleep) is logged once per tick and retried next tick; the last good file stays."""
    while not stop.is_set():
        try:
            write_health_file(path, api_health(graph, {}))
        except Exception as exc:  # noqa: BLE001 - keep the loop alive whatever the cause
            log(f"  health-file: {type(exc).__name__}: {exc}")
        stop.wait(interval_s)


def issue_nonce(store: dict, now: float, ttl: int = NONCE_TTL) -> str:
    for value, expiry in list(store.items()):
        if expiry <= now:
            del store[value]
    nonce = secrets.token_urlsafe(18)
    store[nonce] = now + ttl
    return nonce


def redeem_nonce(store: dict, candidate: str, now: float) -> bool:
    """True once per nonce, and never after it expires."""
    if not candidate:
        return False
    expiry = store.pop(candidate, None)
    return expiry is not None and expiry > now


# --- the text surface ------------------------------------------------------- #

STATE_WORDS = {"live": "live", "invalidated": "superseded", "expired": "retracted"}


def flat(value, width: int) -> str:
    """One field, one row. Summaries and facts in this graph contain newlines."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= width else text[: width - 1] + "\u2026"


def format_explain(p: dict) -> str:
    """Plain text, because every other view of this graph is pixels.

    Readable by a screen reader, by an agent with no vision, and in a terminal;
    diffable, so it can feed a check instead of only being looked at."""
    name = p.get("name", "?")
    instances = p.get("instances") or []
    if not instances:
        return f"{name} - nothing in the graph carries this name." + chr(10)

    groups = p.get("groups") or []
    out = [f"{name} - {len(instances)} node(s) across {len(groups)} groups ({', '.join(groups)})", ""]
    for i in instances:
        noun = "fact " if i["facts"] == 1 else "facts"
        line = f"  {flat(i['group'], 26):<26} {i['uuid'][:8]}  {i['facts']:>4} {noun}"
        if i.get("summary"):
            line += f"  {flat(i['summary'], 70)}"
        out.append(line.rstrip())

    facts = p.get("top_facts") or []
    if facts:
        out += ["", "Strongest facts"]
        for f in facts:
            word = STATE_WORDS.get(f.get("state", "live"), f.get("state", "live"))
            when = (f.get("valid_at") or "")[:10]
            out.append(
                f"  {word:<11} {flat(f['fact'], 64):<64} {flat(f.get('group'), 20):<20} {when}".rstrip()
            )

    days = p.get("window_days", 7)
    out += ["", f"Last {days} days"]
    changed = p.get("superseded_recently", 0)
    out.append(f"  {changed} fact(s) stopped being true." if changed else "  No fact was superseded.")
    for e in p.get("recent_episodes") or []:
        out.append(
            f"  {str(e.get('created_at') or '')[:10]}  {flat(e.get('group'), 20):<20} {flat(e.get('name'), 60)}".rstrip()
        )
    if not (p.get("recent_episodes") or []):
        out.append("  Nothing was written about it.")
    return chr(10).join(out) + "\n"


def api_explain(g: Graph, q):
    name = (q.get("entity", [""])[0] or q.get("name", [""])[0]).strip()
    if not name:
        raise ValueError("entity is required")
    days = clamp_int(q.get("days", ["7"])[0], 1, 365, 7)

    rows = g.read(
        "MATCH (n:Entity) WHERE toLower(n.name) = toLower($name) "
        "OPTIONAL MATCH (n)-[r:RELATES_TO]-() "
        "WITH n, count(r) AS facts "
        "RETURN n.uuid AS uuid, n.group_id AS group, coalesce(n.summary,'') AS summary, facts "
        "ORDER BY facts DESC LIMIT 60",
        {"name": name},
    )
    instances = [{k: jsonable(v) for k, v in r.items()} for r in rows]
    uuids = [i["uuid"] for i in instances]
    payload = {
        "name": name,
        "instances": instances,
        "groups": sorted({i["group"] for i in instances if i.get("group")}),
        "top_facts": [],
        "recent_episodes": [],
        "superseded_recently": 0,
        "window_days": days,
    }
    if not uuids:
        return payload

    facts = g.read(
        "MATCH (a:Entity)-[r:RELATES_TO]-(b:Entity) WHERE a.uuid IN $uuids "
        "RETURN r.fact AS fact, r.group_id AS group, r.valid_at AS valid_at, "
        "r.invalid_at AS invalid_at, r.expired_at AS expired_at, r.created_at AS created_at "
        "ORDER BY coalesce(r.valid_at, r.created_at) DESC LIMIT 25",
        {"uuids": uuids},
    )
    for r in facts:
        state = "invalidated" if r["invalid_at"] else ("expired" if r["expired_at"] else "live")
        payload["top_facts"].append({
            "fact": r["fact"] or "",
            "group": jsonable(r["group"]) or "",
            "valid_at": str(jsonable(r["valid_at"]) or jsonable(r["created_at"]) or ""),
            "state": state,
        })

    since = f"datetime() - duration('P{days}D')"
    payload["superseded_recently"] = (g.one(
        "MATCH (a:Entity)-[r:RELATES_TO]-() WHERE a.uuid IN $uuids "
        f"AND r.invalid_at IS NOT NULL AND r.invalid_at > {since} "
        "RETURN count(r) AS n", {"uuids": uuids}) or {}).get("n", 0)

    eps = g.read(
        "MATCH (e:Episodic)-[:MENTIONS]->(n:Entity) WHERE n.uuid IN $uuids "
        f"AND e.created_at > {since} "
        "RETURN DISTINCT e.name AS name, e.group_id AS group, e.created_at AS created_at "
        "ORDER BY created_at DESC LIMIT 10",
        {"uuids": uuids},
    )
    payload["recent_episodes"] = [{k: jsonable(v) for k, v in r.items()} for r in eps]
    return payload



EXPIRED_HTML = """<!doctype html><meta charset="utf-8"><title>Graphiti Lens - link expired</title>
<style>
 body{background:#101826;color:#e8eef5;font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}
 main{max-width:34rem;padding:2rem}
 h1{font-size:1.35rem;font-weight:500;margin:0 0 1rem}
 p{color:#aab6c6;margin:0 0 1rem}
 b{color:#e8eef5;font-weight:500}
 .k{background:#18222f;border:1px solid #2a3648;border-radius:6px;padding:.15rem .45rem;color:#96cddc}
</style>
<main>
<h1>This link has expired.</h1>
<p>Lens links are <b>single-use</b> and last about 30 seconds, so a copied, bookmarked or
   reopened URL stops working. That is deliberate: a URL ends up in browser history and in
   the server's own log, and a credential sitting there outlives the session it was for.</p>
<p>Open <b>Graphiti Lens</b> instead - on your <span class="k">Desktop</span>, or in
   <span class="k">/Applications</span>. It mints a fresh link every time, so there is
   nothing to remember and nothing to keep.</p>
<p>Keep the app, not the address.</p>
</main>"""


def expired_page() -> str:
    return EXPIRED_HTML


ROUTES = {
    "/api/meta": api_meta,
    "/api/search": api_search,
    "/api/ego": api_ego,
    "/api/group": api_group,
    "/api/node": api_node,
    "/api/episodes": api_episodes,
    "/api/episode": api_episode,
    "/api/bridges": api_bridges,
    "/api/bridge_graph": api_bridge_graph,
    "/api/health": api_health,
}

# Answers in prose rather than JSON. Same queries, different surface.
TEXT_ROUTES = {"/api/explain": api_explain}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "GraphitiLens"
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------- #
    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, code: int, payload: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        cookie = getattr(self, "_cookie", None)
        if cookie:
            # HttpOnly so page scripts cannot read it back out; SameSite=Strict so a
            # page on another origin cannot ride it against this loopback service.
            self.send_header("Set-Cookie", f"lens_session={cookie}; Path=/; HttpOnly; SameSite=Strict")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(jsonable(obj), default=str).encode(), "application/json")

    def _session_ok(self) -> bool:
        for part in (self.headers.get("Cookie") or "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == "lens_session" and value and value in self.server.sessions:
                return True
        return False

    def _authorised(self, query) -> bool:
        token = self.server.token
        if not token:
            return True
        # Cookie first: once the browser holds a session, the dead nonce still sitting
        # in its URL bar (and in app.js) must not turn every later request into a 401.
        if self._session_ok():
            return True
        supplied = str(self.headers.get("X-Lens-Token") or (query.get("t", [None])[0]) or "")
        if supplied and secrets.compare_digest(supplied, token):
            return True
        return redeem_nonce(self.server.nonces, supplied, time.time())

    def _start_session(self):
        """Give the page a cookie so its assets and API calls carry no credential."""
        if not self.server.token or self._session_ok():
            return
        sessions = self.server.sessions
        if len(sessions) > 64:
            sessions.clear()
        sid = secrets.token_urlsafe(18)
        sessions.add(sid)
        self._cookie = sid

    # -- routing ------------------------------------------------------------ #
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path

        if not self._authorised(query):
            # A browser asking for the page gets an explanation; everything else gets JSON.
            wants_html = "text/html" in (self.headers.get("Accept") or "")
            if wants_html and path in ("/", "/index.html"):
                return self._send(401, expired_page().encode(), "text/html; charset=utf-8")
            return self._json(401, {"error": "bad or missing token"})

        if path in TEXT_ROUTES:
            try:
                payload = TEXT_ROUTES[path](self.server.graph, query)
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            except Neo4jError as exc:
                return self._json(400, {"error": f"Neo4j: {exc.message}", "code": exc.code})
            if (query.get("format", [""])[0] or "").lower() == "json":
                return self._json(200, payload)
            return self._send(200, format_explain(payload).encode(), "text/plain; charset=utf-8")

        if path.startswith("/api/"):
            fn = ROUTES.get(path)
            if not fn:
                return self._json(404, {"error": "no such endpoint"})
            try:
                return self._json(200, fn(self.server.graph, query))
            except KeyError as exc:
                return self._json(404, {"error": str(exc)})
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            except Neo4jError as exc:
                return self._json(400, {"error": f"Neo4j: {exc.message}", "code": exc.code})
            except Exception as exc:  # pragma: no cover
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        if path in ("/", "/index.html"):
            self._start_session()
        return self._serve_static(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorised(query):
            return self._json(401, {"error": "bad or missing token"})
        if parsed.path == "/api/session":
            # Only the real token may mint a nonce, and only via the header - never a URL.
            token = self.server.token
            supplied = str(self.headers.get("X-Lens-Token") or "")
            if token and not (supplied and secrets.compare_digest(supplied, token)):
                return self._json(401, {"error": "the header X-Lens-Token must carry the real token"})
            return self._json(200, {"nonce": issue_nonce(self.server.nonces, time.time()),
                                    "expires_in": NONCE_TTL})
        if parsed.path != "/api/cypher":
            return self._json(404, {"error": "no such endpoint"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 200_000:
                return self._json(413, {"error": "query too large"})
            body = json.loads(self.rfile.read(length) or b"{}")
            return self._json(200, api_cypher(self.server.graph, body))
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except Neo4jError as exc:
            return self._json(400, {"error": f"Neo4j: {exc.message}", "code": exc.code})
        except Exception as exc:  # pragma: no cover
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _serve_static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC / rel).resolve()
        try:
            target.relative_to(STATIC.resolve())
        except ValueError:
            return self._json(403, {"error": "forbidden"})
        if not target.is_file():
            return self._json(404, {"error": "not found"})
        ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)


def main():
    ap = argparse.ArgumentParser(description="Graphiti Lens - read-only graph explorer")
    ap.add_argument("--port", type=int, default=7878)
    ap.add_argument("--host", default="127.0.0.1", help="loopback by default; change at your own risk")
    ap.add_argument("--no-token", action="store_true", help="disable the bearer token (loopback only)")
    ap.add_argument("--no-open", action="store_true", help="do not open a browser")
    ap.add_argument("--no-health-file", action="store_true",
                    help=f"do not write the audit to {HEALTH_FILE} every {HEALTH_FILE_INTERVAL_S}s")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    uri, user, pwd = resolve_credentials()
    graph = Graph(uri, user, pwd)

    # LENS_TOKEN pins the bearer token across restarts (needed when run as a
    # LaunchAgent, otherwise the URL changes every boot). Random when unset.
    token = "" if args.no_token else (os.environ.get("LENS_TOKEN") or secrets.token_urlsafe(18))
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.graph = graph
    httpd.token = token
    httpd.verbose = args.verbose
    httpd.nonces = {}
    httpd.sessions = set()
    # Written here so the file is the one place the token lives: no copy in the
    # LaunchAgent plist, none in this log. A restart rotates it, and the app reads
    # the file every time, so nothing needs to know the old value.
    write_token_file(TOKEN_FILE, token)

    # The printed URL carries a short-lived nonce, not the token: stdout is the
    # LaunchAgent's log file, and a token printed there outlives every session.
    opening = issue_nonce(httpd.nonces, time.time(), ttl=300) if token else ""
    url = f"http://{args.host}:{args.port}/" + (f"?t={opening}" if token else "")
    print("\n  Graphiti Lens")
    print(f"  neo4j    {uri} as {user}")
    print(f"  serving  http://{args.host}:{args.port}/")
    if token:
        print("  opening  one-time link, valid 5 minutes (the token stays in .lens-token)")
    print("  mode     READ ONLY - write clauses are refused")
    if not args.no_health_file:
        print(f"  health   {HEALTH_FILE} every {HEALTH_FILE_INTERVAL_S}s")
    print("  ctrl-c to stop\n")
    sys.stdout.flush()

    stop = threading.Event()
    if not args.no_health_file:
        threading.Thread(
            target=health_file_loop,
            args=(graph, HEALTH_FILE, HEALTH_FILE_INTERVAL_S, stop),
            name="health-file",
            daemon=True,
        ).start()
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopping")
    finally:
        stop.set()
        httpd.server_close()
        graph.close()


if __name__ == "__main__":
    main()
