"""巴菲特量化交易 Agent 命令行入口。

用法：
    python buffett_agent.py analyze 600519
    python buffett_agent.py analyze 贵州茅台
    python buffett_agent.py screen --index 000300 --top 20
    python buffett_agent.py screen --index 000300 --json
"""
from __future__ import annotations

import argparse
import json
import sys

from app.tools import buffett as buffett_tool
from app.a2a import orchestrator as committee_tool


def cmd_debate(args) -> int:
    """CLI: 直接运行 A2A 多轮审议（不依赖后端服务），可视化大师 Agent 之间的交锋。

    用法：
        python buffett_agent.py debate 贵州茅台 --question "现在能建仓吗？"
        python buffett_agent.py debate 000001 --no-deliberation   # 仅单轮初判
        python buffett_agent.py debate 宁德时代 --rounds 3
    """
    from app.a2a.orchestrator import run_committee
    from app.a2a.deliberation import DeliberationConfig

    print(f"\n=== A2A 多轮审议：{args.query} ===\n")

    def emit(kind, type_, payload):
        if kind == "master":
            m = payload
            print(f"  [{m.get('name')}] {m.get('verdict')} | 评分 {m.get('score'):.0f} | 动作 {m.get('action')}")
        elif kind == "debate_round":
            print("\n--- 审议交锋 ---")
            for m in payload:
                tag = "质询" if m.get("role") == "challenge" else "辩护"
                print(f"  · {tag} [{m.get('sender')} -> {m.get('receiver')}]: {m.get('text', '')[:120]}")

    committee = run_committee(
        args.query,
        question=getattr(args, "question", "") or "",
        enable_deliberation=not getattr(args, "no_deliberation", False),
        deliberation_config=DeliberationConfig(max_rounds=args.rounds),
        emit=emit,
    )
    # run_committee 返回 {ok, data:{chart, summary}}；CLI 直接用 summary
    if not committee.get("ok"):
        print("未能获取数据：", committee.get("error"))
        return 1
    committee = committee["data"]["summary"]

    print("\n=== 总管最终决议 ===")
    print(f"  结论：{committee.get('verdict')}")
    print(f"  加权评分：{committee.get('score'):.0f}/100")
    print(f"  建议仓位：{int(committee.get('allocation_position', 0) * 100)}%  现金：{int(committee.get('allocation_cash', 0) * 100)}%")
    if committee.get("debate"):
        print(f"  辩论回合数：{committee.get('debate_rounds', 0)}  质询/辩护消息数：{len(committee.get('debate', []))}")
        print("  影响力权重：")
        for m in committee.get("agents", []):
            print(f"    - {m.get('name')}: 影响力 {m.get('influence', 1.0)}  加权分 {m.get('weighted_score', m.get('score'))}")
    if committee.get("conflicts"):
        print("  主要分歧：")
        for c in committee["conflicts"]:
            print(f"    · {c}")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="巴菲特价值投资量化 Agent（数据源：panda_data）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    analyze = sub.add_parser("analyze", help="对单只 A 股输出巴菲特价值投资报告")
    analyze.add_argument("query", help="股票代码或名称，如 600519 / 贵州茅台")

    committee = sub.add_parser("committee", help="启动 A2A 投顾委员会（总管 + 五位大师 Agent）")
    committee.add_argument("query", help="股票代码或名称，如 600519 / 贵州茅台")
    committee.add_argument("--json", action="store_true", help="输出原始 JSON")

    screen = sub.add_parser("screen", help="按大师流派筛选指数股票池")
    screen.add_argument("--index", default="000300", help="指数代码，默认 000300")
    screen.add_argument("--top", type=int, default=20, help="返回前 N 只，默认 20")
    screen.add_argument("--master", default="buffett",
                        choices=["buffett", "lynch", "fisher", "livermore", "graham", "all"],
                        help="筛选流派：buffett(巴菲特)/lynch(林奇)/fisher(费雪)/livermore(利弗莫尔)/graham(格雷厄姆)/all(全部)")
    screen.add_argument("--json", action="store_true", help="输出原始 JSON")

    debate = sub.add_parser("debate", help="运行 A2A 多轮审议，展示大师 Agent 之间的质询与辩护")
    debate.add_argument("query", help="股票代码或名称，如 600519 / 贵州茅台")
    debate.add_argument("--question", default="", help="用户附加问题")
    debate.add_argument("--no-deliberation", action="store_true", help="关闭多轮审议，仅做单轮初判")
    debate.add_argument("--rounds", type=int, default=2, help="最大审议轮数，默认 2")

    args = parser.parse_args()

    if args.cmd == "analyze":
        print(buffett_tool._report_markdown(args.query))
        return 0
    if args.cmd == "committee":
        if args.json:
            print(json.dumps(committee_tool.run_committee(args.query), ensure_ascii=False, indent=2))
        else:
            print(committee_tool.committee_report_markdown(args.query))
        return 0
    if args.cmd == "debate":
        return cmd_debate(args)
    if args.cmd == "screen":
        if args.master == "all":
            res = buffett_tool.screen_all(args.index, args.top)
            if args.json:
                print(json.dumps(res, ensure_ascii=False, indent=2))
            else:
                if not res["ok"]:
                    print("筛选失败：", res["error"])
                else:
                    for m in res["data"]["masters"]:
                        print(f"\n=== {m['name']}（{m['school']}）===")
                        print("  方法：", m["method"])
                        if not m["ok"]:
                            print("  ", m["error"])
                            continue
                        for r in m["summary"]["top"]:
                            print(f"  - {r['name']} 评分 {r['total_score']:.0f} {r['verdict']} PE {r['pe']:.0f} PB {r['pb']:.1f}")
                    inter = res["data"]["intersection"]
                    if inter:
                        print("\n=== 多位大师共同选中 ===")
                        for r in inter:
                            print(f"  - {r['name']}（{r['count']} 位大师：{('、'.join(r['masters']))}）")
            return 0
        if args.json:
            print(json.dumps(buffett_tool.screen_by_master(args.master, args.index, args.top), ensure_ascii=False, indent=2))
        else:
            print(buffett_tool._screen_markdown(args.index, args.top, master=args.master))
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
