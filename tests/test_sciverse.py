"""Contract tests for the Sciverse client, driven from a scripted transport rather than the API.

Every expectation here mirrors something observed against the live deployment, so the tests fail
if a refactor quietly reverts to what the published spec says instead of what the service does.
"""
import io
import json
import urllib.error
import urllib.request

import pytest

from src.tools import sciverse
from src.tools.sciverse import (
    MAX_CONTENT_LIMIT, MAX_DOC_ID_SCOPE, SciverseClient, SciverseEmptyResult, SciverseError,
    SciverseNotFound, SciverseScopeTooLarge, metadata_filters, metadata_sort, semantic_filters,
)

DOC_A = "a" * 64
DOC_B = "b" * 64


class _Response:
    def __init__(self, payload):
        self._body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.status = 200
        self.headers = {"Content-Type": "application/json"}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def http_error(code, payload):
    """A fresh HTTPError; the payload is consumed on read, so each one is single-use."""
    return urllib.error.HTTPError(
        "https://api.sciverse.space/x", code, "error", {},
        io.BytesIO(json.dumps(payload).encode()),
    )


class Transport:
    """Answer urlopen from a scripted queue while recording what was actually sent."""

    def __init__(self, *scripted):
        self.scripted = list(scripted)
        self.calls = []

    def __call__(self, request, timeout=None):
        del timeout
        self.calls.append({
            "method": request.method,
            "url": request.full_url,
            "body": json.loads(request.data.decode()) if request.data else None,
        })
        outcome = self.scripted.pop(0) if self.scripted else {}
        if isinstance(outcome, BaseException):
            raise outcome
        return _Response(outcome)

    @property
    def bodies(self):
        return [call["body"] for call in self.calls]


@pytest.fixture
def client(monkeypatch):
    """A client wired to a scripted transport, with retry backoff removed."""
    def build(*scripted):
        transport = Transport(*scripted)
        monkeypatch.setattr(urllib.request, "urlopen", transport)
        monkeypatch.setattr(sciverse.time, "sleep", lambda _seconds: None)
        instance = SciverseClient(token="sci_test", quiet=True)
        return instance, transport
    return build


def test_a_token_is_required(monkeypatch):
    monkeypatch.delenv("SCIVERSE_API_TOKEN", raising=False)
    with pytest.raises(SciverseError, match="SCIVERSE_API_TOKEN"):
        SciverseClient()


# ------------------------------------------------------------------------------ error handling
def test_an_empty_result_is_a_result_and_not_a_failure(client):
    """The deployment reports "matched nothing" as HTTP 400, which must not look like an outage.

    The cached transport quarantines a failed call as indeterminate, so surfacing this as an
    error would strand an operation in PENDING and demand manual reconciliation for a query that
    simply had no matches.
    """
    api, transport = client(
        http_error(400, {"code": "EMPTY_RESULT", "message": "no matches"}),
        http_error(400, {"code": "EMPTY_RESULT", "message": "no matches"}),
    )
    assert api.agentic_search("nothing matches this") == []
    envelope = api.meta_search("nothing matches this")
    assert envelope["results"] == [] and envelope["total_count"] == 0
    assert len(transport.calls) == 2


def test_the_raw_request_still_raises_so_no_matches_stays_distinguishable(client):
    api, _ = client(http_error(400, {"code": "EMPTY_RESULT", "message": "no matches"}))
    with pytest.raises(SciverseEmptyResult):
        api._request("POST", "/agentic-search", body={"query": "x"}, max_retries=1)


def test_a_structured_error_keeps_the_fields_a_caller_can_act_on(client):
    api, _ = client(http_error(400, {
        "code": "INVALID_REQUEST", "message": "unknown field",
        "request_id": "req-7",
        "details": [{"loc": ["body", "filters", 0, "operator"], "msg": "not an enum"}],
    }))
    with pytest.raises(SciverseError) as caught:
        api.meta_search("query", filters=[{"field": "language", "value": "en"}])
    error = caught.value
    assert error.code == "INVALID_REQUEST"
    assert error.request_id == "req-7"
    assert error.details and error.details[0]["msg"] == "not an enum"
    assert "filters.0.operator" in str(error)
    assert error.as_dict()["status"] == 400


def test_the_nested_envelope_shape_is_understood_too(client):
    api, _ = client(http_error(400, {
        "error": {"code": "INVALID_FILTER_ENUMS", "biz_code": 42, "message": "bad filters"},
    }))
    with pytest.raises(SciverseError) as caught:
        api.agentic_search("query", filters={"__nope__": 1})
    assert caught.value.code == "INVALID_FILTER_ENUMS"
    assert caught.value.biz_code == 42


