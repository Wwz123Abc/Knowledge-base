# RAG 评测

`dataset.py` 生成 100 条稳定测试问题，覆盖事实问答、制度流程、无答案问题和不同问法。运行前先上传 `data/sample_handbook.md`，然后执行：

```powershell
python -m evals.run_eval --output reports/evaluation.json
```

报告包含 Recall@K、MRR、答案关键词正确率、引用结构准确率、无答案准确率和基于词汇重合的忠实度启发式指标。正式试用时，应以真实业务问题替换或补充这些启动数据，并安排人工复核。

机械领域数据集（`--dataset mechanical`）包含 27 个基于已入库机械文档核对的事实点、每题两种问法（54 条可回答），以及 10 条应拒答问题，共 **64 题**；报告额外包含首条实际引用准确率：

```powershell
python -m evals.run_eval --dataset mechanical --timeout-seconds 180 --output reports/mechanical_evaluation.json
```

评测过程会逐题写入报告；单题超时或 HTTP 失败会记录在 `error` 字段，不会丢失整批进度。

需要用模型复核忠实度时可显式增加 `--llm-judge`。该选项默认关闭；开启后每题增加一次模型调用，失败时分数记为 0：

```powershell
python -m evals.run_eval --dataset mechanical --llm-judge --output reports/mechanical_judged.json
```

每类先抽一题进行冒烟评测：

```powershell
python -m evals.run_eval --one-per-category --output reports/deepseek_smoke_10.json
```

全量评测后汇总数据库中的延迟、Token 和费用：

```powershell
python -m evals.run_eval --output reports/deepseek_full_100.json
python -m evals.summarize_traces reports/deepseek_full_100.json
```

2026-08-14 本地真实 DeepSeek 评测结果：100 题核心质量指标均为 100%，P95 为 1.559 秒，36,589 Token，按配置价格估算 0.005472 美元。该结果基于示例手册，不能替代真实业务数据签字。
