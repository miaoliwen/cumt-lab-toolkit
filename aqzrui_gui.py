#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国矿业大学 实验室安全准入平台（aqzrui）速刷工具 —— 图形界面
==============================================================

深色 + 霓虹配色的 tkinter 界面，同进程驱动 `aqzrui_speedrun.py`（不做 HTTP 中转）。

功能模块（与命令行版一一对应，全部保留）：
    登录    账号密码登录（CAS + 就地二次认证）· 手工会话粘贴 · 检查登录
    参数    类别过滤 · 最多考试 · 请求间隔 · 演练开关
    执行    抓答案库 · 学习 · 练习 · 考试 · 全部执行 · 停止
    labsafe 视频   登录 · 枚举课程 · 播放全部 / 单个 · 演练 · 停止
    日志    分级着色 · 复制 · 清空

启动：
    python aqzrui_gui.py
    aqzrui_gui.py --selftest     # 自检：建界面 + 试会话 + 读答案库，写 selftest.txt 后退出

线程模型（勿改）
----------------
长任务丢后台线程，日志经队列回流，**所有 Tk 调用只在主线程**。
Tk 变量只能在主线程读，所以按钮回调先用 `_snapshot()` 把 UI 值快照成普通
dict 再交给线程 —— 后台线程不碰任何 Tk 对象。

快捷键：Enter=检查登录/提交　Esc=停止　F5=刷新统计　Ctrl+L=清空日志
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import traceback
from datetime import datetime
from typing import List

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox, scrolledtext, simpledialog

# ---------------------------------------------------------------------------
# 复用速刷脚本模块
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import aqzrui_speedrun as sr  # noqa: E402
import aqzrui_login as lg     # noqa: E402
import labsafe_client as ls   # noqa: E402
import labsafe_player as lp   # noqa: E402

APP_TITLE = "aqzrui"
ICON = "app.ico"

# ---- 调色板：rich black + neon accent -------------------------------------
BG = "#0b0d10"          # 窗口底
SURF = "#12151a"        # 卡片
SURF2 = "#171b22"       # 输入框 / 按钮
SURF3 = "#1f2530"       # hover
LINE = "#242a35"        # 分隔线 / 边框
FG = "#e6e9ef"
MUTED = "#7b8494"
DIM = "#4b5563"

CYAN = "#22d3ee"
PURPLE = "#a78bfa"
LIME = "#a3e635"
AMBER = "#fbbf24"
ROSE = "#fb7185"

LOG_BG = "#080a0d"
LOG_COLORS = {
    "ts": "#414d5c",
    "info": "#c2cbd8",
    "ok": LIME,
    "err": ROSE,
    "warn": AMBER,
    "run": CYAN,
    "sys": PURPLE,
}


def asset_path(name: str) -> str:
    """资源文件路径：冻结后取 PyInstaller 临时解包目录，否则取脚本目录。"""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


