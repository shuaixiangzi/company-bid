"""Readable task lists retain every task, source quote and review-only block."""
from copy import deepcopy
from zipfile import ZipFile
import hashlib
from xml.etree import ElementTree as ET

from docx import Document
import pytest

from company_bid.models import DocumentPackage
from company_bid.rendering import render_package
from company_bid.review_document import render_task_list, task_list_catalog
from test_review_document_role import package_data, ORIGINAL, REVIEW_CELL


def review_package(tmp_path):
    data=package_data(tmp_path)
    rule='提交截止时间和地点以采购公告为准，办理前逐项核对。'
    commercial='合同价款的适用条件和税费条款应逐项核对，未经确认不得填为已满足。'
    source=tmp_path/'rules.txt';source.write_text(rule+'\n'+commercial,encoding='utf-8')
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    data['sources'].append({'source_id':'RULE-SOURCE-INTERNAL','kind':'tender','title':'合成采购文件','path':str(source),
        'sha256':digest,'locator':'pdf/page[12]/table[0]; cells=r2c1; quote_sha256='+hashlib.sha256(rule.encode()).hexdigest(),
        'synthetic':True,'provenance':'synthetic'})
    data['review_actions']=[]
    for n,(text,kind,status) in enumerate([(rule,'procedure','pending_manual'),(commercial,'commercial_constraint','not_checked')],1):
        rid='R-'+str(n)*20
        data['requirements'].append({'requirement_id':rid,'category':'commercial' if n==2 else 'procedure',
            'response_mode':'procedure','text':text,'source_refs':['RULE-SOURCE-INTERNAL']})
        data['sections'].append({'section_id':'HIDDEN-'+str(n),'title':'原文办理位置'+str(n),'requirement_refs':[rid],
            'document_role':'review','render_body':False,'blocks':[]})
        data['review_actions'].append({'action_id':'RA-'+str(n)*20,'kind':kind,'status':status,'requirement_refs':[rid],
            'source_refs':['RULE-SOURCE-INTERNAL'],'source_requirement':text,'instruction':'独立动作'+str(n)+'：逐项核对原文后办理。'})
    review=next(s for s in data['sections'] if s['section_id']=='S-REVIEW')
    review['blocks'][0]['text']='响应要求（招标原文）：\n'+ORIGINAL
    review['blocks'].append({'kind':'list','items':['额外核对要点一。','额外核对要点二。'],'requirement_refs':['R-004'],'source_refs':['S-TENDER']})
    return DocumentPackage.model_validate(data)


def test_task_list_retains_tasks_quotes_readable_positions_and_private_identity_map(tmp_path):
    package=review_package(tmp_path);before=deepcopy(package.model_dump(mode='json'))
    doc,index=render_task_list(package);text='\n'.join(p.text for p in doc.paragraphs)
    assert [r['issue_id'] for r in index['issues']]==[i.issue_id for i in package.issues]
    assert [r['action_id'] for r in index['actions']]==[a.action_id for a in package.review_actions]
    for issue in package.issues:
        assert issue.issue_id in text and issue.title in text and issue.reason in text and issue.action in text
    for n,action in enumerate(package.review_actions,1):
        assert f'办理{n:03d}' in text and action.instruction in text and action.action_id not in text
        assert all(rid not in text for rid in action.requirement_refs)
    assert '尚未核验' in text and '待线下办理' in text and '合成采购文件 第12页，表1，第3行' in text
    assert 'RULE-SOURCE-INTERNAL' not in text and '.local' not in text
    assert all(source.sha256 not in text and str(source.path) not in text for source in package.sources)
    assert index['source_index']==[source.model_dump(mode='json') for source in package.sources]
    for requirement in package.requirements:
        quote=index['requirement_quote_ids'][requirement.requirement_id]
        parent=next(q for q in index['source_quotes'] if q['display_id']==quote)
        span=next(q for q in parent['contained_quotes'] if q['text']==requirement.text)
        assert parent['text'][span['start']:span['end']]==requirement.text
        assert parent['text'] in text
    assert package.model_dump(mode='json')==before


