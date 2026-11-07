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


def main() -> int:
    if len(sys.argv) > 1:
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