def classify(msg: str) -> str:
    """按内容给日志行分级，用于着色。"""
    m = msg.strip()
    if m.startswith("✔") or "完成" in m:
        return "ok"
    if m.startswith("✗") or "失败" in m or "错误" in m or "异常" in m or "中止" in m:
        return "err"
    if m.startswith("…") or m.startswith("⚠") or "已请求停止" in m:
        return "warn"
    if m.startswith("▶"):
        return "run"
    if m.startswith("[界面]"):
        return "sys"
    return "info"


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.busy = False
        self.runner = None          # 当前 SpeedRun（供「停止」调 cancel）
        self.cas = None             # 登录中的 CasLogin（跨两步二次认证保留）
        self.t0 = 0.0
        self._pick_fonts()
        self._install_log_hook()
        self._build_ui()
        self._bind_keys()
        self._refresh_stats()
        self.root.after(100, self._poll)
        self._log("就绪")

    # ------------------------------------------------------------------
    # 字体：优先用设计向字体，缺失则依次回退
    # ------------------------------------------------------------------
    def _pick_fonts(self) -> None:
        fams = set(tkfont.families(self.root))

        def pick(prefs: List[str], fallback: str) -> str:
            for p in prefs:
                if p in fams:
                    return p
            return fallback

        mono = pick(["Cascadia Code", "Cascadia Mono", "JetBrains Mono",
                     "Fira Code", "Consolas"], "Consolas")
        disp = pick(["Noto Sans SC", "HarmonyOS Sans SC", "Microsoft YaHei UI",
                     "Segoe UI"], "Segoe UI")
        self.F_MONO = (mono, 9)
        self.F_MONO_S = (mono, 8)
        self.F_MONO_B = (mono, 10, "bold")
        self.F_UI = (disp, 9)
        self.F_UI_B = (disp, 9, "bold")
        self.F_HEAD = (disp, 16, "bold")
        self.F_SEC = (disp, 9, "bold")

    # ------------------------------------------------------------------
    # 日志重定向
    # ------------------------------------------------------------------
    def _install_log_hook(self) -> None:
        def gui_log(msg: str) -> None:
            line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
            self.q.put(("log", line, classify(msg)))
            try:
                with sr._open_validated(sr.PATHS.path("speedrun.log"), "a") as f:
                    f.write(line + "\n")
            except (OSError, ValueError):
                pass
        sr.log = gui_log

    def _log(self, msg: str) -> None:
        self.q.put(("log", f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", classify(msg)))

    # ------------------------------------------------------------------
    # 样式
    # ------------------------------------------------------------------
    def _init_style(self) -> None:
        st = ttk.Style()
        st.theme_use("clam")                    # 只有 clam 吃自定义配色
        self.root.configure(bg=BG)

        st.configure(".", background=BG, foreground=FG, font=self.F_UI)
        st.configure("TFrame", background=BG)
        st.configure("Surf.TFrame", background=SURF)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("S.TLabel", background=SURF, foreground=FG)
        st.configure("Muted.TLabel", background=SURF, foreground=MUTED, font=self.F_UI)
        st.configure("Head.TLabel", background=BG, foreground=FG, font=self.F_HEAD)
        st.configure("Sec.TLabel", background=BG, foreground=MUTED, font=self.F_SEC)
        st.configure("Mono.TLabel", background=SURF, foreground=FG, font=self.F_MONO)
        st.configure("Val.TLabel", background=SURF, foreground=CYAN, font=self.F_MONO_B)
        st.configure("SEP.TFrame", background=LINE)

        # 按钮：暗面 + hover 提亮
        st.configure("TButton", padding=(12, 6), relief="flat", borderwidth=0,
                     background=SURF2, foreground=FG, font=self.F_UI,
                     focuscolor=SURF2)
        st.map("TButton",
               background=[("active", SURF3), ("pressed", SURF3), ("disabled", "#12161c")],
               foreground=[("disabled", "#3d4653")])
        # 主操作：青色实心 + 深色字（对比强，一眼是 primary）
        st.configure("Primary.TButton", background=CYAN, foreground="#04121a",
                     font=self.F_UI_B, padding=(14, 6), relief="flat", borderwidth=0)
        st.map("Primary.TButton",
               background=[("active", "#5ae4f7"), ("pressed", "#5ae4f7"),
                           ("disabled", "#1b3b44")],
               foreground=[("disabled", "#5c7c85")])
        # 危险/停止：玫红描边感
        st.configure("Stop.TButton", background=SURF2, foreground=ROSE, font=self.F_UI_B)
        st.map("Stop.TButton",
               background=[("active", "#2a1a20")],
               foreground=[("disabled", "#3d4653")])
        # 演练开关：ON 青绿 / OFF 琥珀（状态用颜色表达，不靠长文字）
        st.configure("DryOn.TButton", background="#10321f", foreground=LIME,
                     font=self.F_MONO_B, padding=(12, 6), relief="flat", borderwidth=0)
        st.map("DryOn.TButton", background=[("active", "#17452a")])
        st.configure("DryOff.TButton", background="#3a2410", foreground=AMBER,
                     font=self.F_MONO_B, padding=(12, 6), relief="flat", borderwidth=0)
        st.map("DryOff.TButton", background=[("active", "#4d3015")])

        st.configure("TEntry", fieldbackground=SURF2, foreground=FG,
                     bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
                     insertcolor=CYAN, relief="flat", padding=5)
        st.map("TEntry",
               bordercolor=[("focus", CYAN)],
               lightcolor=[("focus", CYAN)], darkcolor=[("focus", CYAN)])
        st.configure("TSpinbox", fieldbackground=SURF2, foreground=FG,
                     bordercolor=LINE, arrowcolor=MUTED,
                     insertcolor=CYAN, relief="flat", padding=4, arrowsize=12)
        st.configure("Accent.Horizontal.TProgressbar", background=CYAN,
                     troughcolor="#161b22", borderwidth=0, thickness=5)

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _section(self, parent: tk.Widget, title: str) -> ttk.Frame:
        """标题行（霓虹竖条 + 短标签 + 发丝线）+ 内容容器。"""
        head = ttk.Frame(parent)
        head.pack(fill="x", pady=(14, 7))
        tk.Frame(head, bg=CYAN, width=3, height=13).pack(side="left")
        ttk.Label(head, text=title, style="Sec.TLabel").pack(side="left", padx=(8, 0))
        ttk.Frame(head, style="SEP.TFrame", height=1).pack(
            side="left", fill="x", expand=True, padx=(12, 0))
        body = ttk.Frame(parent, style="Surf.TFrame", padding=(14, 12))
        body.pack(fill="x")
        return body

    def _build_ui(self) -> None:
        self.root.title(f"{APP_TITLE} 速刷")
        self.root.geometry("980x980")
        self.root.minsize(880, 720)
        self._init_style()

        outer = ttk.Frame(self.root, padding=(16, 14, 16, 14))
        outer.pack(fill="both", expand=True)

        # ---- 顶栏 ----
        head = ttk.Frame(outer)
        head.pack(fill="x")
        title_box = ttk.Frame(head)
        title_box.pack(side="left")
        ttk.Label(title_box, text=APP_TITLE, style="Head.TLabel").pack(side="left")
        ttk.Label(title_box, text="速刷", style="Muted.TLabel").pack(
            side="left", padx=(8, 0), pady=(5, 0))
        self.badge = tk.Label(head, text="● 未检查", bg=BG, fg=DIM,
                              font=self.F_MONO_B)
        self.badge.pack(side="right", pady=(6, 0))

        # ---- 登录 ----
        f1 = self._section(outer, "登录")
        lr = ttk.Frame(f1, style="Surf.TFrame")
        lr.pack(fill="x")
        self.row_login = lr
        ttk.Label(lr, text="学号", style="Muted.TLabel", width=6).pack(side="left")
        self.var_user = tk.StringVar()
        ttk.Entry(lr, textvariable=self.var_user, width=15, font=self.F_MONO).pack(side="left")
        ttk.Label(lr, text="密码", style="Muted.TLabel").pack(side="left", padx=(12, 6))
        self.var_pass = tk.StringVar()
        self.ent_pass = ttk.Entry(lr, textvariable=self.var_pass, width=17,
                                  font=self.F_MONO, show="●")
        self.ent_pass.pack(side="left")
        self.btn_login = ttk.Button(lr, text="登录", command=self.on_login, width=8)
        self.btn_login.pack(side="left", padx=(12, 0))

        # 二次认证行（默认隐藏）
        self.f2fa = ttk.Frame(f1, style="Surf.TFrame")
        ttk.Label(self.f2fa, text="验证码", style="Muted.TLabel", width=6).pack(side="left")
        self.var_code = tk.StringVar()
        self.ent_code = ttk.Entry(self.f2fa, textvariable=self.var_code, width=12,
                                  font=self.F_MONO)
        self.ent_code.pack(side="left")
        self.btn_send = ttk.Button(self.f2fa, text="发送", command=self.on_send_code, width=8)
        self.btn_send.pack(side="left", padx=(10, 0))
        self.btn_submit2fa = ttk.Button(self.f2fa, text="提交", command=self.on_submit_code,
                                        width=8, style="Primary.TButton")
        self.btn_submit2fa.pack(side="left", padx=(8, 0))
        self.lbl_2fa = ttk.Label(self.f2fa, text="", style="Muted.TLabel")
        self.lbl_2fa.pack(side="left", padx=(10, 0))

        sr_row = ttk.Frame(f1, style="Surf.TFrame")
        sr_row.pack(fill="x", pady=(10, 0))
        ttk.Label(sr_row, text="会话", style="Muted.TLabel", width=6).pack(side="left")
        self.var_cookie = tk.StringVar()
        self.ent_cookie = ttk.Entry(sr_row, textvariable=self.var_cookie, font=self.F_MONO)
        self.ent_cookie.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.btn_check = ttk.Button(sr_row, text="检查", command=self.on_check, width=8)
        self.btn_check.pack(side="left")

        # ---- 参数 ----
        f2 = self._section(outer, "参数")
        g = ttk.Frame(f2, style="Surf.TFrame")
        g.pack(fill="x")
        ttk.Label(g, text="类别", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.var_kinds = tk.StringVar(value="")
        ttk.Entry(g, textvariable=self.var_kinds, width=16, font=self.F_MONO).grid(
            row=1, column=0, sticky="w", padx=(0, 18))
        ttk.Label(g, text="最多考试", style="Muted.TLabel").grid(row=0, column=1, sticky="w")
        self.var_maxexams = tk.IntVar(value=0)
        ttk.Spinbox(g, from_=0, to=100, textvariable=self.var_maxexams, width=6).grid(
            row=1, column=1, sticky="w", padx=(0, 18))
        ttk.Label(g, text="间隔 s", style="Muted.TLabel").grid(row=0, column=2, sticky="w")
        self.var_delay = tk.DoubleVar(value=0.5)
        ttk.Spinbox(g, from_=0.0, to=5.0, increment=0.1, textvariable=self.var_delay,
                    width=6).grid(row=1, column=2, sticky="w", padx=(0, 18))
        self.var_dry = tk.BooleanVar(value=True)
        self.btn_dry = ttk.Button(g, text="", command=self._toggle_dry, style="DryOn.TButton")
        self.btn_dry.grid(row=1, column=3, sticky="w")
        ttk.Label(g, text="AI 兜底", style="Muted.TLabel").grid(
            row=0, column=4, sticky="w", padx=(18, 0))
        self.var_ai = tk.BooleanVar(value=False)
        ttk.Checkbutton(g, text="答案库未命中时调用", variable=self.var_ai).grid(
            row=1, column=4, sticky="w", padx=(18, 0))
        self.var_retry_study = tk.BooleanVar(value=False)
        ttk.Checkbutton(g, text="重报已标记资料", variable=self.var_retry_study).grid(
            row=2, column=4, sticky="w", padx=(18, 0))
        self._sync_dry()

        # ---- 执行 ----
        f3 = self._section(outer, "执行")
        b = ttk.Frame(f3, style="Surf.TFrame")
        b.pack(fill="x")
        self.btn_bank = ttk.Button(b, text="抓答案库", command=self.on_bank, width=11)
        self.btn_study = ttk.Button(b, text="学习", command=self.on_study, width=8)
        self.btn_practice = ttk.Button(b, text="练习", command=self.on_practice, width=8)
        self.btn_exam = ttk.Button(b, text="考试", command=self.on_exam, width=8)
        self.btn_scores = ttk.Button(b, text="查成绩", command=self.on_scores, width=8)
        self.btn_all = ttk.Button(b, text="全部执行", command=self.on_all, width=11,
                                  style="Primary.TButton")
        self.btn_stop = ttk.Button(b, text="停止", command=self.on_stop, width=7,
                                   style="Stop.TButton")
        for i, w in enumerate((self.btn_bank, self.btn_study, self.btn_practice,
                               self.btn_exam, self.btn_scores, self.btn_all)):
            w.grid(row=0, column=i, padx=(0, 8))
        self.btn_stop.grid(row=0, column=6, padx=(14, 0))

        pr = ttk.Frame(f3, style="Surf.TFrame")
        pr.pack(fill="x", pady=(14, 0))
        self.prog = ttk.Progressbar(pr, mode="indeterminate",
                                    style="Accent.Horizontal.TProgressbar")
        self.prog.pack(side="left", fill="x", expand=True)
        self.lbl_timer = ttk.Label(pr, text="", style="Mono.TLabel", width=8, anchor="e")
        self.lbl_timer.pack(side="left", padx=(10, 0))

        st_row = ttk.Frame(f3, style="Surf.TFrame")
        st_row.pack(fill="x", pady=(12, 0))
        ttk.Label(st_row, text="答案库", style="Muted.TLabel").pack(side="left")
        self.lbl_bank_total = ttk.Label(st_row, text="—", style="Val.TLabel")
        self.lbl_bank_total.pack(side="left", padx=(8, 0))
        self.lbl_bank_mix = ttk.Label(st_row, text="", style="Mono.TLabel")
        self.lbl_bank_mix.pack(side="left", padx=(14, 0))
        ttk.Button(st_row, text="刷新", command=self._refresh_stats, width=7).pack(side="right")
        ttk.Button(st_row, text="目录", command=self._open_state_dir, width=7).pack(
            side="right", padx=(0, 8))

        # ---- labsafe 视频 ----
        f4 = self._section(outer, "labsafe 视频")
        r1 = ttk.Frame(f4, style="Surf.TFrame")
        r1.pack(fill="x")
        ttk.Label(r1, text="Token", style="Muted.TLabel", width=6).pack(side="left")
        self.var_ls_token = tk.StringVar()
        ttk.Entry(r1, textvariable=self.var_ls_token, font=self.F_MONO).pack(
            side="left", fill="x", expand=True)
        self.btn_ls_login = ttk.Button(r1, text="登录", command=self.on_ls_login, width=8)
        self.btn_ls_login.pack(side="left", padx=(8, 0))
        self.btn_ls_enum = ttk.Button(r1, text="枚举", command=self.on_ls_enum, width=8)
        self.btn_ls_enum.pack(side="left", padx=(8, 0))

        r2 = ttk.Frame(f4, style="Surf.TFrame")
        r2.pack(fill="x", pady=(10, 0))
        ttk.Label(r2, text="线程", style="Muted.TLabel").pack(side="left")
        self.var_ls_threads = tk.IntVar(value=2)
        ttk.Spinbox(r2, from_=1, to=16, textvariable=self.var_ls_threads,
                    width=4).pack(side="left", padx=(4, 12))
        ttk.Label(r2, text="时长", style="Muted.TLabel").pack(side="left")
        self.var_ls_duration = tk.IntVar(value=600)
        ttk.Spinbox(r2, from_=30, to=7200, increment=30,
                    textvariable=self.var_ls_duration, width=6).pack(side="left", padx=(4, 12))
        ttk.Label(r2, text="答案", style="Muted.TLabel").pack(side="left")
        self.var_ls_answers = tk.StringVar()
        ttk.Entry(r2, textvariable=self.var_ls_answers, font=self.F_MONO).pack(
            side="left", fill="x", expand=True, padx=(4, 10))
        self.var_ls_dry = tk.BooleanVar(value=True)
        ttk.Checkbutton(r2, text="演练", variable=self.var_ls_dry).pack(side="left")
        self.btn_ls_play = ttk.Button(r2, text="播放全部", command=self.on_ls_play_all,
                                      width=10, style="Primary.TButton")
        self.btn_ls_play.pack(side="left", padx=(10, 0))
        self.btn_ls_one = ttk.Button(r2, text="播放单个", command=self.on_ls_play_one,
                                     width=10)
        self.btn_ls_one.pack(side="left", padx=(8, 0))

        r3 = ttk.Frame(f4, style="Surf.TFrame")
        r3.pack(fill="x", pady=(10, 0))
        ttk.Label(r3, text="graph", style="Muted.TLabel").pack(side="left")
        self.var_ls_graph = tk.StringVar()
        ttk.Entry(r3, textvariable=self.var_ls_graph, width=22,
                  font=self.F_MONO).pack(side="left", padx=(4, 12))
        ttk.Label(r3, text="id", style="Muted.TLabel").pack(side="left")
        self.var_ls_id = tk.StringVar()
        ttk.Entry(r3, textvariable=self.var_ls_id, width=22,
                  font=self.F_MONO).pack(side="left", padx=(4, 12))
        ttk.Label(r3, text="秒", style="Muted.TLabel").pack(side="left")
        self.var_ls_one_dur = tk.IntVar(value=0)
        ttk.Spinbox(r3, from_=0, to=7200, increment=30,
                    textvariable=self.var_ls_one_dur, width=6).pack(side="left", padx=(4, 0))

        # ---- 日志 ----
        head4 = ttk.Frame(outer)
        head4.pack(fill="x", pady=(14, 7))
        tk.Frame(head4, bg=PURPLE, width=3, height=13).pack(side="left")
        ttk.Label(head4, text="日志", style="Sec.TLabel").pack(side="left", padx=(8, 0))
        ttk.Frame(head4, style="SEP.TFrame", height=1).pack(
            side="left", fill="x", expand=True, padx=(12, 0))
        ttk.Button(head4, text="清空", command=self._clear_log, width=7).pack(side="right")
        ttk.Button(head4, text="复制", command=self._copy_log, width=7).pack(
            side="right", padx=(0, 8))

        wrap = tk.Frame(outer, bg=LINE, padx=1, pady=1)
        wrap.pack(fill="both", expand=True)
        self.txt = scrolledtext.ScrolledText(
            wrap, font=self.F_MONO_S, wrap="word", height=8, relief="flat",
            background=LOG_BG, foreground=LOG_COLORS["info"], insertbackground=CYAN,
            borderwidth=0, padx=10, pady=8)
        self.txt.pack(fill="both", expand=True)
        for tag, col in LOG_COLORS.items():
            self.txt.tag_configure(tag, foreground=col)
        self.txt.configure(state="disabled")

        self._set_busy(False)

    def _bind_keys(self) -> None:
        self.ent_cookie.bind("<Return>", lambda _e: self.on_check())
        self.ent_pass.bind("<Return>", lambda _e: self.on_login())
        self.ent_code.bind("<Return>", lambda _e: self.on_submit_code())
        self.root.bind("<Escape>", lambda _e: self.on_stop())
        self.root.bind("<F5>", lambda _e: self._refresh_stats())
        self.root.bind("<Control-l>", lambda _e: self._clear_log())

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for w in (self.btn_bank, self.btn_study, self.btn_practice,
                  self.btn_exam, self.btn_scores, self.btn_all, self.btn_check,
                  self.btn_login, self.btn_send, self.btn_submit2fa,
                  self.btn_ls_login, self.btn_ls_enum,
                  self.btn_ls_play, self.btn_ls_one):
            w.configure(state=state)
        self.btn_stop.configure(state="normal" if busy else "disabled")
        if busy:
            self.t0 = time.time()
            self.prog.configure(mode="indeterminate")
            self.prog.start(12)
        else:
            self.prog.stop()
            self.prog.configure(mode="determinate", value=0)
            self.lbl_timer.configure(text="")

    def _clear_log(self) -> None:
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    def _copy_log(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.txt.get("1.0", "end").strip())
        self._append("已复制", "sys")

    def _append(self, text: str, tag: str = "info") -> None:
        self.txt.configure(state="normal")
        if text.startswith("[") and "]" in text[:12]:
            ts, rest = text.split("]", 1)
            self.txt.insert("end", ts + "]", "ts")
            self.txt.insert("end", rest + "\n", tag)
        else:
            self.txt.insert("end", text + "\n", tag)
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _refresh_stats(self) -> None:
        try:
            s = sr.AnswerBank.load().stats()
        except Exception as e:                       # noqa: BLE001
            self.lbl_bank_total.configure(text=f"读取失败 {e}")
            return
        self.lbl_bank_total.configure(text=str(s["total"]))
        names = {"A": "A", "B": "B", "C": "C"}
        self.lbl_bank_mix.configure(
            text=" · ".join(f"{names.get(k, k)} {v}" for k, v in sorted(s["by_type"].items())))

    def _open_state_dir(self) -> None:
        path = sr.PATHS.root
        try:
            if hasattr(os, "startfile"):
                os.startfile(path)                   # Windows
            else:
                messagebox.showinfo("状态目录", path)
        except OSError as e:
            messagebox.showwarning("打开失败", f"{path}\n{e}")

    # ------------------------------------------------------------------
    # 配置快照：Tk 变量只能在主线程读，开工前先快照成普通 dict
    # ------------------------------------------------------------------
    def _snapshot(self) -> dict:
        return {
            "cookie": self.var_cookie.get().strip().strip('"').strip("'"),
            "kinds": [k.strip() for k in self.var_kinds.get().split(",") if k.strip()],
            "max_exams": int(self.var_maxexams.get() or 0),
            "delay": max(0.0, float(self.var_delay.get() or 0)),
            "dry": bool(self.var_dry.get()),
            "ai": bool(self.var_ai.get()),
            "retry_study": bool(self.var_retry_study.get()),
        }

    def _make_client(self, cfg: dict) -> "sr.SpeedClient":
        kw = {"base": sr.BASE, "delay": cfg["delay"], "timeout": 25,
              "allowed_hosts": set(sr.DEFAULT_ALLOWED_HOSTS)}
        raw = cfg["cookie"]
        if raw:
            if "=" in raw:               # "ASP.NET_SessionId=xxx" 或整串 Cookie
                kw["cookie"] = raw
            else:                        # 只粘了会话值
                kw["session_id"] = raw
        return sr.SpeedClient(**kw)

    def _make_run(self, cfg: dict):
        ai = None
        if cfg.get("ai"):
            try:
                from ai_fallback import AIAnswerer
                ai = AIAnswerer.from_env(
                    log=sr.log,
                    cache=sr._read_json("ai_answers.json", {}),
                    on_save=lambda d: sr._write_json("ai_answers.json", d),
                )
            except Exception as e:                    # noqa: BLE001
                sr.log(f"AI 兜底初始化失败：{type(e).__name__}: {e}")
            if ai is None:
                sr.log("AI 兜底已勾选，但未找到 API Key"
                       "（AQZRUI_AI_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY）")
        return sr.SpeedRun(self._make_client(cfg), sr.AnswerBank.load(),
                           dry_run=cfg["dry"], kinds=cfg["kinds"] or None,
                           max_exams=cfg["max_exams"] or None,
                           ai_answerer=ai,
                           retry_study=cfg.get("retry_study", False))

    def _confirm_write(self, action: str) -> bool:
        if self.var_dry.get():
            return True
        return messagebox.askyesno(
            "确认",
            f"演练已关闭，将以真实账号执行「{action}」：\n会提交答案并改变账号状态。\n\n继续？",
            icon="warning", default="no")

    # ------------------------------------------------------------------
    # 账号密码登录（CAS 主认证 -> 二次认证 -> 自动填会话）
    # ------------------------------------------------------------------
    def _show_2fa(self, show: bool, text: str = "") -> None:
        if show:
            self.f2fa.pack(fill="x", pady=(10, 0), after=self.row_login)
            self.lbl_2fa.configure(text=text)
            self.ent_code.focus_set()
        else:
            self.f2fa.pack_forget()

    def on_login(self) -> None:
        # 密码不进通用快照，单独在主线程读取后交给本次任务
        user = self.var_user.get().strip()
        pwd = self.var_pass.get()
        if not user or not pwd:
            messagebox.showwarning("缺少信息", "请填写学号和密码。")
            return

        def fn() -> None:
            cas = lg.CasLogin(user, pwd, log=sr.log)
            self.q.put(("login", cas.step_main(), cas))
        self._run_async("登录", fn)

    def on_send_code(self) -> None:
        cas = self.cas
        if cas is None:
            self._append("… 请先点「登录」", "warn")
            return

        def fn() -> None:
            # 不硬编码渠道：由当前 reAuthType 决定（3=短信 / 11=邮箱 / 12=钉钉 …）
            ok, msg = cas.step_request_code("")
            wait = getattr(cas, "last_code_wait", 0)
            if ok and wait:
                msg = f"{msg}（{wait}s 内不可重发）"
            self.q.put(("log", f"{'✔' if ok else '✗'} 验证码 {msg}", "ok" if ok else "err"))
        self._run_async("发送验证码", fn)

    def on_submit_code(self) -> None:
        cas = self.cas
        code = self.var_code.get().strip()
        if cas is None:
            self._append("… 请先点「登录」", "warn")
            return
        if not code:
            messagebox.showwarning("缺少验证码", "请填写收到的验证码。")
            return

        def fn() -> None:
            self.q.put(("login", cas.step_submit_2fa(code), cas))
        self._run_async("提交验证码", fn)

    def on_check(self) -> None:
        cfg = self._snapshot()

        def fn() -> None:
            state = self._make_client(cfg).is_login()
            self.q.put(("session", sr.SpeedClient.logged_in(state), state))
        self._run_async("检查登录", fn)

    def on_scores(self) -> None:
        """只读：列出每场考试的得分、通过线和是否通过。"""
        cfg = self._snapshot()

        def fn() -> None:
            client = self._make_client(cfg)
            lst = client.exam_papers(1, 50)
            if sr.needs_login(lst):
                self.q.put(("log", "✗ 登录态无效，无法查成绩", "err"))
                return
            rows = sr.rows_of(lst)
            if not rows:
                self.q.put(("log", "… 没有考试记录", "warn"))
                return
            for e in rows:
                state = sr.pass_state(e.get("ISPASS"))
                mark = "✔ 通过" if state is True else ("✗ 未通过" if state is False else "? 未知")
                self.q.put(("log",
                            f"{e.get('ID')} {e.get('EXAMNAME', '')}: "
                            f"得分={e.get('SCORE', '-')} 通过线={e.get('PASSSCORE', '-')} {mark}",
                            "ok" if state is True else ("err" if state is False else "warn")))
        self._run_async("查成绩", fn)

    # ------------------------------------------------------------------
    # 后台执行
    # ------------------------------------------------------------------
    def _run_async(self, name: str, fn) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self._log(f"▶ {name}")

        def work() -> None:
            try:
                fn()
                self.q.put(("done", f"✔ {name} 完成", "ok"))
            except Exception as e:                    # noqa: BLE001
                self.q.put(("log", f"✗ {name} 失败：{type(e).__name__}: {e}", "err"))
                tb = traceback.format_exc().strip().splitlines()
                self.q.put(("log", f"   {tb[-1] if tb else ''}", "err"))
                self.q.put(("done", f"✗ {name} 失败", "err"))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self) -> None:
        """主线程消费队列：保证所有 Tk 调用都在主线程。"""
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append(item[1], item[2] if len(item) > 2 else "info")
                elif kind == "session":
                    _, ok, state = item
                    if ok:
                        who = state.get("XM") or state.get("USERNO") or ""
                        self.badge.configure(text="● 已登录", fg=LIME)
                        self._append(f"✔ 登录态有效 {who}", "ok")
                    else:
                        self.badge.configure(text="● 未登录", fg=ROSE)
                        self._append(f"✗ 登录态无效：{state.get('Msg') or '登录失效'}", "err")
                elif kind == "login":
                    _, res, cas = item
                    self.cas = cas
                    if res.need_2fa:
                        self._show_2fa(True,
                                       lg.REAUTH_NAMES.get(res.reauth_type, res.reauth_type))
                        names = "、".join(f"{c}={lg.REAUTH_NAMES.get(c, c)}" for c in res.channels)
                        if res.stage == "reauth_expired":
                            self._append(f"⚠ {res.msg}（请重新点「登录」获取新的二次认证会话）", "warn")
                        else:
                            self._append(f"⚠ {res.msg}", "warn")
                        if names:
                            self._append(f"   可选：{names}", "warn")
                    elif res.ok:
                        self._show_2fa(False)
                        self.var_cookie.set(res.cookie)      # 主线程写入
                        self.badge.configure(text="● 已登录", fg=LIME)
                        self._append("✔ 登录成功，会话已填入", "ok")
                    else:
                        self._append(f"✗ 登录失败：{res.msg}", "err")
                elif kind == "ask_code":
                    _, holder, ev = item
                    code = simpledialog.askstring(
                        "labsafe 二次认证", "请输入收到的验证码：",
                        parent=self.root)
                    holder["code"] = (code or "").strip()
                    ev.set()
                elif kind == "ls_token":
                    self.var_ls_token.set(item[1])
                    self._append("✔ labsafe token 已填入", "ok")
                elif kind == "done":
                    self._set_busy(False)
                    self._append(item[1], item[2] if len(item) > 2 else "info")
                    self.runner = None
                    self._refresh_stats()
        except queue.Empty:
            pass
        if self.busy and self.t0:
            el = int(time.time() - self.t0)
            self.lbl_timer.configure(text=f"{el // 60:02d}:{el % 60:02d}")
        self.root.after(120, self._poll)

    # ------------------------------------------------------------------
    # 按钮回调
    # ------------------------------------------------------------------
    def on_bank(self) -> None:
        cfg = self._snapshot()

        def fn() -> None:
            self.runner = self._make_run(cfg)
            self.runner.harvest_bank()
        self._run_async("抓答案库", fn)

    def on_study(self) -> None:
        if not self._confirm_write("学习"):
            return
        cfg = self._snapshot()

        def fn() -> None:
            self.runner = self._make_run(cfg)
            self.runner.run_study()
        self._run_async("学习", fn)

    def on_practice(self) -> None:
        if not self._confirm_write("练习"):
            return
        cfg = self._snapshot()

        def fn() -> None:
            self.runner = self._make_run(cfg)
            self.runner.run_practice()
        self._run_async("练习", fn)

    def on_exam(self) -> None:
        if not self._confirm_write("考试"):
            return
        cfg = self._snapshot()

        def fn() -> None:
            self.runner = self._make_run(cfg)
            self.runner.run_exam()
        self._run_async("考试", fn)

    def on_all(self) -> None:
        if not self._confirm_write("全部执行"):
            return
        cfg = self._snapshot()

        def fn() -> None:
            self.runner = self._make_run(cfg)
            self.runner.run_all()
        self._run_async("全部执行", fn)

    def on_stop(self) -> None:
        lp.request_stop()
        if self.runner is not None:
            self.runner.cancel()
            self._append("… 已请求停止", "warn")
        elif self.busy:
            self._append("… 已请求停止", "warn")
        else:
            self._append("… 当前无可停止的任务", "warn")

    # ------------------------------------------------------------------
    # labsafe 视频播放
    # ------------------------------------------------------------------
    def _ls_log(self, msg: str) -> None:
        """把播放器日志转成 GUI 队列（绿/红/黄分级）。"""
        tag = {"green": "ok", "red": "err", "yellow": "warn"}.get(
            lp._classify(msg), "info")
        self.q.put(("log", f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", tag))

    def _ls_code_provider(self, _res) -> str:
        """等待主线程弹窗输入二次认证验证码。"""
        holder: dict = {}
        ev = threading.Event()
        self.q.put(("ask_code", holder, ev))
        if not ev.wait(timeout=300):
            return ""
        return holder.get("code", "")

    def _ls_token(self) -> str:
        return self.var_ls_token.get().strip()

    def on_ls_login(self) -> None:
        user = self.var_user.get().strip()
        pwd = self.var_pass.get()
        if not user or not pwd:
            messagebox.showwarning("缺少信息", "请在上方「登录」区填写学号和密码。")
            return

        def fn() -> None:
            res, client = ls.login(user, pwd,
                                   code_provider=self._ls_code_provider)
            if res.ok and client:
                self.q.put(("ls_token", client.token))
                self.q.put(("log", f"✔ labsafe 登录成功 {res.msg}", "ok"))
            else:
                self.q.put(("log", f"✗ labsafe 登录未完成[{res.stage}]：{res.msg}",
                            "err"))
        self._run_async("labsafe 登录", fn)

    def on_ls_enum(self) -> None:
        token = self._ls_token()
        if not token:
            messagebox.showwarning("缺少 token", "请先登录或粘贴 LABSAFE_TOKEN。")
            return

        def fn() -> None:
            refs = lp.enumerate_sources(ls.LabsafeClient(token=token))
            self.q.put(("log", f"… labsafe 共 {len(refs)} 个视频",
                        "ok" if refs else "warn"))
            for r in refs[:40]:
                course = r.get("course_name") or r.get("graph_title") or ""
                name = r.get("source_name") or r["source_id"]
                self.q.put(("log", f"   {course} / {name}", "info"))
        self._run_async("labsafe 枚举", fn)

    @staticmethod
    def _int_var(var, default: int) -> int:
        """Spinbox 里可能是空串/非法输入，容错成 default。"""
        try:
            return int(var.get())
        except Exception:                             # noqa: BLE001
            return default

    def _ls_start(self, refs_fn, single: bool) -> None:
        token = self._ls_token()
        if not token:
            messagebox.showwarning("缺少 token", "请先登录或粘贴 LABSAFE_TOKEN。")
            return
        threads = max(1, self._int_var(self.var_ls_threads, 2))
        default_duration = max(0, self._int_var(self.var_ls_duration, 600))
        answers = self.var_ls_answers.get().strip()
        dry = bool(self.var_ls_dry.get())
        one_duration = max(0, self._int_var(self.var_ls_one_dur, 0))
        if not dry and not messagebox.askyesno(
                "确认", "labsafe 播放会以真实账号上报进度，继续？",
                icon="warning", default="no"):
            return

        def fn() -> None:
            refs = refs_fn()
            if not refs:
                self.q.put(("log", "… labsafe 无视频（检查 token / 选课）", "warn"))
                return
            lp.set_logger(self._ls_log)
            lp.clear_stop()
            try:
                done, fails = lp.run_playback(
                    refs, token=token,
                    duration=one_duration if single else 0,
                    default_duration=default_duration, speed=1.0,
                    bank=lp.AnswerBank(answers, "A"), dry=dry,
                    threads=threads, board=None, stop_event=lp.STOP_EVENT)
                self.q.put(("log", f"… labsafe 成功{done} 失败{fails}",
                            "ok" if fails == 0 else "warn"))
            finally:
                lp.reset_logger()
        self._run_async("labsafe 播放", fn)

    def on_ls_play_all(self) -> None:
        token = self._ls_token()
        self._ls_start(
            lambda: lp.enumerate_sources(ls.LabsafeClient(token=token)),
            single=False)

    def on_ls_play_one(self) -> None:
        graph = self.var_ls_graph.get().strip()
        sid = self.var_ls_id.get().strip()
        if not graph or not sid:
            messagebox.showwarning("缺少参数", "请填写 graphId 和视频源 ID。")
            return
        ref = {"graph": graph, "type": "", "course_id": "", "course_name": "",
               "source_id": sid, "source_name": "", "graph_title": "",
               "type_name": ""}
        self._ls_start(lambda: [ref], single=True)

    def _toggle_dry(self) -> None:
        self.var_dry.set(not self.var_dry.get())
        self._sync_dry()

    def _sync_dry(self) -> None:
        """状态用颜色 + 极短文字表达；确认弹窗是真正的安全闸。"""
        if self.var_dry.get():
            self.btn_dry.configure(text="演练 ON", style="DryOn.TButton")
        else:
            self.btn_dry.configure(text="演练 OFF", style="DryOff.TButton")


# ---------------------------------------------------------------------------
# 自检（打包后验证 exe 可用性用）
# ---------------------------------------------------------------------------
def _crypto_selftest() -> str:
    """校验打包后 pycryptodome 可用（登录模块的 AES 加密依赖它）。"""
    try:
        import base64
        enc = lg.encrypt_password("test", "2WjPfofCqTuT7cot")
        n = len(base64.b64decode(enc))
        return "ok" if (len(enc) == 108 and n == 80) else f"bad(len={len(enc)},bytes={n})"
    except Exception as e:                            # noqa: BLE001
        return f"FAIL {type(e).__name__}: {e}"


def selftest() -> int:
    lines: List[str] = []
    ok = True
    root = tk.Tk()
    root.withdraw()
    try:
        root.iconbitmap(asset_path(ICON))
    except Exception:                                 # noqa: BLE001
        pass
    try:
        app = App(root)
        root.update_idletasks()
        lines.append("ui_build=ok")
        lines.append(f"crypto_aes={_crypto_selftest()}")
        lines.append(f"bank_total={sr.AnswerBank.load().stats()['total']}")
        # 网络不通只说明服务端/链路有问题，不代表打包坏了，分开报
        try:
            cfg = app._snapshot()
            st = app._make_client(cfg).is_login()
            lines.append(f"net=ok is_login_flag={st.get('Flag')}")
        except Exception as e:                        # noqa: BLE001
            lines.append(f"net=unavailable ({type(e).__name__})")
        root.destroy()
    except Exception as e:                            # noqa: BLE001
        ok = False
        lines.append(f"error={type(e).__name__}: {e}")
        try:
            root.destroy()
        except Exception:                             # noqa: BLE001
            pass
    lines.append(f"frozen={getattr(sys, 'frozen', False)}")
    lines.append(f"state_dir={sr.PATHS.root}")
    lines.append(f"state_writable={os.access(sr.PATHS.root, os.W_OK)}")
    out = "\n".join(lines)
    try:
        with sr._open_validated(sr.PATHS.path("selftest.txt"), "w") as f:
            f.write(out + "\n")
    except (OSError, ValueError):
        pass
    print(out)
    return 0 if ok else 1


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    try:                                              # Windows 高 DPI 防糊
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:                                 # noqa: BLE001
        pass
    root = tk.Tk()
    try:
        root.iconbitmap(asset_path(ICON))
    except Exception:                                 # noqa: BLE001
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
