# 翻译完整性审计

- 结论：`NEEDS_REPAIR`
- 返修页：1, 2
- 需核对页：1, 2
- 全文译源字量比中位数：None

| 页码 | 风险 | 译源字量比 | 句子保留比 | 图表声明问题 |
|---:|---|---:|---:|---|
| 1 | DOI_LOSS, LOW_SENTENCE_RETENTION, SEVERE_TRANSLATION_COMPRESSION, STATISTICAL_ANCHOR_LOSS, URL_LOSS | 0.0 | 0.0 | - |
| 2 | LOW_SENTENCE_RETENTION, SEVERE_TRANSLATION_COMPRESSION | 0.0 | 0.0 | - |

该脚本把文字提取、阅读顺序、译文完整性和图表重建分开检查。READY 仍不等于语义完全正确；NEEDS_REPAIR 是下一轮制作输入，不是任务终止状态。
