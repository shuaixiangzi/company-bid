"""Physical-page boundaries and partial forms through the ordinary pipeline."""
from copy import deepcopy
import hashlib

from docx import Document
from docx.oxml.ns import qn
import pdfplumber
from pydantic import ValidationError
import pytest
from reportlab.pdfgen import canvas

from company_bid.extraction import _citation
from company_bid.models import PreparedBid
from company_bid.pipeline import build_executor
from company_bid.reading import SourceDocument, ReadingConfig, _read_pdf_page
from company_bid.source_format import _recover_one, _fill_line
from company_bid.source_format_contracts import format_digest
from test_source_format import Context


def scope_fixture(tmp_path, *, partial=False, heading_only=False, cover=False):
    path=tmp_path/'source-scope.pdf';pdf=canvas.Canvas(str(path),pagesize=(600,800))
    pdf.setFont('Helvetica-Bold',14);pdf.drawString(60,740,'Source form')
    pdf.setFont('Helvetica',12);pdf.drawString(60,700,'Protected first condition;')
    if partial:
        pdf.setFont('Helvetica-Bold',14);pdf.drawString(60,620,'Neighbor form')
        pdf.setFont('Helvetica',12);pdf.drawString(60,580,'Neighbor text must remain outside this form.')
    pdf.drawCentredString(300,20,'- 1 -')
    if not partial:
        pdf.showPage();pdf.setFont('Helvetica',12)
        pdf.drawString(60,740,'Continuation of the protected condition.')
        pdf.drawString(60,700,'Signatory:');pdf.line(130,697,260,697)
        pdf.drawCentredString(300,20,'- 2 -')
    pdf.save()
    sha=hashlib.sha256(path.read_bytes()).hexdigest()
    source={'source_id':'T','path':str(path),'sha256':sha,'kind':'tender','title':'Synthetic source','locator':'original','provenance':'synthetic','synthetic':True}
    doc=SourceDocument(source_id='T',path=path,sha256=sha,role='tender',title='Synthetic')
    blocks=[]
    with pdfplumber.open(path) as opened:
        for number,page in enumerate(opened.pages,1):
            unit=_read_pdf_page(doc,page,number,ReadingConfig(),None)
            for item in sorted(unit.blocks,key=lambda b:(b.bbox[1],b.bbox[0])):
                b=item.model_dump(mode='json');b.update(page=number,unit_id=unit.unit_id,ordinal=len(blocks))
                blocks.append(b)
    chosen=[b for b in blocks if not b['text'].startswith('- ') and not b['text'].startswith('Neighbor')]
    if heading_only:chosen=chosen[:1]
    available={b['block_id']:b for b in blocks};sources={'T':source}
    def scoped(citation):
        checked=_citation({**citation,'source_id':'T'},available)
        ref='Q-'+format_digest(checked)[:16]
        sources[ref]={**source,'source_id':ref,'locator':citation['locator']+'; quote_sha256='+hashlib.sha256(citation['quote'].encode()).hexdigest()+
            ('; blocks='+','.join(citation['block_ids']) if citation.get('block_ids') else '')}
        return ref
    fixed=[{'kind':'paragraph','text':b['text'],'source_refs':[scoped({'block_id':b['block_id'],'locator':b['locator'],'quote':b['text']})]} for b in chosen[1:]]
    section={'section_id':'FORM','title':'Source form','kind':'facts','requirement_refs':['R'],'table_refs':[],'fixed_content':fixed,'source_refs':['T']}
    chapter={'title':section['title'],'content_block_ids':[b['block_id'] for b in chosen]}
    if cover:chapter['source_role']='prescribed_cover'
    manifest={'company':{'name':'Synthetic company'},'project':{'name':'Synthetic project'}}
    ctx=Context();native,reason=_recover_one(ctx,section,chapter,available,source,[],[],manifest,scoped)
    assert native is not None and reason is None,reason
    prepared={'synthetic':True,'preparation_note':'Synthetic physical source scope fixture.',
        'project':{'project_id':'P','name':'Synthetic project','source_refs':['T']},'company':{'company_id':'C','name':'Synthetic company','source_refs':['T']},
        'sources':list(sources.values()),'requirements':[{'requirement_id':'R','text':'Preserve the frozen form.','category':'qualification','response_mode':'fixed_text','source_refs':['T']}],
        'sections':[section],'tables':[],'facts':[],'attachments':[],'issues':[]}
    return PreparedBid.model_validate(prepared).model_dump(mode='json'),ctx,chapter,available,source,scoped,manifest