def test_review_only_paragraph_list_original_table_and_image_all_survive(tmp_path):
    package=review_package(tmp_path)
    result=render_package(package,tmp_path/'task-list',review_document_mode='task_list')
    doc=Document(result['issues_docx']['path']);text='\n'.join(p.text for p in doc.paragraphs)
    assert sum(p.text==ORIGINAL for p in doc.paragraphs)==1
    assert '额外核对要点一。' in text and '额外核对要点二。' in text
    assert REVIEW_CELL in '\n'.join(c.text for t in doc.tables for row in t.rows for c in row.cells)
    assert len(doc.inline_shapes)==1 and 'M-002' in text
    index=result['review_document_index']
    assert index['review_sections'][0]['section_id']=='S-REVIEW'
    assert index['review_sections'][0]['quote_ids'] and index['review_sections'][0]['issue_ids']==['M-002']
    attachment=next(a for a in package.attachments if a.section_id=='S-REVIEW')
    with ZipFile(result['issues_docx']['path']) as archive:
        media=[archive.read(name) for name in archive.namelist() if name.startswith('word/media/')]
    assert media==[attachment.path.read_bytes()]


def test_mode_is_explicit_default_full_trace_and_bid_content_unchanged(tmp_path):
    package=review_package(tmp_path)
    original=render_package(package,tmp_path/'full')
    short=render_package(package,tmp_path/'short',review_document_mode='task_list')
    assert 'review_document_index' not in original and short['review_document_index']['mode']=='task_list'
    full_text='\n'.join(p.text for p in Document(original['issues_docx']['path']).paragraphs)
    assert package.sources[0].sha256 in full_text and package.review_actions[0].action_id in full_text
    with ZipFile(original['bid_docx']['path']) as a,ZipFile(short['bid_docx']['path']) as b:
        assert a.read('word/document.xml')==b.read('word/document.xml')
    with pytest.raises(ValueError,match='REVIEW_DOCUMENT_MODE_INVALID'):
        render_package(package,tmp_path/'bad',review_document_mode='omit_everything')
    assert not (tmp_path/'bad').exists()


@pytest.mark.parametrize("mode", ["task_list", "full_trace"])
def test_operator_links_land_on_the_exact_issue_action_and_source(tmp_path, monkeypatch, mode):
    from company_bid import operator_summary
    package = review_package(tmp_path)
    issue, action = package.issues[0], package.review_actions[0]
    quote = task_list_catalog(package)["quote_ids"][issue.source_requirement]
    item = {"title": "核对本次经办事项", "available": ["样例依据"], "next_steps": ["样例操作"],
            "destinations": ["样例表"], "issue_ids": [issue.issue_id], "action_ids": [action.action_id],
            "primary_quote_ids": [quote]}
    summary = {"items": [item], "details_note": "逐项来源在详情中列出。"}
    monkeypatch.setattr(operator_summary, "build_operator_summary", lambda *_: summary)
    result = render_package(package, tmp_path / mode, review_document_mode=mode)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    w = "{" + ns["w"] + "}"
    with ZipFile(result["issues_docx"]["path"]) as archive:
        xml = ET.fromstring(archive.read("word/document.xml"))
    paragraphs = xml.findall("./w:body/w:p", ns)
    visible = lambda p: "".join(t.text or "" for t in p.findall(".//w:t", ns))
    links = {visible(link): link.get(w + "anchor") for p in paragraphs for link in p.findall("w:hyperlink", ns)}
    anchors = {start.get(w + "name"): visible(p) for p in paragraphs for start in p.findall("w:bookmarkStart", ns)}
    assert visible(paragraphs[0]) == "经办摘要"
    assert len(anchors) == len(links) == (3 if mode == "task_list" else 2)
    assert anchors[links[issue.issue_id]].startswith(issue.issue_id + " " + issue.title)
    assert anchors[links["办理001"]].startswith("办理001 " if mode == "task_list" else action.action_id + " ")
    if mode == "task_list":
        assert anchors[links[quote]] == quote
        assert result["review_document_index"]["operator_summary"] == summary


