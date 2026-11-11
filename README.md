# 素材审核与发布排期 Agent

小影校招项目题 1。输入一批素材及硬性发布时间，逐项返回审核结果、风险、建议时段、待确认项、草稿结果和决策记录。排期草稿保存在本地 SQLite。

项目使用 Python 3.9+ 标准库。原生 ReAct 执行器接入 MiniMax 的工具调用接口，Mock 模式可以离线复现业务流程。

## 快速运行

在仓库根目录执行。各例使用独立的 SQLite 文件，重复提交例复用正常例的文件。

```sh
run_dir="$(mktemp -d)"
python3 -m src.main --mode mock --input examples/normal.input.json --db "$run_dir/normal.sqlite3"
python3 -m src.main --mode mock --input examples/normal.input.json --db "$run_dir/normal.sqlite3"
python3 -m src.main --mode mock --input examples/missing.input.json --db "$run_dir/missing.sqlite3"
python3 -m src.main --mode mock --input examples/risk.input.json --db "$run_dir/risk.sqlite3"
python3 -m src.main --mode mock --input examples/normal.input.json --calendar examples/conflict.calendar.json --db "$run_dir/conflict.sqlite3"
python3 -m src.main --mode mock --input examples/confirmed.input.json --calendar examples/conflict.calendar.json --db "$run_dir/conflict.sqlite3"
python3 -m src.main --mode mock --input examples/normal.input.json --db "$run_dir/failure.sqlite3" --inject-save-failure
python3 -m unittest discover -s tests -v
```

## 真实模型

在 Bash 或 Zsh 中用以下命令读取密钥，输入时终端不回显。默认使用中国区 MiniMax 平台与 `MiniMax-M3.1-Flash`。国际站凭据可通过 `MINIMAX_REGION=global` 配置。可选变量见 [.env.example](.env.example)。

```sh
printf 'MiniMax API key: '
IFS= read -rs MINIMAX_API_KEY
printf '\n'
export MINIMAX_API_KEY
run_dir="$(mktemp -d)"
python3 -m src.main --mode minimax --input examples/normal.input.json --db "$run_dir/live-normal.sqlite3"
python3 -m src.main --mode minimax --input examples/normal.input.json --calendar examples/conflict.calendar.json --db "$run_dir/live-conflict.sqlite3"
python3 -m src.main --mode minimax --input examples/normal.input.json --db "$run_dir/live-failure.sqlite3" --inject-save-failure
unset MINIMAX_API_KEY
```

## 一键验收

在仓库根目录执行，默认离线运行 19 组用例。每组使用临时数据库，终端显示各阶段状态、草稿数量和重复提交复用的草稿编号。

```sh
python3 examples/run_acceptance.py
```

导出密钥后可以用同一套验收连接真实模型。报告记录各阶段输入、Trace、数据库记录与断言结果。在 Git 仓库中运行时，也会记录当前提交。脚本默认串行请求，API 调用间隔至少 3.2 秒。

```sh
python3 examples/run_acceptance.py --mode minimax --output "${TMPDIR:-/tmp}/minimax-regression.json"
```

输入为非空 JSON 数组，每项包含 `material_id`、`requested_slot` 和可选的 `confirmed_slot`。时段格式为 `YYYY-MM-DDTHH:MM+08:00`。用户同意改期后保留原始时段并添加 `confirmed_slot`。业务结果写到标准输出，格式或启动错误写到标准错误并返回退出码 2。

## 补查与改期确认

运行 [补查样例](examples/supplement.input.json)。M05 和 M06 都缺少版权字段。Agent 查到本地证据后重新审核，M05 通过并可保存，M06 触发 R01 并阻断。输出的 `audit_evidence` 记录使用过的证据 ID。

