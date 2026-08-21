# academic-pdf-translation 1.1.0 强制重新审查报告

## 1. 最终状态

```
PARTIAL
```

三个 P0 全部复现、修复并验证。剩两件事没做完，写在第 6 节。

## 2. 已复现问题

### P0-1 原文原样冒充译文

```
问题       英文原文原样写入 zh-Hans 作业的 translation 字段，被接受
复现命令   python3 scripts/init_job.py audit/evidence/before/repro-paper.pdf \
             audit/evidence/before/job-p0 --target-language zh-Hans --producer-id audit-repro
           python3 scripts/plan_translation_batches.py audit/evidence/before/job-p0
           python3 scripts/apply_translation_batch.py audit/evidence/before/job-p0 \
             --batch batch-0001 --result audit/evidence/before/p0-1-results.json
           python3 scripts/audit_translation_completeness.py audit/evidence/before/job-p0 \
             --output-json audit/evidence/before/p0-1-completeness.json \
             --output-md audit/evidence/before/p0-1-completeness.md
修改前退出码  apply = 0，completeness = 0
修改前证据路径 audit/evidence/before/p0-1-apply.txt
             audit/evidence/before/p0-1-completeness.json
```

观察到：`coverage.complete` 变成 `true`，`translated_units` = 18，
完整性审查判定 `READY`，`scope_note` 仍写着"等待逐单元翻译"。

### P0-2 任意保留理由绕过全文翻译

```
问题       全篇译文留空、每个单元填自由文本"按学术规范保留原文"，被接受
复现命令   python3 scripts/apply_translation_batch.py audit/evidence/before/job-p0-2 \
             --batch batch-0001 --result audit/evidence/before/p0-2-results.json
           python3 scripts/audit_translation_completeness.py audit/evidence/before/job-p0-2 \
             --output-json audit/evidence/before/p0-2-completeness.json \
             --output-md audit/evidence/before/p0-2-completeness.md
修改前退出码  apply = 0，completeness = 0
修改前证据路径 audit/evidence/before/p0-2-apply.txt
             audit/evidence/before/p0-2-completeness.json
```

观察到：`coverage.complete` = `true`、`translated_units` = 0、
`kept_source_units` = 18；第 1 页和第 2 页都被标成 `reference_page: true`；
最终判定 `READY`。

### P0-3 新作业字体循环依赖

```
问题       全新作业补齐其余输入后，统一入口被 SELECTED_FONTS_MISSING 拦死
复现命令   python3 scripts/init_job.py ... audit/evidence/before/job-p0-3
           python3 scripts/plan_translation_batches.py audit/evidence/before/job-p0-3
           python3 scripts/apply_translation_batch.py audit/evidence/before/job-p0-3 ...
           python3 scripts/set_complex_content.py audit/evidence/before/job-p0-3 --none ...
           （手工补齐 route.selected / decision_reason / terminology_reviewed / inventory_complete）
           python3 scripts/build_first_candidate.py audit/evidence/before/job-p0-3
修改前退出码  build_first_candidate = 2
修改前证据路径 audit/evidence/before/p0-3-build.txt
```

观察到：状态 `BLOCKED_BEFORE_PREFLIGHT`，唯一拦截项就是
`SELECTED_FONTS_MISSING`。自动字体解析写在 `build_candidate._resolve_fonts`
内部，输入就绪检查跑在它之前，所以永远没有机会执行。

完整复现记录：`audit/evidence/before/reproduction.json`。

## 3. 修改内容

### 3.1 译文真实性

```
根本原因   写入路径只检查"字段非空"，不检查"译文是不是目标语言"，
           也不检查"保留原文的理由站不站得住"
修改文件   scripts/translation_truthfulness.py（新增）
           scripts/apply_translation_batch.py
           scripts/audit_translation_completeness.py
           scripts/validate_job.py
           scripts/plan_translation_batches.py
           scripts/prepare_translation_units.py
关键函数   evaluate_unit / check_keep_source / check_translation_language
           evaluate_batch / evaluate_translation / refresh_coverage
           _assert_truthful（写入前调用）
           _is_reference_unit（只认结构化证据）
对应提交   0611037
```

