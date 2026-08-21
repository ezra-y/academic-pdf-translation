# 基准报告目录

| 文件 | 是什么 | 能不能当作当前版本的证据 |
| --- | --- | --- |
| `baseline.json` | 审查开始前那次提交的可复现基准 | 能，作为对照 |
| `optimized.json` | 当前代码的可复现基准 | 能 |
| `comparison.md` | 两者的逐项对比与口径说明 | 能 |
| `historical/*.json` | 2026-08 那一轮性能优化留下的记录 | **不能**。构建哈希已经过期 |

当前的两份报告由同一个脚本生成，结构完全一致：

```bash
python3 benchmarks/make_benchmark_jobs.py
python3 benchmarks/run_benchmark.py --repeats 5 --label optimized \
  --output benchmarks/results/optimized.json
```

`tests/test_benchmark_provenance.py` 会检查 `optimized.json` 的
`renderer_build_id` 是否等于当前源码的构建哈希，对不上就失败。

模型翻译耗时与 Token **不在这套基准的口径内**，两份报告里都显式标记为未测量。
