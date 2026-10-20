#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
labsafe 虚拟播放器（后台多线程播放 + 自动答题 + 合法上报）
==========================================================

模拟线上 courseDetail 播放器的完整时序（chunk-59452944.b1bd404d.js 逐字还原）：

    1. 进入页面     POST /jcedutec/courseSource/updateVisits {id, graphId}
    2. 播放中       每到 10s 整数位置：
                    POST /jcedutec/courseSource/finishRate {id, graphId, watchDuration}
                    （watchDuration 是"当前播放位置"的累计值，不是增量）
    3. 弹题         到达 ejectTime 时 POST /jcedutec/courseSource/submitAnswer
                    {id, graphId, questionId, option}
                    （真实播放器会暂停并要求答题后才能继续）
    4. 播完         POST /jcedutec/courseSource/finish {id, graphId}
    5. 回读验证     getById 的 q1/anum，确认服务端真的记了

"合法上报"的含义：默认 --speed 1.0，按 1x 真实节奏推进时钟，10 秒发一次心跳，
不伪造一次性的大数值；快进/跳段是前端明确禁止的行为（>3s 即回退），这里同样不做。

多线程：每个视频一个工作线程，各自维护自己的播放时钟；线程数由 --threads 控制。
不落盘断点：是否已完成以平台回读的计数为准，重复播放等价于页面里的"重新观看"。

用法：

    # 登录拿 token
    python labsafe_client.py login --user <学号>

    # 单个视频（时长未知时用 --duration 指定真实秒数）
    python labsafe_player.py --token <TOKEN> --graph <G> --id <I> --duration 600

    # 全部课程（自动枚举 myCourse 树 -> 课程 -> 视频源），3 线程并行
    python labsafe_player.py --token <TOKEN> --all --threads 3

    # 演练：只打印将执行的动作，不发任何写请求
    python labsafe_player.py --token <TOKEN> --all --dry-run

    # 提供答案（questionId 或题干 -> 选项），未命中时用 --default-answer
    python labsafe_player.py --token <TOKEN> --all \
        --answers-json '{"1818555631844372481":"A","关于用电安全的说法":["A","C"]}' \
        --default-answer A
"""

from __future__ import annotations

import argparse
import ctypes
import getpass
import json
import os
import sys
import threading
import time
import traceback
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from labsafe_client import (LabsafeClient, LabsafeAuthError, LabsafeError,
                            login)

_HEARTBEAT_STEP = 10          # 前端 timeStamp % 10 === 0 才上报

# ---- 终端颜色 ----
_COLOR_ENABLED = False
_COLORS = {
    "green": "\x1b[32m",
    "red": "\x1b[31m",
    "yellow": "\x1b[33m",
    "reset": "\x1b[0m",
}


def _paint(text: str, color: str = "") -> str:
    if not _COLOR_ENABLED or color not in _COLORS:
        return text
    return f"{_COLORS[color]}{text}{_COLORS['reset']}"


def _classify(msg: str) -> str:
    """按内容给日志分级：红=错误，黄=调试/警告，绿=进度与成功。"""
    low = msg.lower()
    if ("[error]" in low or "[fail]" in low or "失败" in msg or "异常" in msg
            or "错误" in msg or "拒绝" in msg or "token 失效" in low
            or "未成功" in msg or "success=false" in low or "✗" in msg):
        return "red"
    if ("[dry]" in low or "[warn]" in msg or "[字段]" in msg
            or "[skip]" in low or "警告" in msg or "排队" in msg):
        return "yellow"
    if ("[done]" in low or "完成" in msg or "成功" in msg or "上报" in msg
            or "开始" in msg or "弹题" in msg or "已答" in msg or "剩" in msg
            or "✔" in msg or "success=true" in low or "回读" in msg):
        return "green"
    return ""


class _Logger:
    """线程安全日志；仪表盘启用时先清屏、打印、再重绘。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()

    def __call__(self, msg: str) -> None:
        text = _paint(msg, _classify(msg))
        with self.lock:
            board = _BOARD
            if board is not None and board.live:
                board.emit_locked(text)
            else:
                print(text, flush=True)


