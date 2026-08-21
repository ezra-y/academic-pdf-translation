# 性能优化评审与执行计划

- 状态：P0～P3、P3 欠账、P4 第一步已完成；P4 其余部分与 P5、P6 未做。见 [执行结果](performance-results.md)
- 记录日期：2026-08-21
- 评审对象：`main` 分支当前源码
- 评审方式：静态源码审查，未使用真实论文完整跑通端到端流程
- 重要声明：本文中的所有提速比例都是**验收目标**，不是已经测出的结果

---

## 一、总体判断

这个项目能明显提速，而且不用砍掉质量检查。

- **流程设计：比较干净。**
  四步主链路、三档质量、状态文件、哈希和自动检查都写得很清楚。运行依赖也只有
  PyMuPDF、Pillow、ReportLab 三个，JSON 写入使用临时文件替换，基础可靠性不错。
  （见 `SKILL.md`）
- **代码实现：已经偏重。**
  `build_candidate.py` 达到 6176 行，`qa_pdf.py` 达到 2607 行。排版、复杂图表、
  映射、字体搜索、PDF 输出、检查逻辑过度集中，继续加功能会越来越难维护。
  （见 `scripts/build_candidate.py`）
- **性能设计：有明显重复工作。**
  目前的慢，不只是模型翻译慢。初始化、排版试算、预检、QA 都存在重复读取和重复计算。

一句话评价：

> 流程层清楚，实现层过重；质量门禁做得好，但主链路做了太多重复工作。

---

## 二、当前最可能的四个耗时点

### 1. 翻译单元太适合检查，不适合直接执行

原文会被拆成最多约 900 字符的冻结单元，Skill 又要求逐项填写 `translation.json`。
这种小单元适合定位遗漏，却容易让 Agent 反复读取文件、写文件、恢复上下文。
（见 `scripts/prepare_translation_units.py`）

正确做法是分成两层：

- **检查层：** 继续保留现在的小单元和稳定 ID。
- **执行层：** 一次把多个小单元组成翻译批次。

不要简单把 `max_chars` 从 900 改成 1600。那只会让检查粒度变粗，不会真正解决
上下文和重复操作问题。

### 2. 排版字号搜索会多次完整生成 PDF

`build_candidate.py` 会组合多个字号和行距候选。每个候选都会：

1. 重新建立整份 Story；
2. 完整生成一份临时 PDF；
3. 再用 PyMuPDF 打开；
4. 读取页数；
5. 不合格就继续生成下一份。

也就是说，一篇论文可能在得到正式候选前，已经完整生成了多遍。
（见 `scripts/build_candidate.py`）

仓库里已经有独立的 `typography_fit.py`，文档也要求使用它，但统一生成器内部又实现了
一套候选搜索逻辑。这里存在重复设计，也容易发生文档和真实代码不一致。
（见 `scripts/typography_fit.py`）

### 3. 预检会复制作业，再重复分析候选 PDF

`preflight_candidate.py` 会复制整个作业到临时目录，然后依次运行：

- 注册候选；
- 完整 QA；
- 状态校验；
- 翻译完整性审计。

候选 PDF 还会额外执行一次全文内容指纹计算。QA 和完整性审计随后又会分别打开 PDF，
重新抽取文字、字体、图片和绘图对象。（见 `scripts/preflight_candidate.py`）

这些检查本身有必要。问题是：**同一份 PDF 被不同模块重复解析。**

### 4. 初始化阶段重复扫描原文

`init_job.py` 先计算一次原文哈希，然后依次调用：

- `profile_pdf()`
- `extract_source_structure()`

这两个流程都会打开原文，逐页读取文字、图片、绘图和页面信息，而且各自再次计算原文
哈希。结构提取还会先扫描一次页眉页脚，再扫描一次完整页面。
（见 `scripts/init_job.py`）

这部分不是最大瓶颈，但属于非常明确的低风险优化项。

---

## 三、执行计划

以下计划可以直接交给 Codex 或 Claude Code。

### 总体约束