def test_a_missing_document_is_its_own_error(client):
    api, _ = client(http_error(404, {"code": "CONTENT_NOT_FOUND", "message": "absent"}))
    with pytest.raises(SciverseNotFound):
        api.content(DOC_A)


def test_transient_failures_are_retried_and_permanent_ones_are_not(client):
    api, transport = client(
        http_error(503, {"code": "UNAVAILABLE", "message": "backend down"}),
        http_error(502, {"code": "FETCH_FAILED", "message": "upstream"}),
        {"hits": [{"doc_id": DOC_A, "offset": 0}]},
    )
    assert len(api.agentic_search("query", max_retries=3)) == 1
    assert len(transport.calls) == 3

    api, transport = client(http_error(400, {"code": "INVALID_REQUEST", "message": "no"}))
    with pytest.raises(SciverseError):
        api.agentic_search("query", max_retries=4)
    assert len(transport.calls) == 1, "a 400 will not become a 200 on retry"


# ----------------------------------------------------------------------------- semantic search
def test_semantic_search_clamps_the_documented_bounds(client):
    api, transport = client({"hits": []}, {"hits": []}, {"hits": []})
    api.agentic_search("query", top_k=5000)
    api.agentic_search("query", top_k=0)
    api.agentic_search("x" * 9000)
    assert transport.bodies[0]["top_k"] == sciverse.MAX_TOP_K
    assert transport.bodies[1]["top_k"] == 1
    assert len(transport.bodies[2]["query"]) == 4096


def test_a_blank_semantic_query_is_refused_before_it_costs_a_call(client):
    api, transport = client()
    with pytest.raises(SciverseError, match="non-empty query"):
        api.agentic_search("   ")
    assert transport.calls == []


def test_an_explicitly_empty_document_scope_is_forwarded_rather_than_dropped(client):
    """`{"doc_id": []}` means "search nothing"; dropping it would mean "search everything"."""
    api, transport = client({"hits": []})
    api.agentic_search("query", filters=semantic_filters(doc_ids=[]))
    assert transport.bodies[0]["filters"] == {"doc_id": []}


def test_hits_that_are_not_objects_are_discarded(client):
    api, _ = client({"hits": [{"doc_id": DOC_A, "offset": 1}, "junk", None]})
    assert api.agentic_search("query") == [{"doc_id": DOC_A, "offset": 1}]


# ----------------------------------------------------------------------------- metadata search
def test_metadata_search_reads_results_and_sends_only_the_accepted_keys(client):
    api, transport = client({
        "results": [{"unique_id": "paper:1", "title": "t"}], "total_count": 1,
        "page": 1, "page_size": 10, "hits": [{"doc_id": "should be ignored"}],
    })
    envelope = api.meta_search(
        "query",
        filters=metadata_filters(year_to=2024),
        sort=metadata_sort("-citation_count"),
        fields=["unique_id", "title"],
    )
    assert [row["unique_id"] for row in envelope["results"]] == ["paper:1"]
    assert set(transport.bodies[0]) <= set(sciverse._META_SEARCH_KEYS)


def test_metadata_paging_is_clamped_and_the_result_window_is_enforced(client):
    api, transport = client({"results": [], "total_count": 0})
    api.meta_search("query", page_size=500)
    assert transport.bodies[0]["page_size"] == 50

    api, transport = client()
    with pytest.raises(SciverseError, match="result window"):
        api.meta_search("query", page=201, page_size=50)
    assert transport.calls == []


def test_paging_switches_to_a_cursor_at_the_window_edge(client, monkeypatch):
    monkeypatch.setattr(sciverse, "_RESULT_WINDOW", 4)
    api, transport = client(
        {"results": [{"unique_id": "a"}, {"unique_id": "b"}], "next_cursor": "c1"},
        {"results": [{"unique_id": "c"}, {"unique_id": "d"}], "next_cursor": "c2"},
        {"results": [{"unique_id": "e"}], "next_cursor": None},
    )
    seen = [row["unique_id"] for row in api.iter_meta_search("query", page_size=2)]
    assert seen == ["a", "b", "c", "d", "e"]
    assert transport.bodies[0].get("cursor") is None
    assert transport.bodies[1].get("cursor") is None
    assert transport.bodies[2]["cursor"] == "c2", "the third page crosses the window edge"


def test_paging_stops_when_a_cursor_repeats(client, monkeypatch):
    """A deployment that ignores the cursor must not turn iteration into an unbounded spend.

    A cursor can only be shown to repeat after it has been used once, so the guard costs exactly
    one wasted page and then stops -- rather than paging the same window forever.
    """
    monkeypatch.setattr(sciverse, "_RESULT_WINDOW", 2)
    api, transport = client(*[{"results": [{"unique_id": "a"}], "next_cursor": "same"}] * 8)
    assert len(list(api.iter_meta_search("query", page_size=1))) == 3
    assert len(transport.calls) == 3
    assert transport.bodies[-1]["cursor"] == "same"


