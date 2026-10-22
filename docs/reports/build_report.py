from pathlib import Path
import argparse, json, re
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT, WD_TAB_LEADER
from docx.enum.section import WD_SECTION_START
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

HERE = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description='从同目录内容与插图重新生成阶段报告')
parser.add_argument('--output', type=Path, default=HERE / '挑战杯社会调查报告_阶段稿.docx')
args = parser.parse_args()
CONTENT = Path(__file__).with_name('content.json')
OUT = args.output.resolve()
data = json.loads(CONTENT.read_text())
page_cache=Path(__file__).with_name('toc_pages.json')
toc_pages=json.loads(page_cache.read_text()) if page_cache.exists() else {}
doc = Document()
sec = doc.sections[0]
sec.page_width, sec.page_height = Inches(8.5), Inches(11)
sec.top_margin, sec.bottom_margin = Inches(.9), Inches(.85)
sec.left_margin, sec.right_margin = Inches(1), Inches(1)
sec.header_distance, sec.footer_distance = Inches(.35), Inches(.4)

def font(style, size, east='Songti SC', bold=False):
    style.font.name = 'Times New Roman'
    style.font.size = Pt(size)
    style.font.bold = bold
    style.font.italic = False
    style.font.color.rgb = RGBColor(0,0,0)
    rf = style.element.get_or_add_rPr().get_or_add_rFonts()
    rf.set(qn('w:eastAsia'), east)
    rf.set(qn('w:ascii'), 'Times New Roman')
    rf.set(qn('w:hAnsi'), 'Times New Roman')
    for x in ('w:asciiTheme','w:hAnsiTheme','w:eastAsiaTheme'):
        rf.attrib.pop(qn(x),None)

font(doc.styles['Normal'],12)
normal = doc.styles['Normal'].paragraph_format
normal.line_spacing = 1.5
normal.space_after = Pt(3)
normal.first_line_indent = Pt(24)
normal.widow_control = True
for name,size in [('Title',22),('Subtitle',14),('Heading 1',16),('Heading 2',14),('Heading 3',12)]:
    s=doc.styles[name]
    font(s,size,'Heiti SC',name not in ('Subtitle',))
    p=s.paragraph_format
    p.first_line_indent=Pt(0)
    p.line_spacing=1.25
    p.space_before=Pt(15 if name=='Heading 1' else 10)
    p.space_after=Pt(8 if name=='Heading 1' else 5)
    p.keep_with_next=True
    if name=='Title':
        p.space_before=Pt(0);p.space_after=Pt(20)
    # Eliminate inherited theme paragraph borders.
    pp=s.element.find(qn('w:pPr'))
    if pp is not None:
        for x in list(pp):
            if x.tag==qn('w:pBdr'):pp.remove(x)
for name in ['Caption','Header','Footer']:
    font(doc.styles[name],10.5)
    doc.styles[name].paragraph_format.first_line_indent=Pt(0)
front=doc.styles.add_style('Front Heading',1)
font(front,16,'Heiti SC',True)
front.paragraph_format.first_line_indent=Pt(0)
front.paragraph_format.space_after=Pt(12)
front.paragraph_format.line_spacing=1.25

def para(text,style=None,center=False):
    text=re.sub(r'\[\d+(?:[-,]\d+)*\]',lambda m:m.group(0).replace('-','\u2011'),text)
    p=doc.add_paragraph(text,style)
    if center:p.alignment=WD_ALIGN_PARAGRAPH.CENTER;p.paragraph_format.first_line_indent=Pt(0)
    return p

p=para('挑战杯大学生课外学术科技作品竞赛',center=True)
p.paragraph_format.space_before=Pt(26)
p.paragraph_format.space_after=Pt(70)
p.runs[0].font.size=Pt(14)
para(data['title'],'Title',True)
para('社会调查报告','Subtitle',True)
p=para(data['subtitle'],center=True)
p.runs[0].font.size=Pt(14)
p.paragraph_format.space_before=Pt(18)
p.paragraph_format.space_after=Pt(50)
para('申报类别  哲学社会科学类社会调查报告',center=True)
para('学校与学院  [待填写]',center=True)
para('团队成员  [待填写]',center=True)
para('指导教师  [待填写]',center=True)
para('二〇二六年十月',center=True)