def test_partial_scope_keeps_excluded_neighbor_out_of_editable_docx(tmp_path):
    data,*_=scope_fixture(tmp_path,partial=True)
    native=data['sections'][0]['native_format'];scope=native['page_scopes'][0]
    assert scope['scope_kind']=='partial_page'
    assert [m['role'] for m in scope['members']]==['selected','selected','excluded','excluded','page_furniture']
    executor=build_executor(tmp_path/'runtime',{'provider':{'mode':'prepared'}})
    run=executor.create_run({'prepared':data});state=executor.execute(run)
    assert state['status']=='succeeded',state.get('error_code')
    doc=Document(executor.store.root/executor.output(run)['files']['bid_docx']['path'])
    text='\n'.join(p.text for p in doc.paragraphs)
    assert 'Protected first condition;' in text and 'Neighbor text' not in text and 'Neighbor form' not in text


def test_cross_page_format_preserves_explicit_break_and_has_only_one_heading(tmp_path):
    data,*_=scope_fixture(tmp_path)
    native=data['sections'][0]['native_format']
    assert [s['physical_page'] for s in native['page_scopes']]==[1,2]
    assert [e['physical_page'] for e in native['elements']]==[1,2,2]
    executor=build_executor(tmp_path/'runtime',{'provider':{'mode':'prepared'}})
    run=executor.create_run({'prepared':data});state=executor.execute(run)
    assert state['status']=='succeeded',state.get('error_code')
    doc=Document(executor.store.root/executor.output(run)['files']['bid_docx']['path'])
    assert sum(p.style.name=='Heading 1' and p.text=='Source form' for p in doc.paragraphs)==1
    body=list(doc.element.body)
    first=next(i for i,e in enumerate(body) if 'Protected first condition;' in ''.join(e.itertext()))
    second=next(i for i,e in enumerate(body) if 'Continuation of the protected condition.' in ''.join(e.itertext()))
    assert first<second and any(e.xpath('.//w:sectPr') for e in body[first:second])
    assert 'Signatory:' in '\n'.join(p.text for p in doc.paragraphs)


def test_heading_only_source_scope_is_visible_but_empty_house_section_still_fails(tmp_path):
    data,*_=scope_fixture(tmp_path,partial=True,heading_only=True)
    assert data['sections'][0]['native_format']['elements']==[]
    executor=build_executor(tmp_path/'runtime',{'provider':{'mode':'prepared'}})
    run=executor.create_run({'prepared':data});state=executor.execute(run)
    assert state['status']=='succeeded',state.get('error_code')
    doc=Document(executor.store.root/executor.output(run)['files']['bid_docx']['path'])
    assert sum(p.text=='Source form' for p in doc.paragraphs)==1
    data['sections'][0]['native_format']=None
    run=executor.create_run({'prepared':data});state=executor.execute(run)
    assert state['status']=='failed' and state['error_code']=='EMPTY_SECTION'


def test_partial_cross_page_scope_cannot_take_only_next_chapter_heading(tmp_path):
    data,ctx,chapter,blocks,source,scoped,manifest=scope_fixture(tmp_path)
    source_blocks=deepcopy(blocks)
    second=[b for b in source_blocks.values() if b['page']==2 and not b['text'].startswith('- ')]
    chapter=deepcopy(chapter);chapter['content_block_ids'].remove(second[0]['block_id'])
    section=deepcopy(data['sections'][0]);section['fixed_content'].pop(1)
    spec,reason=_recover_one(Context(),section,chapter,source_blocks,source,[],[],manifest,scoped)
    assert spec is None and reason=='unsupported_partial_cross_page_scope'


def test_known_project_number_requires_unique_explicit_fact_and_never_fills_other_numbers():
    manifest={'company':{'name':'C'},'project':{'name':'P'}}
    facts=[{'fact_id':'PN','label':'项目编号','value':'PROJECT-89','source_refs':['T']}]
    item={'text':'项目编号：','source_refs':['T']}
    found=_fill_line(item,item['text'],'B',0,facts,manifest)
    assert item['text']=='项目编号：PROJECT-89' and found[0]['label']=='project_number'
    for text in ['合同编号：','法定代表人：','日期：','项目编号：ALREADY-SET','注：不得填写项目编号：']:
        item={'text':text,'source_refs':['T']};assert _fill_line(item,text,'B',0,facts,manifest)==[] and item['text']==text
    item={'text':'项目编号：','source_refs':['T']}
    assert _fill_line(item,item['text'],'B',0,[*facts,{**facts[0],'fact_id':'OTHER','value':'CONFLICT'}],manifest)==[]
    text='项目名称： 项目编号：'
    end=len(text)
    rule={'fixed_index':0,'start':end,'end':end,'width_pt':72.,'bbox':[300.,100.,372.,100.],
          'locator':'pdf/page[1]/rule[0]','basis':'native_vector_rule'}
    item={'text':text,'source_refs':['T']}
    binding=_fill_line(item,text,'B',0,facts,manifest,source_rules=[rule])[0]
    assert item['text']==text+'PROJECT-89' and binding['source_rule']==rule
