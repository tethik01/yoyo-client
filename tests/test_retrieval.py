from yoyo.rag.retrieve import Passage, build_context, reciprocal_rank_fusion


def test_rrf_prefers_items_ranked_well_in_both_lists():
    dense = [(1, 0.9), (2, 0.8), (3, 0.7)]
    sparse = [(3, 5.0), (1, 4.0), (9, 3.0)]
    fused = reciprocal_rank_fusion([dense, sparse], k=60)
    ids = [cid for cid, _ in fused]
    # 1 and 3 appear in both lists near the top; 9 appears once, last.
    assert set(ids[:2]) == {1, 3}
    assert ids[-1] == 9


def test_rrf_single_list_preserves_order():
    single = [(5, 1.0), (6, 0.5), (7, 0.1)]
    assert [cid for cid, _ in reciprocal_rank_fusion([single])] == [5, 6, 7]


def test_rrf_empty():
    assert reciprocal_rank_fusion([]) == []


def _p(i, text="body"):
    return Passage(
        chunk_id=i, text=text, title=f"t{i}", source_path=f"/x/{i}.md", ordinal=0, score=1.0
    )


def test_build_context_wraps_sources():
    ctx = build_context([_p(1), _p(2)])
    assert ctx.count("<source") == 2
    assert 'id="1"' in ctx


def test_build_context_respects_char_budget():
    passages = [_p(i, "y" * 1000) for i in range(10)]
    ctx = build_context(passages, max_chars=2500)
    assert ctx.count("<source") <= 3


def test_cite_format():
    assert _p(4).cite() == "[t4#0]"
