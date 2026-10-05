"""Community detection and centrality for the interaction graph.

label_propagation() finds communities: nodes start with their own label and
each pass adopts the highest-weight neighbor label (ties go to the smallest
label). Nodes are visited in sorted order so runs are reproducible.

betweenness_centrality() is Brandes' BFS algorithm, normalized for
undirected graphs — the "who connects separate clusters" score.

Both take plain node ids and (a, b, weight) edge tuples.
"""
from collections import defaultdict, deque


def label_propagation(node_ids, edges, max_iters=20):
    """Deterministic weighted label propagation.

    node_ids: iterable of hashable ids.
    edges: iterable of (a, b, weight) — weight >= 0 (use 1.0 for unweighted).
    Returns {node: community_label}; singletons keep their own label.
    """
    nodes = sorted(node_ids, key=str)
    if not nodes:
        return {}
    neighbors = defaultdict(list)
    for a, b, w in edges:
        if a == b:
            continue
        neighbors[a].append((b, float(w or 0)))
        neighbors[b].append((a, float(w or 0)))

    labels = {n: n for n in nodes}
    for _ in range(max_iters):
        changed = False
        for node in nodes:
            if not neighbors[node]:
                continue
            tally = defaultdict(float)
            for nb, w in neighbors[node]:
                tally[labels[nb]] += w
            # deterministic tie-break: highest weight, then smallest label
            top_weight = max(tally.values())
            chosen = min(
                (lbl for lbl, wt in tally.items() if wt == top_weight), key=str
            )
            if chosen != labels[node]:
                labels[node] = chosen
                changed = True
        if not changed:
            break
    return labels


def betweenness_centrality(node_ids, edges):
    """Brandes (2001) unweighted betweenness, normalized to 0..1 for
    undirected graphs. Returns {node: score}."""
    nodes = set(node_ids)
    adj = defaultdict(set)
    for a, b, _w in edges:
        if a == b or a not in nodes or b not in nodes:
            continue
        adj[a].add(b)
        adj[b].add(a)

    bc = {n: 0.0 for n in nodes}
    for source in nodes:
        # single-source shortest paths (BFS)
        stack = []
        preds = {n: [] for n in nodes}
        sigma = defaultdict(float)
        sigma[source] = 1.0
        dist = {source: 0}
        queue = deque([source])
        while queue:
            v = queue.popleft()
            stack.append(v)
            for w in adj[v]:
                if w not in dist:
                    dist[w] = dist[v] + 1
                    queue.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    preds[w].append(v)
        # dependency accumulation
        delta = defaultdict(float)
        while stack:
            w = stack.pop()
            for v in preds[w]:
                delta[v] += (sigma[v] / sigma[w]) * (1 + delta[w]) if sigma[w] else 0
            if w != source:
                bc[w] += delta[w]

    n = len(nodes)
    if n > 2:
        # raw accumulation counts each unordered pair twice (from both
        # endpoints); dividing by (n-1)(n-2) normalizes to 0..1 undirected
        scale = 1.0 / ((n - 1) * (n - 2))
        bc = {k: round(v * scale, 4) for k, v in bc.items()}
    else:
        bc = {k: 0.0 for k in bc}
    return bc