LOG = _Logger()
_BOARD: Optional["ProgressBoard"] = None

# GUI 集成：日志回调可替换，停止信号全局共享
STOP_EVENT = threading.Event()


def set_logger(fn) -> None:
    global LOG
    LOG = fn


def reset_logger() -> None:
    global LOG
    LOG = _Logger()


def request_stop() -> None:
    STOP_EVENT.set()


def clear_stop() -> None:
    STOP_EVENT.clear()


# ---------------------------------------------------------------------------
# 答案库（来自命令行内联 JSON，不读文件）
# ---------------------------------------------------------------------------
class AnswerBank:
    """按 questionId 优先、题干其次匹配答案。"""

    def __init__(self, inline_json: str, default: str) -> None:
        self.by_id: Dict[str, Any] = {}
        self.by_stem: Dict[str, Any] = {}
        self.default = (default or "").strip()
        text = (inline_json or "").strip()
        if not text:
            return
        try:
            raw = json.loads(text)
        except ValueError as e:
            LOG(f"[warn] --answers-json 不是合法 JSON：{e}")
            return
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(k, str) and k.strip():
                    self.by_id[k.strip()] = v
                    self.by_stem[k.strip()] = v

    def pick(self, question: Dict[str, Any]) -> Optional[Any]:
        qid = str(question.get("id") or "").strip()
        stem = str(question.get("stem") or "").strip()
        if qid and qid in self.by_id:
            return self.by_id[qid]
        if stem and stem in self.by_stem:
            return self.by_stem[stem]
        return self.default or None


def normalize_option(kind: Any, option: Any) -> Any:
    """按前端规则归一化：kind 3 是多选（数组），1/2 是单选（字符串）。"""
    if str(kind) == "3":
        if isinstance(option, str):
            return [x for x in option.replace("，", ",").split(",") if x]
        if isinstance(option, list):
            return [str(x).strip() for x in option if str(x).strip()]
        return [str(option)]
    if isinstance(option, list):
        joined = "".join(str(x).strip() for x in option if str(x).strip())
        return joined or None
    text = str(option).strip()
    return text or None


# ---------------------------------------------------------------------------
# 枚举：树 -> 类型 -> 课程 -> 视频源
# ---------------------------------------------------------------------------
def _rows(j: Dict[str, Any]) -> List[Dict[str, Any]]:
    v = j.get("result")
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    if isinstance(v, dict):
        return [v]
    return []


