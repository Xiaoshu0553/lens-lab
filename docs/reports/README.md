# 社会调查报告及生成来源

本目录保存大学生研究范围的阶段报告和可再生成来源。适用人群为18–25岁在读专科生、本科生；三个年龄区间使用相同30题前测和相同校园共创任务，任务含四种私事实情景。两组真实模型实验的差别为额外的澄清系统提示，离线规则演示另行记录。

当前批次为 `student-126-20261022-v2`，126条大学生模拟记录用于验证展示、评分及导出流程。仅前测答案和决策生成与分组独立；人工对话另预设 A／S 每条记录 5／3 张线索，差异为展示假设，不能作为真实用户样本或干预效果证据。前测耗时和实测模型延迟留空，过程提醒从答案与对话派生且不自动排除。旧跨年龄展示库与600条来源记录另行归档，不包含在本目录，也不重标为大学生数据。报告中的文献分析和拟议支持机制不代表已经取得真实实验效果。

## 文件

- `挑战杯社会调查报告_阶段稿.docx`：可阅读的报告。
- `content_source.py`：正文来源，组合第四章、附录和参考文献生成 `content.json`。
- `chapter4.json`、`appendix.json`、`chinese_refs.json`、`foreign_refs.json`：分项内容。
- `build_report.py`：从 `content.json`、`figures/` 和 `toc_pages.json` 生成报告。
- `make_figures.py`、`figures/`：插图来源与已生成图片。
- `toc_pages.json`：本次排版经渲染核对的正文页码。封面不编号，摘要从第1页起。

## 重新生成

使用包含 `python-docx` 的 Python 环境，在任意工作目录运行：

```bash
python path/to/docs/reports/content_source.py
python path/to/docs/reports/build_report.py
```

默认输出在本目录。也可用 `--output /desired/path/report.docx` 指定目标。生成过程不读取原附件、个人聊天记录或工作区外的数据。插图已保存，无需再次生成；若要修改插图，需安装 Pillow 并运行 `make_figures.py`，可用 `REPORT_CJK_FONT` 指定中文字体文件。

正文或字体变化可能影响分页。每次改动后须重新渲染并逐页检查，将正文标题对应的显示页码写回 `toc_pages.json`，再构建并复核目录。当前报告保留原有版式及待填写的学校、团队、教师和研究联系信息；正式招募前须按实际安排补齐知情同意内容。
