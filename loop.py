"""Aloop 主循环: 一句话目标 → agent 循环干活 → 目录验收达标才停。

核心模型: 单一入口的事件循环。每次迭代只看 (todo, doing) 两个目录的四种状态:

  todo 非空且有并发空位 → 派出 worker(todo/ → doing/)
  todo 空               → 派 plan agent 看全局(cache//goal//doing//done/)写新待办
                          (唯一的补货条件; worker 在跑也照派, 落地时新待办已排好)
  doing 有残留           → 中断的卡, 加 中断- 前缀归档 cache/done/ 留痕, 永不机械重派

队列单向流动: todo → doing → done。目录布局见 workspace.py, 角色提示词见 prompts.py。

停止条件只有两个: 验收通过, 或 Ctrl-C。

过夜设计(第一性原理: 跑一晚上不死):
- 单 agent 超时/异常 → 杀掉, 卡片带标记归档 cache/done/, plan 决定是否换思路重提
- worker 连续失败(比如 API 配额耗尽) → 指数退避, 恢复后自动续跑
- 长时间零进展 → 不停机, 下次派 plan 时提示换思路
- 迭代内任何代码异常(Windows 文件锁等) → 打日志, 30s 后继续
- 磁盘将满 / 验收命令超时 → 告警或按未达标处理
- 所有输出同时写 workspace/loop.log, 早上可复盘

用法:
    pip install claude-agent-sdk          # 还需要本机已登录的 claude CLI
    python loop.py "调研某个主题, 产出 3 份报告" --count 3
    python loop.py "..." --verify "your-shell-check-command"
"""

import argparse
import asyncio
import pathlib
import re
import shutil
import sys
import time

from agent import heartbeat, kill_all_agents, run_agent
from goal import objective_done, progress_signature
from log import init_log, log
from prompts import PLAN_PROMPT, WATCHDOG_PROMPT, WORK_PROMPT
from workspace import Workspace

WATCHDOG_INTERVAL_SEC = 3600   # 整理员定时节拍: 每 1h 派一次
WATCHDOG_COOLDOWN_SEC = 2700   # 两次整理最短间隔(冷却), 防事件风暴反复重组
WATCHDOG_DUP_TRIGGER = 3       # 重复- 卡累计到这个数就加派一次整理员(事件加码)
FAIL_BACKOFF_BASE = 30      # worker 连续失败时的退避基数(秒), 逐次翻倍
FAIL_BACKOFF_MAX = 1800     # 退避上限 30 分钟
DISK_WARN_FREE_GB = 1       # 磁盘剩余低于这个值就告警
STALL_HINT_IDLE_SEC = 1800  # 多久没有任何文件变化, 下次派 plan 时提示换思路
TICK_SEC = 3                # 事件循环节拍: worker 以分钟计, 3s 无感, 又能及时收割


_SEQ_RE = re.compile(r"^\d{4}-")


def _next_seq(ws: Workspace) -> str:
    """卡片的递增序号: 进 doing/ 时由 loop 统一打上(比 planner 自觉维护可靠, worker 塞的卡也覆盖)。
    计数器落盘 queue/.seq; 丢了顶多从 1 重排, 不影响正确性。"""
    try:
        n = int(ws.seq_file.read_text(encoding="utf-8").strip()) + 1
    except (OSError, ValueError):
        n = 1
    ws.seq_file.write_text(str(n), encoding="utf-8")
    return f"{n:04d}"


def _bare_name(name: str) -> str:
    """剥掉 loop 打的序号前缀(0042-), 还原卡片本名, 用于重复卡比对。"""
    return _SEQ_RE.sub("", name, count=1)


def _unique_target(ws: Workspace, name: str) -> pathlib.Path:
    """cache/done/ 下的不冲突目标名: 已存在就追加 -2/-3 序号。
    (Windows 下 rename 到已存在文件会抛 FileExistsError)"""
    target = ws.cache_done / name
    n = 2
    while target.exists():
        target = ws.cache_done / f"{target.stem}-{n}{target.suffix}"
        n += 1
    return target


def _archive_card(ws: Workspace, doing_file: pathlib.Path, finished: bool) -> None:
    """卡片归档: 正常完成的原样进 cache/done/, 中断的加 中断- 前缀留痕。
    卡片内容保留 worker 死前写的全部笔记; 是否重做、换什么思路, 归 plan 判断。"""
    ws.cache_done.mkdir(parents=True, exist_ok=True)
    name = doing_file.name if finished else f"中断-{doing_file.name}"
    doing_file.rename(_unique_target(ws, name))