def _first(d: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def enumerate_sources(c: LabsafeClient) -> List[Dict[str, Any]]:
    """返回待播放的视频源列表（含所属课程）。"""
    refs: List[Dict[str, Any]] = []
    tree = _rows(c.raw_get("/jcedutec/knowledgeGraph/queryGraphTree", {}))
    if not tree:
        LOG("  [warn] queryGraphTree 没有返回节点")
    for node in tree:
        graph_id = str(_first(node, "key", "id", "eventKey") or "")
        graph_title = str(_first(node, "title", "name") or "")
        if not graph_id:
            continue
        for t in _rows(c.raw_get("/jcedutec/knowledgeGraph/queryTypeById",
                                 {"id": graph_id})):
            ctype = str(_first(t, "type", "id") or "")
            tname = str(_first(t, "name", "title") or "")
            if not ctype:
                continue
            for course in _rows(c.raw_get(
                    "/jcedutec/knowledgeGraph/queryCourseTypeById",
                    {"id": graph_id, "courseType": ctype})):
                cid = str(_first(course, "id", "courseId") or "")
                cname = str(_first(course, "name", "title") or "")
                if not cid:
                    continue
                srcs = _rows(c.raw_get("/jcedutec/home/detailByTypeAndGraph",
                                       {"graphId": graph_id, "id": cid}))
                if not srcs:
                    # 有些"课程"本身就是一条视频源
                    srcs = [{"id": cid, "name": cname}]
                for s in srcs:
                    sid = str(_first(s, "id") or "")
                    if not sid:
                        continue
                    try:
                        dur = int(float(s.get("duration") or 0))
                    except (TypeError, ValueError):
                        dur = 0
                    refs.append({
                        "graph": graph_id, "graph_title": graph_title,
                        "type": ctype, "type_name": tname,
                        "course_id": cid, "course_name": cname,
                        "source_id": sid,
                        "source_name": str(_first(s, "name", "title") or ""),
                        "duration": dur,
                        "is_finish": str(s.get("isFinish") or "") == "1",
                    })
    return refs


# ---------------------------------------------------------------------------
# 单个视频源的"播放"
# ---------------------------------------------------------------------------
def resolve_duration(detail: Dict[str, Any], args_duration: int,
                     default_duration: int, tag: str,
                     override: int = 0) -> Optional[int]:
    if override > 0:
        return override
    for k in ("duration", "allTime", "videoTime", "timeLength", "longTime",
              "length", "totalTime", "courseTime"):
        v = detail.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
        if isinstance(v, str) and v.isdigit() and int(v) > 0:
            return int(v)
    if args_duration > 0:
        LOG(f"    {tag} 接口未返回时长，使用 --duration {args_duration}")
        return args_duration
    if default_duration > 0:
        LOG(f"    {tag} 接口未返回时长，使用 --default-duration {default_duration}"
            "（注意：若小于真实时长，finish 可能不被计入）")
        return default_duration
    LOG(f"    {tag} 无法确定视频时长，跳过（用 --duration 显式指定）")
    return None


def _bar(pct: int, width: int = 12) -> str:
    """ASCII 进度条，避免部分 Windows 控制台不支持方块字符。"""
    pct = max(0, min(100, pct))
    filled = int(pct * width / 100 + 0.5)
    return "#" * filled + "-" * (width - filled)


def _pad_label(text: str, width: int = 20) -> str:
    """按终端显示宽度截断/补齐，中文按 2 列计算，保证多行对齐。"""
    out: List[str] = []
    used = 0
    for ch in str(text):
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out) + " " * (width - used)


