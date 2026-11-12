"""Narrow source rules for preparation notes, never bidder facts or compliance.

Callers must first prove the native source interval and retain the original
text. These recognizers do not establish a parent scope, fulfill an obligation,
or permit a supplied response format to be replaced.
"""
import re


_ITEM = r"(?:[（(]\d+[）)]|[①②③④⑤⑥⑦⑧⑨⑩])"
_FILE = r"(?:响应|投标|报价)文件"
_BIDDER = r"(?:响应人|投标人|供应商)"


def preparation_rule(text: str) -> str | None:
    """Only closed, fully read administrative instructions are recognized."""
    value = text.strip()
    if re.fullmatch(_ITEM + r"(?:招标|采购|询比)文件要求提供的其他(?:商务类)?相关资料和" +
                    _BIDDER + r"认为必要的其他内容[。.]?", value):
        return "additional_materials_review"
    if re.fullmatch(r"[1-9]\d*[.．、]" + _FILE + r"(?:部分文本)?格式[（(]未提供格式的" +
                    _FILE + r"(?:可)?由" + _BIDDER + r"自定义格式[）)][。.]?", value):
        return "unsupplied_format_permission"
    return None


def manual_signing_submission(text: str) -> bool:
    """Recognize one purely manual cover/sign/submit rule, including line wraps.

    No scope is inferred. The caller may only preserve this as an unresolved
    manual action; it cannot use it to select body content, prices or facts.
    """
    value = text.replace("\n", "").strip()
    match = re.fullmatch(_FILE + r"封面(?:须|应)法定代表人或授权代表(?:签署|签字)并加盖单位公章[，,]"
                         r"并在规定的时间内将" + _FILE + r"递交至([\u4e00-\u9fffA-Za-z0-9]{1,40})"
                         r"(?:电子招标|电子采购|电子交易)平台[。.]?", value)
    return bool(match and not re.search(r"方案|报价|价格|付款|资质|业绩|证明|承诺|并|须|应|需|不得|必须", match[1]))


def proof_compilation(text: str) -> bool:
    """A set of proof documents is not, by this phrase alone, a separate table."""
    value = text.replace("\n", "").strip()
    if re.search(r"不|禁|无需|无须|免予|除外|仅|但是|但须|清单|汇总表|统计表|表格|一览表", value):
        return False
    plain = (_ITEM + r"\s*(?:(?:应|须|需)(?:提交|提供|附上|附))?"
             r"(?:合成|测试)?(?:有效)?(?:(?:项目)?业绩|检测|资格|公司|人员|证书)?"
             r"(?:证明|证据)(?:文件|材料)?汇总(?:[（(]如有[）)])?[。.]?")
    performance = (_ITEM + r"\s*(?:项目)?业绩[（(](?:包含|含)(?:有效)?(?:业绩)?证明文件汇总[，,]如"
                   r"[^。；;！？!?\r\n]{1,80}[）)][（(]如有[）)][。.]?")
    return bool(re.fullmatch(plain, value) or re.fullmatch(performance, value))