def test_summary_ranges_never_hide_holes_or_switch_target_kinds(tmp_path):
    from company_bid.review_document import write_operator_summary

    package = review_package(tmp_path)
    ids = [f"M-{index:03d}" for index in (1, 2, 3, 5, 6, 7, 8)]
    item = {"title": "办理分组", "available": [], "next_steps": [], "destinations": [],
            "issue_ids": ids, "action_ids": [package.review_actions[0].action_id], "primary_quote_ids": []}
    doc = Document()
    bookmarks = write_operator_summary(doc, package, {"items": [item], "details_note": "查看编号。"}, link_quotes=True)
    label = "".join(doc.element.xpath(".//w:p[w:hyperlink]//w:t/text()"))
    assert label == "查阅　M-001、M-002、M-003、M-005 至 M-008、办理001"
    assert set(bookmarks) == {("issue", ident) for ident in [*ids[:4], ids[-1]]} | {("action", item["action_ids"][0])}


def test_shared_full_quote_keeps_exact_child_offsets_not_only_an_abbreviated_summary(tmp_path):
    package=review_package(tmp_path);data=package.model_dump(mode='json')
    review=next(s for s in data['sections'] if s['section_id']=='S-REVIEW')
    full='额外前文。\n'+ORIGINAL+'\n额外后文及例外不得省略。'
    review['blocks'][0]['text']=full
    base=next(s for s in data['sources'] if s['source_id']=='S-TENDER')
    data['sources'].append({**base,'source_id':'EXACT-SCOPE','locator':'pdf/page[1]/line[0]; quote_sha256='+hashlib.sha256(ORIGINAL.encode()).hexdigest()})
    next(r for r in data['requirements'] if r['requirement_id']=='R-004')['source_refs']=['EXACT-SCOPE']
    review['blocks'][0]['source_refs']=['EXACT-SCOPE']
    package=DocumentPackage.model_validate(data)
    catalog=task_list_catalog(package)
    parent=next(q for q in catalog['entries'] if q['display_id']==catalog['quote_ids'][ORIGINAL])
    assert parent['text']==full
    assert sum(ORIGINAL in q['text'] for q in catalog['entries'])==1
    assert any(q['text']==ORIGINAL and full[q['start']:q['end']]==ORIGINAL for q in parent['contained_quotes'])


@pytest.mark.parametrize('shared_scope,repeated',[(False,False),(True,True)])
def test_same_file_or_repeated_short_sentence_does_not_allow_cross_clause_source_reassignment(tmp_path,shared_scope,repeated):
    package=review_package(tmp_path);data=package.model_dump(mode='json')
    review=next(s for s in data['sections'] if s['section_id']=='S-REVIEW')
    full='另一处完整条款。\n'+ORIGINAL+('\n重复：'+ORIGINAL if repeated else '')+'\n不得串用。'
    base=next(s for s in data['sources'] if s['source_id']=='S-TENDER')
    for ref,page,text in [('CHILD-SCOPE',1,ORIGINAL),('PARENT-SCOPE',2,full)]:
        data['sources'].append({**base,'source_id':ref,'locator':f'pdf/page[{page}]/line[0]; quote_sha256='+hashlib.sha256(text.encode()).hexdigest()})
    next(r for r in data['requirements'] if r['requirement_id']=='R-004')['source_refs']=['CHILD-SCOPE']
    review['blocks'][0].update(text=full,source_refs=['CHILD-SCOPE' if shared_scope else 'PARENT-SCOPE'])
    catalog=task_list_catalog(DocumentPackage.model_validate(data))
    assert catalog['quote_ids'][ORIGINAL]!=catalog['quote_ids'][full]
    entry=next(q for q in catalog['entries'] if q['display_id']==catalog['quote_ids'][ORIGINAL])
    assert entry['text']==ORIGINAL and entry['contained_quotes'][0]['start']==0


def test_overlapping_occurrences_are_also_ambiguous_even_if_str_count_is_one():
    from types import SimpleNamespace as Obj
    source=Obj(source_id='S',path='source.txt',sha256='a'*64,kind='tender',title='原件',
               locator='pdf/page[1]/line[0]; quote_sha256='+'b'*64)
    package=Obj(sources=[source],requirements=[Obj(requirement_id='CHILD',text='严严严',source_refs=['S']),
        Obj(requirement_id='PARENT',text='严严严严',source_refs=['S'])],issues=[],review_actions=[],sections=[])
    catalog=task_list_catalog(package)
    assert catalog['quote_ids']['严严严']!=catalog['quote_ids']['严严严严']


