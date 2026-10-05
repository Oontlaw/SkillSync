"""Graph algorithms: deterministic community detection (label propagation)
and Brandes betweenness — pure functions, no DB."""
from graph_algorithms import betweenness_centrality, label_propagation


def test_label_propagation_finds_two_clusters():
    # two triangles joined by one weak bridge
    nodes = list("ABCDEF")
    edges = [
        ("A", "B", 3), ("B", "C", 3), ("A", "C", 3),
        ("D", "E", 3), ("E", "F", 3), ("D", "F", 3),
        ("C", "D", 1),  # the bridge
    ]
    labels = label_propagation(nodes, edges)
    assert labels["A"] == labels["B"] == labels["C"]
    assert labels["D"] == labels["E"] == labels["F"]
    assert labels["A"] != labels["D"]


def test_label_propagation_is_deterministic():
    nodes = list("ABCDE")
    edges = [("A", "B", 1), ("B", "C", 1), ("C", "D", 1), ("D", "E", 1)]
    assert label_propagation(nodes, edges) == label_propagation(nodes, edges)


def test_label_propagation_handles_singletons_and_empty():
    assert label_propagation([], []) == {}
    labels = label_propagation(["solo"], [])
    assert labels == {"solo": "solo"}


def test_betweenness_star_center_highest():
    # star: X connects four leaves that have no other paths
    nodes = ["X", "a", "b", "c", "d"]
    edges = [("X", "a", 1), ("X", "b", 1), ("X", "c", 1), ("X", "d", 1)]
    bc = betweenness_centrality(nodes, edges)
    assert bc["X"] == 1.0  # every leaf-leaf path goes through X
    assert all(bc[leaf] == 0.0 for leaf in "abcd")


def test_betweenness_bridge_vs_clique():
    # two cliques bridged by node M
    nodes = list("ABMXYZ")
    edges = [
        ("A", "B", 1), ("A", "M", 1), ("B", "M", 1),
        ("M", "X", 1), ("M", "Y", 1), ("X", "Y", 1),
    ]
    bc = betweenness_centrality(nodes, edges)
    assert bc["M"] > bc["A"] and bc["M"] > bc["X"]
    assert all(0.0 <= v <= 1.0 for v in bc.values())
