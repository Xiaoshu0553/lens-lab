from pathlib import Path
import os
from PIL import Image, ImageDraw, ImageFont
P=Path(__file__).parent/'figures'
P.mkdir(exist_ok=True)
font_candidates = [os.environ.get('REPORT_CJK_FONT', ''),
                   '/System/Library/Fonts/STHeiti Medium.ttc',
                   '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
                   '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc']
fontpath = next((path for path in font_candidates if path and Path(path).is_file()), None)
if fontpath is None:
    raise SystemExit('请通过 REPORT_CJK_FONT 指定可用中文字体，或直接使用仓库已保存的插图。')
def f(n):return ImageFont.truetype(fontpath,n)
def center(d,xy,text,size=37):
    x,y=xy;d.multiline_text((x,y),text,font=f(size),fill='black',anchor='mm',align='center',spacing=12)
def arrow(d,x1,y1,x2,y2):
    d.line((x1,y1,x2,y2),fill='#444444',width=4)
    if x1==x2:d.polygon([(x2,y2),(x2-10,y2-18),(x2+10,y2-18)],fill='#444444')
    else:d.polygon([(x2,y2),(x2-18,y2-10),(x2-18,y2+10)],fill='#444444')
def box(d,rect,txt,size=36):
    d.rounded_rectangle(rect,radius=8,outline='#666666',fill='#F6F6F6',width=3)
    center(d,((rect[0]+rect[2])/2,(rect[1]+rect[3])/2),txt,size)

im=Image.new('RGB',(1890,610),'white');d=ImageDraw.Draw(im)
box(d,(590,10,1300,115),'交互条件  普通问答  主动澄清',34)
arrow(d,945,120,945,215)
for rect,txt in [((15,225,535,420),'用户特征\nAI信息素养  表达  经验'),((685,225,1205,420),'交互过程\n理解  回应  核验  整合'),((1355,225,1875,420),'任务结果\n决策质量  完成与退出')]:box(d,rect,txt,35)
arrow(d,540,322,680,322);arrow(d,1210,322,1350,322)
center(d,(945,515),'共同评价  整体效果  群体差异  时间成本  主观负担',36)
im.save(P/'framework.png')

im=Image.new('RGB',(1890,480),'white');d=ImageDraw.Draw(im)
for x,txt in [(10,'理解追问'),(485,'补充事实'),(960,'评价反馈'),(1435,'作出决策')]:box(d,(x,35,x+440,180),txt,43)
for x in [450,925,1400]:arrow(d,x+5,108,x+30,108)
center(d,(945,270),'观察重点  题意是否清楚  事实是否补齐  规则是否正确  依据是否整合',31)
center(d,(945,385),'修复方式  改述与解释  保留不确定性  核查依据  提供可选支持',33)
im.save(P/'mechanism.png')