- 不降低现有 QA、完整性和语义检查门槛。
- 不改变现有公开 CLI 命令。
- 尽量保持现有 JSON Schema 向后兼容。
- 每个阶段单独提交，禁止堆积大量修改后只做一个 commit。
- 每次性能修改必须同时提供修改前和修改后的数据。
- 先消除重复工作，再考虑并发。
- 不对 PyMuPDF 代码直接套大范围线程池。
- 所有缓存必须包含版本号和输入哈希，不能因为缓存返回旧结果。

---

## P0：建立真实性能基线

这是第一步。现在已经能判断哪里重复，但还不能确定每个阶段实际占比。

现有 `build_first_candidate.py` 已经记录了 `build`、`pre_render_audit`、`preflight`
三段时间；`run-metrics.json` 也已经是正式统计入口。
（见 `scripts/build_first_candidate.py`）

### Checklist

- [ ] 新建 `benchmarks/corpus.json`。
- [ ] 准备至少 5 篇代表论文：
  - [ ] 普通单栏正文；
  - [ ] 双栏正文；
  - [ ] 结构化表格或模型图；
  - [ ] 图片较多；
  - [ ] 参考文献较多。
- [ ] 每篇分别记录 PDF 页数、文件大小、冻结单元数、复杂页数。
- [ ] 为以下步骤增加独立计时：
  - [ ] `source_hash`
  - [ ] `source_profile`
  - [ ] `source_structure`
  - [ ] `prepare_translation_units`
  - [ ] `translation`
  - [ ] `retained_region_extract`
  - [ ] `typography_estimate`
  - [ ] 每次 `render_attempt`
  - [ ] `candidate_fingerprint`
  - [ ] `qa_candidate_analysis`
  - [ ] `qa_rules`
  - [ ] `completeness_audit`
  - [ ] `review_sheet`
- [ ] 记录以下计数：
  - [ ] 原文 PDF 打开次数；
  - [ ] 候选 PDF 打开次数；
  - [ ] `get_text("dict")` 调用次数；
  - [ ] `get_drawings()` 调用次数；
  - [ ] 完整排版尝试次数；
  - [ ] 翻译批次数；
  - [ ] 缓存命中次数。
- [ ] 每篇冷启动运行 3 次。
- [ ] 每篇缓存状态下运行 3 次。
- [ ] 输出 `benchmarks/results/baseline.json`。
- [ ] 输出一份按阶段排序的耗时报告。
- [ ] 修改前运行：

```bash
./.venv/bin/python scripts/check_bundle.py
./.venv/bin/python scripts/self_test.py
./.venv/bin/python scripts/benchmark_corpus.py \
  benchmarks/corpus.json \
  --output benchmarks/results/baseline.json
```

### 验收标准

- 每篇论文都能看到完整阶段耗时。
- 可以明确回答：时间主要花在翻译、排版、预检还是审查。
- 基线中包含每次排版尝试耗时。
- 基线报告纳入仓库，但不提交受版权保护的原论文。

---

## P1：先做低风险提速

### P1.1 把输入检查移到渲染之前

现在的顺序是先运行 `build_candidate()`，再运行所谓的 `pre_render_audit`。因此，
译文数据、图表清单或复杂内容没有准备好时，也可能先浪费时间生成 PDF。
（见 `scripts/build_first_candidate.py`）

**修改范围**

- `scripts/build_first_candidate.py`
- `scripts/pre_render_audit.py`
- `scripts/validate_job.py`
- `scripts/audit_translation_completeness.py`

**Checklist**

- [ ] 将当前审计拆成两个函数：
  - [ ] `build_input_readiness_audit()`
  - [ ] `build_render_contract_audit()`
- [ ] 在调用 `build_candidate()` 前运行输入就绪检查。
- [ ] 输入检查只检查：
  - [ ] 翻译单元是否完整；
  - [ ] `terminology_reviewed` 是否完成；
  - [ ] 图表清单是否完成；
  - [ ] 复杂页载荷是否 ready；
  - [ ] 字体文件是否存在；
  - [ ] 保留原文区域是否合法；
  - [ ] 作业状态是否允许生成。
