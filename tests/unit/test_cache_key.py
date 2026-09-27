from aftertrace.gateway import cache_key


def route():
    return {
        "project_id": "p",
        "corpus_id": "c",
        "environment": "local",
        "collection_name": "build_b",
        "generation": 2,
        "cache_epoch": 3,
        "manifest_digest": "manifest-b",
    }


def test_cache_epoch_changes_identity():
    old = route()
    new = {**old, "cache_epoch": old["cache_epoch"] + 1}
    query = {"query_vector": [1.0, 0.0], "top_k": 1}
    assert cache_key(old, query, ["tenant:a"]) != cache_key(new, query, ["tenant:a"])


def test_principals_do_not_share_cache():
    query = {"query_vector": [1.0, 0.0], "top_k": 1}
    assert cache_key(route(), query, ["tenant:a"]) != cache_key(route(), query, ["tenant:b"])


def test_generation_changes_identity():
    old = route()
    new = {**old, "generation": old["generation"] + 1}
    query = {"query_vector": [1.0, 0.0], "top_k": 1}
    assert cache_key(old, query, ["tenant:a"]) != cache_key(new, query, ["tenant:a"])


def test_collection_changes_identity():
    old = route()
    new = {**old, "collection_name": "build_c"}
    query = {"query_vector": [1.0, 0.0], "top_k": 1}
    assert cache_key(old, query, ["tenant:a"]) != cache_key(new, query, ["tenant:a"])
