# 元素管线与核查层

这份文档讲 `academic_pdf_translation/` 这个包：它负责什么、数据怎么流、
哪些结论是算出来的、以及现在还做不到什么。

它和 [architecture.md](architecture.md) 的分工是：那份讲整个 Skill 的
作业契约与文件格式，这份讲**新的元素管线和核查层**。

## 为什么要有这一层

生成器原来会给自己打分：预检通过就返回 `READY_TO_REGISTER`。

后来把一份 `READY_TO_REGISTER` 的产物交给独立复审，人看完判了不合格，
列出 11 条问题：整张结构图消失、表格被压成一行流水文字、图题与图分在两页、
图内标签被排成章节标题。

结论很简单：**生成器说自己没问题，不等于产物没问题。** 所以有了这一层——
它不看计划、不看任何人写进 JSON 的状态，只打开产出的 PDF 去看。

## 包结构

```
academic_pdf_translation/
├── contracts/    共享定义：枚举、坐标、字体解析、作业迁移
├── analysis/     从原文得出元素清单，并把译文单元绑到元素上
├── planning/     按质量档位给每个元素定渲染策略，并备好降级链
├── render/       11 个渲染器 + 版面块 + 页面合成器 + 计划翻译层
├── verify/       只看产出的 PDF：定位、对账、挑页、返修
├── delivery/     把上面串成一条线，给出唯一结论
└── qa/           QA 判据：区域几何、文本信号、排版度量
```

### contracts —— 大家都得认的定义

| 模块 | 负责 |
| --- | --- |
| `enums.py` | 质量档位、路线、23 种元素类型 |
| `models.py` | `SourceElement`、坐标运算（`normalize_bbox` 等） |
| `fonts.py` | 跨平台字体发现与 ReportLab 真实探测 |
| `migration.py` | 旧作业升级到当前 schema |

### analysis —— 原文里有什么

`source_elements.build_inventory` 从 `source_structure.json` 得出元素清单；
`detectors/` 下按类型分别识别图、表、公式、脚注、图题、文字角色、页眉页脚。

`unit_binding.bind_units` 把译文单元绑到元素上，结果写进
`unit_bindings.json`。**注意**：`source_elements.json` 里的
`translation_unit_ids` 是空的——绑定是另一个阶段算出来的，没有回填进清单。
拿那个空字段当归属，每个文字元素都会"找不到"。

### planning —— 每个元素怎么画

`render_plan.build_render_plan(inventory, quality_mode, forced_strategies=)`
给每个元素定策略，并由 `fallback_policy.build_chain` 备好三级降级链：

1. 按正常策略生成；
2. 保留原始元素区域（`preserve-element-region`）；
3. 保留整张原文页（`preserve-full-source-page`）。

第三级不漂亮，但它不会让读者拿到一份消失了图、压平了表格的 PDF。

`forced_strategies` 是内部返修用的，**只许沿降级链往下**。往回调等于让
失败过的策略再试一次，那不是返修，是重试，会被拒绝并进 `plan.problems`。

### render —— 画

11 个渲染器各管一类内容。几条贯穿的规矩：

- **公式不重新输入。** 原样搬区域，候选不得比原文多出数学字体伪影。
- **表格不变成段落。** 网格置信度、行列、数字映射、合并单元格、表题表注、
  粗体语义六项全确定才允许结构化重建，否则保留原表区域加中文翻译键。
- **位图不放大。** 原图有效分辨率不到 300 DPI 时缩放比例封顶 1.0。
- **角色只来自元素清单。** `heading_renderer` 不看字号、不看行长——一旦让
  排版特征参与，作者单位和图内标签就又会被提升成章节标题。

`plan_bridge.py` 是翻译层：把渲染计划里定到保留级的元素，翻成生成器
（`scripts/build_candidate.py`）已经认识的复杂内容条目，并按图像 xref 把
图级图题挂上去，让图题跟着图走。

### verify —— 只看产出的 PDF

| 模块 | 回答的问题 |
| --- | --- |
| `candidate_mapping.py` | 每个源元素**真的**落在候选哪一页 |
| `structural_audit.py` | 合起来看，这还是原来那篇论文吗 |
| `visual_review.py` | 哪几页值得人用眼睛看，看什么 |
| `repair.py` | 能安全自动修的有哪些，哪些必须交给人 |

`candidate_mapping` 有四条定位判据，各有各的证明力：

| 判据 | 用在哪 | 说明 |
| --- | --- | --- |
| 图像字节哈希 | 位图 | 最硬。xref 搬进新文档会变，字节不会 |
| 像素指纹 | 保留下来的区域 | 16×16 灰度比对；它在候选里是图片，没有文字层也没有绘图对象 |
| 文字探针 | 文字元素 | 按译文或按策略保留的原文查 |
| 文字锚点 | 矢量图 | 通道数、尺寸标注这类数字 |

绘图对象数量**只能证明"不在"，不能证明"在"**——一页有 300 个绘图对象，
不代表其中就有你要的那 213 个。所以它单独用时置信度只给 0.30。

### delivery —— 一个结论

`first_delivery.run_first_delivery` 把生成、核查、一次返修、再核查串成一条线，
只给三种结论：

| 结论 | 含义 | 退出码 |
| --- | --- | ---: |
| `delivered` | 核查全过 | 0 |
| `handover` | 还有问题，机器已修过一轮，剩下的必须人看 | 2 |
| `blocked` | 返修弄坏了别的，或生成本身失败 | 1 |

### qa —— 判据的词汇表

`geometry.py` 答"这块东西落在页面哪里、占多大"；`text_signals.py` 是占位符、
拉丁散文、汉字、原文页标的正则；`typography.py` 量字号、行距、留白、行宽、
孤字行、大空隙。它们**只量，不判**——阈值留在调用方，因为同一个数字在不同
档位下的含义不一样。

