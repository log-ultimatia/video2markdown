#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
app.py —— YouTube → Markdown 图形界面

设计要点：
  · 所有路径都用 __file__ 推导，不依赖当前工作目录
    （桌面快捷方式启动时 cwd 可能是任意位置，用相对路径必炸）
  · pipeline.py 以【子进程】方式运行，而不是 import
      - 能实时看到进度（下载百分比、各层日志）
      - 点「停止」就是终止子进程，立刻生效，不用等
      - 子进程崩了不会带崩界面
  · 只有【流水线那个 python.exe】带 CREATE_NO_WINDOW（它是控制台程序，
    不带会弹黑框）；打开资源管理器/编辑器这类 GUI 程序【绝不能】带 ——
    实测会让它们以 0xC0000142（DLL 初始化失败）退出
  · 用 venv 里的 python 跑流水线，保证依赖齐全

启动方式：
  · 双击 run_gui.vbs / 桌面快捷方式（无控制台窗口）
  · 或命令行： .venv\\Scripts\\python.exe app.py
"""

from __future__ import annotations

import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# ---------------------------------------------------------------------------
# 路径（全部相对本文件，与 cwd 无关）
# ---------------------------------------------------------------------------
LAB_DIR = Path(__file__).resolve().parent
VENV_PY = LAB_DIR / ".venv" / "Scripts" / "python.exe"
PIPELINE = LAB_DIR / "pipeline.py"
CHECK_ENV = LAB_DIR / "check_env.py"
OUTPUT_DIR = LAB_DIR / "output"
DOWNLOAD_DIR = LAB_DIR / "downloads"
ASSETS = LAB_DIR / "assets"
ICON = ASSETS / "youtube2md.ico"

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 诊断日志。pythonw 没有 stderr，界面上出的错如果不写文件就彻底丢了。
LOG_DIR = LAB_DIR / "logs"
GUI_LOG = LOG_DIR / "gui.log"

# 内容类型里可选择的路由策略：改成调 pipeline 的 --proxy / --no-proxy
DEFAULT_PROXY = "http://127.0.0.1:7890"

# 流水线的 5 个阶段（与 pipeline.py 的 banner 文案对应）
STAGE_NAMES = ("代理", "下载", "提取", "转写", "输出")

# 语言：None = 自动检测（推荐）。指定语言能省掉检测步骤，边界场景更稳。
LANG_CHOICES = (
    # 不指定语言 = 逐窗口自动检测，适合中英混排；指定语言则锁死该语言
    ("自动检测/混合", None),
    ("中文 zh", "zh"),
    ("英语 en", "en"),
    ("日语 ja", "ja"),
    ("韩语 ko", "ko"),
)

MODEL_CHOICES = (
    ("tiny", "最快，准确率一般（39MB）"),
    ("base", "默认，速度与准确率平衡（145MB）"),
    ("small", "中文推荐，明显更准（484MB）"),
    ("medium", "最准，慢约 2 倍（1.5GB）"),
)

DEVICE_CHOICES = (
    ("自动（推荐）", "auto"),
    ("CPU（int8）", "cpu"),
    ("NVIDIA CUDA GPU", "cuda"),
    ("Intel Ultra NPU（OpenVINO）", "npu"),
)

BROWSER_COOKIE_CHOICES = (
    ("Chrome", "chrome"),
    ("Edge", "edge"),
    ("Firefox", "firefox"),
    ("Brave", "brave"),
    ("Opera", "opera"),
    ("Vivaldi", "vivaldi"),
)

# ---------------------------------------------------------------------------
# 配色
# ---------------------------------------------------------------------------
C_BG = "#f4f6fa"
C_CARD = "#ffffff"
C_BORDER = "#dbe2ea"
C_TEXT = "#1c2b3a"
C_MUTED = "#6b7c8f"
C_ACCENT = "#1f3a5f"
C_OK = "#12805c"
C_WARN = "#b45309"
C_ERR = "#c0392b"
C_STEP = "#1d4ed8"

UI_FONT = ("Microsoft YaHei UI", 9)
UI_FONT_SM = ("Microsoft YaHei UI", 8)
UI_FONT_BIG = ("Microsoft YaHei UI", 16, "bold")
MONO_FONT = ("Consolas", 9)

MAX_LOG_LINES = 800


def enable_dpi_awareness() -> None:
    """高分屏下让界面不发糊。必须在创建 Tk 之前调用。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ---------------------------------------------------------------------------