```sh
workflow_dir="$(mktemp -d)"
python3 -m src.main --mode mock --input examples/supplement.input.json --db "$workflow_dir/supplement.sqlite3"
python3 -m src.main --mode mock --input examples/normal.input.json \
  --calendar examples/conflict.calendar.json --db "$workflow_dir/confirm.sqlite3"
```

遇到占用时，第二条命令返回 `needs_confirmation` 和 `confirmation.confirmation_id`。操作者确认后，复制该编号，将以下请求保存到本地 JSON，再用相同的日历和 SQLite 文件运行。确认时段如果已被占用，Agent 会重新查候选并等待下一次确认。

```json
[
  {
    "material_id": "M01",
    "requested_slot": "2026-11-15T18:00+08:00",
    "confirmed_slot": "2026-11-15T19:00+08:00",
    "confirmation_id": "替换为上一步返回的确认编号"
  }
]
```

已有上游授权的操作者也可以直接提供 `confirmed_slot`，用于兼容原 CLI 输入。带编号的流程会额外校验编号与素材、原时段、改期时段是否匹配，并在同一事务关联确认记录和草稿 ID。

## 追问与补齐资料

本地证据无法补齐缺项时，Agent 可以调用 `ask_user`。结果保留 `insufficient_info`，并在 `clarification.questions` 中列出字段、规则和需要的凭证。操作者核实资料后，用相同的请求和数据库重跑。提供 `--evidence` 可替换本次运行的证据文件。整个素材不存在时，使用 `--materials` 指定补齐后的素材文件。

```sh
clarification_dir="$(mktemp -d)"
python3 -m src.main --mode mock --input examples/missing.input.json \
  --db "$clarification_dir/drafts.sqlite3"
python3 -m src.main --mode mock --input examples/missing.input.json \
  --evidence examples/clarification.evidence.json --db "$clarification_dir/drafts.sqlite3"
```

第二条命令使用模拟的可信审核记录 E02。程序重新查证和审核，资料本身的值仍按原规则判断。

同一审核字段可提供单条证据对象或多条证据的数组。模拟证据由操作者提供，类型与审核规则一致的记录参与判断。不同来源结论相反时，返回 `evidence_conflict`，列出双方记录并请操作者核实，核实前不保存草稿。使用 [冲突证据样例](examples/conflicting.evidence.json) 作为 `--evidence` 参数可以复现。

元数据与证据结论相反时也按冲突处理，输出用 `metadata:素材ID` 标注元数据来源。操作者核实后，应更新有误的元数据或证据，再重新运行审核。

## 上下文预算

默认每项素材的上下文预算是 24 KiB，超过一半时压缩较早的完整工具交互，单次工具观察最多 2 KiB。结果中的 `context.rounds` 记录每轮压缩前后的字节数、移除的交互数和超限状态。以下命令用较低的预算复现压缩，返回的建议仍需确认。

```sh
context_run_dir="$(mktemp -d)"
python3 -m src.main --mode mock --input examples/normal.input.json \
  --calendar examples/conflict.calendar.json --context-budget-bytes 5200 \
  --db "$context_run_dir/drafts.sqlite3"
```

## 评审入口

- [需求与成功标准](docs/01-requirements.md)
- [模型循环、工具与保存约束](docs/02-solution.md)
- [验收矩阵、实际输出与 Trace](docs/03-evaluation.md)
- [AI 使用记录](docs/04-ai-usage.md)
- [复盘与限制](docs/05-retrospective.md)

## 正式提交

提交题号 1、GitHub 仓库 URL，以及该仓库最终提交的完整 SHA。上传后在仓库根目录执行 `git rev-parse HEAD` 获取 SHA，私有仓库需向指定评审账号开放读取权限。

个人用时、架构贡献及工具使用范围见 [贡献说明](docs/04-ai-usage.md)，技术复盘见 [复盘](docs/05-retrospective.md)。提交前请核对记录与实际经历一致。已知限制包括单渠道离散时段、固定 +08:00 时间格式、可信操作者提供资料，以及最多八轮模型决策。