- [ ] 候选生成后再检查：
  - [ ] 单元是否全部被消费；
  - [ ] 是否溢出；
  - [ ] 标题位置；
  - [ ] 孤行；
  - [ ] 字体实际使用情况；
  - [ ] CJK 禁则；
  - [ ] 页面映射。
- [ ] 保留原有错误码，避免外部调用失效。

**验收标准**

- 输入不完整时，不生成任何临时候选 PDF。
- 同一错误在修改前后返回相同或更明确的错误码。
- 合法作业的输出不发生变化。

### P1.2 合并原文扫描

新增建议：

```text
scripts/source_analysis.py
```

它负责一次打开 PDF，生成统一的 `SourceAnalysis`。

**Checklist**

- [ ] 原文 SHA-256 只计算一次。
- [ ] 原文 PDF 只打开一次。
- [ ] 每页只执行一次 `get_text("dict")`。
- [ ] 从同一份页面结果派生：
  - [ ] `source_manifest.json`
  - [ ] `source_structure.json`
  - [ ] `source_units.json`
  - [ ] 语言估计；
  - [ ] 图片信号；
  - [ ] 矢量图信号；
  - [ ] 双栏信号；
  - [ ] 页眉页脚候选；
  - [ ] 复杂页候选。
- [ ] `pdf_profile.py` 改为薄包装，只负责输出兼容格式。
- [ ] `extract_source_structure.py` 改为复用同一分析结果。
- [ ] `init_job.py` 将已计算的哈希传入分析函数。
- [ ] 新增：

```text
source-analysis.json
```

- [ ] 缓存键包含：
  - [ ] 原文 SHA-256；
  - [ ] 分析 Schema 版本；
  - [ ] PyMuPDF 主版本；
  - [ ] 分析器构建版本。

**验收标准**

- 每篇原文只发生一次完整页面解析。
- 旧版与新版冻结单元 ID、文字、页码和坐标保持一致。
- 复杂页、扫描页和双栏页判断没有回退。
- 初始化阶段目标提速不低于 30%。

### P1.3 增加进程内哈希缓存

**Checklist**

- [ ] 新增 `ArtifactFingerprintCache`。
- [ ] 同一进程内，相同文件不重复计算 SHA-256。
- [ ] 缓存索引至少包含：
  - [ ] 绝对路径；
  - [ ] 文件大小；
  - [ ] `mtime_ns`。
- [ ] 文件变化后自动失效。
- [ ] 不把“路径没变”当作“内容没变”。
- [ ] 运行结束后将正式输入哈希写入结果文件，不能只依赖内存缓存。

**验收标准**

- `source.pdf` 初始化时只读取一次完整文件用于哈希。
- 候选流水线中同一候选只计算一次文件 SHA-256。
- 修改文件内容后不会错误命中缓存。

### P1.4 缩小预检临时副本

**Checklist**

- [ ] 第一版先保留临时隔离机制。
- [ ] 删除整目录 `copytree()`。
- [ ] 只复制预检真正需要的 JSON。
- [ ] `source.pdf` 和候选 PDF 优先使用硬链接。
- [ ] 不支持硬链接时自动回退到 `copy2()`。
- [ ] 不复制历史候选、对照图、旧审查材料和无关 staging 文件。
- [ ] 加测试证明正式作业在预检前后完全不变。

**验收标准**

- `formal_job_unchanged` 仍然成立。
- 预检结果和当前版本一致。
- 大 PDF 不再因为临时预检完整复制多份。

---

## P2：重做翻译执行链路

这是最推荐优先做的核心项。

### 目标结构

```text
source_units.json
        ↓
translation-plan.json
        ↓
translation-batches/
  batch-0001.json
  batch-0002.json
        ↓
apply_translation_batch.py
        ↓
translation.json
```

小单元继续负责检查。批次负责让模型一次完成较完整的上下文。

### 新增文件建议

```text
scripts/plan_translation_batches.py
scripts/apply_translation_batch.py
scripts/translation_cache.py
```

### Checklist

