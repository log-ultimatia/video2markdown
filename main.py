#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
main.py —— 统一入口（打包成 exe 后也用它）

## 为什么需要这个文件

`app.py` 的图形界面是用【子进程】调用 `pipeline.py` 的：

    python.exe -u pipeline.py <链接> --model small ...

开发时这没问题，但打包成 exe 之后 `sys.executable` 变成了 exe 自己，
再拼上 `pipeline.py` 就成了"再启动一次 GUI" —— 子进程永远跑不到流水线。

所以打包版改成让 exe 用参数分派自己的模式：

    video2markdown.exe                         -> 图形界面
    video2markdown.exe --run-pipeline <参数…>   -> 流水线（等价于 python pipeline.py）
    video2markdown.exe --run-check-env  <参数…>  -> 环境自检

`app.py` 里会根据 `sys.frozen` 自动选这两种拼法，见 `_pipeline_cmd()`。

## 开发时也能用

    python main.py                    # 打开界面
    python main.py --run-pipeline URL # 跑流水线
"""

from __future__ import annotations

import sys

FROZEN = bool(getattr(sys, "frozen", False))


def _drop_dispatch_arg(name: str) -> None:
    """去掉 argv 里的分派参数，让被调模块看到自己期望的 argv。"""
    if name in sys.argv:
        sys.argv.remove(name)


def run_pipeline() -> int:
    _drop_dispatch_arg("--run-pipeline")
    if sys.argv:
        sys.argv[0] = "pipeline.py"
    import pipeline
    return pipeline.main()


def run_check_env() -> int:
    _drop_dispatch_arg("--run-check-env")
    if sys.argv:
        sys.argv[0] = "check_env.py"
    import check_env
    return check_env.main()


def selfcheck() -> int:
    """
    打印"打包后到底会调用什么"，用来排查"打包版点了没反应/找不到文件"。

    打包版里 app.py 是编进 exe 的，外面看不到源码，只能靠它自报家门：
        v2m-cli.exe --selfcheck
    """
    import app

    print("=== 运行环境 ===")
    print(f"  frozen      : {app.FROZEN}")
    print(f"  可执行文件  : {sys.executable}")
    print(f"  资源目录    : {app.LAB_DIR}")
    print(f"  数据目录    : {app.DATA_DIR}")
    print(f"  输出目录    : {app.OUTPUT_DIR}")
    print()
    print("=== 子系统命令 ===")
    pipe = app._pipeline_cmd("URL")
    env = app._check_env_cmd()
    print(f"  流水线: {pipe}")
    print(f"  自检  : {env}")
    print()
    print("=== 依赖文件是否存在 ===")
    from pathlib import Path

    for label, path in (("v2m-cli.exe", Path(pipe[0])),
                        ("pipeline.py", app.PIPELINE),
                        ("check_env.py", app.CHECK_ENV)):
        print(f"  {label:<14} {'存在' if path.exists() else '不存在'}  {path}")
    return 0


def main() -> int:
    if len(sys.argv) > 1:
        if sys.argv[1] == "--selfcheck":
            return selfcheck()
        if sys.argv[1] == "--run-pipeline":
            return run_pipeline()
        if sys.argv[1] == "--run-check-env":
            return run_check_env()
        if sys.argv[1] in ("-h", "--help", "-?"):
            print(__doc__)
            return 0
    import app
    return app.main()


if __name__ == "__main__":
    sys.exit(main())