def test_an_iteration_limit_is_honoured(client):
    api, _ = client({"results": [{"unique_id": str(index)} for index in range(50)]})
    assert len(list(api.iter_meta_search("query", page_size=50, limit=3))) == 3


# --------------------------------------------------------------------------- filter and sort
def test_metadata_filters_render_the_list_shape_the_endpoint_requires():
    built = metadata_filters(lang="en", year_from=2020, year_to=2024, require_full_text=True)
    assert built == [
        {"field": "language", "value": "en"},
        {"field": "publication_published_year", "value": 2020, "operator": "FILTER_OP_GTE"},
        {"field": "publication_published_year", "value": 2024, "operator": "FILTER_OP_LTE"},
        {"field": "doc_id", "value": "", "operator": "FILTER_OP_NE"},
    ]


def test_full_text_is_selected_by_doc_id_because_the_accessibility_flag_lies():
    """`is_content_accessible` reads false even for documents whose text loads, so it is unusable."""
    assert {"field": "doc_id", "value": "", "operator": "FILTER_OP_NE"} in metadata_filters(
        require_full_text=True,
    )


def test_the_reverse_citation_lookup_is_expressible():
    assert metadata_filters(cites="paper:10.1109/cvpr.2016.90") == [
        {"field": "references_unique_id", "value": "paper:10.1109/cvpr.2016.90"},
    ]


def test_an_unknown_filter_operator_is_refused_locally():
    with pytest.raises(SciverseError, match="unknown filter operator"):
        metadata_filters(extra=[{"field": "language", "value": "en", "operator": "GTE"}])


def test_an_incomplete_extra_filter_is_refused():
    with pytest.raises(SciverseError, match="needs 'field' and 'value'"):
        metadata_filters(extra=[{"field": "language"}])


def test_sort_uses_the_long_order_names_and_rejects_unsortable_fields():
    assert metadata_sort("-citation_count", "publication_published_year") == [
        {"field": "citation_count", "order": "SORT_ORDER_DESC"},
        {"field": "publication_published_year", "order": "SORT_ORDER_ASC"},
    ]
    with pytest.raises(SciverseError, match="not sortable"):
        metadata_sort("title")


def test_semantic_filters_render_the_object_shape_with_inclusive_ranges():
    assert semantic_filters(
        lang="en", year_from=2005, year_to=2010, domain="Physical Sciences",
    ) == {
        "lang": "en",
        "publication_published_year": {"gte": 2005, "lte": 2010},
        "topics": {"logic": "and", "dimensions": {"primary_topic_domain": "Physical Sciences"}},
    }


def test_document_ids_are_deduplicated_in_order_and_validated():
    assert semantic_filters(doc_ids=[DOC_B, DOC_A, DOC_B])["doc_id"] == [DOC_B, DOC_A]
    with pytest.raises(SciverseError, match="64 hex characters"):
        semantic_filters(doc_ids=["not-a-doc-id"])


def test_an_oversized_document_scope_is_refused_before_the_server_refuses_it():
    too_many = [f"{index:064x}" for index in range(MAX_DOC_ID_SCOPE + 1)]
    with pytest.raises(SciverseScopeTooLarge, match="metadata search"):
        semantic_filters(doc_ids=too_many)


# ----------------------------------------------------------------------------------- content
def test_content_clamps_the_slice_to_the_documented_maximum(client):
    api, transport = client({"text": "x", "bytes_returned": 1, "more": False, "next_offset": 1})
    api.content(DOC_A, offset=0, limit=10 ** 7)
    assert f"limit={MAX_CONTENT_LIMIT}" in transport.calls[0]["url"]


def test_a_negative_offset_is_refused_locally(client):
    api, transport = client()
    with pytest.raises(SciverseError, match="cannot be negative"):
        api.content(DOC_A, offset=-1)
    assert transport.calls == []


def test_a_document_is_assembled_by_following_the_offsets_the_server_reports(client):
    """Continuation must follow `next_offset`: offsets count bytes while `text` counts characters."""
    api, transport = client(
        {"text": "café ", "bytes_returned": 6, "more": True, "next_offset": 6},
        {"text": "tail", "bytes_returned": 4, "more": False, "next_offset": 10},
    )
    document = api.read_document(DOC_A)
    assert document["text"] == "café tail"
    assert document["pages"] == 2 and document["end_offset"] == 10
    assert document["truncated"] is False
    assert "offset=0" in transport.calls[0]["url"] and "offset=6" in transport.calls[1]["url"]


