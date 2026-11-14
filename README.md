# 量化大富翁 · A2A 投顾委员会

一个以 PandaData 数据接口驱动的 A 股量化分析系统，采用 Agent-to-Agent（A2A）架构：

- 总览管家 Agent：拆解任务、并行派发、汇总分歧、传达最终投顾决议
- 巴菲特 Agent：能力圈、高 ROE、护城河、低负债、安全边际
- 彼得·林奇 Agent：成长股分类与 PEG，寻找合理价格的好成长
- 菲利普·费雪 Agent：研发、成长、毛利、管理层，只买真正出色的公司
- 杰西·利弗莫尔 Agent：趋势、量价、突破与止损的右侧交易
- 本杰明·格雷厄姆 Agent：低 PE/PB、高股息、低负债的防御价值

## 快速开始

```bash
pip install -r requirements.txt
copy .env.example .env   # Windows；编辑 PANDA_USERNAME / PANDA_PASSWORD
python run.py
```

浏览器打开 http://127.0.0.1:8000 。输入股票代码/名称后点击“召开会议”，
总览管家会并行派发任务给五位大师 Agent，右侧展示每位大师的独立建议与总管决议。
图表库（ECharts / marked）需放置到 `app/static/vendor/`，见下方“前端依赖”说明。
页面右上角的“设置 Key”可以直接填写大模型 API Key（DeepSeek / Qwen / OpenAI 兼容接口），
保存后立即生效并写回 `.env`，无需重启服务；清空 Key 即切回本地规则模式。

## 命令行

```bash
# 单股巴菲特价值投资报告
python buffett_agent.py analyze 600519
python buffett_agent.py analyze 贵州茅台

# A2A 投顾委员会（总管 + 五位大师）
python buffett_agent.py committee 600519
python buffett_agent.py committee 600519 --json

# 全市场筛选（默认沪深300，前 20 名）
python buffett_agent.py screen --index 000300 --top 20
python buffett_agent.py screen --index 000300 --json
```

## 数据源

全部核心数据来自 `panda_data` SDK（行情、财务三表、分红、回购、股东、审计、指数估值、因子）。
账号配置在 `.env`：

```dotenv
PANDA_USERNAME=86你的手机号
PANDA_PASSWORD=你的官网密码
```

如果需要大模型综合研判，再配置：

```dotenv
DEEPSEEK_API_KEY=sk-your-key-here
DEEPSEEK_BASE_URL=https://api.deepseek.com
MODEL=deepseek-chat
```

## 架构

```text
浏览器（对话 + ECharts）
      │ SSE
      ▼
FastAPI (app/main.py)
      ├─ Agent 编排 (app/agent/core.py)   DeepSeek function-calling / 本地规则模式
      ├─ 工具注册 (app/agent/tool_registry.py)
      │    ├─ a2a_investment_committee   A2A 投顾委员会（总管 + 五位大师）
      │    ├─ buffett_full_analysis   单股巴菲特完整分析
      │    ├─ buffett_screen          全市场筛选
      │    ├─ buffett_market_timing   指数估值与市场情绪
      │    ├─ technical_analysis      技术面（前复权行情）
      │    └─ macro_snapshot          宏观快照
      ├─ A2A 协调层 (app/a2a/orchestrator.py)  总管派发/汇总
      ├─ 大师 Agent (app/a2a/masters.py)       巴菲特/林奇/费雪/利弗莫尔/格雷厄姆
      └─ 数据层 (app/data/pandadata_client.py)  panda_data + TTL 缓存
```

巴菲特分析引擎在 `app/tools/buffett.py`，规则透明可复现：

- 企业质量 35%：盈利、稳定性、现金流、杠杆、成长
- 估值安全边际 25%：owner earnings DCF 折价、PE/PB、股息率
- 管理层 20%：分红、回购、治理、审计
- 市场时机 20%：指数估值分位与趋势

## 免责声明

本项目输出仅用于研究学习与策略验证，不构成任何投资建议。数据来自 PandaData 公开接口，
可能存在延迟、缺失或统计口径差异，请以官方公告为准。

## 稳定性：点击启动

- 应用不会开机自启，也没有后台常驻守护进程。
- 桌面“量化大富翁”快捷方式：双击时自动启动 8000 服务（不健康会先清理僵尸进程再重启），
  就绪后自动打开浏览器；再点一次不会重复启动。
- 日志文件：`logs/server.log`（服务日志）。
- 停止服务：双击桌面“停止量化大富翁”快捷方式。
