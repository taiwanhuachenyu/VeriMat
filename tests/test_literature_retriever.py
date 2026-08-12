from src.evaluation.literature_retriever import (
    CachedSciverseTransport, SciverseBenchmarkRetriever,
)


class Client:
    def __init__(self):
        self.search_calls = 0
        self.content_calls = 0

    def agentic_search(self, query, **kwargs):
        self.search_calls += 1
        return [
            {"doc_id": "eligible", "offset": 10,
             "publication_published_date": "2020-06-01"},
            {"doc_id": "post-cutoff", "offset": 20,
             "publication_published_date": "2020-08-01"},
            {"doc_id": "unknown-same-year", "offset": 30,
             "publication_published_year": 2020},
        ]

    def content(self, doc_id, **kwargs):
        self.content_calls += 1
        return {"text": f"content for {doc_id}"}


def test_retriever_enforces_exact_cutoff_rereads_and_replays_cache(tmp_path):
    client = Client()
    transport = CachedSciverseTransport(
        client=client, operation_db=tmp_path / "retrieval.db",
    )
    retriever = SciverseBenchmarkRetriever(
        transport=transport, index_snapshot_id="fixture-snapshot", top_k=3,
    )
    reserved = []
    kwargs = dict(
        query_id="counter-0", query="query", intent="counterevidence",
        cutoff_date="2020-06-30", operation_id="operation",
        reserve_call=reserved.append,
    )
    first = retriever.search(**kwargs)
    second = retriever.search(**kwargs)
    assert [item.doc_id for item in first.passages] == ["eligible"]
    assert second == first
    assert first.usage.calls == 2
    assert client.search_calls == 1 and client.content_calls == 1
    assert len(reserved) == 4
    assert reserved[0] == reserved[2] == "search"
    assert reserved[1] == reserved[3] and reserved[1].startswith("content:0:")
    assert not retriever.provenance_manifest()["publication_ready"]


def test_year_only_hit_is_allowed_at_year_end_cutoff(tmp_path):
    client = Client()
    retriever = SciverseBenchmarkRetriever(
        transport=CachedSciverseTransport(
            client=client, operation_db=tmp_path / "retrieval.db",
        ),
        index_snapshot_id="fixture", top_k=3,
    )
    result = retriever.search(
        query_id="q", query="query", intent="support",
        cutoff_date="2020-12-31", operation_id="operation",
        reserve_call=lambda _key: None,
    )
    assert {item.doc_id for item in result.passages} == {
        "eligible", "post-cutoff", "unknown-same-year",
    }


def test_completed_reservation_race_returns_cached_search_without_network(tmp_path):
    client = Client()
    transport = CachedSciverseTransport(
        client=client, operation_db=tmp_path / "retrieval.db",
    )
    request = {"query": "query", "top_k": 2, "filters": {"year": 2020}}
    request_hash = transport._hash("search", request)
    transport.conn.execute(
        "INSERT INTO retrieval_operations VALUES (?,?,?,?,?,?)",
        (
            "operation", request_hash, "COMPLETED",
            '[{"doc_id":"cached","offset":4}]', 1.0, 2.0,
        ),
    )
    original_lookup = transport._lookup
    lookup_calls = 0

    def race_lookup(operation_id, candidate_hash):
        nonlocal lookup_calls
        lookup_calls += 1
        if lookup_calls == 1:
            return None
        return original_lookup(operation_id, candidate_hash)

    transport._lookup = race_lookup
    assert transport.search(operation_id="operation", **request) == [
        {"doc_id": "cached", "offset": 4},
    ]
    assert client.search_calls == 0
