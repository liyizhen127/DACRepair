# 指定 agent 进行多样本评测

以下命令在仓库根目录执行。需要 Python 3.12+、可用的 Docker，以及具体 agent 所需的依赖和 API 配置。

## 1. 接入 agent

在 `baseline/<agent>/adapter.py` 中提供 `generate(task, environment) -> str`：

- `task` 包含 `instance_id`、`repo`、`base_commit`、`problem_statement`。
- `environment` 提供 `execute()` 和 `get_patch()`。
- 返回最终 unified diff 字符串；若直接修改环境中的源码，可返回 `environment.get_patch()`。

下面以 Agentless 为例。需先自行下载并完成其适配接口；当前仓库尚未提供该实现。

## 2. 指定 agent 和样本 CSV

运行 WSL 第一批的全部样本：

```bash
python3 -m benchmark.experiment run \
  --agent baseline.agentless.adapter:generate \
  --results splits/wsl/Agentless-batch1.csv \
  --run-id Agentless-MODEL_NAME-experiment-1 \
  --jobs 12 --cpus 12 --memory 28g
```

- **选择 agent**：修改 `--agent`，格式为 `baseline.<agent>.<模块>:<函数>`。
- **选择设备和批次**：修改 `--results`，例如 `splits/linux1/Agentless-batch2.csv`。CSV 中的样本 ID 就是本次任务清单。
- **标识实验**：在 `--run-id` 中体现模型等实验信息，同一组实验的三台设备使用相同标识。实际模型和 API Key 在 agent 侧配置。
- **设置运行资源**：`--jobs` 为编译并行数，`--cpus` 和 `--memory` 为运行容器的 CPU、内存限额。

切换基线或模型实验时，另建结果 CSV，保留所需样本 ID，将“是否通过”列留空，不复用旧的 `.state.json`。

## 3. 结果与续跑

每完成一条，公共运行层自动更新 CSV：原始结果为 `PASS` 时记为 `PASS`，为 `FAIL` 或 `ERROR` 时统一记为 `FAIL`；只有未运行的样本留空。进度统计也将 `ERROR` 计入 `FAIL`，同名 `.state.json` 保留原始状态和原因，便于排查及重试。

保留 CSV、同名 `.state.json` 和 `runs/`，中断后重新执行原命令即可续跑。已完成的样本自动跳过，已保存的候选补丁会复用。续跑时保持 `run-id`、agent 入口、样本清单和资源参数不变。

- 加 `--limit 1`：先处理一个尚未评测的样本。
- 加 `--retry-errors`：解决阻塞或临时错误后，重试已有 ERROR 的样本。

查看当前进度：

```bash
python3 -m benchmark.experiment status \
  --results splits/wsl/Agentless-batch1.csv
```