- [ ] 不修改现有冻结单元 ID。
- [ ] 不合并或删除 `source_units.json` 中的单元。
- [ ] 根据章节、标题、页面顺序生成翻译批次。
- [ ] 默认每批包含约 8～20 个单元。
- [ ] 默认每批原文控制在约 8000～12000 字符。
- [ ] 标题尽量与后续首段进入同一批次。
- [ ] 图题、表题和相关说明尽量进入同一批次。
- [ ] 跨页续句必须进入同一批次。
- [ ] 翻译开始前只做一次：
  - [ ] 论文标题识别；
  - [ ] 摘要读取；
  - [ ] 章节目录；
  - [ ] 术语表；
  - [ ] 专有名词表；
  - [ ] 缩写表。
- [ ] 每个批次携带：
  - [ ] 论文标题；
  - [ ] 当前章节标题；
  - [ ] 已锁定术语；
  - [ ] 上一批末尾少量上下文；
  - [ ] 下一批开头少量上下文；
  - [ ] 本批单元列表。
- [ ] 模型只返回：

```json
[
  {
    "id": "p0003-u0012",
    "translation": "……",
    "keep_source_reason": null,
    "review_flags": []
  }
]
```

- [ ] `apply_translation_batch.py` 检查：
  - [ ] ID 必须存在；
  - [ ] ID 不得重复；
  - [ ] 不得修改 source；
  - [ ] 必填数字和锚点不能丢失；
  - [ ] 输出数量必须与批次一致。
- [ ] 每批成功后原子写入 `translation.json`。
- [ ] 单个批次失败时，只重做该批。
- [ ] 已完成批次不能因为后续失败而丢失。
- [ ] 缓存键包含：
  - [ ] 每个原文单元的内容哈希；
  - [ ] 目标语言；
  - [ ] 术语表哈希；
  - [ ] 翻译提示版本；
  - [ ] 模型标识；
  - [ ] 翻译策略版本。
- [ ] 第一个版本先使用串行批处理。
- [ ] 串行版本稳定后，再增加最多 2 个并发批次。
- [ ] 并发前必须锁定术语表。
- [ ] 并发合并按照原单元 ID，不按完成顺序。
- [ ] 在 `run-metrics.json` 自动记录每批时间、输入量、输出量和重试次数。
- [ ] 更新 `SKILL.md`，明确“逐单元校验，按批次翻译”。

### 验收标准

- 每个冻结单元仍然恰好出现一次。
- 中断后可以从最后成功批次恢复。
- 修改第 8 批时，不重新翻译前 7 批。
- 术语一致性不低于旧链路。
- 数字、统计值和否定词保留率不低于旧链路。
- 模型工具调用或文件修改次数降低至少 60%。
- 翻译阶段墙钟时间目标降低至少 35%。

---

## P3：减少完整 PDF 试排次数

**修改范围**

- `scripts/build_candidate.py`
- `scripts/typography_fit.py`
- `scripts/reportlab_layout.py`
- `scripts/build_first_candidate.py`

### Checklist

- [ ] 将 `typography_fit.py` 设为唯一字号搜索入口。
- [ ] 删除或迁移 `build_candidate.py` 内重复的 `_typography_candidates()`。
- [ ] 先用页面密度、段落数量、标题数量和可用版心做轻量估算。
- [ ] 第一尝试使用推荐字号与推荐行距。
- [ ] 超出页面扩张限制时，使用粗到细搜索。
- [ ] 标准论文完整试排最多 3 次：
  1. 推荐值；
  2. 估算边界值；
  3. 最终修正值。
- [ ] 特殊复杂论文可以回退到当前完整搜索。
- [ ] 回退必须记录原因，不能静默执行。
- [ ] 临时试排优先写入 `io.BytesIO`，避免每次落盘。
- [ ] 只在最终选定后写正式临时 PDF。
- [ ] 图片、保留原文区域和复杂内容解析结果只准备一次。
- [ ] 不复用会被 ReportLab 修改的 Story 对象。
- [ ] 可以复用：
  - [ ] 图片字节；
  - [ ] 图片尺寸；
  - [ ] 表格载荷；
  - [ ] 复杂图形数据；
  - [ ] 字体注册结果；
  - [ ] 保留区域抽取结果。