要点：

- `keep_source_code` 固定枚举 8 个取值，每个只能用于允许的单元类型或明确的
  原文片段；`bibliography-entry` 必须来自 reference/bibliography 单元类型，
  或 `retained_source.json` 中覆盖该单元坐标的参考文献区域。
- `keep_source_reason` 降为补充说明，单独不再豁免。
- 跨语言任务里标准化后 `translation == source` 一律拒绝。
- 目标语言字符占比分三层判定：单元 0.50、批次 0.70、文档 0.80；
  另有全篇保留原文字符占比上限 0.50。
- 检查在写 `translation.json` 与缓存**之前**执行；缓存写回走同一条路径。
- `coverage` 新增 `validated_translated_units`、`validated_kept_source_units`、
  `invalid_or_unverified_units`；`complete` 只在全部单元通过后为 `true`；
  `scope_note` 同步更新。
- 完整性审查与 `validate_job` 各自独立重算，不看自报的 `complete`。
- 旧作业只有自由文本理由时报出可理解的迁移错误，不静默放行。

### 3.2 字体准备顺序

```
根本原因   字体解析写在 build_candidate 内部，而输入就绪检查跑在排版之前
修改文件   scripts/font_preparation.py（新增，从 build_candidate 拆出）
           scripts/init_job.py
           scripts/build_first_candidate.py
           scripts/pre_render_audit.py
           scripts/build_candidate.py
关键函数   prepare_job_fonts / fonts_are_current / resolve_job_fonts
           _stale_font_evidence_issues
对应提交   3d27aa2
```

初始化时解析并冻结绝对路径与文件 sha256；统一入口在检查之前再确认一次；
字体文件被换掉时哈希对不上会重新解析。字体嵌入、字符覆盖和复制文本检查
一项没删。

### 3.3 批次执行链路

```
根本原因   只有"写回"一步，没有执行器，也没有身份约束；重新编排先删光批次
           文件再按译文内容反推"已完成"
修改文件   scripts/plan_translation_batches.py
           scripts/apply_translation_batch.py
           scripts/translation_cache.py
           scripts/run_translation_batches.py（新增）
关键函数   _assert_plan_ready / _assert_model_matches_plan / cache_identity
           verify_plan_execution / run_translation_batches
对应提交   0c8f627
```

要点：术语表未确认拒绝正式编排（`--preview` 只看不写）；计划记录模型、
提示版本、策略版本与术语表哈希；写回校验实际模型；没有模型标识不进正式缓存；
缓存写回逐项复核身份；重新编排按 `cache_key` 与单元边界继承已完成批次的证据；
执行器最多 2 批并发、按计划顺序写回、单批失败只重试该批、结束强制核账。

### 3.4 PDF 重复解析与计数

```
根本原因   性能计数器只统计 _common.open_pdf，而生产代码全部直接调 fitz.open
修改文件   scripts/candidate_analysis.py（新增）
           qa_pdf / audit_translation_completeness / validate_job /
           preflight_candidate / register_candidate / candidate_page_map /
           review_risk_report / make_review_sheet / build_candidate /
           retained_source
关键函数   CandidateAnalysis / open_candidate_analysis / shared_candidate_analysis
对应提交   2c89841
```

一次预检里 QA、作业校验和完整性审查共用同一次打开。顺带修好两处从未关闭的
文档句柄。QA 项目一项没减。

### 3.5 基准与打包

```
根本原因   包内报告的构建哈希与源码对不上；语料用拉丁字体排中文，
           还靠"恒等译文"绕过完整性审查
修改文件   benchmarks/run_benchmark.py（新增）
           benchmarks/compare_results.py（新增）
           benchmarks/make_benchmark_jobs.py / make_real_jobs.py
           scripts/renderer_identity.py / scripts/check_bundle.py
           assets/representative-benchmark.json
           references/validation.md / docs/performance-results.md
           pyproject.toml / .claude-plugin/plugin.json
对应提交   cc73ff2
```

版本对齐到 1.1.0 并加入一致性检查；打包检查发现缓存或字节码文件就失败。

## 4. 自动测试

