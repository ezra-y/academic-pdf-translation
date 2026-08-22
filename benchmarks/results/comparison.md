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
| git 提交 | `025b7a3434` | `09b9656ae4` |
| renderer_build_id | `2eb01fb6e630ce3b96187bb3e832d6c1443a16ae837ace5f983b8623d86c3a8f` | `9ef0e7e8eeb85caf8fd5647af69ce4140738ed1456355129401392f9ea96ba2e` |
| 每种状态重复次数 | 5 | 3 |
| 运行时 1 分钟负载 | 6.72 | 7.33 |
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
| single-column-body | 1.551 | 1.553 | +0.2% | 1.440 | 1.780 | +23.6% |
| two-column-body | 1.565 | 1.886 | +20.5% | 1.631 | 1.809 | +10.9% |
| structured-table-and-model | 0.413 | 0.652 | +57.8% | 0.405 | 0.643 | +58.9% |
| image-heavy | 0.285 | 0.553 | +93.7% | 0.250 | 0.516 | +106.2% |
| reference-heavy | 1.418 | 1.716 | +21.0% | 1.548 | 1.713 | +10.7% |
| **合计** | **5.232** | **6.360** | **+21.6%** | **5.274** | **6.462** | **+22.5%** |

### 单次运行内部的抖动

| 案例 | 基线冷启动极差 | 修改后冷启动极差 |
| --- | ---: | ---: |
| single-column-body | 17% | 11% |
| two-column-body | 4% | 7% |
| structured-table-and-model | 24% | 5% |
| image-heavy | 15% | 6% |
| reference-heavy | 4% | 8% |

把抖动和版本间差异放在一起看：**本次不宣称任何提速**。
本次性能工作的可验证结论在下一节，是重复读取的账终于对得上了。

## 重复读取计数（冷启动，五个案例合计）

| 计数器 | 基线 | 修改后 | 说明 |
| --- | ---: | ---: | --- |
| `pdf_open` | 0 | 85 | 基线是 0。计数器只统计 `_common.open_pdf`，而生产代码全部直接调用 `fitz.open`，等于完全没测。 |
| `source_pdf_open` | 0 | 53 | 同上。 |
| `candidate_pdf_open` | 0 | 32 | 同上。修改后一次预检里候选被打开 2 次（待检 PDF 与影子副本各一次），由 `test_candidate_pdf_analysis_is_reused` 固定上界。 |
| `sha256_file_read` | 85 | 105 | 修改后增加的部分来自新增的字体证据哈希与批次计划哈希。 |

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