def test_registered_internal_action_wording_is_display_only_and_auditable(tmp_path):
    package=review_package(tmp_path);data=package.model_dump(mode='json')
    original='本次agent视觉查看不是用户核验；父文档页范围仍为未核实的登记关系。'
    display='历史证明的本次适用性仍待核对；与人工标书的页码对应关系尚未核实。'
    issue=next(i for i in data['issues'] if i['issue_id']=='M-002')
    issue['action']='核对合同主要页。；'+original+'不要据此认定资格满足。'
    package=DocumentPackage.model_validate(data);before=deepcopy(package.model_dump(mode='json'))
    doc,index=render_task_list(package);text='\n'.join(p.text for p in doc.paragraphs)
    assert original not in text and issue['action'].replace(original,display) in text
    row=next(i for i in index['issues'] if i['issue_id']=='M-002')
    assert row['original_issue']==issue
    assert row['display_action']==issue['action'].replace(original,display)
    assert row['display_transformations']==[{'field':'action','rule':'historical_proof_manual_review_wording_v1',
        'original_text':original,'display_text':display,'occurrences':1}]
    assert row['quote_ids']==[task_list_catalog(package)['quote_ids'][issue['source_requirement']]]
    assert ORIGINAL in text and package.model_dump(mode='json')==before


def test_unregistered_similar_wording_and_original_source_are_not_rewritten(tmp_path):
    package=review_package(tmp_path);data=package.model_dump(mode='json')
    known='本次agent视觉查看不是用户核验；父文档页范围仍为未核实的登记关系。'
    similar='本次agent视觉查看不代表用户核验；父文档页范围仍须确认。'
    issue=next(i for i in data['issues'] if i['issue_id']=='M-002')
    issue.update(action=similar,source_requirement=known)
    package=DocumentPackage.model_validate(data);doc,index=render_task_list(package)
    assert similar in '\n'.join(p.text for p in doc.paragraphs)
    row=next(i for i in index['issues'] if i['issue_id']=='M-002')
    assert row['original_issue']==issue and row['display_action']==similar and row['display_transformations']==[]
    quote=next(q for q in index['source_quotes'] if q['display_id']==row['quote_ids'][0])
    assert quote['text']==known and any(p.text==known for p in doc.paragraphs)


def test_source_appendix_number_and_locator_stay_with_first_source_paragraph(tmp_path):
    package=review_package(tmp_path);doc,index=render_task_list(package)
    positions={p.text:i for i,p in enumerate(doc.paragraphs) if p.text.startswith('原文')}
    for entry in index['source_quotes']:
        start=positions[entry['display_id']]
        number,locator,source=doc.paragraphs[start:start+3]
        assert number.paragraph_format.keep_with_next is True
        assert locator.text=='；'.join(entry['locations']) and locator.paragraph_format.keep_with_next is True
        assert source.text==entry['text'] and source.paragraph_format.keep_with_next is False


def test_dictionary_preamble_display_separates_stages_with_exact_trace_mapping(tmp_path):
    package = review_package(tmp_path); data = package.model_dump(mode="json")
    original = "当前表达尚未完成可支持的材料检索，不能据此认定公司没有材料。"
    issue = next(i for i in data["issues"] if i["issue_id"] == "M-002")
    issue["reason"] = original + "仍有资料未完成读取。\n【R-X】语义目录补查：已补查冻结目录，未找到可用候选。"
    issue["source_requirement"] = original
    package = DocumentPackage.model_validate(data); before = deepcopy(package.model_dump(mode="json"))
    doc, index = render_task_list(package); text = "\n".join(p.text for p in doc.paragraphs)
    row = next(i for i in index["issues"] if i["issue_id"] == "M-002")
    assert row["original_issue"] == issue
    assert row["display_reason"].startswith("词典阶段：当前表达未形成可用检索结果；后续目录补查见逐要求记录")
    assert row["display_reason"] in text and "【R-X】语义目录补查：已补查冻结目录" in text
    assert row["display_transformations"] == [{"field": "reason", "rule": "dictionary_stage_unsearched_wording_v1",
        "original_text": original, "display_text": row["display_reason"].partition("仍有资料")[0], "occurrences": 1}]
    assert any(p.text == original for p in doc.paragraphs)  # source appendix stays verbatim
    assert package.model_dump(mode="json") == before


