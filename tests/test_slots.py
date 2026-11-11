import pytest

from company_bid.slots import has_fill_marker, whole_cell_slot


@pytest.mark.parametrize("value", ["", "  ", "________", "＿＿＿＿", "（ ）", "()"])
def test_whole_blank_cells_are_safe_targets(value):
    assert whole_cell_slot(value)


@pytest.mark.parametrize("value", ["________万元", "响应人：________（盖章）", "（响应人名称）", "（请填写注册时间）"])
def test_composite_markers_preserve_protected_text_and_require_explicit_resolution(value):
    assert not whole_cell_slot(value)
    assert has_fill_marker(value)


@pytest.mark.parametrize("value", ["统一社会信用代码", "价格（万元）", "注：不得改变格式", "不接受联合体"])
def test_fixed_text_is_not_a_blank(value):
    assert not whole_cell_slot(value)
    assert not has_fill_marker(value)