def _enable_ansi() -> bool:
    """Windows 控制台开启 VT 转义；失败则退回逐行日志。"""
    if os.name != "nt":
        return True
    try:
        k = ctypes.windll.kernel32
        handle = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(k.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:                                  # noqa: BLE001
        return False


class ProgressBoard:
    """原地刷新的多视频进度面板（仅终端；重定向时自动关闭）。"""

    def __init__(self, total: int, speed: float, max_rows: int = 8) -> None:
        self.total = total
        self.speed = speed
        self.max_rows = max(1, max_rows)
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.order: List[str] = []
        self.done = 0
        self.failed = 0
        self._drawn = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.live = bool(sys.stdout.isatty()) and _enable_ansi()

    # ---- 生命周期 ----
    def start(self) -> None:
        if not self.live:
            return
        with LOG.lock:
            sys.stdout.write("\x1b[?25l")          # 隐藏光标
            sys.stdout.flush()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(0.5):
            with LOG.lock:
                self._redraw_locked()

    def stop(self) -> None:
        if not self.live:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        with LOG.lock:
            self._clear_locked()
            sys.stdout.write("\x1b[?25h")          # 恢复光标
            sys.stdout.flush()

    # ---- 状态 ----
    def register(self, key: str, label: str, duration: int) -> None:
        if not self.live:
            return
        with LOG.lock:
            if key not in self.rows:
                self.order.append(key)
            self.rows[key] = {"label": label, "duration": duration, "pos": 0}
            self._redraw_locked()

    def update(self, key: str, pos: int) -> None:
        if not self.live:
            return
        with LOG.lock:
            row = self.rows.get(key)
            if row is not None:
                row["pos"] = pos

    def finish(self, key: str, ok: bool) -> None:
        if not self.live:
            return
        with LOG.lock:
            if key in self.rows:
                self.rows.pop(key, None)
                if key in self.order:
                    self.order.remove(key)
            if ok:
                self.done += 1
            else:
                self.failed += 1
            self._redraw_locked()

    # ---- 绘制 ----
    def _render_rows(self) -> List[str]:
        etas = []
        for key in self.order:
            row = self.rows.get(key)
            if row and row["duration"] and self.speed > 0:
                etas.append(int((row["duration"] - row["pos"]) / self.speed))
        eta_txt = f"  最慢剩{max(etas)}s" if etas else ""
        rows = [_paint(f"播放 {len(self.rows)}/{self.total}  完成{self.done}  失败{self.failed}  "
                       f"{self.speed}x{eta_txt}", "green")]
        for key in self.order[:self.max_rows]:
            row = self.rows.get(key)
            if not row:
                continue
            d = row["duration"] or 0
            p = row["pos"]
            pct = int(p * 100 / d) if d else 0
            eta = int((d - p) / self.speed) if d and self.speed > 0 else 0
            rows.append(_paint(
                f"  {_pad_label(row['label'], 20)} [{_bar(pct)}] {pct:3d}%  "
                f"{p}/{d}s  剩{eta}s", "green"))
        if len(self.order) > self.max_rows:
            rows.append(_paint(f"  … 另有 {len(self.order) - self.max_rows} 个排队",
                               "yellow"))
        return rows

    def _clear_locked(self) -> None:
        if not self._drawn:
            return
        sys.stdout.write(f"\x1b[{self._drawn}A")
        for _ in range(self._drawn):
            sys.stdout.write("\x1b[2K\n")
        sys.stdout.write(f"\x1b[{self._drawn}A")
        self._drawn = 0

    def _redraw_locked(self) -> None:
        if not self.live:
            return
        rows = self._render_rows()
        count = max(self._drawn, len(rows))
        if self._drawn:
            sys.stdout.write(f"\x1b[{self._drawn}A")
        for i in range(count):
            line = rows[i] if i < len(rows) else ""
            sys.stdout.write("\x1b[2K" + line + "\n")
        if count > len(rows):
            sys.stdout.write(f"\x1b[{count - len(rows)}A")
        self._drawn = len(rows)
        sys.stdout.flush()

    def emit_locked(self, msg: str) -> None:
        """清掉面板 -> 打印普通日志 -> 立即重绘。"""
        self._clear_locked()
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()
        self._redraw_locked()


def play_one(ref: Dict[str, Any], token: str, duration_arg: int,
             default_duration: int, speed: float, bank: AnswerBank,
             answer_delay: float, dry: bool,
             durations: Optional[Dict[str, int]] = None,
             progress_step: int = 5,
             board: Optional[ProgressBoard] = None,
             stop_event: Optional[threading.Event] = None) -> Tuple[bool, str]:
    """按真实播放器时序跑完一个视频源。返回 (是否完整走完, 说明)。"""
    sid = ref["source_id"]
    gid = ref["graph"]
    tag = f"[{ref.get('source_name') or sid}]"
    ev = stop_event if stop_event is not None else STOP_EVENT

    c = LabsafeClient(token=token, log=lambda m: None)

    my = c.raw_get("/jcedutec/courseSource/ismyCourse", {"id": sid})
    mine = str(my.get("message") or "").strip().lower()
    if mine and mine != "true":
        return False, f"{tag} 不是本人课程（ismyCourse={mine}），跳过"

    dj = c.raw_get("/jcedutec/courseSource/getById", {"id": sid, "graphId": gid})
    detail = dj.get("result") if isinstance(dj.get("result"), dict) else {}
    override = int((durations or {}).get(sid, 0) or 0)
    if not override:
        override = int(ref.get("duration") or 0)
    duration = resolve_duration(detail, duration_arg, default_duration, tag,
                                override)
    if not duration:
        return False, f"{tag} 缺少视频时长"

    qj = c.raw_get("/jcedutec/courseSource/queryCourseQuestionRelaByMainId",
                   {"id": sid, "graphId": gid})
    questions = _rows(qj)

    key = f"{gid}:{sid}"
    if board is not None:
        board.register(key, ref.get("source_name") or sid, duration)

    LOG(f"{tag} 开始 {duration}s 弹题{len(questions)}"
        + (f" 《{ref.get('course_name')}》" if ref.get("course_name") else ""))

    if not dry:
        try:
            c.update_visits(sid, gid)
        except LabsafeError as e:
            LOG(f"{tag} updateVisits 失败: {e}")

    t0 = time.monotonic()
    next_mark = _HEARTBEAT_STEP
    answered: set = set()
    reports = 0
    last_pct = -max(1, progress_step)
    step = max(1, min(100, progress_step))

    while True:
        if ev.is_set():
            LOG(f"{tag} 已停止")
            return False, f"{tag} 已停止"
        pos = min(duration, int((time.monotonic() - t0) * speed))

        # 进度展示：终端里用原地刷新的面板；重定向时退回每 step% 一行
        pct = int(pos * 100 / duration) if duration else 100
        if board is not None and board.live:
            board.update(key, pos)
        elif pct >= last_pct + step or (pct >= 100 and last_pct < 100):
            last_pct = pct
            remain = max(0, int((duration - pos) / speed))
            LOG(f"{tag} [{_bar(pct)}] {pct:3d}%  {pos}/{duration}s  剩{remain}s")

        # 弹题：到达 ejectTime 先答题再推进（真实播放器此时是暂停等待）
        for q in questions:
            qid = str(q.get("id") or "")
            if not qid or qid in answered:
                continue
            eject = q.get("ejectTime")
            eject = int(eject) if isinstance(eject, (int, float)) else 0
            if eject > pos:
                continue
            opt = normalize_option(q.get("kind"), bank.pick(q))
            if not opt:
                LOG(f"{tag} 弹题{qid} 无答案，跳过")
                answered.add(qid)
                continue
            if dry:
                LOG(f"[dry] {tag} 弹题{qid} -> {opt}")
            else:
                try:
                    c.submit_answer(sid, gid, qid, opt)
                    LOG(f"{tag} 弹题{qid} -> {opt}")
                except LabsafeError as e:
                    LOG(f"{tag} 弹题{qid} 失败: {e}")
            answered.add(qid)
            time.sleep(answer_delay)

        # 心跳：按 10s 整数位置上报（与前端 timeupdate 相同）
        while next_mark <= pos:
            if dry:
                LOG(f"[dry] {tag} 上报 {next_mark}s")
            else:
                try:
                    j = c.finish_rate(sid, gid, next_mark)
                    if not j.get("success"):
                        LOG(f"{tag} 上报{next_mark}s 失败: "
                            f"{str(j.get('message') or j)[:120]}")
                except LabsafeError as e:
                    LOG(f"{tag} 上报{next_mark}s 异常: {e}")
            reports += 1
            next_mark += _HEARTBEAT_STEP

        if pos >= duration:
            break
        time.sleep(0.2)

    # 收尾：把最后一段不足 10s 的位置补上，再 finish（对应前端 ended 事件）
    if duration % _HEARTBEAT_STEP != 0:
        if dry:
            LOG(f"[dry] {tag} 补尾上报 {duration}s")
        else:
            try:
                c.finish_rate(sid, gid, duration)
            except LabsafeError as e:
                LOG(f"{tag} 补尾上报失败: {e}")

    if dry:
        LOG(f"[dry] {tag} finish")
        return True, f"{tag} 演练 {reports}次心跳"

    elapsed = int(time.monotonic() - t0)

    try:
        c.finish(sid, gid)
        ok = True
    except LabsafeError as e:
        LOG(f"{tag} finish 失败: {e}")
        ok = False

    try:
        dj2 = c.raw_get("/jcedutec/courseSource/getById",
                        {"id": sid, "graphId": gid})
        d2 = dj2.get("result") if isinstance(dj2.get("result"), dict) else {}
        LOG(f"{tag} 已答{d2.get('q1')}/{d2.get('anum')} 错{d2.get('unCorrect')}")
    except LabsafeError as e:
        LOG(f"{tag} 回读失败: {e}")

    return ok, (f"{tag} {duration}s/{elapsed}s 心跳{reports} "
                f"弹题{len(answered)}/{len(questions)}")


# ---------------------------------------------------------------------------
# 播放编排（CLI 与 GUI 共用）
# ---------------------------------------------------------------------------
def run_playback(refs: List[Dict[str, Any]], *,
                 token: str,
                 duration: int = 0,
                 default_duration: int = 600,
                 speed: float = 1.0,
                 bank: Optional[AnswerBank] = None,
                 answer_delay: float = 0.5,
                 dry: bool = False,
                 durations: Optional[Dict[str, int]] = None,
                 progress_step: int = 5,
                 threads: int = 2,
                 board: Optional[ProgressBoard] = None,
                 stop_event: Optional[threading.Event] = None,
                 skip_finished: bool = True,
                 ) -> Tuple[int, int]:
    """跑一批视频源，返回 (成功数, 失败数)。"""
    threads = max(1, int(threads))
    bank = bank or AnswerBank("", "A")
    ev = stop_event if stop_event is not None else STOP_EVENT
    if skip_finished:
        keep = [r for r in refs if not r.get("is_finish")]
        skipped = len(refs) - len(keep)
        if skipped:
            LOG(f"跳过已完成 {skipped} 个，待播 {len(keep)} 个")
        refs = keep
    if not refs:
        LOG("没有待播放的视频")
        return 0, 0
    LOG(f"开始 {len(refs)} 个视频  线程{threads}  {speed}x"
        f"{'  演练' if dry else ''}")
    done = 0
    fails = 0
    try:
        with ThreadPoolExecutor(max_workers=threads) as pool:
            futs = {pool.submit(play_one, r, token, duration,
                                default_duration, speed, bank,
                                answer_delay, dry, durations,
                                progress_step, board, ev): r
                    for r in refs}
            for fut in as_completed(futs):
                r = futs[fut]
                key = f"{r['graph']}:{r['source_id']}"
                try:
                    ok, msg = fut.result()
                except LabsafeAuthError as e:
                    LOG(f"[stop] token失效: {e}")
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
                except Exception as e:                 # noqa: BLE001
                    ok, msg = False, f"线程异常: {type(e).__name__}: {e}"
                if board is not None:
                    board.finish(key, ok)
                LOG(f"[{'done' if ok else 'fail'}] {msg}")
                if ok:
                    done += 1
                else:
                    fails += 1
                if ev.is_set():
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
    finally:
        if board is not None:
            board.stop()
    LOG(f"结束 成功{done} 失败{fails}")
    return done, fails


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="labsafe 虚拟播放器")
    ap.add_argument("--login", action="store_true",
                    help="CAS 登录并打印 LABSAFE_TOKEN（不播放）")
    ap.add_argument("--user", default="", help="--login 时的学号/工号")
    ap.add_argument("--password", default="", help="--login 时的密码（建议留空走交互）")
    ap.add_argument("--code", default="", help="二次认证验证码（非交互时用）")
    ap.add_argument("--token", default=os.environ.get("LABSAFE_TOKEN", ""))
    ap.add_argument("--all", action="store_true", help="枚举全部我的课程并播放")
    ap.add_argument("--graph", default="", help="单个视频时必填 graphId")
    ap.add_argument("--id", default="", help="单个视频时必填视频源 ID")
    ap.add_argument("--duration", type=int, default=0,
                    help="单个视频的真实时长（秒）")
    ap.add_argument("--default-duration", type=int, default=600,
                    help="--all 模式下接口未返回时长时的缺省值")
    ap.add_argument("--durations-json", default="",
                    help='按视频源指定真实时长，如 {"<sourceId>": 930}')
    ap.add_argument("--threads", type=int, default=2, help="并行播放线程数")
    ap.add_argument("--progress-step", type=int, default=5,
                    help="非终端模式下进度日志步长（百分比），默认 5")
    ap.add_argument("--progress-rows", type=int, default=8,
                    help="实时面板最多显示几行，默认 8")
    ap.add_argument("--no-live", action="store_true",
                    help="关闭原地刷新面板，改为逐行进度日志")
    ap.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                    help="颜色：auto=仅终端启用（默认），always=强制，never=关闭")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="时钟倍速；1.0=真实节奏（合法上报默认值）")
    ap.add_argument("--answers-json", default="",
                    help='答案库 JSON 字符串，如 {"题目ID":"A","题干":["A","C"]}')
    ap.add_argument("--default-answer", default="A",
                    help="答案未命中时的缺省选项")
    ap.add_argument("--answer-delay", type=float, default=0.5,
                    help="每次提交弹题后的间隔秒数")
    ap.add_argument("--include-finished", action="store_true",
                    help="已完成(isFinish=1)的视频也重播；默认跳过")
    ap.add_argument("--dry-run", action="store_true",
                    help="演练：不发送任何写请求")
    args = ap.parse_args(argv)
    _init_color(args.color)

    if args.login:
        user = (args.user or os.environ.get("LABSAFE_USER")
                or os.environ.get("AQZRUI_USER") or "").strip()
        pwd = (args.password or os.environ.get("LABSAFE_PASSWORD")
               or os.environ.get("AQZRUI_PASSWORD") or "")
        if not user:
            user = input("学号/工号: ").strip()
        if not pwd:
            pwd = getpass.getpass("密码（不回显）: ")

        def provider(_res: Any) -> str:
            return args.code or input("请输入收到的验证码: ").strip()

        try:
            res, client = login(user, pwd, code_provider=provider)
        except Exception as e:                         # noqa: BLE001
            _err(f"[ERROR] 登录异常：{type(e).__name__}: {e}")
            return 1
        if res.ok and client:
            print(_paint(f"[OK] {res.msg}", "green"))
            print(_paint(f"LABSAFE_TOKEN={client.token}", "green"))
            return 0
        _err(f"[ERROR] 登录未完成[{res.stage}]：{res.msg}")
        return 1

    token = (args.token or "").strip()
    if not token:
        _err("缺少 token：--token 或环境变量 LABSAFE_TOKEN")
        return 2
    if args.speed <= 0:
        _err("--speed 必须大于 0（合法上报请保持 1.0）")
        return 2
    if args.threads < 1:
        args.threads = 1
    if abs(args.speed - 1.0) > 1e-9:
        LOG(f"[warn] 倍速 {args.speed}x 会压缩真实耗时；"
            "若平台校验真实播放时长，可能不被计入")

    bank = AnswerBank(args.answers_json, args.default_answer)

    durations: Dict[str, int] = {}
    if args.durations_json.strip():
        try:
            raw = json.loads(args.durations_json)
        except ValueError as e:
            _err(f"[ERROR] --durations-json 不是合法 JSON：{e}")
            return 2
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    continue
                if iv > 0:
                    durations[str(k)] = iv

    c = LabsafeClient(token=token, log=lambda m: None)
    try:
        if args.id:
            if not args.graph:
                _err("单个视频模式需要 --graph")
                return 2
            refs: List[Dict[str, Any]] = [{
                "graph": args.graph, "type": "", "course_id": "",
                "course_name": "", "source_id": args.id,
                "source_name": "", "graph_title": "", "type_name": ""}]
        else:
            if not args.all:
                _err("指定 --all 枚举全部课程，或用 --graph/--id 播放单个视频")
                return 2
            LOG("枚举课程")
            refs = enumerate_sources(c)
            LOG(f"共 {len(refs)} 个视频")
        if not refs:
            LOG("无视频（检查 token / 选课）")
            return 0

        global _BOARD
        board = ProgressBoard(len(refs), args.speed, args.progress_rows)
        if args.no_live:
            board.live = False
        _BOARD = board
        board.start()
        try:
            _, fails = run_playback(
                refs, token=token, duration=args.duration,
                default_duration=args.default_duration, speed=args.speed,
                bank=bank, answer_delay=args.answer_delay, dry=args.dry_run,
                durations=durations, progress_step=args.progress_step,
                threads=args.threads, board=board,
                skip_finished=not args.include_finished)
        finally:
            _BOARD = None
        return 0 if fails == 0 else 1

    except LabsafeAuthError as e:
        _err(f"[ERROR] {e}")
        return 1
    except LabsafeError as e:
        _err(f"[ERROR] {e}")
        return 1