def test_reason_display_does_not_rewrite_embedded_quote_or_similar_unregistered_text():
    from company_bid.review_document import _display_reason
    original = "当前表达尚未完成可支持的材料检索，不能据此认定公司没有材料。"
    for reason in ["原文引用：" + original, "当前表达尚未完成材料检索，不能据此认定公司没有材料。"]:
        assert _display_reason(reason) == (reason, [])


def test_exact_material_headers_use_existing_source_quote_ids_with_identity_map(tmp_path):
    package = review_package(tmp_path); data = package.model_dump(mode="json")
    issue = next(i for i in data["issues"] if i["issue_id"] == "M-002")
    second = "R-" + "1" * 20
    issue["requirement_refs"] = ["R-004", second]
    next(s for s in data["sections"] if s["section_id"] == issue["section_id"])["requirement_refs"].append(second)
    requirements = {r["requirement_id"]: r for r in data["requirements"]}
    prefixes = {}
    for rid in issue["requirement_refs"]:
        short = " ".join(requirements[rid]["text"].split())
        short = short[:36] + ("…" if len(short) > 36 else "")
        prefixes[rid] = f"【{rid}｜{short}】词典阶段："
    issue["reason"] = "原原因及条件保留。\n" + "\n".join(prefix + "未检索；语义目录补查：尚未完成补找。" for prefix in prefixes.values())
    package = DocumentPackage.model_validate(data); before = deepcopy(package.model_dump(mode="json"))
    catalog = task_list_catalog(package); doc, index = render_task_list(package)
    row = next(i for i in index["issues"] if i["issue_id"] == "M-002")
    text = "\n".join(p.text for p in doc.paragraphs)
    assert row["original_issue"] == issue and package.model_dump(mode="json") == before
    expected = issue["reason"]
    for rid, original in prefixes.items():
        qid = catalog["requirement_quote_ids"][rid]
        display = original.replace(rid, qid, 1)
        expected = expected.replace(original, display)
        assert rid not in row["display_reason"] and display in text
        assert qid in row["quote_ids"]
        change = next(c for c in row["display_transformations"] if c["requirement_id"] == rid)
        assert change == {"field": "reason", "rule": "material_requirement_header_source_id_v1",
            "original_text": original, "display_text": display, "occurrences": 1,
            "requirement_id": rid, "quote_id": qid, "source_refs": requirements[rid]["source_refs"]}
        quote = next(q for q in index["source_quotes"] if q["display_id"] == qid)
        assert any(c["text"] == requirements[rid]["text"] for c in quote["contained_quotes"])
    assert row["display_reason"] == expected


def test_material_header_display_does_not_replace_inline_foreign_or_inexact_prefixes(tmp_path):
    from company_bid.review_document import _display_material_requirement_labels
    from types import SimpleNamespace
    package = review_package(tmp_path); catalog = task_list_catalog(package)
    rid = "R-004"; short = " ".join(catalog["requirements"][rid].text.split())
    short = short[:36] + ("…" if len(short) > 36 else "")
    prefix = f"【{rid}｜{short}】词典阶段："
    issue = SimpleNamespace(requirement_refs=[rid])
    for text in ["原文引用：" + prefix + "未检索。", "\n " + prefix + "未检索。",
                 "\n" + prefix.replace(short, "并非原要求短引", 1), "\n" + prefix.replace("词典阶段：", "词典阶段待核：")]:
        assert _display_material_requirement_labels(text, issue, catalog) == (text, [], [])
    foreign = SimpleNamespace(requirement_refs=[])
    assert _display_material_requirement_labels("\n" + prefix, foreign, catalog) == ("\n" + prefix, [], [])
    # Matching text inside the source appendix is outside the reason-only transform.
    data = package.model_dump(mode="json")
    target = next(i for i in data["issues"] if i["issue_id"] == "M-002")
    target["source_requirement"] = prefix + "来源原文完整保留。"
    package = DocumentPackage.model_validate(data); doc, index = render_task_list(package)
    assert any(p.text == target["source_requirement"] for p in doc.paragraphs)
    row = next(i for i in index["issues"] if i["issue_id"] == "M-002")
    assert row["original_issue"]["source_requirement"] == target["source_requirement"]
