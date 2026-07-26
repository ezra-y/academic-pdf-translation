---
name: academic-pdf-translation
description: 将学术 PDF 翻译并重建为可读、可检索、可逐页验收的目标语言版本。用于全文翻译、旧译本修复、版式与行距审查、图表和截图文字本地化、语义忠实度复核、批量文献画像、源译对照图生成及 Zotero 收尾。简体中文流程已通过代表样本验证；繁体中文、日语、韩语及拉丁字母语言提供实验性配置，须先验收代表页。
license: MIT
---

# PDF 翻译

原文是证据原件，译文服务于阅读、引用和检索。主流程只有四步：

1. 检查全篇并选择路线；
2. 完成全文翻译；
3. 一次生成并注册首版 PDF；
4. 按用户档位一次审查、集中返修并收尾。

不要把自动检查、风险报告和制作人自检拆成多轮人工审查。自动脚本负责找技术
问题；审查智能体只完整读取一次源译对照图，汇总问题后一次性交给制作智能体。

## 开始前

用户没有选择质量档位时，只问一次：

> 这次选择哪种质量档位？
> - 快速：基础检查通过即可，速度最快。
> - 平衡（推荐）：完整看一遍源译对照，集中修改一次。
> - 精细：同样只完整看一遍，返修后更细地核对关键内容和受影响页面。

对应参数为 `fast`、`balanced`、`precise`。批量任务整批只问一次。

执行脚本时，若 Skill 根目录存在 `.venv`，优先使用其中的 Python；否则使用
系统 `python3`。同一任务不要混用两个 Python 环境。

除非用户明确指定现有作业，新请求先在 Skill 内部 `Workspace/` 创建一个批次。
同一请求的全部 PDF 共用该批次，标题概括用户任务，数量由输入文件自动计算：

```bash
python3 scripts/workspace.py create \
  --title "本批次标题" \
  /path/to/paper-01.pdf \
  /path/to/paper-02.pdf
```

继续工作时使用命令返回的批次路径。用户只从 `input/` 取原文、从 `output/`
取正式译本；所有过程文件位于隐藏的 `.work/`。详细目录和最终回复格式只以
[workspace.md](references/workspace.md) 为准。

开始前按任务读取：

- 工作区与文件去向：[workspace.md](references/workspace.md)
- 交付判断：[quality-contract.md](references/quality-contract.md)
- 路线选择：[routing.md](references/routing.md)
- 排版与行距：[layout-readability.md](references/layout-readability.md)
- 翻译范围：[translation-scope.md](references/translation-scope.md)
- 高风险语义：[semantic-review.md](references/semantic-review.md)
- 非简体中文：[language-profiles.md](references/language-profiles.md)
- 发布或比较通过率：[validation.md](references/validation.md)

## 不可跳过

1. 不覆盖原文，不直接替换已验收译本。
2. 不调用 LM Studio、`lms`、Ollama、本地或小语言模型，也不把论文发送给
   第三方机器翻译 API。
3. 除白名单外，标题、正文、图表、图注、脚注、附录、界面和声明全部翻译。
4. 否定、不确定性、证据强度、样本边界、数字、引文和因果边界必须保留。
5. 译文正文只表达作者原意；项目判断和证据评价写入独立笔记。
6. 可读性优先于机械复刻碎文本框；不得靠缩字、压行距、收窄版心或制造异常
   段距塞入页面。
7. 全篇正文使用统一字号和行距。按实际译文字量试排，不逐页缩放。
8. 自动检查始终执行；完整人工审查次数只服从用户选择。
9. 同一轮问题先汇总再集中修改，不为单个箭头、标点或元数据反复注册版本。
10. 生产脚本不得写死作者、标题、期刊、量表、固定页码、翻译单元 ID 或本机
    路径；所有论文内容从作业 JSON 读取，样例常量只允许出现在自测夹具中。

## 第一步：全篇检查

初始化独立作业：

```bash
python3 scripts/init_job.py \
  /path/to/Workspace/<批次>/input/source.pdf \
  --workspace /path/to/Workspace/<批次> \
  --target-language zh-Hans \
  --review balanced \
  --producer-id producer-agent-01
```

初始化会先按原文 SHA-256 搜索本批次 `.work/jobs/` 中全部 `job.json`；存在
同一原文时恢复已有作业，不再创建第二份。恢复旧项目时仍可直接传入
`/path/to/job --job-root /path/to/all-jobs`。

初始化会自动生成：

- `source_structure.json`：PDF 自带文字顺序、坐标推断顺序、文字块坐标、
  栏位、图片和矢量图信号；