| 测试名称 | 命令 | 退出码 | 验证的风险 |
| --- | --- | --- | --- |
| tests/test_translation_truthfulness.py（7 条） | `python3 -m pytest -q tests/test_translation_truthfulness.py` | 0 | 原文冒充译文、译文不是目标语言、自报 complete、缓存绕过检查 |
| tests/test_keep_source_policy.py（8 条） | `python3 -m pytest -q tests/test_keep_source_policy.py` | 0 | 自由文本理由豁免、全篇冒充参考文献、坐标证据缺失、旧作业静默放行 |
| tests/test_font_preparation.py（4 条） | `python3 -m pytest -q tests/test_font_preparation.py` | 0 | 全新作业字体循环依赖、字体文件变化未检出 |
| tests/test_translation_batch_resume.py（8 条） | `python3 -m pytest -q tests/test_translation_batch_resume.py` | 0 | 术语表未确认就编排、少翻一批、重复翻译、证据丢失、按译文反推状态 |
| tests/test_translation_cache_identity.py（6 条） | `python3 -m pytest -q tests/test_translation_cache_identity.py` | 0 | 缓存跨模型复用、模型未声明、术语表变更后误命中 |
| tests/test_candidate_analysis_reuse.py（4 条） | `python3 -m pytest -q tests/test_candidate_analysis_reuse.py` | 0 | 候选 PDF 重复完整解析、未计数的 fitz.open |
| tests/test_benchmark_provenance.py（9 条） | `python3 -m pytest -q tests/test_benchmark_provenance.py` | 0 | 报告与源码构建哈希脱钩、模型阶段被填估算值 |
| 全部测试（48 条） | `python3 -m pytest -q` | 0 | 上述全部 + 原有 self_test 与包检查 |

## 5. 性能结果

```
修改前中位耗时   冷启动合计 5.184s；单案例中位 1.457s
修改后中位耗时   冷启动合计 5.151s（-0.6%）；单案例中位 1.417s
冷启动结果       五个案例各跑 5 次取中位数，见 benchmarks/results/*.json
缓存结果         修改前合计 5.124s，修改后 5.140s（+0.3%）
模型翻译是否实际测量   否，未验证。model_translation.measured = false，
                 模型、调用次数、重试次数与 Token 全部为 null
当前 renderer_build_id
                 a2591c765830c76f3b23c9b6aa3b0d6b95e58b981652d7e09a196688d2156ebb
```

**不宣称提速。** 版本间差异（-0.6%）小于单次运行内部的抖动（5%~16%），
测量机负载也不低。可验证的结论是重复读取的账：`pdf_open` 从 0 变成 87，
基线那个 0 不是"没打开"，是"打开了没被记下来"。

## 6. 剩余问题

1. **真实模型端到端翻译未验证。** 执行器接口已完成，并用假模型跑通全链路
   （编排 → 逐批翻译 → 原子写回 → 断点续跑 → 核账）。当前环境没有可调用的
   翻译模型，因此没有真实模型的耗时、Token 与首版通过率。
2. **文字抽取计数不完整。** `get_text_dict` / `get_text_blocks` /
   `get_text_plain` 三个计数器只统计经过 `CandidateAnalysis` 的抽取。
   QA 与完整性审查内部直接调用 `page.get_text(...)` 的次数仍未计数，
   因此"单次预检的完整文字抽取次数"还没有端到端的自动测试。
3. **耗时对比不构成结论。** 测量机不是空闲机器，需要在空闲机器上重跑才能
   给出可用的耗时结论。
4. **`build_candidate.py` 仍有约 6400 行。** 本次只把字体解析和候选分析拆
   出去，CLI 与实现的彻底分离没有做。
5. **旧作业需要人工迁移。** 只有 `keep_source_reason` 的旧作业会报出明确的
   迁移错误，但没有提供自动迁移工具。
6. **合成语料 reference-heavy 案例触发页数扩张保护。** 基线与修改后行为一致
   （11 页 / 扩张比 1.833，上限 1.6），不是本次引入，但该案例的 PDF 阶段
   只测到排版搜索失败为止。

## 7. 证据矩阵

见 `audit/evidence/sha256.json`（记录每份证据文件的 SHA-256）。
