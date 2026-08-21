# 基准对比：审查前 vs 审查后

本文件由 `benchmarks/compare_results.py` 从两份 JSON 报告生成，
不手工整理。两份报告由同一个脚本 `benchmarks/run_benchmark.py`
背靠背跑出，结构完全一致。

语料是 `benchmarks/corpus.json` 描述的五类合成论文：单栏正文、双栏正文、
复杂图表、图片密集、参考文献密集。合成语料只比较耗时与重复读取，
不能替代真实论文的视觉抽查。

## 口径

| 项目 | 基线 | 修改后 |
| --- | --- | --- |
| git 提交 | `025b7a3434` | `2c89841cb9` |
| renderer_build_id | `2eb01fb6e630ce3b96187bb3e832d6c1443a16ae837ace5f983b8623d86c3a8f` | `a2591c765830c76f3b23c9b6aa3b0d6b95e58b981652d7e09a196688d2156ebb` |
| 每种状态重复次数 | 5 | 5 |
| 运行时 1 分钟负载 | 5.38 | 6.04 |
| CPU 核数 | 15 | 15 |
| Python | 3.14.5 | 3.14.5 |

- **模型翻译未测量**。本次没有调用真实模型；译文是确定性伪译文，
  只为触发同一批代码路径。模型标识、调用次数、重试次数、
  输入/输出 Token 和翻译耗时在两份报告里都是 `null`，不做任何估算。
- **翻译时间与 PDF 时间分开记**：`translation_stage_seconds` 只含初始化、
  原文结构提取、批次编排与写回；`pdf_stage_seconds` 含试排、注册、QA、
  作业校验、完整性审查与预检。
- 两次运行的 `job.quality.selected_fonts` 对齐成同一组真实中文字体。
  基线原本用拉丁字体排中文，不对齐就不是同一件事。

## 端到端耗时（秒，中位数）

| 案例 | 基线冷启动 | 修改后冷启动 | 变化 | 基线缓存 | 修改后缓存 | 变化 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| single-column-body | 1.486 | 1.554 | +4.5% | 1.457 | 1.411 | -3.2% |
| two-column-body | 1.597 | 1.514 | -5.2% | 1.571 | 1.673 | +6.5% |
| structured-table-and-model | 0.381 | 0.397 | +4.3% | 0.373 | 0.366 | -1.9% |
| image-heavy | 0.263 | 0.269 | +2.4% | 0.252 | 0.267 | +6.0% |
| reference-heavy | 1.457 | 1.417 | -2.8% | 1.471 | 1.424 | -3.2% |
| **合计** | **5.184** | **5.151** | **-0.6%** | **5.124** | **5.140** | **+0.3%** |

### 单次运行内部的抖动

| 案例 | 基线冷启动极差 | 修改后冷启动极差 |
| --- | ---: | ---: |
| single-column-body | 10% | 11% |
| two-column-body | 5% | 16% |
| structured-table-and-model | 39% | 9% |
| image-heavy | 4% | 10% |
| reference-heavy | 9% | 11% |

把抖动和版本间差异放在一起看：**本次不宣称任何提速**。
本次性能工作的可验证结论在下一节，是重复读取的账终于对得上了。

## 重复读取计数（冷启动，五个案例合计）

| 计数器 | 基线 | 修改后 | 说明 |
| --- | ---: | ---: | --- |
| `pdf_open` | 0 | 87 | 基线是 0。计数器只统计 `_common.open_pdf`，而生产代码全部直接调用 `fitz.open`，等于完全没测。 |
| `source_pdf_open` | 0 | 53 | 同上。 |
| `candidate_pdf_open` | 0 | 34 | 同上。修改后一次预检里候选被打开 2 次（待检 PDF 与影子副本各一次），由 `test_candidate_pdf_analysis_is_reused` 固定上界。 |
| `sha256_file_read` | 85 | 100 | 修改后增加的部分来自新增的字体证据哈希与批次计划哈希。 |

基线那一列的 0 不代表没有打开 PDF，代表打开了但没被记下来。
这就是旧性能报告不能直接当作当前版本证据的原因。

## 已知限制

1. **模型翻译未测量**：没有真实模型端到端数据，不估算。
2. `get_text_dict` / `get_text_blocks` / `get_text_plain` 三个计数器目前
   只统计经过 `CandidateAnalysis` 的抽取。QA 与完整性审查内部直接调用
   `page.get_text(...)` 的次数仍未计数，这一项尚未完成。
3. 合成语料的 reference-heavy 案例在基线和修改后都触发页数扩张保护
   （11 页 / 扩张比 1.833，上限 1.6），两侧行为一致，不是本次引入的回归。
   它的耗时覆盖到排版搜索失败为止。
4. 其余四个案例判定为 NEEDS_REPAIR：合成伪译文本来就过不了内容完整性
   审查。全部阶段都已执行，因此耗时口径不受影响。
5. 测量机不是空闲机器。要得到更硬的耗时结论，需要在空闲机器上重跑。

## 复现

```bash
python3 benchmarks/make_benchmark_jobs.py
python3 benchmarks/run_benchmark.py --repeats 5 --label optimized \
  --output benchmarks/results/optimized.json
python3 benchmarks/compare_results.py
python3 -m pytest -q tests/test_benchmark_provenance.py
```
