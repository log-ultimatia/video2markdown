#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
diag_open.py —— 诊断「打开文件 / 打开目录」为什么失败

在【有控制台】的环境下运行（这样能看到过程），结果同时写入 diag_open.log，
把那个日志发给开发者就能定位问题。

用法（在 youtube2md_lab 目录下）：
    .venv\\Scripts\\python.exe diag_open.py
或者直接双击同目录下的「诊断打开方式.bat」。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

LAB = Path(__file__).resolve().parent
LOG_PATH = LAB / "diag_open.log"
NUL = subprocess.DEVNULL

# ShellExecute 的返回值含义（<=32 都是错误码）
SHELL_CODES = {
    0: "内存不足 / 执行失败",
    2: "找不到文件 (ERROR_FILE_NOT_FOUND)",
    3: "找不到路径 (ERROR_PATH_NOT_FOUND)",
    5: "访问被拒绝 (ERROR_ACCESS_DENIED)",
    26: "共享冲突 (ERROR_SHARING_VIOLATION)",
    27: "关联不完整 (ERROR_FILE_ASSOCIATION_INCOMPLETE)",
    28: "DDE 超时",
    29: "DDE 忙",
    30: "DDE 失败",
    31: "没有关联的程序 (ERROR_NO_ASSOCIATION)",
    32: "DLL 加载失败 (ERROR_DLL_NOT_FOUND)",
}

_log = open(LOG_PATH, "w", encoding="utf-8")


def log(msg: str = "") -> None:
    print(msg)
    _log.write(str(msg) + "\n")
    _log.flush()


def shell_open(target: str, params: str | None = None) -> int:
    """调 ShellExecuteW，返回原始返回值（>32 为成功）。"""
    try:
        return int(ctypes.windll.shell32.ShellExecuteW(
            None, "open", target, params, None, 1))
    except Exception as exc:
        log(f"    ShellExecuteW 抛异常: {type(exc).__name__}: {exc}")
        return -1


def report_shell(label: str, target: str, params: str | None = None) -> None:
    log(f"\n--- {label} ---")
    log(f"  target = {target}")
    if params:
        log(f"  params = {params}")
    rc = shell_open(target, params)
    if rc > 32:
        log(f"  ShellExecuteW 返回 {rc}  -> ✓ 成功")
    elif rc < 0:
        log(f"  ShellExecuteW 抛异常")
    else:
        desc = SHELL_CODES.get(rc, "未知错误码")
        log(f"  ShellExecuteW 返回 {rc}  -> ✗ 失败：{desc}")
    time.sleep(2.5)


def report_popen(label: str, cmd: list) -> None:
    log(f"\n--- {label} ---")
    log(f"  cmd = {cmd}")
    try:
        p = subprocess.Popen(cmd, stdin=NUL, stdout=NUL, stderr=NUL)
        log(f"  Popen 成功，pid={p.pid}")
        for i in range(3):
            time.sleep(1)
            code = p.poll()
            if code is not None:
                extra = "（0xC0000142 = DLL 初始化失败）" if (code & 0xFFFFFFFF) == 0xC0000142 else ""
                log(f"  {i + 1} 秒后已退出，退出码 = 0x{code & 0xFFFFFFFF:08X}{extra}")
                break
        else:
            log("  3 秒后仍在运行（正常）")
    except Exception as exc:
        log(f"  Popen 抛异常: {type(exc).__name__}: {exc}")
    time.sleep(1.5)


def report_startfile(label: str, path: str) -> None:
    log(f"\n--- {label} ---")
    log(f"  path = {path}")
    try:
        os.startfile(path)
        log("  os.startfile 返回（不报错不代表窗口一定出来了）")
    except OSError as exc:
        log(f"  os.startfile 失败: [WinError {exc.winerror}] {exc.strerror}")
    time.sleep(2.5)


def main() -> int:
    log("=" * 70)
    log(" 打开方式诊断")
    log("=" * 70)
    log(f"Python      : {sys.executable}")
    log(f"版本        : {sys.version.split()[0]}")
    log(f"cwd         : {os.getcwd()}")
    log(f"stdin       : {sys.stdin!r}")
    log(f"stdout      : {sys.stdout!r}")
    log(f"stderr      : {sys.stderr!r}")
    log(f"是否 pythonw: {'是（无控制台）' if sys.stdout is None else '否（有控制台）'}")

    # 找一个真实的 md 文件
    out_dir = LAB / "output"
    files = sorted(out_dir.glob("*.md")) if out_dir.exists() else []
    if not files:
        log(f"\n!! {out_dir} 里没有 .md 文件，无法测试。先随便生成一份笔记。")
        return 1
    target = files[0]
    log(f"\n测试文件    : {target.name}")
    log(f"所在目录    : {target.parent}")
    log(f"文件存在    : {target.exists()}")

    log("\n" + "=" * 70)
    log(" 一、打开所在目录")
    log("=" * 70)
    report_shell("1) ShellExecuteW: open 目录", str(target.parent))
    report_popen("2) Popen: explorer 目录", ["explorer", str(target.parent)])
    report_startfile("3) os.startfile: 目录", str(target.parent))

    log("\n" + "=" * 70)
    log(" 二、定位并选中文件")
    log("=" * 70)
    report_shell("4) ShellExecuteW: explorer /select",
                 "explorer.exe", f'/select,"{target}"')
    report_popen("5) Popen: explorer /select (一个参数)",
                 ["explorer", f"/select,{target}"])
    report_popen("6) Popen: explorer /select (两个参数)",
                 ["explorer", "/select,", str(target)])

    log("\n" + "=" * 70)
    log(" 三、打开文件本身")
    log("=" * 70)
    report_shell("7) ShellExecuteW: open 文件", str(target))
    report_startfile("8) os.startfile: 文件", str(target))

    # 关联查询
    log("\n" + "=" * 70)
    log(" 四、.md 的关联状态")
    log("=" * 70)
    try:
        shlwapi = ctypes.WinDLL("shlwapi", use_last_error=True)
        shlwapi.AssocQueryStringW.argtypes = [
            ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p,
            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
        shlwapi.AssocQueryStringW.restype = ctypes.c_long
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.c_ulong(1024)
        hr = shlwapi.AssocQueryStringW(0, 2, ".md", None, buf, ctypes.byref(n))
        log(f"  AssocQueryStringW 返回 0x{hr & 0xFFFFFFFF:08X}")
        log(f"  -> 打开程序: {buf.value or '(空)'}")
    except Exception as exc:
        log(f"  查关联失败: {type(exc).__name__}: {exc}")

    log("\n" + "=" * 70)
    log(" 诊断结束")
    log(f" 日志已写入: {LOG_PATH}")
    log("=" * 70)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        log("未捕获异常:\n" + traceback.format_exc())
        sys.exit(1)
    finally:
        _log.close()