async def main() -> None:
    args = parse_args()
    ws = Workspace(args.workspace.resolve())
    ws.ensure()
    init_log(ws.root / "loop.log")
    state: dict = {"extra": ""}   # 不占待办卡片的 agent(plan/整理员), 心跳报出来
    asyncio.create_task(heartbeat(ws.queue_doing, ws.goal, state))

    running: dict[asyncio.Task, pathlib.Path] = {}  # 在跑的 worker: task → doing/ 卡片
    planning: asyncio.Task | None = None            # 在跑的 plan agent(同时最多一个)
    watching: asyncio.Task | None = None            # 在跑的整理员(同时最多一个)
    fail_streak = 0          # worker 连续失败计数 → 指数退避
    backoff_until = 0.0      # 退避到这个时间点之前不派新工、不派 plan
    sig = progress_signature(ws.root)
    last_progress = time.monotonic()
    last_plan_sig: tuple | None = None  # 上次派 plan 时的盘面指纹(None=还没派过)
    last_disk_warn = 0.0     # 磁盘告警节流: 最多每 10 分钟一次
    last_watchdog = time.monotonic()  # 上次派整理员的时间(定时节拍 + 冷却都看它)
    dup_since_watchdog = 0   # 距上次整理以来归档的 重复- 卡数(整理员的事件加码)
    agents_spawned = 0       # 累计派出的 agent 数(预算护栏 + 心跳用)

    while True:
        try:
            # 1. 磁盘守卫: 写满了 agent 会全体失败, 提前喊(节流, 否则每次迭代刷一条)
            now = time.monotonic()
            free_gb = shutil.disk_usage(ws.root).free / 2**30
            if free_gb < DISK_WARN_FREE_GB and now - last_disk_warn > 600:
                log(f"!!! 磁盘只剩 {free_gb:.1f}GB, 来清理, 否则 agent 写不动文件 !!!")
                last_disk_warn = now

            # 1b. 预算护栏(唯一参数): 派工数到顶就停 —— 验收条件写死(永远不达标)时的保险丝
            if args.max_agents is not None and agents_spawned >= args.max_agents:
                log(f"*** 预算到顶(已派 {agents_spawned} 个 agent), 停止 ***")
                if running:
                    kill_all_agents()
                break

            # 2. 收割完成的 worker: 收结果, 没归档的卡代为归档
            for task in [t for t in running if t.done()]:
                doing_file = running.pop(task)
                out = "" if task.cancelled() else task.result()
                if doing_file.exists():
                    _archive_card(ws, doing_file, finished=bool(out))
                log(f"[{doing_file.name}] {out[:300]}")
                if out:
                    fail_streak = 0
                    backoff_until = 0.0
                else:
                    fail_streak += 1
                    backoff = min(FAIL_BACKOFF_BASE * 2 ** (fail_streak - 1), FAIL_BACKOFF_MAX)
                    backoff_until = now + backoff
                    log(f"--- worker 连续失败 {fail_streak} 个, 退避 {backoff}s(疑似 API 故障) ---")

            # 2b. 孤儿卡: doing/ 里没有 task 认领的卡(启动残留/派发中途异常) → 按中断归档
            tracked = set(running.values())
            for orphan in ws.queue_doing.glob("*.md"):
                if orphan not in tracked:
                    _archive_card(ws, orphan, finished=False)
                    log(f"--- 孤儿卡: {orphan.name} 无 task 认领, 按中断归档 ---")

            # 3. 收割 plan / 整理员(失败同样进退避, 防止 API 故障时空转刷请求)
            if planning is not None and planning.done():
                out = planning.result()
                log((out or "[plan 无输出]")[:500])
                planning = None
                state["extra"] = ""
                if not out:
                    fail_streak += 1
                    backoff = min(FAIL_BACKOFF_BASE * 2 ** (fail_streak - 1), FAIL_BACKOFF_MAX)
                    backoff_until = now + backoff
            if watching is not None and watching.done():
                watching = None
                state["extra"] = ""

            # 4. 整理员: 定时 1h 兜底 + 重复卡事件加码, 共用 45min 冷却
            since_wd = now - last_watchdog
            wd_reason = None
            if since_wd >= WATCHDOG_INTERVAL_SEC:
                wd_reason = "定时例行整理"
            elif dup_since_watchdog >= WATCHDOG_DUP_TRIGGER and since_wd >= WATCHDOG_COOLDOWN_SEC:
                wd_reason = (f"最近归档了 {dup_since_watchdog} 张重复待办卡, "
                             "重点归置 cache/done/(按主题开子目录), 帮 planner 查重")
            if wd_reason and watching is None:
                log(f"--- watchdog: {wd_reason} ---")
                state["extra"] = "整理员在收拾 cache/"
                watching = asyncio.create_task(run_agent(
                    WATCHDOG_PROMPT.format(goal=args.goal) + f"\n\n本次触发原因: {wd_reason}",
                    ws.root, args.model, args.timeout))
                last_watchdog = now
                dup_since_watchdog = 0
                agents_spawned += 1

            # 5. 验收: 代码检查 goal/, 不看模型说什么
            if objective_done(args, ws.root):
                log("*** 验收通过, 目标达成 ***")
                if running:
                    log(f"--- 还有 {len(running)} 个 worker 在跑, 杀掉退出; "
                        "卡片下次启动时按中断归档 ---")
                    kill_all_agents()
                break

            # 6. 单一入口的四种情况
            todos = sorted(ws.queue_todo.glob("*.md"))
            free = args.parallel - len(running)
            if todos and free > 0 and now >= backoff_until:
                dispatched = 0
                in_flight = {_bare_name(p.name) for p in ws.queue_doing.glob("*.md")}
                for todo_file in todos[:free]:                  # 情况 2/3: 有空位就派
                    if _bare_name(todo_file.name) in in_flight:
                        # plan 重复提出了在途任务(同名卡已在 doing) → 重复- 前缀归档留痕, 不派发
                        ws.cache_done.mkdir(parents=True, exist_ok=True)
                        todo_file.rename(_unique_target(ws, f"重复-{todo_file.name}"))
                        dup_since_watchdog += 1
                        log(f"--- 重复待办(与在跑卡同名): {todo_file.name} 归档留痕 ---")
                        continue
                    # 认领: todo/ → doing/, 同时打上递增序号(新旧一眼可见, 中断/重复归档后血缘可追)
                    doing_file = ws.queue_doing / f"{_next_seq(ws)}-{todo_file.name}"
                    todo_file.rename(doing_file)
                    prompt = WORK_PROMPT.format(
                        goal=args.goal,
                        todo=doing_file.read_text(encoding="utf-8"),
                        todo_path=doing_file.name,
                        timeout=args.timeout,
                    )
                    running[asyncio.create_task(
                        run_agent(prompt, ws.root, args.model, args.timeout))] = doing_file
                    dispatched += 1
                if dispatched:
                    agents_spawned += dispatched
                    log(f"=== 派出 {dispatched} 个 worker"
                        f"(在跑 {len(running)}, 排队 {len(todos) - dispatched}) ===")
            elif not todos and planning is None and now >= backoff_until:
                # 情况 00/01: todo 空 → 补货。worker 照跑它的, 互不等。
                # 但盘面没变(cachedone/cache 无新内容)就不重复想: 同样的输入只会得到同样的结论
                stalled = now - last_progress > STALL_HINT_IDLE_SEC
                if last_plan_sig is None or sig != last_plan_sig or stalled:
                    hint = ""
                    if stalled:
                        hint = ("\n注意: 很长时间没有任何新进展, 不要重复旧方向, "
                                "换一批思路(更宽或更深)。")
                        last_progress = now
                        log("!!! 长时间零进展, 提示 plan 换思路 !!!")
                    log("=== 待办空了, 派 plan agent 看全局想新方向 ===")
                    state["extra"] = "plan agent 在想方向"
                    planning = asyncio.create_task(run_agent(
                        PLAN_PROMPT.format(goal=args.goal) + hint, ws.root,
                        args.plan_model or args.model, args.timeout))
                    last_plan_sig = sig
                    agents_spawned += 1
            # 其余情况(plan 在飞 / 退避期): 什么都不派, 下一次迭代再来看

            # 7. 进展指纹(只看 cache/goal, queue 抖动不算进展): 变了刷新时间戳
            cur = progress_signature(ws.root)
            if cur != sig:
                sig = cur
                last_progress = time.monotonic()

            state["spawned"] = agents_spawned   # 心跳报预算用
            await asyncio.sleep(TICK_SEC)
        except Exception as exc:
            # 迭代级兜底: Windows 文件锁/编码错误等任何意外都不许杀死 loop
            log(f"[迭代异常, 30s 后继续] {type(exc).__name__}: {exc}")
            await asyncio.sleep(30)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Aloop: 目标驱动 agent 死循环")
    p.add_argument("goal", help="一句话目标")
    p.add_argument("--workspace", type=pathlib.Path, default=pathlib.Path("workspace"),
                   help="工作目录(黑板目录的家), 默认 ./workspace")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--count", type=int, help="验收: goal/ 下一级子目录数 >= N 即达标(一个子目录 = 一份成果)")
    g.add_argument("--verify", help="验收: 自定义 shell 命令, 退出码 0 即达标")
    p.add_argument("--parallel", type=int, default=6, help="最大并发 agent 数, 默认 6")
    p.add_argument("--model", default="deepseek-v4.1-flash",
                   help="执行类 agent(worker/整理员)用的模型, 默认 deepseek-v4.1-flash")
    p.add_argument("--plan-model", dest="plan_model", default=None,
                   help="plan agent(规划方向)用的模型, 默认同 --model; 规划是全局判断, 值得用更强的模型")
    p.add_argument("--timeout", type=int, default=25,
                   help="单个 agent 的超时分钟数, 超时就杀掉、卡片带标记归档, 默认 25;"
                        " 别设太长: agent 零上下文新建很便宜, session 跑太久上下文反而烂掉")
    p.add_argument("--max-agents", type=int, default=None,
                   help="预算护栏: 累计派出 agent 到 N 个就停, 默认不限;"
                        " 验收条件写死(永远不达标)时的保险丝")
    return p.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Ctrl-C = 你主动停。状态全在 workspace 磁盘上, 重跑即续。
        kill_all_agents()   # 连带杀掉在跑的 claude CLI 进程树, 不留孤儿
        print("\n已停止。重跑同样的命令即可从断点继续。")
        sys.exit(130)