- `source_units.json`：按段落或语义区域拆分并冻结的原文单元；
- `translation.json`：与每个冻结单元一一绑定的待翻译骨架。

两种阅读顺序冲突、缺少文本层或存在图表时，只标记需要看图的页，不擅自宣称
提取顺序正确。不得删除冻结单元，也不得把多个单元重新合成“整页原文加一段
中文摘要”。

按原尺寸检查脚本标出的页面，并扫视其余原文页，先回答一个问题：

> 这页能否用普通正文生成器完整、顺序正确、结构清楚地重建？

不能就登记为复杂页。复杂内容不限于表格，还包括量表、表单、模型图、流程图、
公式、承担研究信息的截图或界面、扫描页、多栏错序、横向页、混合尺寸和其他
特殊结构。照片、装饰图和不依赖内部文字理解的截图保持原图，只翻译图注，
不因“图片里有字”自动升级为复杂翻译页。

确认没有复杂页：

```bash
python3 scripts/set_complex_content.py /path/to/job \
  --none \
  --notes "已按原尺寸检查全部原文页，均适合普通正文路线。"
```

登记复杂页：

```bash
python3 scripts/set_complex_content.py /path/to/job \
  --page "6,structured-table,structured-table-rebuild,统计表需保持行列关系" \
  --page "8,figure-with-text,vector-rebuild,模型图需保持标签和箭头关系"
```

命令会同时生成 `complex_content.json` 模板。复杂页翻译前必须把原页结构写成
机器可检查的载荷：

- 表格：表题、行数、列数、单元格和表注；
- 模型图：图、标签、节点及边/连线；
- 需要本地化的截图、统计图或 OCR：区域、显示尺寸和承担信息的区域文字；
- 阅读顺序重建：按顺序排列的原文块 ID。

填写完成后：

```bash
python3 scripts/set_complex_payload.py /path/to/job \
  --page 8 \
  --payload /path/to/page-8-figure-data.json \
  --source-evidence "已按原尺寸核对全部节点、箭头、标签和数值" \
  --ready
```

存在任一复杂页时，整篇路线最低为 `hybrid-complex-pages`；复杂页多或结构贯穿
全文时使用 `custom-layout`。具体类型见 [routing.md](references/routing.md)。

跨页表格、跨页模型或其他跨页复杂结构必须在同一载荷中列出全部
`source_pages` 和逐页 `source_bboxes`。生成器按这些坐标替代原始碎片，
每项复杂结构独立计算覆盖范围；夹在两项复杂结构之间的普通正文必须保留。
四象限图、坐标图等载荷还必须列出轴名称、正负方向、刻度或类别，不能只写
象限标题和说明文字。

## 第二步：全文翻译

逐项填写初始化生成的 `translation.json`，只修改 `translation`、
`keep_source_reason` 和需要的 `review_flags`：

```json
{
  "id": "p0003-u0012",
  "source_ref": "p0003-u0012",
  "page": 3,
  "kind": "body",
  "source": "Original paragraph...",
  "source_bbox": [57.1, 220.4, 291.0, 338.7],
  "translation": "目标语言译文……",
  "keep_source_reason": null,
  "review_flags": []
}
```

要求：

- 保持整篇上下文和统一术语，不按孤立字符或碎 span 翻译；
- 翻译前确认 `translation.terminology`，即使为空也把
  `terminology_reviewed` 设为 `true`；
- 不修改 `source_ref`、`source`、`page` 或 `source_bbox`；
- 每个冻结原文单元必须恰好出现一次，不得遗漏、重复或合并；
- 跨栏和跨页续句只翻译一次；
- 完整更新 `translation.json.coverage`；
- 参考文献等保留原文区域写入 `retained_source.json`；
- 同页存在分栏参考文献时，每个逻辑栏单独登记区域；保留区按左栏到右栏、
  栏内从上到下排版，并排除页眉、页脚和孤立页码；
- 图表、截图和复杂页状态写入 `figure_inventory.json`，并为每项选择
  `translate-embedded-text`、`translate-caption-only`、
  `preserve-original` 或 `omit-nonsemantic`；
- 高风险语句使用 [semantic-review.md](references/semantic-review.md) 的稳定标记。

超过 10 页时，每约 5 页保存一次可恢复检查点：

```bash
python3 scripts/record_work_checkpoint.py /path/to/job \
  --completed-through 5 \
  --phase translation \
  --note "第1至5页已完整翻译并落盘。"
```

完成后：

```bash
python3 scripts/audit_translation_completeness.py /path/to/job
python3 scripts/validate_job.py /path/to/job --stage translated --advance
```

候选尚未生成时，完整性审计先检查正文是否异常压缩、数字和统计量是否丢失、
章节是否错配，不检查尚不存在的图表重建结果。

