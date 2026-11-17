# OpenBidKit_Yibiao 接入层

对 [FB208/OpenBidKit_Yibiao](https://github.com/FB208/OpenBidKit_Yibiao)（Electron 标书工具）的评测接入。第三方仓库不入库，先自行 clone 到本目录的 `../repo/`：

```bash
cd integrations/openbidkit
mkdir -p repo && git clone --depth 1 https://github.com/FB208/OpenBidKit_Yibiao repo
```

## 接入原理

Electron 项目无法以 HTTP 服务方式直接测，但 `client/electron/services/bidAnalysisTask.cjs`
等核心任务模块**不依赖 electron**，可在纯 Node 里运行。因此：

```
adapter.py (Python)  →  spawn node_runner.cjs  →  require 仓库真实 bidAnalysisTask.cjs
                                                       └─ 注入 aiService（OpenAI 兼容接口，默认 DeepSeek）
```

被测代码路径 = 仓库真实函数 `runInvalidBidAndRejectionItemsExtraction({aiService, fileContent, sectionHint})`。

## 运行

```bash
export FDE_MODEL_KEY="sk-..."          # OpenAI 兼容 key
export FDE_MODEL_URL="https://api.deepseek.com/v1/chat/completions"   # 可选
cd integrations/openbidkit && python3 run_eval.py --output report.json
```

评估集：`evalset.json`（6 条，query 即招标文件文本）。`node_runner.cjs` 与 `adapter.py` 可作为「Node 桥接」接入模式的模板复用到其他 Electron/Node 项目。

`agent_eval/` 是框架副本（接入目录自包含设计，与根目录框架同源）。
