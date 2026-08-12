from src.evaluation.offline_sanity import SnapshotCorpusRetriever


def test_snapshot_retriever_enforces_cutoff_and_exposes_no_gold_relation(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    path.write_text(
        '{"snapshot_id":"old","content":"LLZO lithium filament","content_sha256":'
        '"a","doi":"10.1/old","publication_date":"2019-01-01"}\n'
        '{"snapshot_id":"new","content":"LLZO lithium filament","content_sha256":'
        '"b","doi":"10.1/new","publication_date":"2021-01-01"}\n',
        encoding="utf-8",
    )
    # Replace fixture hashes after constructing exact content.
    import hashlib, json
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        row["content_sha256"] = hashlib.sha256(row["content"].encode()).hexdigest()
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    result = SnapshotCorpusRetriever(path).search(
        query_id="q", query="LLZO filament", intent="counterevidence",
        cutoff_date="2020-01-01", operation_id="operation",
    )
    assert [passage.passage_id for passage in result.passages] == ["old@q"]
    assert not hasattr(result.passages[0], "relation")


def test_same_snapshot_has_distinct_observation_ids_across_queries(tmp_path):
    import hashlib, json

    content = "LLZO lithium filament"
    row = {
        "snapshot_id": "snapshot", "content": content,
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "doi": "10.1/example", "publication_date": "2019-01-01",
    }
    path = tmp_path / "snapshots.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    retriever = SnapshotCorpusRetriever(path)
    support = retriever.search(
        query_id="support-0", query="LLZO filament", intent="support",
        cutoff_date="2020-01-01", operation_id="support-operation",
    )
    counter = retriever.search(
        query_id="counter-0", query="LLZO filament", intent="counterevidence",
        cutoff_date="2020-01-01", operation_id="counter-operation",
    )
    assert support.passages[0].passage_id == "snapshot@support-0"
    assert counter.passages[0].passage_id == "snapshot@counter-0"
    assert support.passages[0].content_sha256 == counter.passages[0].content_sha256