若返回 `NEEDS_REPAIR`，读取 `reviews/repair-plan.json`，集中完成全部返修任务
后重新运行本步骤。`NEEDS_REPAIR` 是下一轮制作输入，不是停止工作或向用户交付
失败报告的条件。

## 第三步：生成首版

普通正文按实际译文字量用 `typography_fit.py` 计算全篇统一字号，并优先复用
`reportlab_layout.py`。复杂页按第一步登记的方法单独重建。

排版器先完整试排到内存，读取 `translation.json` 和
`complex_content.json`，不得把表格、图、标题或数值重新写死在论文专属代码
里。试排后写入 `generator-layout-log.json.render_contract`，证明：

- 每个冻结译文单元都被消费；
- 所有标题、正文、脚注、声明、图注和复杂页文字区域都已测量；
- 图题或表题与首个图表结构保持同页，不产生孤立标题；
- 没有溢出和单字孤行；
- 实际字体与 `job.json` 一致；
- 目标语言需要时，断行禁则已经启用。

段距检查按页面全部可见内容判断。参考文献、图表或其他非正文内容占据两段之间
的空间时，不得把该区域误判为机械拉大的空白。

连续出现多张结构化表格时，让标题、表头、数据行和表注按剩余空间自然跨页，
只保证标题与表头或首行同页。不得为每张表机械插入新页，造成只剩一两行表注
或大面积空白的候选页。

单幅图片与图注作为一个阅读单元排版。多图并排时，由图组的行列容器保持每幅
图片与自身图注的关系，不在单元格内再嵌套无界高度的整组绑定；图组必须经过
实际页面高度试排。并排图组高于完整版心时，自动改为可分页纵排，并让图内
文字对照使用完整行宽，不通过缩字或删除标签维持并排。

首版只调用一次统一入口：

```bash
python3 scripts/build_first_candidate.py /path/to/job
```

入口先完成候选试排和排版合同，再刷新一次总检查，最后运行唯一一次注册前
预检。中途失败不会注册候选。默认输出到
`staging/candidate-first.pdf`；集中返修时使用：

```bash
python3 scripts/build_first_candidate.py /path/to/job \
  --attempt-label repair
```

预检临时副本必须先进入 `candidate` 状态，再自动运行完整性审计，并检查
`figure_inventory.json` 声称的表格、模型图或截图是否真的以网格、轴线、
连线、矢量或图像结构出现在候选中。只有文字层存在、但正文被写成摘要，
把图表缩成几句说明，或遗漏坐标轴和方向，会返回 `NEEDS_REPAIR` 和逐页任务。
候选译文比对先按坐标去除固定页眉和页脚孤立页码，再拼接映射到同一单元的
候选页；正文中的样本量、年份和统计数字必须保留。

返回结果：

1. `READY_TO_REGISTER`：进入注册；
2. 第一次 `NEEDS_REPAIR`：一次性汇总全部问题，只集中返修一次；重新运行
   导出前总检查并再生成一次；
3. 第二次仍失败：返回 `GENERATOR_FIX_REQUIRED`。修复排版器共性问题并产生
   新的代码构建哈希，不得继续对单篇 PDF 逐项返修。

同一候选页面指纹重复检查不增加次数。统一生成器按实际代码构建哈希计数，
手改显示版本号不会重置次数；同一构建最多检查首版和集中返修版。

预检通过后只注册一次首版：

```bash
python3 scripts/register_candidate.py \
  /path/to/job \
  /path/to/generated.pdf \
  --renderer academic-pdf-layout \
  --renderer-version <build-report-version> \
  --renderer-build-id <build-report-hash> \
  --notes "首版；复杂页已专用重建"

python3 scripts/qa_pdf.py /path/to/job
python3 scripts/validate_job.py /path/to/job --stage candidate --advance
python3 scripts/review_risk_report.py /path/to/job \
  --output-json /path/to/job/reviews/risk-report.json \
  --output-md /path/to/job/reviews/risk-report.md
python3 scripts/audit_translation_completeness.py /path/to/job
```

自动检查负责页数、尺寸、文本层、字体、字号、行距、越界、乱码、留白、原文
残留和检索风险。它不是第二次人工审查。门槛见
[qa-acceptance.md](references/qa-acceptance.md)。

统一生成器登记的正文和参考文献字号属于候选证据。字号一致性检查以该锁定值
为准，并排除参考文献、脚注和正式出版元数据的专用字号；不得用整篇字符多数
反推正文字号。最后一页内容完整且沿用全篇锁定字号时允许自然收尾留白。

## 第四步：一次审查与收尾

### 快速

