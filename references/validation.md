# 验证范围

## 两种"通过"，千万别混为一谈

这是这份文档最要紧的一句话：

| 说法 | 谁说的 | 意味着什么 |
| --- | --- | --- |
| `READY_TO_REGISTER` | **生成器给自己打的分** | 它自己的预检没发现问题 |
| `delivered` | **核查层打开产出的 PDF 看出来的** | 元素都在、结构对得上 |

两者会不一样，而且不一样的时候恰恰是最要命的时候。曾经把一份
`READY_TO_REGISTER` 的产物交给独立复审，人看完判了不合格，列出 11 条问题：
整张结构图消失、表格被压成一行流水文字、图题与图分在两页。

所以**引用"首版成功率"时必须写明是哪一种**。下面凡是写
`READY_TO_REGISTER` 的地方，都只是生成器的自评，不是产物合格的证明。

## 三份报告，别搞混

| 报告 | 内容 | 什么时候能引用 |
| --- | --- | --- |
| `../benchmarks/results/first-delivery.md` | **机器单独跑**（不调用模型、无复审）的压力测试，含 delivered/handover/blocked 分布 | 需要说明**自动化部分单独**能走多远时。它不回答"正常使用（模型在环）效果如何" |
| `../benchmarks/results/comparison.md` | 当前代码的可复现性能基准，绑定当前 `renderer_build_id` | 需要说明**当前版本**的耗时与重复读取时 |
| `../assets/representative-benchmark.json` | 一次历史运行的记录，构建哈希已经过期 | 只能作为历史参考，**不能当作当前版本的证据** |

`tests/test_benchmark_provenance.py` 会检查当前基准报告的构建哈希是否等于
当前源码的构建哈希；对不上就失败。

## 当前实测：首版交付

见 [../benchmarks/results/first-delivery.md](../benchmarks/results/first-delivery.md)。

- 语料：6 篇真实开放获取论文（原文不随仓库分发）；
- 其中 **1 篇是真实模型译文**，5 篇是确定性合成译文，只为触发代码路径，
  **不代表译文质量**；两类分开统计，混在一起报数字就没意义；
- 结果：`delivered` **0** 篇，`handover` 2 篇，`blocked` 4 篇；
- 4 篇 blocked 全部卡在生成前的字体覆盖检查
  （合成译文里带着从原文抄来的 `∈` 与私用区字符）；
- 2 篇走完整条链，返修部分生效后停在 `handover`。

**翻译性能未验证**：本套基准不调用真实模型翻译，耗时与 Token 一概未测量。

## 一次历史运行（生成器自评，非产物合格证明）

下面这一节描述的是**那次历史运行**的范围。它记录的 5/5 是
`READY_TO_REGISTER`，即生成器自评，**不能当作首版可交付的证据**：

- 目标语言：简体中文；
- 作业档位：平衡档；
- 样本：5 篇已准备完整译文数据的真实学术 PDF；
- 覆盖：混合参考文献、双栏跨页续句、长参考文献、结构化表格、承担信息的
  截图、四象限模型和密集正文；
- 自动首版：5/5 返回 `READY_TO_REGISTER`（生成器自评，见本页开头的区分）；
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

首版交付基准用 `benchmarks/run_first_delivery_benchmark.py`：

```bash
python3 benchmarks/run_first_delivery_benchmark.py \
  --work-dir <临时目录> --real-translation real-translation
```

它按译文来源分开统计；跑不动的记为"未验证"并点名，不从分母里消失。
结果文件只存哈希、页数和派生结论——论文受版权保护，不进仓库。
