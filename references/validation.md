# 验证范围

## 两份报告，别搞混

| 报告 | 内容 | 什么时候能引用 |
| --- | --- | --- |
| `../benchmarks/results/comparison.md` | 当前代码的可复现基准，绑定当前 `renderer_build_id` | 需要说明**当前版本**的耗时与重复读取时 |
| `../assets/representative-benchmark.json` | 一次历史运行的记录，构建哈希已经过期 | 只能作为历史参考，**不能当作当前版本的证据** |

`tests/test_benchmark_provenance.py` 会检查当前基准报告的构建哈希是否等于
当前源码的构建哈希；对不上就失败。

下面这一节描述的是**那次历史运行**的范围：

- 目标语言：简体中文；
- 作业档位：平衡档；
- 样本：5 篇已准备完整译文数据的真实学术 PDF；
- 覆盖：混合参考文献、双栏跨页续句、长参考文献、结构化表格、承担信息的
  截图、四象限模型和密集正文；
- 自动首版：5/5 返回 `READY_TO_REGISTER`；
- 串行中位耗时：4.452 秒/篇；
- 整批墙钟时间：24.828 秒；
- 视觉抽查：通过，未发现裁切、重叠、孤立标题或不可读复杂结构。

这些结果只衡量已有译文数据进入 PDF 后的确定性生成、完整性门禁和版式表现，
不衡量语言模型的翻译准确率。快速档、精细档和非简体中文配置尚无同口径样本，
因此不发布对应通过率、耗时或 token 数。

复测时用 `scripts/benchmark_corpus.py`，并保留报告中的代码构建哈希、并行度、
逐篇状态和视觉抽查记录。不同构建的结果不得合并计算通过率。

合成语料上的可复现基准用 `benchmarks/run_benchmark.py`：

```bash
python3 benchmarks/make_benchmark_jobs.py
python3 benchmarks/run_benchmark.py --repeats 5 --label optimized \
  --output benchmarks/results/optimized.json
```

模型翻译耗时和 Token **不在这套基准的口径内**，报告里一律标记为未测量。
