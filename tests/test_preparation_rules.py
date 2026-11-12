"""Preparation rules cannot swallow mixed or substantive obligations."""
import pytest

from company_bid.preparation_rules import manual_signing_submission, preparation_rule, proof_compilation


OTHER = "（6）采购文件要求提供的其他商务类相关资料和供应商认为必要的其他内容。"
PERMISSION = "2.投标文件部分文本格式（未提供格式的投标文件由投标人自定义格式）"
MANUAL = "投标文件封面须法定代表人或授权代表签署并加盖单位公章，并在规定的时间内将投标\n文件递交至合成电子招标平台。"


def test_closed_source_rules_do_not_claim_completion():
    assert preparation_rule(OTHER) == "additional_materials_review"
    assert preparation_rule("④采购文件要求提供的其他相关资料和供应商认为必要的其他内容。") == "additional_materials_review"
    assert preparation_rule(PERMISSION) == "unsupplied_format_permission"
    assert manual_signing_submission(MANUAL)


@pytest.mark.parametrize("suffix", ["另须提供检测方案。", "并提供价格明细表。", "另须提供资质证书。", "不得改变指定格式。"])
@pytest.mark.parametrize("base", [OTHER, PERMISSION])
def test_mixed_instruction_never_becomes_a_generic_pending_rule(base, suffix):
    assert preparation_rule(base + suffix) is None


@pytest.mark.parametrize("text", [
    "（6）采购文件要求提供其他检测设备证明。",
    "（6）供应商必须提供其余业绩证明和财务审计报告。",
    "2.投标文件部分文本格式（已提供格式的投标文件由投标人自定义格式）",
    "2.投标文件部分文本格式（未提供格式的投标文件不得自定义格式）",
    OTHER.replace("必要", "不必要"),
    OTHER.replace("其他内容", "其他内容和检测方案"),
])
def test_concrete_missing_materials_and_changed_permissions_stay_unrecognized(text):
    assert preparation_rule(text) is None


@pytest.mark.parametrize("text", [
    MANUAL + "必须同时编写检测方案。",
    MANUAL.replace("签署", "核实投标报价并签署"),
    MANUAL.replace("加盖单位公章", "填写价格并加盖单位公章"),
    MANUAL.replace("在规定的时间内", "在收到付款后"),
    "①" + MANUAL,
    MANUAL.replace("投标文件封面", "技术方案"),
    MANUAL.replace("合成电子招标平台", "合成并提交资质电子招标平台"),
])
def test_manual_scope_exception_is_not_a_general_procedure_fallback(text):
    assert not manual_signing_submission(text)


def test_document_compilation_does_not_hide_an_explicit_list_or_table():
    assert proof_compilation("（2）业绩（含有效证明文件汇总，如合同、订单复印件）（如有）。")
    for explicit in ("清单", "汇总表", "统计表", "表格", "一览表"):
        assert not proof_compilation("应提交证明文件汇总及" + explicit + "。")


@pytest.mark.parametrize("text", [
    "（2）不得提交合成检测证明文件汇总。",
    "（2）无须提交合成检测证明文件汇总。",
    "（2）禁止提交合成检测证明文件汇总。",
    "（2）严禁提交合成检测证明文件汇总。",
    "（2）拒绝提交合成检测证明文件汇总。",
    "（2）避免提交合成检测证明文件汇总。",
    "（2）应提交合成检测证明文件汇总；但须另附资格证书。",
    "（2）应提交合成检测证明文件汇总及检测方案。",
    "（2）仅提供合成检测证明文件汇总。",
])
def test_negative_or_mixed_compilation_is_not_a_positive_collection_rule(text):
    assert not proof_compilation(text)