sec=doc.add_section(WD_SECTION_START.NEW_PAGE)
sec.footer.is_linked_to_previous=False
pg=OxmlElement('w:pgNumType');pg.set(qn('w:start'),'1');sec._sectPr.append(pg)
p=sec.footer.paragraphs[0];p.alignment=WD_ALIGN_PARAGRAPH.CENTER
field=OxmlElement('w:fldSimple');field.set(qn('w:instr'),'PAGE')
p._p.append(field)

para('摘 要','Front Heading',True)
for t in data['abstract']:para(t)
p=para('关键词  '+ '；'.join(data['keywords']))
p.paragraph_format.first_line_indent=Pt(0)
p.paragraph_format.space_before=Pt(12)

doc.add_page_break()
para('目 录','Front Heading',True)
toc_entries=[]
for s in data['sections']:
    toc_entries.append((s['title'],1))
    for b in s['blocks']:
        if isinstance(b,dict):
            if 'h2' in b:toc_entries.append((b['h2'],2))
            elif 'h3' in b:toc_entries.append((b['h3'],3))
toc_entries += [('参考文献',1),('附录 后续调查与编码工具',1)]
for b in data['appendix']:
    if isinstance(b,dict) and 'h2' in b:toc_entries.append((b['h2'],2))
toc_paragraphs=[]
for title,level in toc_entries:
    p=para(title+'\t'+str(toc_pages.get(title,1)))
    toc_paragraphs.append(p)
    p.paragraph_format.first_line_indent=Pt(0)
    p.paragraph_format.left_indent=Pt((level-1)*12)
    p.paragraph_format.line_spacing=1.15
    p.paragraph_format.space_after=Pt(3)
    p.paragraph_format.keep_with_next=False
    for r in p.runs:r.font.size=Pt(10.5);r.font.bold=level==1
    p.paragraph_format.tab_stops.add_tab_stop(Inches(6.35),WD_TAB_ALIGNMENT.RIGHT,WD_TAB_LEADER.DOTS)
# A native TOC field retains the verified display while allowing Word to update it.
start=OxmlElement('w:r');fc=OxmlElement('w:fldChar');fc.set(qn('w:fldCharType'),'begin');fc.set(qn('w:dirty'),'false');start.append(fc)
inst=OxmlElement('w:r');it=OxmlElement('w:instrText');it.set(qn('xml:space'),'preserve');it.text=' TOC \\o "1-3" \\h \\z \\u ';inst.append(it)
sep=OxmlElement('w:r');fc=OxmlElement('w:fldChar');fc.set(qn('w:fldCharType'),'separate');sep.append(fc)
for i,e in enumerate([start,inst,sep],1):toc_paragraphs[0]._p.insert(i,e)
end=OxmlElement('w:r');fc=OxmlElement('w:fldChar');fc.set(qn('w:fldCharType'),'end');end.append(fc);toc_paragraphs[-1]._p.append(end)
doc.add_page_break()

def table(spec):
    para(spec['caption'],'Caption',True).paragraph_format.keep_with_next=True
    t=doc.add_table(rows=1, cols=len(spec['headers']))
    t.alignment=WD_TABLE_ALIGNMENT.CENTER;t.autofit=False
    for c,w in zip(t.columns,spec['widths']):c.width=Inches(w)
    for i,txt in enumerate(spec['headers']):t.rows[0].cells[i].text=txt
    rep=OxmlElement('w:tblHeader');t.rows[0]._tr.get_or_add_trPr().append(rep)
    for row in spec['rows']:
        cells=t.add_row().cells
        for i,txt in enumerate(row):cells[i].text=txt
    pr=t._tbl.tblPr
    borders=OxmlElement('w:tblBorders')
    for side in ['top','left','bottom','right','insideH','insideV']:
        e=OxmlElement('w:'+side);e.set(qn('w:val'),'single');e.set(qn('w:sz'),'4');e.set(qn('w:color'),'D9D9D9');borders.append(e)
    pr.append(borders)
    for ri,row in enumerate(t.rows):
        nr=OxmlElement('w:cantSplit');row._tr.get_or_add_trPr().append(nr)
        for ci,c in enumerate(row.cells):
            c.width=Inches(spec['widths'][ci]);c.vertical_alignment=WD_CELL_VERTICAL_ALIGNMENT.CENTER
            cp=c._tc.get_or_add_tcPr()
            margins=OxmlElement('w:tcMar')
            for side,val in [('top','90'),('bottom','90'),('left','100'),('right','100')]:
                e=OxmlElement('w:'+side);e.set(qn('w:w'),val);e.set(qn('w:type'),'dxa');margins.append(e)
            cp.append(margins)
            if ri==0:
                shade=OxmlElement('w:shd');shade.set(qn('w:fill'),'EEEEEE');cp.append(shade)
            for p in c.paragraphs:
                p.paragraph_format.first_line_indent=Pt(0);p.paragraph_format.space_after=Pt(0);p.paragraph_format.line_spacing=1.2
                if ci==0:p.alignment=WD_ALIGN_PARAGRAPH.CENTER
                for r in p.runs:r.font.size=Pt(10.5);r.font.bold=ri==0
    p=para(spec.get('note',''))
    p.paragraph_format.first_line_indent=Pt(0);p.paragraph_format.space_after=Pt(6)
    for r in p.runs:r.font.size=Pt(10)