def _err(text: str) -> None:
    """错误统一走红色 stderr。"""
    print(_paint(text, "red"), file=sys.stderr, flush=True)


def _init_color(mode: str = "auto") -> None:
    """auto=仅终端启用；always=强制；never=关闭。"""
    global _COLOR_ENABLED
    if mode == "always":
        _COLOR_ENABLED = True
        if os.name == "nt":
            _enable_ansi()
    elif mode == "auto":
        _COLOR_ENABLED = bool(sys.stdout.isatty())
        if _COLOR_ENABLED and os.name == "nt":
            _enable_ansi()
    else:
        _COLOR_ENABLED = False


def _pause_if_frozen() -> None:
    """双击运行时不要让输出一闪而过。"""
    if not getattr(sys, "frozen", False):
        return
    try:
        if sys.stdin and sys.stdin.isatty():
            input("\n按回车退出...")
    except (EOFError, OSError):
        pass


def _interactive() -> int:
    """无参数启动（双击）时进入的交互模式。"""
    _init_color("auto")
    print("=" * 52)
    print(" labsafe 虚拟播放器")
    print("=" * 52)
    print(" 1) 登录并播放全部课程")
    print(" 2) 使用已有 token 播放")
    print(" 3) 只登录，打印 token")
    print(" 0) 退出")
    try:
        choice = input("请选择: ").strip()
    except (EOFError, KeyboardInterrupt):
        return 0
    if choice in ("", "0", "q", "Q"):
        return 0

    def do_login() -> Optional[str]:
        try:
            user = input("学号/工号: ").strip()
            pwd = getpass.getpass("密码（不回显）: ")
        except (EOFError, KeyboardInterrupt):
            return None
        if not user or not pwd:
            _err("[ERROR] 学号或密码为空")
            return None

        def provider(_res: Any) -> str:
            return input("请输入收到的验证码: ").strip()

        try:
            res, client = login(user, pwd, code_provider=provider)
        except Exception as e:                         # noqa: BLE001
            _err(f"[ERROR] 登录异常：{type(e).__name__}: {e}")
            return None
        if res.ok and client:
            print(_paint(f"[OK] {res.msg}", "green"))
            print(_paint(f"LABSAFE_TOKEN={client.token}", "green"))
            return client.token
        _err(f"[ERROR] 登录未完成[{res.stage}]：{res.msg}")
        return None

    if choice == "3":
        return 0 if do_login() else 1

    if choice == "1":
        tok = do_login()
        if not tok:
            return 1
        try:
            threads = input("并行线程数（默认 2）: ").strip() or "2"
            dry = input("先演练不提交？(Y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return 0
        argv = ["--token", tok, "--all", "--threads", threads]
        if dry not in ("n", "no", "否"):
            argv.append("--dry-run")
        return main(argv)

    if choice == "2":
        try:
            tok = input("LABSAFE_TOKEN: ").strip()
            if not tok:
                return 2
            mode = input("播放全部课程？(Y/n): ").strip().lower()
            if mode in ("n", "no", "否"):
                graph = input("graphId: ").strip()
                sid = input("视频源 ID: ").strip()
                dur = input("视频真实时长(秒，留空=600): ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if mode in ("n", "no", "否"):
            if not graph or not sid:
                _err("[ERROR] graphId 和视频源 ID 必填")
                return 2
            argv = ["--token", tok, "--graph", graph, "--id", sid]
            if dur.isdigit():
                argv += ["--duration", dur]
            return main(argv)
        try:
            threads = input("并行线程数（默认 2）: ").strip() or "2"
        except (EOFError, KeyboardInterrupt):
            return 0
        return main(["--token", tok, "--all", "--threads", threads])

    print("未知选项")
    return 2


if __name__ == "__main__":
    if len(sys.argv) == 1:
        try:
            code = _interactive()
        except KeyboardInterrupt:
            code = 130
        except BaseException:                           # noqa: BLE001
            traceback.print_exc()
            code = 1
        _pause_if_frozen()
        sys.exit(code)

    try:
        code = main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    except KeyboardInterrupt:
        code = 130
    except BaseException:                               # noqa: BLE001
        traceback.print_exc()
        code = 1
    sys.exit(code)