def test_assembly_stops_when_the_server_stops_advancing(client):
    api, transport = client(*[{"text": "loop", "more": True, "next_offset": 0}] * 4)
    assert api.read_document(DOC_A)["pages"] == 1
    assert len(transport.calls) == 1


def test_assembly_respects_a_byte_ceiling(client):
    api, transport = client(
        {"text": "12345", "more": True, "next_offset": 5},
        {"text": "67890", "more": True, "next_offset": 10},
    )
    document = api.read_document(DOC_A, max_bytes=8, chunk_limit=5)
    assert document["truncated"] is True and document["bytes"] == 10
    assert len(transport.calls) == 2


# ----------------------------------------------------------------------- catalog and relations
def test_the_catalog_is_fetched_per_collection_and_validated(client):
    api, transport = client({
        "fields": [{"name": "citation_count", "filterable": True, "sortable": True}],
        "default_fields": ["title"], "filter_operators": ["EQ", "IN"],
    })
    catalog = api.meta_catalog("papers", include_sample_values=True)
    assert catalog["filter_operators"] == ["EQ", "IN"]
    assert "include_sample_values=true" in transport.calls[0]["url"]
    with pytest.raises(SciverseError, match="collection must be one of"):
        api.meta_catalog("nonsense")


def test_relations_validate_the_relation_and_the_paging_window(client):
    api, transport = client({
        "items": [{"id": "paper:2", "id_type": "sciverse", "title": "t"}],
        "total_count": 68, "total_pages": 14,
    })
    relations = api.paper_relations("paper:1", "REFERENCES", page_size=5)
    assert relations["total_count"] == 68
    assert transport.bodies[0] == {
        "unique_id": "paper:1", "relation": "REFERENCES", "page": 1, "page_size": 5,
    }

    api, transport = client()
    with pytest.raises(SciverseError, match="relation must be one of"):
        api.paper_relations("paper:1", "SIBLINGS")
    with pytest.raises(SciverseError, match="page_size"):
        api.paper_relations("paper:1", "REFERENCES", page_size=500)
    with pytest.raises(SciverseError, match="references_unique_id"):
        api.paper_relations("paper:1", "REFERENCES", page=500, page_size=200)
    assert transport.calls == []


def test_resource_refuses_traversal_before_asking_the_server(client):
    api, transport = client()
    for name in ("../etc/passwd", "/etc/passwd", "images\\fig.png", ""):
        with pytest.raises(SciverseError, match="must be relative"):
            api.resource(name)
    assert transport.calls == []


def test_a_resource_is_returned_as_bytes(client):
    api, _ = client(b"\x89PNG\r\n")
    assert api.resource("images/fig1.png") == b"\x89PNG\r\n"


# ------------------------------------------------------------------------------- audit chain
def test_every_call_appends_a_projected_record_to_the_evidence_chain(client, tmp_path, capsys):
    log = tmp_path / "nested" / "audit.jsonl"
    api, _ = client(
        {"hits": [{"doc_id": DOC_A, "chunk_id": "c1", "offset": 7, "page_no": 2, "score": 0.9,
                   "recall_source": "milvus", "model_name": "mineru", "model_version": "2",
                   "publication_published_year": 2020, "chunk": "a very long body"}]},
        {"results": [{"unique_id": "paper:1", "title": "t", "citation_count": 3.0}],
         "total_count": 1},
    )
    api.audit_log = str(log)
    api.agentic_search("query")
    api.meta_search("query")
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [record["tool"] for record in records] == ["agentic-search", "meta-search"]

    chunk = records[0]["hits"][0]
    assert chunk["doc_id"] == DOC_A and chunk["model_name"] == "mineru"
    assert "doi" not in chunk, "semantic hits carry no doi; recording one recorded only nulls"
    assert "chunk" not in chunk, "the projection is a citation index, not a copy of the corpus"
    assert records[0]["response_sha256"] and records[0]["schema_version"] == 3
    assert records[1]["hits"][0]["unique_id"] == "paper:1"
    assert capsys.readouterr().err == "", "quiet clients stay quiet"


def test_saturation_is_recorded_because_it_bounds_what_recall_can_be_claimed(client, tmp_path):
    log = tmp_path / "audit.jsonl"
    api, _ = client({"hits": [{"doc_id": f"{index:064x}", "offset": index}
                              for index in range(sciverse.SATURATION_TOP_K)]})
    api.audit_log = str(log)
    api.agentic_search("query", top_k=100)
    record = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert record["saturated"] is True and record["n_hits"] == sciverse.SATURATION_TOP_K
