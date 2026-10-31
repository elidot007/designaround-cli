from designaround_cli.batch import format_top, top_hits


def test_top_column_uses_shape_score():
    rows = top_hits({"ranked": [{"number": "USD1000001S", "shape_score": 0.91}]})
    assert rows == [{"number": "USD1000001S", "shape": 0.91}]
    assert format_top(rows) == "USD1000001S (0.91)"


def test_top_column_lists_screen_hits_when_nothing_was_ranked():
    rows = top_hits({"ranked": [], "hits": [{"number": "USD1000002S", "title": "Pen holder"}]})
    assert format_top(rows) == "USD1000002S"
    assert "(0" not in format_top(rows)