- [ ] `generator-layout-log.json` 记录：
  - [ ] 估算结果；
  - [ ] 每次尝试参数；
  - [ ] 每次尝试耗时；
  - [ ] 页数；
  - [ ] 失败原因；
  - [ ] 是否触发旧算法回退。

### 验收标准

- 95% 的普通论文完整试排不超过 3 次。
- 原来只需一次试排的论文不能变慢。
- 字号和行距仍在质量档位允许范围内。
- 页面扩张限制保持不变。
- 首版自动检查通过率不能降低。
- 原来需要 4 次以上试排的样本，候选构建时间目标降低至少 40%。
- 复杂样本出现异常时，可以自动使用旧算法完成。

---

## P4：让 QA 和完整性审计共用一次候选分析

新增建议：

```text
scripts/candidate_analysis.py
```

统一生成：

```text
CandidateAnalysis
├── file_sha256
├── content_fingerprint
├── page_count
├── page_sizes
├── normalized_text
├── pages[]
│   ├── text_dict
│   ├── spans
│   ├── fonts
│   ├── drawings
│   ├── images
│   ├── text_blocks
│   └── geometry
└── mapping
```

### Checklist

- [ ] 候选 PDF 在单次预检中只打开一次。
- [ ] 每页只抽取一次 `text_dict`。
- [ ] 每页只读取一次绘图和图片信息。
- [ ] 内容指纹基于同一份页面分析结果生成。
- [ ] `qa_pdf.py` 接收 `CandidateAnalysis`。
- [ ] `audit_translation_completeness.py` 接收同一对象。
- [ ] `validate_job.py` 只读取需要的摘要字段。
- [ ] 完整性审计不再自行打开候选 PDF。
- [ ] QA 规则改为纯函数：
  - [ ] 输入页面分析；
  - [ ] 返回问题；
  - [ ] 不自行读取文件；
  - [ ] 不自行修改作业状态。
- [ ] 预检层统一负责写入：
  - [ ] `qa.json`
  - [ ] repair plan
  - [ ] validation result
  - [ ] preflight result。
- [ ] 先保持串行页面分析。
- [ ] 单次分析仍慢时，再增加可选进程池。
- [ ] 进程池默认最多 2～4 个 worker。
- [ ] 小于 10 页的 PDF 默认不开进程池，避免启动成本反而拖慢。

### 验收标准

- 单次预检中候选 PDF 打开次数降到 1。
- QA 和完整性审计的问题代码与旧版一致。
- 候选内容指纹保持稳定。
- 预检阶段目标提速至少 30%。
- 峰值内存不能超过旧版两倍。

---

## P5：拆分大文件，清理重复实现

性能稳定后再做结构重构。不要先大拆文件，再猜哪里变快了。

### 推荐目录

```text
academic_pdf_translation/
├── models.py
├── pipeline.py
├── artifacts.py
├── source/
│   ├── analysis.py
│   ├── structure.py
│   └── units.py
├── translation/
│   ├── batching.py
│   ├── cache.py
│   └── validation.py
├── render/
│   ├── typography.py
│   ├── story.py
│   ├── complex_content.py
│   ├── retained_content.py
│   └── output.py
└── qa/
    ├── candidate_analysis.py
    ├── layout_rules.py
    ├── content_rules.py
    └── report.py

scripts/
└── 保留现有名称的薄 CLI
```

### Checklist

- [ ] `scripts/build_candidate.py` 只保留 CLI 和流程调用。
- [ ] 将普通正文、图片、表格、模型图、保留原文分别拆开。
- [ ] 将 `qa_pdf.py` 拆为“分析”和“规则”两层。
- [ ] `_common.py` 只保留真正通用的文件与基础工具。
- [ ] 使用 `dataclass`、`TypedDict` 或明确的数据类替代大量自由字典。
- [ ] 第一版不引入 Pydantic，保持运行依赖简单。
- [ ] 新增 `pyproject.toml`。
- [ ] 增加开发工具：
  - [ ] Ruff；
  - [ ] pytest；
  - [ ] 类型检查；
  - [ ] 覆盖率。
