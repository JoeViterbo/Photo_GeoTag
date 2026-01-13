import geotag_cascade_gcv_multi as g


def test_get_bias_exact_index():
    per_index = {1: (10.0, 10.0, "A"), 5: (20.0, 20.0, "B")}
    assert g.get_bias_from_plan_or_hint(per_index, 1, []) == ((10.0, 10.0), "A")
    assert g.get_bias_from_plan_or_hint(per_index, 5, []) == ((20.0, 20.0), "B")


def test_get_bias_between_blocks_uses_previous():
    per_index = {10: (10.0, 10.0, "A"), 20: (20.0, 20.0, "B")}
    # idx between 10..19 should use previous block (10)
    assert g.get_bias_from_plan_or_hint(per_index, 15, []) == ((10.0, 10.0), "A")


def test_get_bias_before_first_block_returns_none():
    per_index = {10: (10.0, 10.0, "A"), 20: (20.0, 20.0, "B")}
    assert g.get_bias_from_plan_or_hint(per_index, 1, []) == (None, None)


def test_get_bias_after_last_block_uses_last():
    per_index = {10: (10.0, 10.0, "A"), 20: (20.0, 20.0, "B")}
    assert g.get_bias_from_plan_or_hint(per_index, 25, []) == ((20.0, 20.0), "B")