基础检查和图表清单通过后直接验收：

```bash
python3 scripts/validate_job.py /path/to/job --stage accepted --advance
```

### 平衡（推荐）

一条命令生成供审查智能体读取的材料：

```bash
python3 scripts/make_review_sheet.py /path/to/job
```

默认每张审查图包含两组左右对照页，并按 PDF 顺序覆盖全文。脚本只生成审查
图包、索引和一份对照 PDF；原文和候选哈希不变时直接复用缓存。

审查智能体一次完成：

1. 依次读取 `comparisons/sheets/` 的全部审查图；
2. 对照原文和译文检查信息、遗漏、数字、图表、阅读顺序和可读性；
3. 把全部问题一次性写入 `reviews/independent.json`；
4. 不发现一个问题就退回，不重复生成对照材料。

只有疑点页需要放大时才生成高清图：

```bash
python3 scripts/make_review_sheet.py /path/to/job \
  --detail-pages "3,7-9"
```

复审完成后立即记录：

```bash
python3 scripts/record_review_round.py /path/to/job
```

若结果为 PASS，直接进入 accepted。若结果为 FAIL，制作智能体按完整问题清单
集中返修一次并注册第二版，随后运行全篇自动回归、重新生成对照图，只目视
检查改动页、相邻页和同类受影响页。确认通过后记录：

```bash
python3 scripts/record_post_repair_confirmation.py /path/to/job \
  --reviewer-id reviewer-agent-01 \
  --changed-pages "3,7-9" \
  --same-type-pages "12"
```

### 精细

与平衡档相同，也只有一轮全文审查和一次集中返修。区别是返修后额外定向核对
量表计分、关键统计、核心定义、图表及所有受影响页面。

精细档的返修确认还需为统计、核心定义、量表和图表逐项提供
`--key-check category=PASS:核对依据`。最后运行：

```bash
python3 scripts/validate_job.py /path/to/job --stage accepted --advance
```

初始化时的 `producer_id` 与复审记录中的 `reviewer_id` 必须不同；独立审查者
不能参与当前候选的翻译或排版。

## 正式输出与 Zotero

只在 `accepted` 后：

1. 按 [workspace.md](references/workspace.md) 把唯一正式译本写入当前批次
   `output/`，并记录绝对路径和哈希；
2. 原文和译文挂到同一 Zotero 父条目；
3. 分别读回一条原文和译文检索锚点；
4. 把正式路径、哈希和 Zotero 键写入 `finalization.json`；
5. 运行：

```bash
python3 scripts/validate_job.py /path/to/job --stage finalized --advance
```

Zotero 只保留题录、原文、正式译本和用户明确要求的研究笔记，不导入检查报告。
Zotero 操作见 [zotero-finalization.md](references/zotero-finalization.md)。

回复用户前运行 `workspace.py outputs <批次路径>`，逐篇交付正式 PDF 的可点击
文件名和绝对路径。不要交付 `.work/` 内的候选或检查材料。

## 自检

修改 Skill 后运行：

```bash
python3 scripts/check_bundle.py
python3 scripts/self_test.py
```

改变候选页面组成时更新稳定发布号；预检身份以代码构建哈希为准。批量发布前，
至少选择 5 篇覆盖
普通正文、结构化表格或模型图、照片、混合正文与参考文献的代表样本；每篇都
必须在该版本第一次预检时返回 `READY_TO_REGISTER`。失败时修复共性输入或
生成逻辑，不通过降低门槛或增加单篇例外放行。

准备一份含 `cases[].id`、`cases[].job_dir` 和可选 `tags` 的 JSON 清单后，
可在隔离副本中复跑基准：

```bash
python3 scripts/benchmark_corpus.py /path/to/benchmark.json \
  --output /path/to/benchmark-report.json
```

默认串行，避免 PDF 渲染争用内存和 CPU；需要时显式传 `--workers`。基准报告
的首版通过率只代表自动门禁，视觉抽查仍单独执行。

时间和 token 只从 `run-metrics.json` 统计。候选流水线会自动记时；翻译和
复审阶段可补记：

```bash
python3 scripts/run_metrics.py record /path/to/job \
  --stage translation --status complete --elapsed-seconds 900 \
  --model <model> --input-tokens 10000 --output-tokens 18000
python3 scripts/run_metrics.py summarize /path/to/job-root
```

失败候选和历史证据保留在作业目录；正式译本只在验收后产生。数据接口和扩展点
见 [architecture.md](references/architecture.md)。

验收完成后可先预览再清理旧 staging 产物：

```bash
python3 scripts/prune_staging.py /path/to/job
python3 scripts/prune_staging.py /path/to/job --apply
```
