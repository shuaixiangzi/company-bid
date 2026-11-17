"""Synthetic native grid fixtures; no company or tender text is embedded."""
import hashlib

import pytest
from docx import Document
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from company_bid.models import DocumentStyle, file_sha256
from company_bid.source_format_rendering import recover_review_source_tables, render_review_source_table


@pytest.fixture
def source_grid(tmp_path):
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=400)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
        DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    # 3 rows, 2 columns. Last cell deliberately blank.
    stream.set_data(b'0.5 w 50 200 m 350 200 l S 50 250 m 350 250 l S '
        b'50 300 m 350 300 l S 50 350 m 350 350 l S '
        b'50 200 m 50 350 l S 200 200 m 200 350 l S 350 200 m 350 350 l S '
        b'BT /F1 12 Tf 60 320 Td (Label) Tj ET BT /F1 12 Tf 210 320 Td (Score) Tj ET '
        b'BT /F1 12 Tf 60 270 Td (Alpha) Tj ET BT /F1 12 Tf 210 270 Td (8) Tj ET '
        b'BT /F1 12 Tf 60 220 Td (Beta) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    path = tmp_path / 'native.pdf'
    with path.open('wb') as out:
        writer.write(out)
    bid = 'b-' + hashlib.sha256(b'original::pdf/page[1]/table[0]').hexdigest()[:24]
    text = 'Intro\nLabel\tScore\nAlpha\t8\nBeta\t\nTail'
    source = {'source_id':'original','title':'Synthetic source','path':str(path),
              'sha256':file_sha256(path),'kind':'tender','locator':'original'}
    cells = ','.join(f'{bid}:r{r}c{c}' for r in range(3) for c in range(2))
    scope = {**source,'source_id':'scope','locator':f'pdf/page[1]/line[0]; basis=consecutive_mixed_blocks_lf_v1; '
        f'quote_sha256={hashlib.sha256(text.encode()).hexdigest()}; cells={cells}; '
        f'through=pdf/page[1]/table[0]; blocks={bid}'}
    return text, {'original':source,'scope':scope}


def recover(fixture):
    text, sources = fixture
    return recover_review_source_tables(text, ['scope'], sources)


def test_native_columns_empty_cell_and_mixed_order(source_grid):
    result = recover(source_grid)
    assert result['status'] == 'restored'
    assert [f['kind'] for f in result['fragments']] == ['paragraph','table','paragraph']
    assert result['fragments'][1]['rows'] == [['Label','Score'],['Alpha','8'],['Beta','']]
    assert result['fragments'][0]['text'] == 'Intro\n'
    assert result['fragments'][-1]['text'] == '\nTail'
    assert result['proofs'][0]['source_rows'] == [0,1,2]
    assert result['proofs'][0]['parser_version']


def test_render_editable_grid_without_added_header(source_grid):
    result = recover(source_grid)
    doc = Document()
    render_review_source_table(doc, result['fragments'][1], DocumentStyle())
    assert len(doc.tables[0].rows) == 3
    assert doc.tables[0].cell(1,1).text == '8'
    assert doc.tables[0].cell(2,1).text == ''
    assert '列1' not in doc.tables[0]._tbl.xml


@pytest.mark.parametrize('change,reason', [
    ('hash','source_hash_mismatch'), ('quote','exact_quote_scope_unavailable'),
    ('cell','partial_row_scope'), ('basis','unsupported_quote_basis'),
    ('identity','native_table_identity_unresolved'),('duplicate','duplicate_cell_scope'),
    ('outside','cell_block_outside_scope'),('page','source_page_out_of_range'),
])
def test_scope_failures_never_guess(source_grid, change, reason):
    text,sources=source_grid
    scope=sources['scope']
    if change=='hash': scope['sha256']='0'*64
    elif change=='quote': text=text.replace('8','9')
    elif change=='cell': scope['locator']=scope['locator'].replace(next(c for c in scope['locator'].split('cells=')[1].split(';')[0].split(',') if c.endswith(':r1c1')) + ',', '')
    elif change=='basis': scope['locator']=scope['locator'].replace('consecutive_mixed_blocks_lf_v1','unverified')
    elif change=='identity':
        sources.pop('original')
    elif change=='duplicate':
        scope['locator']=scope['locator'].replace('; through=',','+scope['locator'].split('cells=')[1].split(',')[0]+'; through=')
    elif change=='outside': scope['locator']=scope['locator'].replace('; blocks=', '; blocks=other')
    elif change=='page': scope['locator']=scope['locator'].replace('page[1]','page[2]')
    result=recover_review_source_tables(text,['scope'],sources)
    assert result['status'] != 'restored'
    assert result['reason'] == reason
    assert result['fragments'] == [{'kind':'paragraph','text':text}]
    assert not result['proofs']


def test_ambiguous_scope_is_preserved(source_grid):
    text,sources=source_grid
    sources['scope2']={**sources['scope'],'source_id':'scope2'}
    result=recover_review_source_tables(text,['scope','scope2'],sources)
    assert result['reason']=='ambiguous_quote_scope'


def test_missing_source_and_non_table_text(source_grid):
    assert recover_review_source_tables('plain',['absent'],{})['status']=='not_applicable'
    assert recover_review_source_tables('a\tb',['absent'],{})['reason']=='source_ref_missing'


def test_modified_source_bytes_rejected(source_grid):
    from pathlib import Path
    text,sources=source_grid
    with Path(sources['scope']['path']).open('ab') as stream: stream.write(b'\n')
    assert recover(source_grid)['reason']=='source_hash_mismatch'


def test_source_quote_mismatch_keeps_original(source_grid):
    text,sources=source_grid
    text=text.replace('Alpha','Altered')
    sources['scope']['locator']=sources['scope']['locator'].replace(
        hashlib.sha256(source_grid[0].encode()).hexdigest(),hashlib.sha256(text.encode()).hexdigest())
    result=recover_review_source_tables(text,['scope'],sources)
    assert result['reason']=='native_grid_quote_mismatch'



def test_cache_reuses_parse_but_rechecks_file_hash(source_grid, monkeypatch):
    from pathlib import Path
    import pdfplumber
    text,sources=source_grid
    cache={}
    first=recover_review_source_tables(text,['scope'],sources,cache)
    assert cache
    def forbidden(*args,**kwargs):
        raise AssertionError('cached tables must not reparse')
    monkeypatch.setattr(pdfplumber,'open',forbidden)
    assert recover_review_source_tables(text,['scope'],sources,cache)==first
    with Path(sources['scope']['path']).open('ab') as stream: stream.write(b'\n')
    assert recover_review_source_tables(text,['scope'],sources,cache)['reason']=='source_hash_mismatch'


def merged_grid(source_grid):
    from pathlib import Path
    from pypdf import PdfReader
    text,sources=source_grid
    path=Path(sources['scope']['path'])
    reader=PdfReader(path)
    writer=PdfWriter()
    page=writer.add_page(reader.pages[0])
    stream=DecodedStreamObject()
    stream.set_data(page.get_contents().get_data().replace(b'50 250 m 350 250 l S',b'200 250 m 350 250 l S'))
    page[NameObject('/Contents')]=writer._add_object(stream)
    with path.open('wb') as out: writer.write(out)
    text=text.replace('Alpha\t8\nBeta\t','Alpha\nBeta\t8\n\t')
    old_hash=hashlib.sha256(source_grid[0].encode()).hexdigest()
    for source in sources.values(): source['sha256']=file_sha256(path)
    sources['scope']['locator']=sources['scope']['locator'].replace(old_hash,hashlib.sha256(text.encode()).hexdigest())
    return text,sources


def test_native_merge_retains_anchor_and_empty_continuation(source_grid):
    text,sources=merged_grid(source_grid)
    result=recover_review_source_tables(text,['scope'],sources)
    assert result['status']=='restored'
    table=result['fragments'][1]
    assert table['merges']==[{'row':1,'column':0,'row_span':2,'col_span':1}]
    assert table['rows'][1]==['Alpha\nBeta','8']
    assert table['rows'][2]==['','']
    doc=Document()
    render_review_source_table(doc,table,DocumentStyle())
    assert doc.tables[0].cell(1,0)._tc is doc.tables[0].cell(2,0)._tc


def test_merge_clipped_by_selected_rows_is_not_reconstructed(source_grid):
    text,sources=merged_grid(source_grid)
    old_hash=hashlib.sha256(text.encode()).hexdigest()
    text='Alpha\nBeta\t8'
    scope=sources['scope']
    cells=scope['locator'].split('cells=')[1].split(';')[0]
    selected=','.join(cell for cell in cells.split(',') if ':r1c' in cell)
    scope['locator']=scope['locator'].replace(cells,selected).replace(old_hash,hashlib.sha256(text.encode()).hexdigest())
    assert recover_review_source_tables(text,['scope'],sources)['reason']=='merge_crosses_quote_boundary'


@pytest.mark.parametrize('merged',[False,True])
def test_private_reader_grid_equivalent_to_frozen_native_contract(source_grid,merged):
    import pdfplumber
    from company_bid.reading import SourceDocument,_pdf_table
    from company_bid.source_format_rendering import _review_native_table
    text,sources=merged_grid(source_grid) if merged else source_grid
    original=sources['original']
    with pdfplumber.open(original['path']) as pdf:
        page=pdf.pages[0]
        table=page.find_tables()[0]
        expected=_pdf_table(SourceDocument(**{key:original[key] for key in
            ('source_id','title','path','sha256')},role='tender'),page,table,'pdf/page[1]/table[0]')
        actual=_review_native_table(page,table,'original','pdf/page[1]/table[0]')
    assert actual=={key:expected.model_dump(mode='json')[key] for key in actual}


def test_recovery_has_no_runtime_reading_dependency(source_grid,monkeypatch):
    import builtins
    original_import=builtins.__import__
    def guarded_import(name,*args,**kwargs):
        if name=='reading' or name.endswith('.reading'):
            raise AssertionError('N6 must not import N1 reading code')
        return original_import(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',guarded_import)
    assert recover(source_grid)['status']=='restored'


def test_private_reader_rejects_overlapping_cells():
    from types import SimpleNamespace
    from company_bid.source_format_rendering import _review_native_table
    table=SimpleNamespace(cells=[(0,0,20,20),(0,0,20,20)],bbox=(0,0,20,20))
    with pytest.raises(ValueError,match='OVERLAP'):
        _review_native_table(SimpleNamespace(chars=[]),table,'synthetic','pdf/page[1]/table[0]')


def test_private_reader_rejects_text_in_grid_hole():
    from types import SimpleNamespace
    from company_bid.source_format_rendering import _review_native_table
    table=SimpleNamespace(cells=[(0,0,10,10),(10,10,20,20)],bbox=(0,0,20,20))
    page=SimpleNamespace(chars=[{'x0':12,'x1':14,'top':2,'bottom':4,'text':'X'}])
    with pytest.raises(ValueError,match='UNASSIGNED_TEXT'):
        _review_native_table(page,table,'synthetic','pdf/page[1]/table[0]')


def aggregate_fixture(source_grid,partial=False):
    import re
    text,sources=source_grid
    base=sources.pop('scope')
    def child(ref,text,selected_rows):
        scope={**base,'source_id':ref}
        cells=base['locator'].split('cells=')[1].split(';')[0]
        chosen=','.join(c for c in cells.split(',') if int(re.search(r':r(\d+)c',c)[1]) in selected_rows)
        loc=re.sub(r'quote_sha256=[a-f0-9]{64}','quote_sha256='+hashlib.sha256(text.encode()).hexdigest(),base['locator'])
        scope['locator']=loc.replace(cells,chosen)+f'; chars[0:{len(text)}]'
        return scope
    first='Label\tScore'
    second='Alpha\t8\nBeta\t' if not partial else 'Altered\t8\nBeta\t'
    sources['part1']=child('part1',first,{0})
    sources['part2']=child('part2',second,{1,2})
    return first+'\n'+second,sources


def test_aggregate_unique_full_coverage_and_lifted_offsets(source_grid):
    text,sources=aggregate_fixture(source_grid)
    result=recover_review_source_tables(text,['part1','part2'],sources)
    assert result['status']=='restored'
    assert len(result['scope_parts'])==2 and len(result['proofs'])==2
    assert [(p['start'],p['end']) for p in result['proofs']]==[(0,11),(12,len(text))]
    assert result['proofs'][1]['child_start']==0
    assert result['proofs'][1]['aggregate_quote_sha256']==hashlib.sha256(text.encode()).hexdigest()
    assert ''.join(f['text'] if f['kind']=='paragraph' else '\n'.join('\t'.join(r) for r in f['rows']) for f in result['fragments'])==text


def test_aggregate_partial_child_keeps_text_and_explicit_warning(source_grid):
    text,sources=aggregate_fixture(source_grid,partial=True)
    result=recover_review_source_tables(text,['part1','part2'],sources)
    assert result['status']=='partially_restored' and len(result['proofs'])==1
    unresolved=[f for f in result['fragments'] if f.get('layout_status')=='unsupported']
    assert len(unresolved)==1 and unresolved[0]['layout_reason']=='native_grid_quote_mismatch'
    assert unresolved[0]['quote_start']==12 and unresolved[0]['text']==text[12:]


@pytest.mark.parametrize('change,reason,status',[
    ('repeat','aggregate_child_quote_ambiguous','unsupported'),
    ('gap','aggregate_scopes_do_not_cover_quote','unsupported'),
    ('bad_hash','aggregate_source_hash_mismatch','failed'),
    ('missing','aggregate_source_ref_missing','failed'),
    ('non_tender','aggregate_non_tender_scope','failed'),
    ('absent','aggregate_child_quote_absent','unsupported'),
    ('missing_chars','aggregate_precise_quote_scope_required','unsupported'),
])
def test_aggregate_rejects_unproved_compositions(source_grid,change,reason,status):
    text,sources=aggregate_fixture(source_grid)
    refs=['part1','part2']
    if change=='repeat':text+='\nLabel\tScore'
    elif change=='gap':text=text.replace('\nAlpha','\nUnbound\nAlpha')
    elif change=='bad_hash':sources['part2']['sha256']='0'*64
    elif change=='missing':sources.pop('part2')
    elif change=='non_tender':sources['part2']['kind']='company_material'
    elif change=='absent':text=text.replace('Alpha','Other')
    elif change=='missing_chars':sources['part2']['locator']=sources['part2']['locator'].split('; chars[')[0]
    result=recover_review_source_tables(text,refs,sources)
    assert result['status']==status and result['reason']==reason
    assert result['fragments']==[{'kind':'paragraph','text':text}]


def test_aggregate_rejects_match_inside_line_boundary(source_grid):
    text,sources=aggregate_fixture(source_grid)
    result=recover_review_source_tables('prefix'+text,['part1','part2'],sources)
    assert result['status']=='unsupported' and result['reason']=='aggregate_child_quote_absent'


def test_aggregate_hash_work_is_explicitly_bounded(source_grid,monkeypatch):
    import company_bid.source_format_rendering as module
    text,sources=aggregate_fixture(source_grid)
    monkeypatch.setattr(module,'_REVIEW_AGGREGATE_HASH_BYTE_BUDGET',10)
    result=recover_review_source_tables(text,['part1','part2'],sources)
    assert result['status']=='unsupported' and result['reason']=='aggregate_hash_work_limit'
    assert result['fragments']==[{'kind':'paragraph','text':text}]


def test_aggregate_records_actual_hash_byte_work(source_grid):
    text,sources=aggregate_fixture(source_grid)
    result=recover_review_source_tables(text,['part1','part2'],sources)
    assert result['status']=='restored'
    assert 0 < result['hash_work_bytes'] <= result['hash_work_byte_budget'] == 8*1024*1024


def test_large_line_dense_aggregate_stops_at_default_hash_budget(source_grid,monkeypatch):
    import re
    import company_bid.source_format_rendering as module
    text,sources=aggregate_fixture(source_grid)
    text='\t\n'+'X\n'*49998+'Z'
    missing='Y'*50001
    for ref in ('part1','part2'):
        loc=sources[ref]['locator']
        loc=re.sub(r'quote_sha256=[a-f0-9]{64}','quote_sha256='+hashlib.sha256(missing.encode()).hexdigest(),loc)
        sources[ref]['locator']=re.sub(r'chars\[0:\d+\]',f'chars[0:{len(missing)}]',loc)
    original_sha=hashlib.sha256
    hashed_bytes=[]
    def counted_sha(data=b'',*args,**kwargs):
        if data:hashed_bytes.append(len(data))
        return original_sha(data,*args,**kwargs)
    monkeypatch.setattr(hashlib,'sha256',counted_sha)
    result=recover_review_source_tables(text,['part1','part2'],sources)
    assert result['reason']=='aggregate_hash_work_limit'
    # Includes the outer complete-quote digest. The guard never hashes the
    # candidate that would exceed the cumulative 8 MiB candidate allowance.
    assert sum(hashed_bytes) <= module._REVIEW_AGGREGATE_HASH_BYTE_BUDGET+len(text.encode())


def test_aggregate_keeps_interior_duplicate_ambiguous(source_grid):
    text,sources=aggregate_fixture(source_grid)
    # Only one occurrence starts at an LF boundary, but repeated source text
    # still cannot be silently assigned to one display location.
    text=text+'\nprefixLabel\tScoresuffix'
    result=recover_review_source_tables(text,['part1','part2'],sources)
    assert result['status']=='unsupported' and result['reason']=='aggregate_child_quote_ambiguous'