- [ ] 运行依赖与开发依赖分开。
- [ ] 将 `typography_fit.py` 和真实生成器统一。
- [ ] 将 `reportlab_layout.py` 接入正式路径，或者删除失效的重复抽象。
- [ ] 更新 `SKILL.md` 与实际调用链。
- [ ] 保留所有旧 CLI 名称。
- [ ] 保留现有作业目录兼容性。
- [ ] 每拆出一个模块就提交一次。
- [ ] 每次提交后都运行自测和代表样本。

### 验收标准

- `build_candidate.py` 不再同时承担所有排版职责。
- `qa_pdf.py` 不再自行管理全部文件、状态和检查规则。
- 文档描述与真实调用关系一致。
- 没有失效的“看起来应该被调用、实际没有调用”的模块。
- 现有命令和已有作业仍能继续运行。

---

## P6：最后再优化审查图

目前审查图已经有整份文档级缓存：原文、候选、风险报告和参数哈希不变时，会直接复用
结果。因此它不是第一优先级。候选一旦变化，当前实现会清空并重建全部审查图。
（见 `scripts/make_review_sheet.py`）

### Checklist

- [ ] 增加单页渲染缓存。
- [ ] 缓存键包含 PDF 页内容指纹、页码和 DPI。
- [ ] 返修后只重画发生变化的候选页。
- [ ] 只重新合成受影响的 sheet。
- [ ] 原文页图片跨候选版本永久复用。
- [ ] 高清疑点页单独缓存。
- [ ] 不改变最终审查 PDF 的页面顺序。

### 验收标准

- 只修改 2 页时，不再重新渲染整篇原文。
- 修订版审查图生成时间随改动页数增长，而不是随全文页数增长。
- 审查图视觉结果与旧版一致。

---

## 四、最终性能验收

以下数字应当作为开发目标，目前还不是实测结果。

| 指标 | 目标 |
| --- | --- |
| 初始化阶段中位耗时 | 降低至少 30% |
| 翻译文件操作次数 | 降低至少 60% |
| 翻译阶段墙钟时间 | 降低至少 35% |
| 多次试排样本的候选构建时间 | 降低至少 40% |
| 预检阶段中位耗时 | 降低至少 30% |
| 缓存状态下的返修链路 | 降低至少 50% |
| 端到端中位耗时 | 降低至少 35% |

不可退让的质量项：

- 首次预检 `READY_TO_REGISTER` 比例不能下降。
- QA 硬错误数量不能因为“提速”而被隐藏。
- 五类代表样本全部通过视觉抽查。

最终运行：

```bash
./.venv/bin/python scripts/check_bundle.py
./.venv/bin/python scripts/self_test.py

./.venv/bin/python scripts/benchmark_corpus.py \
  benchmarks/corpus.json \
  --output benchmarks/results/optimized.json
```

- 自动生成 `baseline.json` 与 `optimized.json` 的逐阶段对比。
- 性能未提升的修改不得仅凭“代码更优雅”合入性能分支。

---

## 五、建议的实际开发顺序

第一轮只做：

1. **P0** 性能基线
2. **P1** 提前检查、合并原文扫描、减少复制
3. **P2** 翻译批处理
4. **P3** 减少完整试排

这四项完成后，再决定是否值得大拆 `build_candidate.py` 和 `qa_pdf.py`。

其中最值得先改的是：

> 翻译批处理 → 排版尝试次数 → 候选单次分析复用

临时只想快速看论文时，可以先使用现有 `fast` 档。它会跳过独立审查，但翻译、试排和
自动预检仍然存在，所以它只能缓解一部分耗时，不能解决根本问题。（见 `SKILL.md`）

---

## 六、本次评审的边界

本次属于基于当前 `main` 源码的静态审查，没有拿真实论文完整运行。因此，上面的提升
比例是验收目标，不是已经测出的结果。