for s in data['sections']:
    para(s['title'],'Heading 1')
    for b in s['blocks']:
        if isinstance(b,str):para(b)
        elif 'h2' in b:para(b['h2'],'Heading 2')
        elif 'h3' in b:para(b['h3'],'Heading 3')
        elif 'table' in b:table(b['table'])
        elif 'figure' in b:
            p=para('',center=True)
            p.paragraph_format.keep_with_next=True
            p.add_run().add_picture(str(HERE / b['figure']['file']),width=Inches(6.3))
            para(b['figure']['caption'],'Caption',True)

para('参考文献','Heading 1')
for i,ref in enumerate(data['references'],1):
    p=para(f'[{i}] {ref}')
    p.paragraph_format.first_line_indent=Pt(-20)
    p.paragraph_format.left_indent=Pt(20)
    p.paragraph_format.line_spacing=1.25
    p.paragraph_format.space_after=Pt(6)
    p.paragraph_format.keep_together=True
    for r in p.runs:r.font.size=Pt(10.5)

p=para('附录 后续调查与编码工具','Heading 1')
p.paragraph_format.page_break_before=True
for b in data['appendix']:
    if isinstance(b,str):
        for part in filter(None,re.split(r'(?=[ABD]\d+\s)',b)):
            p=para(part)
            p.paragraph_format.line_spacing=Pt(18)
            p.paragraph_format.space_after=Pt(5)
            p.paragraph_format.keep_together=True
            p.paragraph_format.first_line_indent=Pt(0 if re.match(r'[ABD]\d+',part) else 22)
            for r in p.runs:r.font.size=Pt(11)
    elif 'h2' in b:
        p=para(b['h2'],'Heading 2')
        for r in p.runs:r.font.size=Pt(12)
    elif 'table' in b:table(b['table'])

doc.core_properties.title=data['title'].replace('\n','')
doc.core_properties.subject='社会调查报告'
doc.core_properties.author=''
doc.core_properties.keywords='生成式人工智能 信息公平性 信息素养 主动澄清'
OUT.parent.mkdir(parents=True,exist_ok=True)
doc.save(OUT)
alltext='\n'.join(p.text for p in doc.paragraphs)+'\n'+'\n'.join(c.text for t in doc.tables for row in t.rows for c in row.cells)
bodytexts=[]
for s in data['sections']:
    bodytexts += [s['title']]+[b if isinstance(b,str) else b.get('h2',b.get('h3','')) for b in s['blocks']]
body='\n'.join(bodytexts)
def wordunit(t):return len(re.findall(r'[\u4e00-\u9fff]|[A-Za-z0-9]+(?:[-_./:][A-Za-z0-9]+)*',t))
counts={'all_non_whitespace_characters':len(re.sub(r'\s','',alltext)), 'chinese_characters':len(re.findall(r'[\u4e00-\u9fff]',alltext)), 'word_units_all':wordunit(alltext),'body_non_whitespace_characters':len(re.sub(r'\s','',body)),'body_chinese_characters':len(re.findall(r'[\u4e00-\u9fff]',body)),'body_word_units':wordunit(body),'abstract_non_whitespace_characters':len(re.sub(r'\s','', ''.join(data['abstract']))),'references':len(data['references']),'output':OUT.name}
Path(__file__).with_name('counts.json').write_text(json.dumps(counts,ensure_ascii=False,indent=2))
print(json.dumps(counts,ensure_ascii=False))