## 数据怎么流

```
source.pdf
  └─ analysis            → source_elements.json + unit_bindings.json
       └─ planning       → render_plan.json（含降级链）
            └─ plan_bridge → 并进 complex_content
                 └─ build_candidate.py → candidate.pdf
                      └─ verify        → 映射 / 对账 / 视觉检查计划
                           └─ repair   → repair/forced_strategies.json
                                └─ 重算渲染计划 → 重建（唯一一轮）
                                     └─ 再核查 → delivered / handover / blocked
```

## 三条原则，和它们在代码里的位置

### 一、"通过"是算出来的，不是可写字段

任何人往 JSON 里写 `"complete": true` 都不作数。这些结论全部是只读属性：

| 属性 | 位置 |
| --- | --- |
| `CandidateMapping.complete` | `verify/candidate_mapping.py` |
| `StructuralAudit.passed` | `verify/structural_audit.py` |
| `DeliveryResult.delivered` | `delivery/first_delivery.py` |
| `RenderPlan.complete` | `planning/render_plan.py` |

测试里直接断言给它们赋值会抛 `AttributeError`
（`tests/test_candidate_mapping.py`、`tests/test_first_delivery.py`）。

### 二、返修最多一轮，只许往安全的方向修

`verify/repair.py`：

- `MAX_REPAIR_ROUNDS = 1`。第二次调用 `plan_repair` **一条都不修**，
  直接拒绝——不是少修几条。跑够多轮，任何检查都能被磨过去。
- `ALLOWED_ACTIONS` 只有降级与重排：`preserve-element-region`、
  `preserve-full-source-page`、`keep-caption-with-target`、
  `recompose-reading-order`。
- `FORBIDDEN_ACTIONS` 出现即抛错，没有开关可以放行：`lower-threshold`、
  `widen-whitelist`、`skip-check`、`drop-element`、`relax-qa`、
  `mark-complete`。它们让报告变好看，不让产出变好。
- 返修跑完却一个字没改，`delivery/first_delivery.py` 会比对返修前后的
  内容哈希（用页面文字与绘图、图像数量算，避开 PDF 时间戳），完全相同就报
  `blocked` 并写明"降级指令没有落到生成器上"。报成"修了没修好"，
  读的人会以为已经试过了。

### 三、查不到与判不了要分开

`verify/candidate_mapping.py` 用两个不同的 method 区分：

- `not-found`：探针可用但没命中，**是真缺陷**；
- `no-locatable-evidence`：可用文字只剩一两个数学字体残渣字符
  （`X`、`!`、`p`），**判不了**——查不到不等于内容丢了。

两者都会在必需元素上报出来，但性质不同，风险权重也不同
（`visual_review.py` 里前者 10 分，后者 4 分）。

同样的纪律也用在阅读顺序上：同一页里两个元素只要有一个量不到纵坐标，
这一对就跳过，既不算逆序也不进分母。拿 0 顶替等于宣称"它在这一页最上面"，
而那是编出来的。

## 唯一 CLI 入口

```bash
python3 scripts/deliver_first_candidate.py <作业目录>
```

| 参数 | 作用 |
| --- | --- |
| `--delivery-dir` | 证据写到哪里，默认 `<作业目录>/delivery` |
| `--page-budget` | 最多渲染几页给人细看，默认 6 |
| `--no-repair` | 只核查，不执行那一轮返修 |
| `--json` | 只输出 JSON |

退出码就是结论：`0` 可以交付，`2` 交给人处理，`1` 停下别交。

跑完后交付目录里有：两轮的映射 / 对账 / 视觉检查计划、返修计划、
返修前后对比、以及渲染出来供人细看的页面图片。**没有证据的结论不算结论。**

作业需要先具备 `source_elements.json` 与 `unit_bindings.json`：

```bash
python3 scripts/analyze_source_elements.py <作业目录>
python3 scripts/bind_translation_units.py <作业目录>
```

## 现在做不到什么

这一节只写实测过的边界，不写猜测。

- **首版直接可交付的比例，实测是 0/6。** 见
  [../benchmarks/results/first-delivery.md](../benchmarks/results/first-delivery.md)。
  两篇走完整条链的论文停在 `handover`，四篇在生成前被字体覆盖检查拦下。
- **保留区域是栅格，不是矢量。** 分辨率不低于 300 DPI 且不放大，
  但放得很大仍会看出像素。矢量保留需要在输出后用 PyMuPDF `show_pdf_page`
  二次贴图，尚未实现。
- **矢量图的图题绑定覆盖不到。** 图级图题按图像 xref 挂到复杂条目上，
  矢量图没有 xref。不按页码猜——猜错会把图题挂到别的图上，比不挂更糟。
- **翻译性能未验证。** 本套基准不调用真实模型翻译，耗时与 Token 一概
  未测量，也不做估计。
- **只有 1 篇语料是真实模型译文。** 其余 5 篇用确定性合成译文，只为触发
  代码路径，不代表译文质量。

## 怎么复现

```bash
# 全量测试
./.venv/bin/python -m pytest

# 性能基准（改到 renderer_build_id 输入后必须重跑）
./.venv/bin/python benchmarks/run_benchmark.py \
    --output benchmarks/results/optimized.json

# 首版交付基准
./.venv/bin/python benchmarks/run_first_delivery_benchmark.py \
    --work-dir <临时目录> --real-translation real-translation
```

`tests/test_benchmark_provenance.py` 会检查性能报告里的构建哈希是否等于
当前源码的构建哈希，对不上就失败——报告是用别的代码跑出来的，不能算数。
