"""goal: 验收与进展度量 —— 代码说了算, 模型说了不算。"""

import argparse
import pathlib
import subprocess

from log import log


def objective_done(args: argparse.Namespace, workspace: pathlib.Path) -> bool:
    """验收目标是否达成。返回 True 才停。"""
    if args.verify is not None:
        # 自定义验收命令: shell 跑, 退出码 0 = 达标; 命令自己挂住按未达标算
        try:
            return subprocess.run(args.verify, cwd=workspace, shell=True,
                                  capture_output=True, timeout=60).returncode == 0
        except subprocess.TimeoutExpired:
            log("[验收命令超时 60s, 当作未达标]")
            return False
    # 默认验收: goal/ 下一级子目录数 >= count —— 一个子目录 = 一份成果单元
    # (成果连同它的附件/截图都在自己目录里, 嵌套多深都只算一份)
    goal_dir = workspace / "goal"
    count = sum(1 for p in goal_dir.iterdir() if p.is_dir()) if goal_dir.exists() else 0
    return count >= args.count


def progress_signature(workspace: pathlib.Path) -> tuple[int, int]:
    """(文件数, 总字节数): cache/goal 两个目录的指纹, 变了才算有进展。
    不含 queue/: 待办文件被编辑是"工作在继续"的假象, 僵尸待办每轮
    更新自己的进展笔记会让指纹永远变化, 零进展检测将永不触发。"""
    files, size = 0, 0
    for d in ("cache", "goal"):
        for f in (workspace / d).rglob("*"):
            if f.is_file():
                files += 1
                size += f.stat().st_size
    return files, size