class LaunchError(RuntimeError):
    """启动外部程序失败。与代码 bug 区分开，好给出针对性的提示。"""


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.queue: queue.Queue = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self.worker: threading.Thread | None = None
        self.running = False
        self.stop_requested = False
        self.last_md: Path | None = None

        # 关键：接管 tkinter 的异常回调。
        # pythonw.exe 的 stderr 是 None，回调里抛出的异常默认写向 stderr，
        # 结果是【完全静默】—— 用户只看到"点了没反应"，没有任何线索。
        # 这里统一兜住：写日志 + 弹框，把真正的原因暴露出来。
        self.root.report_callback_exception = self._on_callback_error

        # 阶段进度状态
        self._stage_idx = -1          # 当前进行到第几个阶段（-1 = 未开始）
        self._stage_t0 = 0.0          # 当前阶段的开始时刻
        self._stage_done = [0.0] * 5  # 各阶段已用秒数
        self._stage_state = ["idle"] * 5
        self._run_t0 = 0.0            # 整轮开始时刻

        self._setup_window()
        self._setup_style()
        self._build()

        self.refresh_outputs()
        self._drain_queue()
        self._boot_log()

    # ---- 窗口 ----
    def _setup_window(self) -> None:
        self.root.title("YouTube → Markdown")
        self.root.configure(bg=C_BG)
        # 先给个临时尺寸，_build() 结束后会按内容实测需求收紧（见 _fit_window）
        self.root.geometry("980x900")
        self.root.minsize(760, 560)
        # 图标（没有也不影响）
        try:
            if ICON.exists():
                self.root.iconbitmap(default=str(ICON))
        except Exception:
            pass
        # 任务栏用独立图标分组，而不是显示 python 的图标
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "youtube2md.lab.app")
        except Exception:
            pass

    def _setup_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=C_BG)
        style.configure("TLabel", background=C_BG, foreground=C_TEXT, font=UI_FONT)
        style.configure("Card.TFrame", background=C_CARD)
        style.configure("Section.TLabel", background=C_BG, foreground=C_ACCENT,
                        font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Hint.TLabel", background=C_BG, foreground=C_MUTED,
                        font=UI_FONT_SM)
        style.configure("Status.TLabel", background=C_BG, foreground=C_MUTED,
                        font=UI_FONT_SM)
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"),
                        padding=(16, 7))
        style.configure("TButton", font=UI_FONT, padding=(10, 5))
        style.configure("TCombobox", font=UI_FONT)
        style.configure("Treeview", font=UI_FONT, rowheight=24,
                        background=C_CARD, fieldbackground=C_CARD)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TProgressbar", thickness=8)

    # ---- 界面骨架 ----
    def _build(self) -> None:
        outer = tk.Frame(self.root, bg=C_BG)
        outer.pack(fill="both", expand=True)

        self._build_header(outer)
        body = tk.Frame(outer, bg=C_BG)
        body.pack(fill="both", expand=True, padx=18, pady=(8, 12))

        self._build_source(body)
        self._build_actions(body)
        self._build_outputs(body)
        self._build_log(body)
        self._fit_window()

    def _fit_window(self) -> None:
        """
        按【内容实际需求】决定窗口大小。

        为什么不写死高度：开启 DPI 感知后，Tk 的单位≈物理像素，
        系统缩放 175% 时 tk scaling = 2.33，所有控件连字体一起放大约 75%，
        内容实际需要 900+ px。写死 780/840 的后果是日志区被 pack 压成一条缝
        （实测压到 57px，几乎看不见）。

        这里量出来再定，上限是屏幕物理高度 —— 小屏幕时降级为让日志区变矮，
        而不是整个底部被切掉。
        """
        self.root.update_idletasks()
        need_h = self.root.winfo_reqheight()
        need_w = self.root.winfo_reqwidth()
        screen_h = self.root.winfo_screenheight()
        screen_w = self.root.winfo_screenwidth()

        # 屏幕矮的时候，内容装不下，pack 会把最后 pack 的日志区压成一条缝。
        # 日志是主要工作区，不能压；宁可先隐藏那行纯提示文字（--dnd_hint--）。
        if need_h + 8 > screen_h - 100:
            try:
                if self.dnd_hint.winfo_manager():
                    self.dnd_hint.pack_forget()
                    self.root.update_idletasks()
                    need_h = self.root.winfo_reqheight()
                    print("[布局] 屏幕偏矮，已隐藏提示行以保证日志区可见")
            except (AttributeError, tk.TclError):
                pass

        # +8: 留一点余量，避免正好卡在临界值上触发一次收缩
        h = max(560, min(need_h + 8, screen_h - 100))
        w = max(900, min(max(need_w + 40, 980), screen_w - 120))
        self.root.geometry(f"{w}x{h}")

    def _open_via_cmd(self, cmdline: str, what: str = "") -> bool:
        """
        用 cmd 的 start 命令打开目标 —— 【实测唯一在受限环境下可用的方式】。

        为什么它能行、直接 Popen 不行（这是实测结论，不是推测）：
            直接 subprocess.Popen(["explorer", ...]) 时，explorer 成为 pythonw
            的子进程，继承 pythonw 那套【无效的标准句柄】（pythonw 的
            sys.stdout/sys.stderr 都是 None），于是 explorer 在 DLL 初始化
            阶段就崩：0xC0000142；Code.exe 则崩在 0x80000003(STATUS_BREAKPOINT)。

            走 cmd /c start 时，目标程序是由 cmd.exe 创建的。cmd 本身是控制台
            进程，会为自己建立正常的控制台/句柄环境，目标程序因此能正常初始化。

        诊断实测（同一台机器）：
            Popen explorer         -> 0xC0000142
            Popen Code.exe         -> 0x80000003
            Popen cmd /c start     -> 仍在运行 ✓

        cmdline 直接给完整命令行字符串（不走 list，避免 list2cmdline 把
        /select,<路径> 这种参数加引号，破坏 explorer 的参数解析）。
        """
        try:
            subprocess.Popen(
                cmdline, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            return True
        except Exception as exc:
            self._write_log(f"_open_via_cmd 失败: {cmdline}\n  {exc}")
            self.log_line(f"  cmd 启动失败：{exc}", "warn")
            return False

    def _shell_open(self, target: str, params: str | None = None) -> bool:
        """
        用 ShellExecuteW 打开目标。返回是否成功。

        为什么优先用它，而不是 subprocess.Popen：
        Popen 走 CreateProcess，需要传 STARTUPINFO + 一组【可继承的句柄】。
        pythonw.exe 自己没有标准句柄（stdout/stderr 都是 None），
        在这种父进程下，explorer.exe 这类系统程序可能在初始化阶段就失败，
        报 0xc0000142（STATUS_DLL_INIT_FAILED）。

        ShellExecuteW 走的是 Windows 外壳，由 shell 负责解析关联并启动目标程序，
        完全不涉及我们这边的句柄继承 —— 这是"打开文件/打开目录"最正统的做法。

        返回值 > 32 才算成功（这是 ShellExecute 的约定，<=32 都是错误码）。
        """
        if os.name != "nt":
            return False
        try:
            import ctypes
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "open", target, params, None, 1)   # SW_SHOWNORMAL = 1
            if rc > 32:
                return True
            self._write_log(
                f"ShellExecuteW 返回 {rc}（<=32 视为失败）\n"
                f"  target={target}\n  params={params}")
        except Exception as exc:
            self._write_log(f"ShellExecuteW 异常: {type(exc).__name__}: {exc}")
        return False

    # ==================================================================
    # 诊断：异常与日志
    # ==================================================================
    def _write_log(self, text: str) -> None:
        """把诊断信息追加到 logs/gui.log（pythonw 下没有 stderr，只能靠文件）。"""
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(GUI_LOG, "a", encoding="utf-8") as fh:
                fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
                fh.write(text.rstrip() + "\n")
        except Exception:
            pass

    def _on_callback_error(self, exc_type, exc, tb) -> None:
        """
        tkinter 回调里未捕获的异常统一走这里。

        以前这些异常写到 sys.stderr，而 pythonw 下 sys.stderr 是 None，
        等于彻底消失 —— 这正是"点按钮没反应"最可能的表象。
        """
        detail = "".join(traceback.format_exception(exc_type, exc, tb))
        self._write_log(detail)
        self.log_line("", "")
        self.log_line(f"[错误] {exc_type.__name__}: {exc}", "err")
        self.log_line(f"       详细信息已写入 {GUI_LOG}", "dim")
        try:
            messagebox.showerror(
                "操作失败",
                f"{exc_type.__name__}: {exc}\n\n"
                f"详细信息已写入：\n{GUI_LOG}\n\n"
                f"可以先试试「打开」（程序内预览），它不依赖外部程序。")
        except Exception:
            pass

    def _spawn(self, cmd: list, what: str) -> bool:
        """
        启动一个外部程序，失败时把原因说清楚。

        为什么要 stdin/stdout/stderr 都指到 DEVNULL：
        GUI 程序（pythonw.exe）自己没有标准句柄（sys.stdout 是 None）。
        子进程默认【继承】这些无效句柄，某些程序在 CRT 初始化 stdio 时
        会直接失败并报 0xc0000142（STATUS_DLL_INIT_FAILED），窗口永远不出现。
        显式给 DEVNULL 就绕开了这个坑。

        另外启动后短暂检查一下是否立刻退出 —— 立刻退出说明参数或环境有问题，
        这时候要报错，而不是让用户以为"点了个寂寞"。

        为什么【不】传 CREATE_NO_WINDOW：
        这个标志是给"控制台程序"隐藏黑框用的。explorer / Code.exe / notepad
        本来就是 GUI 程序，没有黑框可隐藏，传了纯属多余；更糟的是，
        实测给 explorer 带上它会报 0xc0000142（STATUS_DLL_INIT_FAILED）。
        隐藏黑框的需求由 DEVNULL + GUI 子系统本身满足。
        """
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            raise LaunchError(
                f"找不到可执行文件：{cmd[0]}\n"
                f"（{what}）")
        except OSError as exc:
            raise LaunchError(
                f"启动 {cmd[0]} 失败：{type(exc).__name__}: {exc}\n"
                f"（{what}）")
        # 等一小会儿看它是不是启动即崩。
        # 这段是在主线程跑的（按钮回调里），所以别太长 —— 0.5 秒既够看出
        # "立刻退出"，又不至于让界面明显卡顿。
        time.sleep(0.5)
        code = proc.poll()
        if code not in (None, 0, 1):
            # 退出码 1 是 explorer 正常转发后的返回，不算错
            raise LaunchError(
                f"{cmd[0]} 启动后立即退出（退出码 0x{code & 0xFFFFFFFF:X}）。\n"
                f"（{what}）\n"
                f"退出码 0xC0000142 表示程序初始化失败，通常和权限/环境有关。")
        return True

    def _build_header(self, parent) -> None:
        head = tk.Frame(parent, bg=C_ACCENT)
        head.pack(fill="x")
        tk.Label(head, text="YouTube → Markdown", bg=C_ACCENT, fg="white",
                 font=UI_FONT_BIG).pack(anchor="w", padx=20, pady=(11, 1))
        tk.Label(head, text="填入视频链接，自动下载音频、识别语音、生成带时间戳的 Markdown",
                 bg=C_ACCENT, fg="#c7d6e8", font=UI_FONT_SM).pack(
            anchor="w", padx=20, pady=(0, 11))

    def _build_source(self, parent) -> None:
        ttk.Label(parent, text="① 视频来源", style="Section.TLabel").pack(
            anchor="w", pady=(2, 5))

        card = tk.Frame(parent, bg=C_CARD, highlightbackground=C_BORDER,
                        highlightthickness=1)
        card.pack(fill="x")

        row = tk.Frame(card, bg=C_CARD)
        row.pack(fill="x", padx=12, pady=(10, 4))
        self.source_var = tk.StringVar()
        self.entry = tk.Entry(
            row, textvariable=self.source_var, font=UI_FONT,
            bg="white", fg=C_TEXT, relief="solid", bd=1,
            insertbackground=C_TEXT)
        self.entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.entry.bind("<Return>", lambda _e: self.start())
        self._attach_paste_menu(self.entry)

        ttk.Button(row, text="选择本地文件…", command=self._browse).pack(
            side="left", padx=(8, 0))

        # 代理
        prow = tk.Frame(card, bg=C_CARD)
        prow.pack(fill="x", padx=12, pady=(1, 3))
        tk.Label(prow, text="代理：", bg=C_CARD, fg=C_MUTED,
                 font=UI_FONT).pack(side="left")
        self.proxy_var = tk.StringVar(value=DEFAULT_PROXY)
        self.proxy_entry = tk.Entry(
            prow, textvariable=self.proxy_var, font=UI_FONT, width=28,
            bg="white", fg=C_TEXT, relief="solid", bd=1)
        self.proxy_entry.pack(side="left", ipady=4)
        self.proxy_state = tk.Label(prow, text="", bg=C_CARD, fg=C_MUTED,
                                    font=UI_FONT_SM)
        self.proxy_state.pack(side="left", padx=(8, 0))
        ttk.Button(prow, text="检测", width=6,
                   command=self.test_proxy).pack(side="left", padx=(8, 0))
        self.no_proxy_var = tk.BooleanVar(value=False)
        tk.Checkbutton(prow, text="不使用代理", variable=self.no_proxy_var,
                       bg=C_CARD, fg=C_MUTED, font=UI_FONT_SM,
                       activebackground=C_CARD, selectcolor=C_CARD).pack(
            side="left", padx=(10, 0))

        brow = tk.Frame(card, bg=C_CARD)
        brow.pack(fill="x", padx=12, pady=(1, 3))
        # 默认【关】：公开视频不需要 cookie。默认去读浏览器 cookie 库既让人意外，
        # 又容易因为浏览器占用 cookie DB 而失败。需要登录的视频用户自己勾。
        self.browser_cookies_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            brow, text="从浏览器读取 Cookies", variable=self.browser_cookies_var,
            bg=C_CARD, fg=C_MUTED, font=UI_FONT_SM,
            activebackground=C_CARD, selectcolor=C_CARD,
            command=self._on_cookie_mode_change).pack(side="left")
        self.browser_cookie_var = tk.StringVar(value="Chrome")
        self.browser_cookie_box = ttk.Combobox(
            brow, textvariable=self.browser_cookie_var, state="readonly", width=10,
            values=[label for label, _ in BROWSER_COOKIE_CHOICES], font=UI_FONT)
        self.browser_cookie_box.pack(side="left", padx=(8, 0))
        tk.Label(brow, text="（可取消后使用 Netscape 文件）", bg=C_CARD,
                 fg=C_MUTED, font=UI_FONT_SM).pack(side="left", padx=(8, 0))

        crow = tk.Frame(card, bg=C_CARD)
        crow.pack(fill="x", padx=12, pady=(1, 3))
        tk.Label(crow, text="Netscape Cookie：", bg=C_CARD, fg=C_MUTED,
                 font=UI_FONT).pack(side="left")
        self.cookie_var = tk.StringVar()
        self.cookie_entry = tk.Entry(crow, textvariable=self.cookie_var,
                                     font=UI_FONT, width=42, bg="white",
                                     fg=C_TEXT, relief="solid", bd=1)
        self.cookie_entry.pack(side="left", ipady=4)
        self.cookie_button = ttk.Button(
            crow, text="选择 Netscape Cookie…", command=self._browse_cookie)
        self.cookie_button.pack(
            side="left", padx=(8, 0))
        self._on_cookie_mode_change()

        # 模型
        mrow = tk.Frame(card, bg=C_CARD)
        mrow.pack(fill="x", padx=12, pady=(2, 7))
        tk.Label(mrow, text="模型：", bg=C_CARD, fg=C_MUTED,
                 font=UI_FONT).pack(side="left")
        self.model_var = tk.StringVar(value="small")
        self.model_box = ttk.Combobox(
            mrow, textvariable=self.model_var, state="readonly", width=8,
            values=[m for m, _ in MODEL_CHOICES], font=UI_FONT)
        self.model_box.pack(side="left")
        self.model_tip = tk.Label(mrow, text="", bg=C_CARD, fg=C_MUTED,
                                  font=UI_FONT_SM)
        self.model_tip.pack(side="left", padx=(8, 0))
        self.model_box.bind("<<ComboboxSelected>>", self._on_model_change)
        self._on_model_change()

        # 语言：默认自动检测；指定后能省掉检测步骤，也避免误判
        tk.Label(mrow, text="语言：", bg=C_CARD, fg=C_MUTED,
                 font=UI_FONT).pack(side="left", padx=(14, 0))
        self.lang_var = tk.StringVar(value="自动检测/混合")
        self.lang_box = ttk.Combobox(
            mrow, textvariable=self.lang_var, state="readonly", width=12,
            values=[label for label, _ in LANG_CHOICES], font=UI_FONT)
        self.lang_box.pack(side="left")

        # 断点续跑：转写按 5 分钟分块存盘，中途停止后可以从缺的块继续
        self.resume_var = tk.BooleanVar(value=True)
        tk.Checkbutton(mrow, text="断点续跑", variable=self.resume_var,
                       bg=C_CARD, fg=C_MUTED, font=UI_FONT_SM,
                       activebackground=C_CARD, selectcolor=C_CARD).pack(
            side="left", padx=(14, 0))


        drow = tk.Frame(card, bg=C_CARD)
        drow.pack(fill="x", padx=12, pady=(1, 3))
        tk.Label(drow, text="设备：", bg=C_CARD, fg=C_MUTED,
                 font=UI_FONT).pack(side="left")
        self.device_var = tk.StringVar(value="自动（推荐）")
        self.device_box = ttk.Combobox(
            drow, textvariable=self.device_var, state="readonly", width=16,
            values=[label for label, _ in DEVICE_CHOICES], font=UI_FONT)
        self.device_box.pack(side="left")
        self.device_box.bind("<<ComboboxSelected>>", self._on_device_change)

        # 说话人分离：多人对话/访谈用。默认关 —— 它会让总耗时大约翻倍。
        # 放在设备这一行，是为了不额外占一行把日志区压扁。
        tk.Frame(drow, width=14, bg=C_CARD).pack(side="left")
        self.speakers_var = tk.BooleanVar(value=False)
        tk.Checkbutton(drow, text="说话人分离", variable=self.speakers_var,
                       bg=C_CARD, fg=C_MUTED, font=UI_FONT_SM,
                       activebackground=C_CARD, selectcolor=C_CARD).pack(
            side="left")
        self.speaker_count_var = tk.StringVar(value="自动")
        self.speaker_count_box = ttk.Combobox(
            drow, textvariable=self.speaker_count_var, state="readonly",
            width=6, values=["自动", "2 人", "3 人", "4 人", "5 人"],
            font=UI_FONT)
        self.speaker_count_box.pack(side="left", padx=(4, 0))

        self.dnd_hint = tk.Label(
            card, text="提示：直接粘贴 YouTube 链接即可；中英混排的视频请保持「自动检测/混合」",
            bg=C_CARD, fg=C_MUTED, font=UI_FONT_SM)
        self.dnd_hint.pack(anchor="w", padx=12, pady=(0, 6))

    def _build_actions(self, parent) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(11, 0))

        self.start_btn = ttk.Button(bar, text="▶  开始生成", style="Primary.TButton",
                                    command=self.start)
        self.start_btn.pack(side="left")

        self.stop_btn = ttk.Button(bar, text="■  停止", command=self.stop,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))

        self.env_btn = ttk.Button(bar, text="环境自检", command=self.check_env)
        self.env_btn.pack(side="left", padx=(8, 0))

        # 打开方式的现场诊断：结果直接打在日志区，截图就能看到
        self.diag_btn = ttk.Button(bar, text="诊断", command=self.run_diagnostics)
        self.diag_btn.pack(side="left", padx=(8, 0))

        ttk.Button(bar, text="打开输出目录",
                   command=lambda: self._open_path(OUTPUT_DIR)).pack(side="right")
        ttk.Button(bar, text="清空日志", command=self._clear_log).pack(
            side="right", padx=(0, 8))

        prog = ttk.Frame(parent)
        prog.pack(fill="x", pady=(8, 0))
        # 精确进度（按阶段 + 转写百分比推进），不再是来回滚动的动画条
        self.progress = ttk.Progressbar(prog, mode="determinate",
                                        maximum=100, value=0)
        self.progress.pack(fill="x")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(prog, textvariable=self.status_var, style="Status.TLabel").pack(
            anchor="w", pady=(4, 0))

        # 阶段耗时面板：每个阶段的状态和秒数一眼可见
        self.stage_bar = tk.Frame(parent, bg=C_CARD, highlightbackground=C_BORDER,
                                  highlightthickness=1)
        self.stage_bar.pack(fill="x", pady=(6, 0))
        self.stage_labels: list[tk.Label] = []
        inner = tk.Frame(self.stage_bar, bg=C_CARD)
        inner.pack(fill="x", padx=10, pady=6)
        for i, name in enumerate(STAGE_NAMES):
            lbl = tk.Label(inner, text="", bg=C_CARD, fg=C_MUTED,
                           font=("Consolas", 9))
            lbl.pack(side="left", padx=(0, 14))
            self.stage_labels.append(lbl)
        self.total_var = tk.StringVar(value="总耗时 0.0s")
        tk.Label(inner, textvariable=self.total_var, bg=C_CARD, fg=C_MUTED,
                 font=("Consolas", 9)).pack(side="right")
        self._render_stages()

    def _build_outputs(self, parent) -> None:
        ttk.Label(parent, text="② 生成的笔记（Markdown）",
                  style="Section.TLabel").pack(anchor="w", pady=(12, 5))

        card = tk.Frame(parent, bg=C_CARD, highlightbackground=C_BORDER,
                        highlightthickness=1)
        card.pack(fill="x")

        prow = tk.Frame(card, bg=C_CARD)
        prow.pack(fill="x", padx=12, pady=(8, 5))
        self.open_tip = tk.Label(
            prow, text="单击在下方预览 · 双击打开独立预览窗口",
            bg=C_CARD, fg=C_MUTED, font=UI_FONT_SM)
        self.open_tip.pack(side="left")
        # 一键拿到全部内容/路径 —— 不启动任何外部程序，永远不会失败
        ttk.Button(prow, text="复制全文", width=9,
                   command=self.copy_selected_text).pack(side="right", padx=(0, 6))
        ttk.Button(prow, text="复制路径", width=9,
                   command=self.copy_selected_path).pack(side="right", padx=(0, 6))
        ttk.Button(prow, text="外部程序", width=10,
                   command=self.open_external).pack(side="right", padx=(0, 6))
        ttk.Button(prow, text="打开", width=6,
                   command=self.open_selected).pack(side="right", padx=(0, 6))
        ttk.Button(prow, text="所在文件夹", width=10,
                   command=self.reveal_selected).pack(side="right", padx=(0, 6))

        # 固定高度 + 滚动条：列表长了不会把页面撑变形
        list_row = tk.Frame(card, bg=C_CARD, height=104)
        list_row.pack(fill="x", padx=12, pady=(0, 8))
        list_row.pack_propagate(False)

        cols = ("name", "size", "paras", "mtime")
        # 关键：Treeview 的父容器必须是 list_row 本身，
        # 否则它会挤在空 Frame 之外，把下面的提示挤到右边。
        self.tree = ttk.Treeview(list_row, columns=cols, show="headings",
                                 height=3, selectmode="browse")
        self.tree.heading("name", text="文件名")
        self.tree.heading("size", text="大小")
        self.tree.heading("paras", text="段落")
        self.tree.heading("mtime", text="生成时间")
        self.tree.column("name", width=520, anchor="w")
        self.tree.column("size", width=80, anchor="e")
        self.tree.column("paras", width=70, anchor="e")
        self.tree.column("mtime", width=130, anchor="center")

        sb = ttk.Scrollbar(list_row, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self.open_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._preview_selected())

    def _build_log(self, parent) -> None:
        ttk.Label(parent, text="③ 运行日志", style="Section.TLabel").pack(
            anchor="w", pady=(12, 5))

        wrap = tk.Frame(parent, bg=C_CARD, highlightbackground=C_BORDER,
                        highlightthickness=1)
        wrap.pack(fill="both", expand=True)
        wrap.pack_propagate(False)
        wrap.configure(height=170)

        self.log = tk.Text(wrap, height=10, font=MONO_FONT, bg="#101822",
                           fg="#d6e2f0", relief="flat", wrap="none",
                           insertbackground="#d6e2f0")
        self.log.pack(side="left", fill="both", expand=True, padx=(8, 0),
                      pady=8)
        lsb = ttk.Scrollbar(wrap, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y", pady=8, padx=(0, 4))
        self.log.tag_configure("ok", foreground="#4ade80")
        self.log.tag_configure("warn", foreground="#fbbf24")
        self.log.tag_configure("err", foreground="#f87171")
        self.log.tag_configure("step", foreground="#60a5fa")
        self.log.tag_configure("dim", foreground="#7b8ba0")
        self.log.configure(state="disabled")

    # ==================================================================
    # 剪贴板右键菜单
    # tkinter 的 Entry 在 Windows 上没有内置右键菜单，而大多数人从浏览器
    # 复制链接后的习惯就是右键→粘贴，所以这里补上。
    # ==================================================================
    def _attach_paste_menu(self, widget) -> None:
        menu = tk.Menu(widget, tearoff=0, font=UI_FONT)

        def gen(seq):
            widget.focus_set()
            widget.event_generate(seq)

        menu.add_command(label="粘贴", accelerator="Ctrl+V",
                         command=lambda: gen("<<Paste>>"))
        menu.add_command(label="复制", accelerator="Ctrl+C",
                         command=lambda: gen("<<Copy>>"))
        menu.add_separator()
        menu.add_command(label="全选", accelerator="Ctrl+A",
                         command=lambda: widget.select_range(0, "end"))
        menu.add_command(label="清空", command=lambda: widget.delete(0, "end"))

        def popup(event):
            widget.focus_set()
            menu.tk_popup(event.x_root, event.y_root)

        widget.bind("<Button-3>", popup)
        widget.bind("<Control-a>", lambda _e: (widget.select_range(0, "end"), "break")[1])
        return menu

    # ==================================================================
    # 小功能
    # ==================================================================
    def _on_model_change(self, _e=None) -> None:
        for name, tip in MODEL_CHOICES:
            if name == self.model_var.get():
                self.model_tip.configure(text=tip)
                return

    def _on_device_change(self, _e=None) -> None:
        mode = self._selected_device()
        if mode == "npu":
            self.device_tip.configure(
                text="OpenVINO Whisper NPU",
                fg=C_MUTED)
        elif mode == "cuda":
            self.device_tip.configure(
                text="需要 NVIDIA CUDA/cuBLAS/cuDNN 运行库",
                fg=C_MUTED)
        elif mode == "cpu":
            self.device_tip.configure(text="强制使用 CPU int8", fg=C_MUTED)
        else:
            self.device_tip.configure(
                text="自动检测 CUDA，不可用时使用 CPU", fg=C_MUTED)

    def _browse(self) -> None:
        path = filedialog.askopenfilename(
            title="选择视频或音频文件",
            filetypes=[("视频/音频", "*.mp4 *.webm *.mkv *.mov *.m4a *.mp3 *.wav"),
                       ("所有文件", "*.*")])
        if path:
            self.source_var.set(path)

    def _browse_cookie(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 Netscape HTTP Cookie File",
            filetypes=[("Cookie 文件", "*.txt *.cookies *.tsv"),
                       ("所有文件", "*.*")])
        if path:
            self.cookie_var.set(path)

    def _on_cookie_mode_change(self) -> None:
        state = "disabled" if self.browser_cookies_var.get() else "normal"
        self.cookie_entry.configure(state=state)
        self.cookie_button.configure(state=state)
        self.browser_cookie_box.configure(
            state="readonly" if self.browser_cookies_var.get() else "disabled")

    def test_proxy(self) -> None:
        """就地检测代理端口通不通（不启动流水线）。"""
        raw = self.proxy_var.get().strip() or DEFAULT_PROXY
        host, port = _split_hostport(raw)
        if host is None:
            self.proxy_state.configure(text="地址格式看不懂", fg=C_ERR)
            return
        try:
            with socket.create_connection((host, port), timeout=2):
                self.proxy_state.configure(text=f"✓ {host}:{port} 在监听", fg=C_OK)
        except Exception as exc:
            self.proxy_state.configure(
                text=f"✗ 连不上（{type(exc).__name__}）", fg=C_ERR)

    def check_env(self) -> None:
        """跑 check_env.py，把结果打进日志区。"""
        if self.running:
            return
        py = _python_exe()
        if not CHECK_ENV.exists():
            messagebox.showerror("缺文件", f"找不到 {CHECK_ENV.name}")
            return
        self.log_line("=== 环境自检 ===", "step")
        cmd = [str(py), str(CHECK_ENV)]
        if self.no_proxy_var.get():
            cmd.append("--no-proxy")
        else:
            proxy = self.proxy_var.get().strip() or DEFAULT_PROXY
            cmd += ["--proxy", proxy]
        self._launch(cmd,
                     "正在检查环境…", on_done_status=None)

    def run_diagnostics(self) -> None:
        """
        把"打不开文件/目录"相关的现场信息打到日志区。

        设计成在这里输出而不是写文件：用户习惯截图反馈，
        打在日志区一眼就能看到，比我让他去找日志文件省事。
        """
        self._clear_log()
        self.log_line("=== 打开方式诊断 ===", "step")
        self.log_line(f"  文件      : {__file__}")
        self.log_line(f"  Python    : {sys.executable}")
        self.log_line(f"  pythonw   : {'是（无控制台）' if sys.stdout is None else '否'}")
        self.log_line(f"  stdout    : {sys.stdout!r}")
        self.log_line(f"  cwd       : {os.getcwd()}")

        # .md 的关联
        exe = _associated_exe(".md")
        self.log_line("", "")
        self.log_line(f"  .md 关联  : {exe or '(无)'}")

        # 资源管理器窗口数
        self.log_line(f"  explorer 窗口数: {count_explorer_windows()}")

        # ShellExecute 各写法的返回值
        self.log_line("", "")
        self.log_line("  ShellExecuteW 实测（<=32 为失败码）:", "step")
        tests = [
            ('explorer.exe /select,<文件>', "explorer.exe", None),
            ('explorer.exe "<目录>"', "explorer.exe", None),
            ("open <文件>", None, None),
            ("open <目录>", str(LAB_DIR), None),
        ]
        p = self._selected_path()
        if p:
            for label, target, params in tests:
                if label.startswith("explorer.exe /select"):
                    rc = self._shell_probe("explorer.exe", f'/select,"{p}"')
                elif label.startswith('explorer.exe "'):
                    rc = self._shell_probe("explorer.exe", f'"{p.parent}"')
                elif label == "open <文件>":
                    rc = self._shell_probe(str(p), None)
                else:
                    rc = self._shell_probe(str(p.parent), None)
                self.log_line(f"    {label:32} 返回 {rc:3d}  {'✓' if rc > 32 else '✗ 失败'}")
        else:
            self.log_line("    （列表里没有选中文件，跳过）")

        # 各种"启动子进程"的方式对比（关键：看在你这台机器上哪种能活下来）
        self.log_line("", "")
        self.log_line("  启动子进程的多种方式（看退出码）:", "step")

        exe_code = _associated_exe(".md") or ""
        variants = [
            ("Popen explorer（无重定向）", ["explorer", str(LAB_DIR)], False),
            ("Popen explorer（DEVNULL）", ["explorer", str(LAB_DIR)], True),
            ("Popen cmd /c start", ["cmd", "/c", "start", "", str(LAB_DIR)], True),
        ]
        if exe_code:
            variants.append((f"Popen {Path(exe_code).name}",
                             [exe_code, str(p) if p else ""], True))
            variants.append((f"Popen {Path(exe_code).name}（无重定向）",
                             [exe_code, str(p) if p else ""], False))

        CODE_HINTS = {
            0xC0000142: "DLL 初始化失败",
            0x80000003: "STATUS_BREAKPOINT（命中断点）",
            0xC0000005: "访问冲突",
            0xC0000135: "缺少 DLL",
            0xC0000409: "栈缓冲区溢出/安全机制",
        }
        for label, cmd, use_devnull in variants:
            try:
                if use_devnull:
                    proc = subprocess.Popen(
                        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
                else:
                    proc = subprocess.Popen(cmd)
                time.sleep(1.5)
                code = proc.poll()
                if code is None:
                    self.log_line(f"    {label:34} 仍在运行 ✓", "ok")
                else:
                    u = code & 0xFFFFFFFF
                    hint = CODE_HINTS.get(u, "")
                    tag = "err" if u >= 0x80000000 else ""
                    self.log_line(
                        f"    {label:34} 退出 0x{u:08X} {hint}", tag)
            except Exception as exc:
                self.log_line(f"    {label:34} 异常 {type(exc).__name__}: {exc}", "err")

        self.log_line("", "")
        self.log_line(f"  失败日志会写到: {GUI_LOG}", "dim")
        self.log_line("=== 诊断结束（把这一段截图发给开发者）===", "step")

    def _shell_probe(self, target: str, params) -> int:
        """只调 ShellExecuteW 拿返回码，不写日志。"""
        if os.name != "nt":
            return -1
        try:
            import ctypes
            return int(ctypes.windll.shell32.ShellExecuteW(
                None, "open", target, params, None, 1))
        except Exception:
            return -1

    def _open_path(self, path: Path) -> None:
        """打开一个目录（供「打开输出目录」用）。"""
        path.mkdir(parents=True, exist_ok=True)
        if self._open_via_cmd(f'cmd /c start "" "{path}"'):
            return
        if self._shell_open("explorer.exe", f'"{path}"') \
                or self._shell_open(str(path)):
            return
        try:
            self._spawn(["explorer", str(path)], f"打开目录 {path.name}")
        except LaunchError as exc:
            messagebox.showerror("打不开目录", f"{exc}\n\n路径：{path}")

    # ==================================================================
    # 启动流水线
    # ==================================================================
    def _selected_lang(self) -> str | None:
        """把下拉框的中文标签翻回语言代码。"""
        cur = self.lang_var.get()
        for label, code in LANG_CHOICES:
            if label == cur:
                return code
        return None

    def _selected_device(self) -> str:
        cur = self.device_var.get()
        for label, code in DEVICE_CHOICES:
            if label == cur:
                return code
        return "auto"

    def _selected_browser_cookie(self) -> str:
        cur = self.browser_cookie_var.get()
        for label, code in BROWSER_COOKIE_CHOICES:
            if label == cur:
                return code
        return "chrome"

    def _selected_speaker_count(self) -> int:
        """把下拉框的"自动 / 3 人"翻成 -1 或具体人数。"""
        txt = self.speaker_count_var.get().strip()
        digits = "".join(ch for ch in txt if ch.isdigit())
        return int(digits) if digits else -1

    def _build_cmd(self, source: str) -> list:
        cmd = [str(_python_exe()), "-u", str(PIPELINE), source,
               "--model", self.model_var.get()]
        lang = self._selected_lang()
        if lang:
            cmd += ["--lang", lang]
        cmd += ["--device", self._selected_device()]
        if not self.resume_var.get():
            cmd.append("--no-resume")
        if self.no_proxy_var.get():
            cmd.append("--no-proxy")
        else:
            proxy = self.proxy_var.get().strip()
            if proxy:
                cmd += ["--proxy", proxy]
        if self.browser_cookies_var.get():
            cmd += ["--cookies-from-browser", self._selected_browser_cookie()]
        else:
            cookies = self.cookie_var.get().strip()
            if cookies:
                cmd += ["--cookies", cookies]
        if self.speakers_var.get():
            cmd.append("--diarize")
            n = self._selected_speaker_count()
            if n > 0:
                cmd += ["--num-speakers", str(n)]
        return cmd

    def start(self) -> None:
        source = self.source_var.get().strip()
        if not source:
            messagebox.showwarning("还没填", "请先填视频链接，或点「选择本地文件…」。")
            self.entry.focus_set()
            return
        if not PIPELINE.exists():
            messagebox.showerror("缺文件", f"找不到 {PIPELINE.name}")
            return

        cmd = self._build_cmd(source)
        self.log_line("", "")
        self.log_line(f"=== 开始生成 ===", "step")
        self.log_line(f"  模型 {self.model_var.get()}　"
                      f"设备 {self._selected_device()}　"
                      f"{'直连' if self.no_proxy_var.get() else '代理 ' + (self.proxy_var.get() or DEFAULT_PROXY)}",
                      "dim")
        self._launch(cmd, "正在启动…", on_done_status="完成")

    def _launch(self, cmd: list, status: str, on_done_status=None) -> None:
        if self.running:
            messagebox.showinfo("正在运行", "已经有任务在跑了。")
            return
        self._begin(status)
        self.worker = threading.Thread(
            target=self._run, args=(cmd, on_done_status), daemon=True)
        self.worker.start()

    def _begin(self, status: str) -> None:
        self.running = True
        self.stop_requested = False
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.env_btn.configure(state="disabled")
        self._reset_stages()
        self._tick_stages()
        self.set_status(status)

    def stop(self) -> None:
        if not self.running or self.proc is None:
            return
        self.log_line("", "")
        self.log_line("收到停止请求，正在终止…", "warn")
        self.stop_requested = True
        try:
            self.proc.terminate()
        except Exception as exc:
            self.log_line(f"  终止失败：{exc}", "err")

    # ==================================================================
    # 子线程：跑子进程 + 收集输出
    # ==================================================================
    def _run(self, cmd: list, on_done_status) -> None:
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(LAB_DIR), env=self._env(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=CREATE_NO_WINDOW)
            assert self.proc.stdout is not None
            # 立刻回一条"已启动"，让用户知道不是没反应
            self.queue.put(("log", f"  流水线已启动（PID {self.proc.pid}），等待第一行输出…", "dim"))
            self.queue.put(("status", "已启动，正在准备…", None))
            for raw in self.proc.stdout:
                line = raw.replace("\r", "").rstrip()
                if line:
                    self.queue.put(("log", line, _classify(line)))
            code = self.proc.wait()
        except Exception as exc:
            self.queue.put(("log", f"启动失败：{type(exc).__name__}: {exc}", "err"))
            code = -1
        finally:
            self.proc = None

        self.queue.put(("done", code, on_done_status))

    def _env(self) -> dict:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        # 【关键】关闭子进程 stdout 的块缓冲。
        # Python 的 stdout 被重定向到管道时默认是【块缓冲】（约 8KB）：
        # 阶段 1~3（代理/下载/提取）输出很少，攒不满一个缓冲区就一直不吐，
        # 界面看起来就像"卡在正在启动"，直到转写阶段输出变多才一次性刷出来。
        # 加上这个之后每一行 print 都立刻到达，阶段面板的秒数才是实时的。
        env["PYTHONUNBUFFERED"] = "1"
        # 模型下载走国内镜像；用户自己设了 HF_ENDPOINT 就以他的为准
        env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        return env

    # ==================================================================
    # 消息泵
    # ==================================================================
    def _drain_queue(self) -> None:
        try:
            while True:
                kind, a, b = self.queue.get_nowait()
                if kind == "log":
                    self.log_line(a, b)
                elif kind == "status":
                    self.status_var.set(a)
                elif kind == "done":
                    self._on_done(a, b)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    def _on_done(self, code: int, done_status) -> None:
        self.running = False
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.env_btn.configure(state="normal")
        self._stage_finish_all(ok=(code == 0))

        if code == 0:
            self.set_status(done_status or "完成")
            self.log_line("", "")
            self.log_line("✓ 完成", "ok")
            self.refresh_outputs()
        elif self.stop_requested:
            self.set_status("已停止")
            self.log_line("已停止", "warn")
        else:
            self.set_status("失败（原因见日志）")

    # ==================================================================
    # 日志 / 状态
    # ==================================================================
    def set_status(self, text: str) -> None:
        self.queue.put(("status", text, ""))

    # ------------------------------------------------------------------
    # 阶段耗时与进度
    # ------------------------------------------------------------------
    def _reset_stages(self) -> None:
        self._stage_idx = -1
        self._stage_t0 = 0.0
        self._stage_done = [0.0] * 5
        self._stage_state = ["idle"] * 5
        self._run_t0 = time.time()
        self.progress.configure(value=0)
        self._render_stages()

    def _stage_begin(self, idx: int) -> None:
        """进入某个阶段：把上一个阶段结算掉。"""
        now = time.time()
        if 0 <= self._stage_idx < 5 and self._stage_state[self._stage_idx] == "run":
            self._stage_done[self._stage_idx] = now - self._stage_t0
            self._stage_state[self._stage_idx] = "done"
        if not (0 <= idx < 5):
            return
        self._stage_idx = idx
        self._stage_t0 = now
        self._stage_state[idx] = "run"
        # 状态栏也跟着报当前阶段，别一直停在"正在启动"
        self.set_status(f"第 {idx + 1} / 5 阶段：{STAGE_NAMES[idx]}")
        # 阶段进度：每完成一个阶段推进 20%
        self.progress.configure(value=min(100, idx * 20))
        self._render_stages()

    def _stage_finish_all(self, ok: bool = True) -> None:
        now = time.time()
        for i in range(5):
            if self._stage_state[i] == "run":
                self._stage_done[i] = now - self._stage_t0
                self._stage_state[i] = "done" if ok else "fail"
        self._stage_idx = -1
        if ok:
            self.progress.configure(value=100)
        self._render_stages()

    def _render_stages(self) -> None:
        if not getattr(self, "stage_labels", None):
            return
        now = time.time()
        for i, lbl in enumerate(self.stage_labels):
            st = self._stage_state[i]
            if st == "idle":
                txt, fg = f"{i + 1}·{STAGE_NAMES[i]}  ⏳", C_MUTED
            elif st == "run":
                txt = f"{i + 1}·{STAGE_NAMES[i]}  ⟳ {now - self._stage_t0:.1f}s"
                fg = C_STEP
            elif st == "done":
                txt, fg = f"{i + 1}·{STAGE_NAMES[i]}  ✓ {self._stage_done[i]:.1f}s", C_OK
            else:
                txt, fg = f"{i + 1}·{STAGE_NAMES[i]}  ✗", C_ERR
            lbl.configure(text=txt, fg=fg)
        if self._run_t0:
            total = now - self._run_t0
            self.total_var.set(f"总耗时 {total:.1f}s")

    def _tick_stages(self) -> None:
        """
        每 100ms 刷新一次进行中阶段的秒数。

        【bug 修复】原来的写法是：
            if self._stage_idx >= 0 and self.running:   # 第一次调用时 _stage_idx 还是 -1
                self.root.after(200, self._tick_stages)  # 于是这一行永远没机会执行
        结果是定时刷新链【从来没有启动过】，秒数只在阶段切换那一刻渲染一次，
        看起来就像"等阶段结束才给出总时间"。

        现在改成：只要还在跑就无条件排下一次，渲染与否由 _render_stages 自己判断。
        """
        if not self.running:
            return
        self._render_stages()
        self.root.after(100, self._tick_stages)

    def _track_output(self, text: str) -> None:
        """从流水线输出里识别阶段切换和进度百分比。"""
        m = re.search(r"第\s*(\d)\s*层", text)
        if m:
            self._stage_begin(int(m.group(1)) - 1)
            return
        if "各阶段耗时" in text:
            self._stage_finish_all(True)
            return
        # 转写进度 -> 映射到 60%~95% 这一段
        m = re.search(r"转写进度\s*(\d+)%", text)
        if m:
            self.progress.configure(value=60 + int(m.group(1)) * 0.35)
            return
        # 下载百分比 -> 20%~40%
        m = re.search(r"下载\s*(\d+)%", text)
        if m:
            self.progress.configure(value=20 + int(m.group(1)) * 0.20)

    def log_line(self, text: str, tag: str = "") -> None:
        if threading.current_thread() is not threading.main_thread():
            self.queue.put(("log", text, tag))
            return
        self._track_output(text)
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _boot_log(self) -> None:
        self.log_line("准备好了。填入 YouTube 链接，点「开始生成」。", "dim")
        self.log_line("首次运行会下载 Whisper 模型（base 约 145 MB）。", "dim")
        py = _python_exe()
        if py != VENV_PY:
            self.log_line(f"⚠ 没找到项目虚拟环境，改用 {py}", "warn")

    # ==================================================================
    # 输出列表
    # ==================================================================
    def refresh_outputs(self) -> None:
        for row in self.tree.get_children():
            self.tree.delete(row)
        if not OUTPUT_DIR.exists():
            return
        items = sorted(OUTPUT_DIR.glob("*.md"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for path in items:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                text = ""
            paras = len(re.findall(r"(?m)^## ", text))
            self.tree.insert(
                "", "end", iid=str(path),
                values=(path.name,
                        f"{path.stat().st_size / 1024:.1f} KB",
                        f"{paras} 段",
                        time.strftime("%m-%d %H:%M",
                                      time.localtime(path.stat().st_mtime))))

    def _selected_path(self) -> Path | None:
        sel = self.tree.selection()
        if not sel:
            return None
        p = Path(sel[0])
        return p if p.exists() else None

    def _preview_selected(self) -> None:
        p = self._selected_path()
        if not p:
            return
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            self.log_line(f"读取失败：{exc}", "err")
            return
        head = "\n".join(text.splitlines()[:14])
        self._clear_log()
        self.log_tag_open = True
        self.log_line(f"=== 预览：{p.name} ===", "step")
        self.log_line(head)
        self.log_line("", "")
        self.log_line(f"（文件共 {len(text.splitlines())} 行，双击打开完整内容）", "dim")

    def open_selected(self) -> None:
        """双击 / 点「打开」→ 在程序内打开预览窗口。"""
        p = self._selected_path()
        if not p:
            messagebox.showinfo("没选中", "请先在列表里点一行。")
            return
        self._show_preview_window(p)

    def open_external(self) -> None:
        """用系统关联的外部程序打开（找不到关联时自动挑编辑器）。"""
        p = self._selected_path()
        if not p:
            messagebox.showinfo("没选中", "请先在列表里点一行。")
            return
        self._open_file(p)

    def copy_selected_text(self) -> None:
        """
        把选中文件的【全部内容】复制到剪贴板。

        这是最省事的路径：一次点击 -> 粘到任何编辑器。
        完全不启动外部进程，所以在"什么都启动不了"的机器上照样可用。
        """
        p = self._selected_path()
        if not p:
            messagebox.showinfo("没选中", "请先在列表里点一行。")
            return
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.log_line(f"已复制全文：{p.name}（{len(text)} 字符）", "ok")
            self.set_status(f"全文已复制（{len(text)} 字符），可以直接粘贴")
        except Exception as exc:
            messagebox.showerror("复制失败", f"{type(exc).__name__}: {exc}")

    def copy_selected_path(self) -> None:
        """
        把选中文件的完整路径复制到剪贴板。

        这是「所在文件夹」和「外部程序」都失败时的兜底 ——
        拿到路径后手动粘到资源管理器/编辑器里就行，不依赖任何外部程序启动。
        """
        p = self._selected_path()
        if not p:
            messagebox.showinfo("没选中", "请先在列表里点一行。")
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(str(p))
            self.log_line(f"已复制路径：{p}", "ok")
            self.set_status("路径已复制到剪贴板")
        except Exception as exc:
            messagebox.showerror("复制失败", f"{type(exc).__name__}: {exc}")

    def _show_preview_window(self, path: Path) -> None:
        """
        在程序内打开一个只读预览窗口。

        为什么自带查看器、而不是依赖系统关联：
        这台机器上 .md 默认关联到 VS Code，但 VS Code 的更新没完成
        （安装目录里残留 new_Code.exe），GUI 起不来 —— 双击文件后
        系统调起了 VS Code，它静默退出，用户看到的就是"什么都没发生"。
        程序内预览不依赖任何外部程序，一定能看到内容。
        """
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            messagebox.showerror("读不了", f"{type(exc).__name__}: {exc}")
            return

        win = tk.Toplevel(self.root)
        win.title(f"{path.name}  —  预览")
        win.configure(bg=C_BG)
        _w, _h = 960, 720
        _px, _py = self.root.winfo_rootx(), self.root.winfo_rooty()
        win.geometry(f"{_w}x{_h}+{_px + 40}+{_py + 30}")
        win.transient(self.root)
        try:
            if ICON.exists():
                win.iconbitmap(default=str(ICON))
        except Exception:
            pass

        head = tk.Frame(win, bg=C_ACCENT)
        head.pack(fill="x")
        tk.Label(head, text=path.name, bg=C_ACCENT, fg="white",
                 font=("Microsoft YaHei UI", 11, "bold"),
                 anchor="w").pack(fill="x", padx=16, pady=(10, 1))
        tk.Label(head, text=f"{len(text.splitlines())} 行 · {len(text)} 字符 · {path.parent.name}/",
                 bg=C_ACCENT, fg="#c7d6e8", font=UI_FONT_SM,
                 anchor="w").pack(fill="x", padx=16, pady=(0, 9))

        # 关键：按钮栏用 side="bottom" 先占位，再让正文区 expand。
        # 反过来的话（正文区先 expand）窗口一矮，按钮栏就会被挤出可视区。
        bar = tk.Frame(win, bg=C_BG)
        bar.pack(side="bottom", fill="x", padx=12, pady=(4, 10))

        box = tk.Frame(win, bg=C_CARD)
        box.pack(fill="both", expand=True, padx=12, pady=(10, 6))
        view = tk.Text(box, wrap="word", font=("Consolas", 10), bg="white",
                       fg=C_TEXT, relief="flat", padx=12, pady=10)
        vsb = ttk.Scrollbar(box, orient="vertical", command=view.yview)
        view.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        view.pack(side="left", fill="both", expand=True)
        view.insert("1.0", text)
        view.configure(state="disabled")

        def copy_all():
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.log_line("已复制全文到剪贴板", "ok")

        ttk.Button(bar, text="复制全文", command=copy_all).pack(side="left")
        ttk.Button(bar, text="用外部程序打开",
                   command=lambda: self._open_file(path)).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="关闭", command=win.destroy).pack(side="right")
        tk.Label(bar, text="双击列表即在此预览，不依赖系统关联",
                 bg=C_BG, fg=C_MUTED, font=UI_FONT_SM).pack(side="right", padx=(0, 10))
        win.bind("<Escape>", lambda _e: win.destroy())

    def reveal_selected(self) -> None:
        """
        在资源管理器里定位选中的文件。

        三级降级，全部失败才报错：
          1. ShellExecuteW 让 explorer 显示并选中该文件（最正统，不碰句柄继承）
          2. ShellExecuteW 直接打开所在目录
          3. subprocess 启动 explorer（传统方式，作为兜底）
        最后仍失败 -> 弹框 + 把路径给你，方便手动粘贴到资源管理器。
        """
        p = self._selected_path()
        if not p:
            messagebox.showinfo("没选中", "请先在列表里点一行。")
            return

        # 1. 选中文件。
        #    注意：ShellExecute 返回成功【不代表】窗口出来了 ——
        #    explorer 可能启动后立刻崩溃（0xC0000142），而 ShellExecute 照样返回 >32。
        #    所以这里必须再等一个真实窗口出现来确认。
        before = count_explorer_windows()
        if self._open_via_cmd(f'cmd /c start "" explorer.exe /select,"{p}"') \
                and wait_new_explorer_window(before, 1.0):
            self.log_line("  已在资源管理器中定位", "ok")
            return
        self.log_line("  cmd 方式没有响应，尝试 ShellExecute…", "warn")
        before = count_explorer_windows()
        if self._shell_open("explorer.exe", f'/select,"{p}"') \
                and wait_new_explorer_window(before, 1.5):
            self.log_line("  已在资源管理器中定位（ShellExecute）", "ok")
            return
        self.log_line("  资源管理器仍然没有响应，尝试只打开目录…", "warn")

        # 2. 只打开目录（不要求选中文件）
        before = count_explorer_windows()
        if self._open_via_cmd(f'cmd /c start "" "{p.parent}"') \
                and wait_new_explorer_window(before, 2.5):
            return
        self.log_line("  打开目录也没有响应…", "warn")

        # 3. 传统方式兜底
        try:
            self._spawn(["explorer", f"/select,{p}"], "在资源管理器中定位文件")
            return
        except LaunchError as exc:
            self.log_line(f"  {exc}", "warn")

        # 全失败 -> 把路径复制到剪贴板，让用户手动粘（这个永远可行）
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(str(p.parent))
        except Exception:
            pass
        self.log_line(f"  已把目录路径复制到剪贴板：{p.parent}", "warn")
        messagebox.showwarning(
            "资源管理器打不开",
            f"你机器上的 explorer.exe 启动失败（0xC0000142）。\n\n"
            f"目录路径已经复制到剪贴板，直接粘到资源管理器地址栏按回车即可：\n"
            f"{p.parent}")

    def _open_file(self, path: Path) -> None:
        """
        用外部程序打开文件。核心原则：【不信 API，只信窗口】。

        ShellExecute 返回 > 32 只说明"进程被成功创建"，它无法知道进程
        随后是否崩溃 —— 实测 explorer/Code 可能启动后立刻以 0xC0000142 退出，
        而 ShellExecute 依然报成功。所以每种方式之后都要真的等到窗口出现。
        """
        stem = path.stem[:40]

        # 1. cmd /c start（实测唯一在受限环境下可靠的启动方式）
        before = count_windows_with_title(stem)
        if self._open_via_cmd(f'cmd /c start "" "{path}"') \
                and self._wait_title(stem, before, 1.8):
            self.log_line("  已用系统默认程序打开", "ok")
            return
        self.log_line("  cmd 方式没有响应，尝试 ShellExecute…", "warn")

        # 2. ShellExecute + 等窗口
        before = count_windows_with_title(stem)
        if self._shell_open(str(path)) and \
                self._wait_title(stem, before, 2.0):
            self.log_line("  已交给系统默认程序打开（ShellExecute）", "ok")
            return
        self.log_line("  系统默认程序没有响应…", "warn")

        # 3. 直接起关联的 exe（会因为句柄问题崩溃，放在后面兜底并检测）
        exe = _associated_exe(path.suffix)
        if exe:
            try:
                before = count_windows_with_title(stem)
                proc = subprocess.Popen(
                    [exe, str(path)], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(1.2)
                code = proc.poll()
                if code is None or self._wait_title(stem, before, 1.0):
                    self.log_line(f"  已用 {Path(exe).name} 打开", "ok")
                    return
                if code not in (None, 0, 1):
                    self.log_line(
                        f"  {Path(exe).name} 启动后立即退出（0x{code & 0xFFFFFFFF:X}）",
                        "warn")
            except Exception as exc:
                self.log_line(f"  直启失败：{exc}", "warn")

        # 4. 已知编辑器
        name, editor = _pick_editor()
        if editor:
            try:
                self._spawn([editor, str(path)], f"用 {name} 打开")
                self.log_line(f"  已用 {name} 打开", "ok")
                return
            except LaunchError as exc:
                self.log_line(f"  {name} 启动失败：{exc}", "warn")

        # 5. 全失败：路径进剪贴板
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(str(path))
        except Exception:
            pass
        messagebox.showwarning(
            "打不开",
            f"没找到能打开它的程序。\n\n"
            f"完整路径已复制到剪贴板，可以粘到编辑器/资源管理器里：\n{path}\n\n"
            f"也可以直接用「打开」看程序内预览。")

    def _wait_title(self, substr: str, before: int, timeout: float) -> bool:
        """等一个标题含 substr 的窗口出现（用于确认文件真的被打开了）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if count_windows_with_title(substr) > before:
                return True
            time.sleep(0.25)
        return False

    def _open_editor_name(self) -> str:
        if _associated_exe(".md"):
            return "系统默认程序"
        name, _exe = _pick_editor()
        return name or "系统默认程序"


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def count_explorer_windows() -> int:
    """
    数当前有多少个"资源管理器文件窗口"（窗口类名 CabinetWClass）。

    为什么要这个：ShellExecute 返回 > 32 只表示"进程启动成功"，
    它**无法知道进程随后是否崩溃**。实测 explorer.exe 可能在启动后立刻
    以 0xC0000142 退出，而 ShellExecute 照样报成功。
    只有数窗口才能真正判断"用户是不是真的看到了窗口"。
    """
    if os.name != "nt":
        return -1
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found = [0]
        CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lp):
            buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, buf, 64)
            if buf.value in ("CabinetWClass", "ExploreWClass"):
                found[0] += 1
            return True

        user32.EnumWindows(CB(_cb), 0)
        return found[0]
    except Exception:
        return -1


def count_windows_with_title(substr: str) -> int:
    """数标题里包含某段文字的顶层窗口（用来确认"文件真的被打开了"）。"""
    if os.name != "nt" or not substr:
        return -1
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found = [0]
        CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        needle = substr.lower()

        def _cb(hwnd, _lp):
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            if needle in buf.value.lower():
                found[0] += 1
            return True

        user32.EnumWindows(CB(_cb), 0)
        return found[0]
    except Exception:
        return -1


def wait_new_explorer_window(before: int, timeout: float = 2.0) -> bool:
    """等一个新的资源管理器窗口出现。出现返回 True。"""
    import time as _t
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        now = count_explorer_windows()
        if now > before:
            return True
        _t.sleep(0.2)
    return False


def _python_exe() -> Path:
    """
    优先用项目 venv 里的解释器（依赖都在那），
    找不到就退回当前解释器 —— 后者可能缺依赖，所以界面会给出警告。
    """
    return VENV_PY if VENV_PY.exists() else Path(sys.executable)


# 已知 Markdown 编辑器的候选路径（按优先级）。
# 用 Code.exe 而不是 code.cmd：后者是批处理包装，会闪一个控制台窗口。
EDITOR_CANDIDATES = (
    ("VS Code", (r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
                 r"%ProgramFiles%\Microsoft VS Code\Code.exe")),
    ("Typora", (r"%LOCALAPPDATA%\Programs\Typora\Typora.exe",
                r"%ProgramFiles%\Typora\Typora.exe")),
    ("Obsidian", (r"%LOCALAPPDATA%\Programs\Obsidian\Obsidian.exe",
                  r"%ProgramFiles%\Obsidian\Obsidian.exe")),
    ("Notepad++", (r"%ProgramFiles%\Notepad++\notepad++.exe",
                   r"%ProgramFiles(x86)%\Notepad++\notepad++.exe")),
    ("记事本", (r"%SystemRoot%\System32\notepad.exe",)),
)


def _associated_exe(suffix: str):
    """
    查 Windows 给某个扩展名分配的打开程序，返回 exe 路径；没有关联返回 None。

    用 shell32 的 AssocQueryString：它会完整走一遍 Windows 的解析逻辑
    （UserChoice → 用户级 Classes → 机器级 Classes → 系统默认），
    比自己去翻注册表可靠。
    """
    if os.name != "nt" or not suffix:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        # 注意：AssocQueryStringW 在 shlwapi.dll，不在 shell32.dll。
        # 用 shell32 会抛 AttributeError（被下面的 except 吞掉），
        # 结果所有扩展名都"查不到关联"——这个坑踩过一次。
        shlwapi = ctypes.WinDLL("shlwapi", use_last_error=True)
        shlwapi.AssocQueryStringW.argtypes = [
            wintypes.DWORD,            # ASSOCF
            ctypes.c_int,              # ASSOCSTR
            wintypes.LPCWSTR,          # pszAssoc（扩展名）
            wintypes.LPCWSTR,          # pszExtra
            wintypes.LPWSTR,           # pszOut
            ctypes.POINTER(wintypes.DWORD),   # pcchOut
        ]
        shlwapi.AssocQueryStringW.restype = ctypes.c_long      # HRESULT

        ASSOCSTR_EXECUTABLE = 2
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        hr = shlwapi.AssocQueryStringW(
            0, ASSOCSTR_EXECUTABLE, suffix, None, buf, ctypes.byref(size))
        # 0x80070483 = 没有关联的程序
        if hr == 0 and buf.value:
            return buf.value
    except Exception:
        pass
    return None


def _pick_editor():
    """按优先级找一个能打开文本的编辑器，返回 (名字, exe 路径)；都没有返回 (None, None)。"""
    for name, templates in EDITOR_CANDIDATES:
        for tpl in templates:
            path = os.path.expandvars(tpl)
            if os.path.exists(path):
                return name, path
    return None, None


def _split_hostport(raw: str):
    try:
        from urllib.parse import urlparse
        u = urlparse(raw if "://" in raw else f"http://{raw}")
        if not u.hostname:
            return None, None
        return u.hostname, u.port or 7890
    except Exception:
        return None, None


def _classify(line: str) -> str:
    """给日志行挑个颜色。"""
    if re.match(r"^\s*第 \d 层", line):
        return "step"
    if "[OK]" in line or line.startswith("✓"):
        return "ok"
    if "[警告]" in line:
        return "warn"
    if "[失败]" in line or "ERROR" in line or "Traceback" in line:
        return "err"
    if re.search(r"下载\s+\d+%", line) or line.strip().startswith("·"):
        return "dim"
    return ""


def main() -> int:
    enable_dpi_awareness()
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
