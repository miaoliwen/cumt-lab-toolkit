#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国矿业大学 实验室安全准入平台（aqzrui）速刷脚本
=================================================

思路
----
平台的学习题目接口（`index.asmx/selfStudyNew`）在未登录时即可返回
`Caption` / `TxCode` / `optionA..F` / **`AnSwer`**，而答案的编码格式与
交卷接口 `AQKS.asmx/PaperSubmit` 的答案串格式**完全一致**：

    TxCode=A 单选  AnSwer="B"         -> radio : "<分>@<题目ID>-B,..."
    TxCode=B 多选  AnSwer="B|C|D|E"   -> check : "<分>@<题目ID>-B|C|D|E,..."
    TxCode=C 判断  AnSwer="Y"         -> judge : "<分>@<题目ID>-Y,..."

所以先把全部安全类别的学习题抓成本地**答案库**，再用答案库去刷练习
（`indexJdz.asmx/tjtm` 逐题提交）与在线考试
（`GetOnePaper` -> `getonebyone` -> `viewExamLab` -> `PaperSubmit`），
即可做到"取题即答、答即命中"。

登录
----
平台 Web 登录走 CAS 统一认证 + 二次认证（短信/邮箱/钉钉/安全令牌）。
脚本**不绕过任何认证**：请在浏览器里本人完成一次登录，然后把
`ASP.NET_SessionId` 交给脚本：

    set AQZRUI_SESSION=<浏览器 DevTools → Application → Cookies 里的值>
    python aqzrui_speedrun.py check

也支持 `--cookie "ASP.NET_SessionId=xxx"` 与 `--token <localStorage.Token>`。

常用命令
--------
    python aqzrui_speedrun.py check                 # 会话存活检查
    python aqzrui_speedrun.py bank                  # 抓答案库（匿名即可）
    python aqzrui_speedrun.py bank --show           # 抓完打印统计
    python aqzrui_speedrun.py study                 # 刷学习时长/进度
    python aqzrui_speedrun.py practice              # 刷练习（需登录）
    python aqzrui_speedrun.py exam --dry-run        # 演练考试组卷（不交卷）
    python aqzrui_speedrun.py exam                  # 刷考试（需登录）
    python aqzrui_speedrun.py all                   # study + practice + exam

安全与节奏
----------
    --delay 0.5      每个请求之间间隔秒数（默认 0.5）
    --dry-run        只读演练：取卷/取题/算答案，但不提交、不改状态
    --kinds 11,13    只处理指定类别 ID
    --max-exams 3    最多处理几场考试
    --state-dir      本地状态目录

本地文件访问说明：脚本只读写固定白名单文件名（answer_bank.json /
checkpoint.json / speedrun.log / missing_questions.json），且全部经
`_open_validated()` —— 该函数在 open 前即时做 realpath 归一化、拒绝含
'..' 的路径、校验文件名在白名单内、并确认最终路径位于 state 目录之下。
没有任何用户输入会作为文件名或目录片段进入 open()。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import requests

# ---------------------------------------------------------------------------
# 复用已测绘好的底层客户端（信封解析 / 容错 JSON / Cookie 注入）
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from aqzrui_api import BASE, AqzruiClient, _lenient_json, _unwrap  # noqa: E402

DEFAULT_ALLOWED_HOSTS = {"dygx.cumt.edu.cn"}


def app_dir() -> str:
    """
    程序所在目录。

    注意：冻结成单文件 exe 后 `__file__` 指向 PyInstaller 的临时解包目录
    （退出即删），若拿它当状态目录，答案库每次运行都会丢。所以冻结态必须
    改用可执行文件所在目录。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def default_state_dir() -> str:
    """
    状态目录：优先 exe/脚本 同级的 .speedrun（便携、肉眼可见）；
    同级不可写（如装在 Program Files）时退回 %LOCALAPPDATA%。
    """
    beside = os.path.join(app_dir(), ".speedrun")
    if os.access(app_dir(), os.W_OK):
        return beside
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "aqzrui-speedrun")


DEFAULT_STATE_DIR = default_state_dir()

# 允许在 state 目录内读写的固定文件名白名单（无用户可控片段）
STATE_FILENAMES = (
    "answer_bank.json",
    "checkpoint.json",
    "speedrun.log",
    "missing_questions.json",
    "ai_answers.json",
    "selftest.txt",
)

LOGIN_HINT = "需要登录态：用「登录」走账号密码登录，或粘贴 ASP.NET_SessionId。"

# updateWatchtime 的 times 缺省值：getPreStudyDocOne 没给出时长时用它
# 可用 AQZRUI_STUDY_SECONDS 覆盖（例如平台要求更长的观看时长时）
try:
    STUDY_FALLBACK_SECONDS = int(os.environ.get("AQZRUI_STUDY_SECONDS") or 600)
except ValueError:
    STUDY_FALLBACK_SECONDS = 600

# 真实节奏上报：AQZRUI_STUDY_REALTIME=1 时，按视频时长真实推进并分段上报，
# 用于服务端校验“会话真实耗时”的场景。分段间隔由 AQZRUI_STUDY_HEARTBEAT 控制。
STUDY_REALTIME = str(os.environ.get("AQZRUI_STUDY_REALTIME") or "").lower() in (
    "1", "true", "yes", "on")
try:
    STUDY_HEARTBEAT_SECONDS = max(5, int(os.environ.get("AQZRUI_STUDY_HEARTBEAT") or 30))
except ValueError:
    STUDY_HEARTBEAT_SECONDS = 30


# ---------------------------------------------------------------------------
# 目标校验（仅 http/https；拒绝环回 / 私有 / 保留地址字面量）
# ---------------------------------------------------------------------------
def validate_target(url: str, allowed_hosts: Optional[Iterable[str]] = None) -> None:
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError(f"仅允许 http/https，收到 {p.scheme!r}")
    host = p.hostname
    if not host:
        raise ValueError(f"URL 缺少主机名: {url!r}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        hosts = set(allowed_hosts or DEFAULT_ALLOWED_HOSTS)
        if hosts and host not in hosts:
            raise ValueError(f"主机不在允许列表内: {host}（如需放行请用 --allow-host）")
        return
    if (ip.is_loopback or ip.is_private or ip.is_reserved
            or ip.is_link_local or ip.is_multicast or ip.is_unspecified):
        raise ValueError(f"拒绝环回/私有/保留地址: {host}")


# ---------------------------------------------------------------------------
# 状态目录与安全文件访问
# ---------------------------------------------------------------------------
class StateDir:
    """state 目录；目录内只放白名单文件，文件名全部由程序常量给出。"""

    def __init__(self, state_dir: str = DEFAULT_STATE_DIR) -> None:
        self.root = os.path.realpath(state_dir)
        os.makedirs(self.root, exist_ok=True)
        if not os.path.isdir(self.root):
            raise ValueError(f"state 路径不是目录: {state_dir}")

    def path(self, name: str) -> str:
        """取白名单内的状态文件绝对路径。name 必须是已登记常量。"""
        if name not in STATE_FILENAMES:
            raise ValueError(f"未登记的状态文件: {name!r}")
        full = os.path.join(self.root, name)
        if os.path.basename(full) != name:       # 结构性阻断穿越
            raise ValueError(f"非法状态文件名: {name!r}")
        return full


PATHS = StateDir(DEFAULT_STATE_DIR)


def _open_validated(path: str, mode: str):
    """
    唯一允许的 open() 入口：open 前即时校验。
      1) realpath 归一化；
      2) 原路径不得含 '..'；
      3) 文件名必须在 STATE_FILENAMES 白名单内；
      4) 归一化后必须位于当前 state 根目录之下。
    任何一条不满足即抛错，不发生文件访问。
    """
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ValueError("非法路径")
    if ".." in path:
        raise ValueError(f"路径含 '..'，已拒绝: {path}")
    if os.path.basename(path) not in STATE_FILENAMES:
        raise ValueError(f"未登记的状态文件: {path}")
    full = os.path.realpath(path)
    root = os.path.realpath(PATHS.root)
    if full != root and not full.startswith(root + os.sep):
        raise ValueError(f"路径越界（不在 {root} 内）: {path}")
    return open(full, mode, encoding="utf-8")


def _read_json(name: str, default: Any) -> Any:
    p = PATHS.path(name)
    if not os.path.exists(p):
        return default
    try:
        with _open_validated(p, "r") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        log(f"读取 {name} 失败: {e}，使用默认值")
        return default


def _write_json(name: str, obj: Any) -> None:
    p = PATHS.path(name)
    with _open_validated(p, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with _open_validated(PATHS.path("speedrun.log"), "a") as f:
            f.write(line + "\n")
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def norm_caption(s: str) -> str:
    """题干归一化：去标签、去空白、去全角空格。仅供库内匹配使用。"""
    s = re.sub(r"<[^>]+>", "", s or "")
    s = s.replace("\u3000", "").replace("\xa0", "")
    return re.sub(r"\s+", "", s)


def norm_answer(tx_code: str, raw: Any) -> Optional[str]:
    """把 AnSwer 归一化成提交串里使用的格式。"""
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    tc = (tx_code or "").upper()
    if tc == "C":                                   # 判断：Y / N
        if s in ("Y", "N"):
            return s
        if s in ("T", "TRUE", "正确", "1", "A"):
            return "Y"
        if s in ("F", "FALSE", "错误", "0", "B"):
            return "N"
        return None
    letters = re.findall(r"[A-F]", s)               # 单选 / 多选
    if not letters:
        return None
    return "|".join(sorted(set(letters)))


def js_literal(obj: Dict[str, Any]) -> str:
    """把 dict 序列化成前端同款 JS 字面量（字符串值一律单引号）。"""
    parts = []
    for k, v in obj.items():
        if isinstance(v, bool):
            parts.append(f"{k}:{'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            parts.append(f"{k}:{v}")                 # 裸数字，与前端一致
        else:
            safe = str(v).replace("'", "").replace("\\", "")
            parts.append(f"{k}:'{safe}'")
    return "{" + ",".join(parts) + "}"


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------
class SpeedClient(AqzruiClient):
    """在测绘客户端之上补：裸 JS 字面量 POST、会话探测、速率限制。"""

    def __init__(self, *a, delay: float = 0.5, allowed_hosts=None, **kw):
        super().__init__(*a, **kw)
        self.delay = max(0.0, float(delay))
        self.allowed_hosts = set(allowed_hosts or DEFAULT_ALLOWED_HOSTS)
        self._last = 0.0
        validate_target(self.base, self.allowed_hosts)

    def _pace(self) -> None:
        if self.delay <= 0:
            return
        gap = time.time() - self._last
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last = time.time()

    def _post(self, path: str, body: str, retries: int = 2) -> Any:
        last_exc = None
        for attempt in range(retries + 1):
            self._pace()
            try:
                r = self.s.post(
                    self.base + path,
                    data=body.encode("utf-8"),
                    headers={
                        "Content-Type": "application/json; charset=utf-8",
                        "Authorization": "WanXinKey2019",
                    },
                    timeout=self.timeout,
                )
                r.encoding = "utf-8"
                return _unwrap(_lenient_json(r.text))
            except requests.RequestException as e:
                last_exc = e
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"POST {path} 失败: {last_exc}")

    def post_js(self, path: str, payload: Dict[str, Any], retries: int = 2) -> Any:
        """发送前端同款 JS 字面量（服务端 JavaScriptSerializer 可解析）。"""
        return self._post(path, js_literal(payload), retries)

    # 父客户端的 post_json / post_form 没有重试；这里补上，
    # 避免一次瞬时连接重置（10054）就把整段流程打断。
    def post_json(self, path: str, payload: Dict[str, Any],
                  auth: bool = True, retries: int = 2) -> Any:
        return self._retrying("POST", path,
                              lambda: super(SpeedClient, self).post_json(path, payload, auth),
                              retries)

    def post_form(self, path: str, data: Dict[str, Any],
                  auth: bool = True, retries: int = 2) -> Any:
        return self._retrying("POST", path,
                              lambda: super(SpeedClient, self).post_form(path, data, auth),
                              retries)

    def _retrying(self, verb: str, path: str, fn, retries: int) -> Any:
        last: Optional[Exception] = None
        for attempt in range(retries + 1):
            self._pace()
            try:
                return fn()
            except requests.RequestException as e:
                last = e
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"{verb} {path} 失败: {last}")

    def is_login(self) -> Dict[str, Any]:
        r = self._post("wxInterface/index.asmx/IsLogin", "{}")
        return r if isinstance(r, dict) else {}

    @staticmethod
    def logged_in(state: Any) -> bool:
        """IsLogin 的 Flag 才是登录态：1=已登录，0=登录失效。
        注意 IsCheckLogin 只表示"启用登录校验"，不代表已登录。"""
        if not isinstance(state, dict):
            return False
        if str(state.get("Flag", "")) == "1":
            return True
        return bool(state.get("ID")) and "失效" not in str(state.get("Msg", "")) \
            and "重新登录" not in str(state.get("Msg", ""))


# ---------------------------------------------------------------------------
# 答案库
# ---------------------------------------------------------------------------
@dataclass
class BankEntry:
    id: str
    txcode: str
    answer: str
    caption: str = ""
    source: str = "study"          # study | practice | manual


class AnswerBank:
    def __init__(self) -> None:
        self.by_id: Dict[str, BankEntry] = {}
        self.by_caption: Dict[str, BankEntry] = {}

    @classmethod
    def load(cls) -> "AnswerBank":
        b = cls()
        raw = _read_json("answer_bank.json", {})
        for item in (raw or {}).get("entries", []):
            try:
                b.add(BankEntry(**item))
            except TypeError:
                continue
        return b

    def save(self) -> None:
        _write_json("answer_bank.json", {
            "updated": datetime.now().isoformat(timespec="seconds"),
            "entries": [vars(e) for e in self.by_id.values()],
        })

    def add(self, e: BankEntry) -> None:
        if not e.answer:
            return
        self.by_id[e.id] = e
        key = norm_caption(e.caption)
        if key:
            self.by_caption.setdefault(key, e)

    def ingest(self, questions: Iterable[Dict[str, Any]], source: str = "study") -> int:
        n = 0
        for q in questions:
            qid = str(q.get("ID") or q.get("id") or "").strip()
            if not qid:
                continue
            tc = str(q.get("TxCode") or q.get("TCODE") or "").upper()
            raw = q.get("AnSwer") if "AnSwer" in q else q.get("answer")
            ans = norm_answer(tc, raw)
            if not ans:
                continue
            self.add(BankEntry(qid, tc, ans, str(q.get("Caption") or ""), source))
            n += 1
        return n

    def lookup(self, q: Dict[str, Any]) -> Optional[str]:
        qid = str(q.get("ID") or "").strip()
        if qid and qid in self.by_id:
            return self.by_id[qid].answer
        key = norm_caption(str(q.get("Caption") or ""))
        if key and key in self.by_caption:
            return self.by_caption[key].answer
        return None

    def stats(self) -> Dict[str, Any]:
        from collections import Counter
        return {"total": len(self.by_id),
                "by_type": dict(Counter(e.txcode for e in self.by_id.values()))}


def extract_questions(resp: Any) -> List[Dict[str, Any]]:
    """兼容两种题面形状：data.row[]（分页）与 data[0].A/B/C（整卷分组）。"""
    if not isinstance(resp, dict):
        return []
    data = resp.get("data")
    if isinstance(data, dict):
        rows = data.get("row") or data.get("rows") or []
        return [q for q in rows if isinstance(q, dict)]
    if isinstance(data, list) and data and isinstance(data[0], dict):
        if any(k in data[0] for k in ("A", "B", "C")):
            out: List[Dict[str, Any]] = []
            for k in ("A", "B", "C"):
                v = data[0].get(k)
                if isinstance(v, list):
                    for q in v:
                        if isinstance(q, dict):
                            q.setdefault("TxCode", k)
                            out.append(q)
            return out
        return [q for q in data if isinstance(q, dict)]
    return []


def flagged(resp: Any) -> Tuple[str, str]:
    if isinstance(resp, dict):
        msg = resp.get("message")
        if isinstance(msg, list) and msg and isinstance(msg[0], dict):
            return str(msg[0].get("ReturnFlag", "")), str(msg[0].get("ReturnMsg", ""))
        if "Flag" in resp:
            return str(resp.get("Flag", "")), str(resp.get("Msg", ""))
    return "", ""


def needs_login(resp: Any) -> bool:
    """
    只把明确的登录失效信号判为真。
    注意不要把 `{"total":0,"rows":[]}` 当成未登录 —— 空列表是合法结果，
    早期版本这样判会把“该类别没有资料”误报成登录失效，进而中止整个学习阶段。
    """
    if isinstance(resp, dict) and "_raw" in resp:
        raw = str(resp.get("_raw") or "")
        return "访问禁止" in raw or "登录" in raw
    flag, msg = flagged(resp)
    if flag == "4" or "登录" in msg or "超时" in msg:
        return True
    return flag == "0" and "参数错误" in msg


def rows_of(resp: Any) -> List[Dict[str, Any]]:
    """从分页响应里取行：兼容 rows / data.row / data[] 三种形状。"""
    if not isinstance(resp, dict):
        return []
    rows = resp.get("rows")
    if rows is None:
        d = resp.get("data")
        if isinstance(d, dict):
            rows = d.get("row") or d.get("rows")
        elif isinstance(d, list):
            rows = d
    return [x for x in (rows or []) if isinstance(x, dict)]


def _raw_snippet(resp: Any, limit: int = 120) -> str:
    if isinstance(resp, dict):
        raw = resp.get("_raw")
        if raw:
            return re.sub(r"\s+", " ", str(raw))[:limit]
    return str(resp)[:limit]


def num_or_none(v: Any) -> Optional[float]:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def pass_state(v: Any) -> Optional[bool]:
    """把 ISPASS 的各种写法归一成 True/False/None（未知）。"""
    s = str(v).strip().lower()
    if s in ("1", "true", "是", "y", "yes", "通过", "合格"):
        return True
    if s in ("0", "false", "否", "n", "no", "未通过", "不合格"):
        return False
    return None


# ---------------------------------------------------------------------------
# 刷分引擎
# ---------------------------------------------------------------------------
class SpeedRun:
    def __init__(self, client: SpeedClient, bank: AnswerBank,
                 dry_run: bool = False, kinds: Optional[List[str]] = None,
                 max_exams: Optional[int] = None,
                 ai_answerer: Optional[Any] = None,
                 retry_study: bool = False):
        self.c = client
        self.bank = bank
        self.dry = dry_run
        self.kinds_filter = set(kinds) if kinds else None
        self.max_exams = max_exams
        self.ai = ai_answerer
        self.retry_study = retry_study
        self._ai_stats = {"bank": 0, "ai": 0, "guess": 0}
        self.cp = _read_json("checkpoint.json",
                             {"done_practice": [], "done_exam": [], "watchtime": []})
        self.missing: List[Dict[str, Any]] = []
        self.stop = threading.Event()

    def cancel(self) -> None:
        """请求停止：在循环边界生效（GUI 的「停止」按钮用）。"""
        self.stop.set()

    def _stopped(self) -> bool:
        return self.stop.is_set()

    # --- checkpoint ---
    def _save_cp(self) -> None:
        _write_json("checkpoint.json", self.cp)

    def _done(self, bucket: str, key: str) -> bool:
        return key in self.cp.get(bucket, [])

    def _mark(self, bucket: str, key: str) -> None:
        self.cp.setdefault(bucket, [])
        if key not in self.cp[bucket]:
            self.cp[bucket].append(key)
            self._save_cp()

    def _unmark(self, bucket: str, key: str) -> None:
        """解除完成标记（例如旧版本把“已交卷”误记成“已通过”）。"""
        items = self.cp.get(bucket)
        if isinstance(items, list) and key in items:
            items.remove(key)
            self._save_cp()

    # --- 类别 ---
    def all_kinds(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        page = 1
        while True:
            r = self.c.security_kinds(page, 100)
            got = r.get("rows") if isinstance(r, dict) else None
            if not got:
                break
            rows += [x for x in got if isinstance(x, dict)]
            if len(got) < 100:
                break
            page += 1
        if self.kinds_filter:
            rows = [x for x in rows if str(x.get("ID")) in self.kinds_filter]
        return rows

    # --- 1) 答案库 ---
    def harvest_bank(self) -> Dict[str, Any]:
        log("抓取答案库（匿名接口 index.asmx/selfStudyNew）…")
        total_new = 0
        kinds = self.all_kinds()
        if not kinds:
            log("未取到类别列表，检查网络/Authorization 头")
            return self.bank.stats()
        for k in kinds:
            if self._stopped():
                log("已请求停止，中止抓库")
                break
            kid, name = str(k.get("ID")), k.get("TYPENAME", "")
            got_all: List[Dict[str, Any]] = []
            page = 1
            while True:
                qs = extract_questions(self.c.learn_questions_new(kid, "xx", page, 200))
                if not qs:
                    break
                got_all += qs
                if len(qs) < 200:
                    break
                page += 1
            n = self.bank.ingest(got_all, "study")
            total_new += n
            log(f"  类别 {kid} {name}: 取 {len(got_all)} 题，入库 {n}")
        self.bank.save()
        log(f"答案库更新完成：新增 {total_new}，总计 {len(self.bank.by_id)}")
        return self.bank.stats()

    # --- 2) 学习 ---
    def _study_progress(self) -> Dict[str, tuple]:
        """
        getStudyinfo → {类别ID: (必修数, 选修数, 已读必修, 已读选修)}
        对应前端表格 Count[0..3]：Count[2]/[3] 就是「已读」计数，
        链接是 StudyDoc.html?isdown=1&ismust=…。未登录时这几项是字符串"未登录"。
        """
        try:
            r = self.c.study_info()
        except Exception as e:                        # noqa: BLE001
            log(f"getStudyinfo 失败：{e}")
            return {}
        if needs_login(r):
            return {}
        data = r.get("data") if isinstance(r, dict) else None
        rows = data.get("AQLB") if isinstance(data, dict) else None
        out: Dict[str, tuple] = {}
        for e in (rows or []):
            if not isinstance(e, dict):
                continue
            c = e.get("Count") or []

            def num(i: int) -> Optional[int]:
                v = c[i] if i < len(c) else None
                if isinstance(v, int):
                    return v
                return int(v) if str(v).isdigit() else None

            out[str(e.get("ID"))] = (num(0), num(1), num(2), num(3))
        # 注意：Count[0]/[1] 是"资料总数"，匿名访问也有真实数字；
        # 只有 Count[2]/[3]（已读必修/选修）是用户维度的，未登录时是字符串"未登录"
        # （解析后为 None）。所以判"是否登录"必须只看 [2]/[3]，看整条会漏判。
        if out and all(t[2] is None and t[3] is None for t in out.values()):
            return {}
        return out

    def _docs_of_kind(self, kind_id: str, must: str) -> Tuple[List[Dict[str, Any]], bool]:
        """
        取某类别下的资料列表（must='1' 必修 / '0' 选修），分页拉全。
        返回 (资料列表, 是否请求失败)。

        空列表是合法结果（该类确实没资料），不能当成登录失效；
        真正的拒绝（访问禁止/登录页/异常）会带 `_raw` 或明确的登录标志。
        """
        docs: List[Dict[str, Any]] = []
        page = 1
        while page <= 20:
            try:
                r = self.c.study_docs(page, 50, security_kind_id=kind_id, ismust=must)
            except Exception as e:                    # noqa: BLE001
                log(f"  类别 {kind_id}（{must}）资料列表请求失败：{e}")
                return docs, True
            if isinstance(r, dict) and "_raw" in r:
                log(f"  类别 {kind_id}（{must}）资料列表被服务端拒绝：{_raw_snippet(r)}")
                return docs, True
            if needs_login(r):
                log(f"  类别 {kind_id}（{must}）资料列表需要登录：{_raw_snippet(r)}")
                return docs, True
            rows = rows_of(r)
            if not rows:
                break
            docs.extend(rows)
            try:
                total = int(r.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
            if total and len(docs) >= total:
                break
            if not total and len(rows) < 50:
                break
            page += 1
        return docs, False

    def run_study(self) -> None:
        """
        学习：枚举资料 -> 逐条上报观看时长。

        资料集合由两部分合并、按资料 ID 去重：
          1. `getPreStudyDocIndex` 首页索引（旧版稳定路径，实测上报过 20 条）；
          2. `getPreStudyDoc` 按类别分页（覆盖更全，但该接口在部分网络下会被
             服务端以“访问禁止”拒绝，所以不能只依赖它）。

        关键点：`updateWatchtime` 的 studyid 必须是 `getPreStudyDocOne(ID)`
        返回的 **RID（学习记录ID）**，不是资料 ID（前端 PlayVideoNew.html 原文：
        `parent.$('#hfstudyid').val(Data[0].RID);//学习记录ID`）。
        传资料 ID 时服务端照样回 Flag=1 但不会记进度 —— 所以这里上报完还要用
        getStudyinfo 复核计数有没有真的涨。
        """
        log("== 学习：枚举资料 -> 上报观看时长 ==")
        before = self._study_progress()
        if not before:
            log("拿不到学习统计（未登录），本阶段跳过")
            log(LOGIN_HINT)
            return

        docs: Dict[str, Tuple[str, str, str, str]] = {}   # id -> (id, label, kind_name, DOCKIND)

        # 1) 首页索引：先拿这一份，保证即使分类接口被拒也有资料可报
        try:
            idx = self.c.study_doc_index(top=50)
            if isinstance(idx, dict) and "_raw" in idx:
                log(f"  首页资料索引被拒绝：{_raw_snippet(idx)}")
            elif needs_login(idx):
                log("  首页资料索引需要登录（继续尝试按类别）")
            else:
                for d in rows_of(idx):
                    did = str(d.get("ID") or "").strip()
                    if did:
                        docs.setdefault(did, (did, "索引", "首页资料",
                                              str(d.get("DOCKIND") or "")))
                log(f"  首页资料索引 {len(docs)} 条")
        except Exception as e:                        # noqa: BLE001
            log(f"  首页资料索引失败：{e}")

        # 2) 按类别分页（失败不致命，已取到的资料仍会继续处理）
        net_fails = 0
        abort = False
        for k in self.all_kinds():
            if self._stopped() or abort:
                break
            kid, name = str(k.get("ID")), k.get("TYPENAME", "")
            for must, label in (("1", "必修"), ("0", "选修")):
                rows, failed = self._docs_of_kind(kid, must)
                if failed:
                    net_fails += 1
                    # “访问禁止”是 IIS/WAF 层拦截：立刻连打只会更糟，退避后再试
                    backoff = min(10 * net_fails, 30)
                    log(f"  列表请求被拒，退避 {backoff}s 后继续")
                    time.sleep(backoff)
                    if net_fails >= 5:
                        log("连续 5 个类别取资料列表失败，跳过剩余类别（已取到的资料继续上报）")
                        abort = True
                        break
                    continue
                net_fails = 0
                if not rows:
                    continue
                log(f"  类别 {kid} {name} · {label}资料 {len(rows)} 条")
                for d in rows:
                    did = str(d.get("ID") or "").strip()
                    if did:
                        docs.setdefault(did, (did, label, name,
                                              str(d.get("DOCKIND") or "")))

        if self._stopped():
            log("已请求停止，中止学习阶段")

        if not docs:
            log("没有取到任何学习资料（列表接口均失败，或该账号确实没有资料）")
            self._report_study_delta(before, self._study_progress(), 0)
            return

        log(f"待处理资料 {len(docs)} 条")
        sent = 0
        marked_keys: List[str] = []
        dbg_fields = False
        dbg_resp = False
        for did, label, name, dockind in docs.values():
            if self._stopped():
                log("已请求停止，中止学习阶段")
                break
            key = f"doc:{did}"
            if self._done("watchtime", key) and not self.retry_study:
                continue
            # 前端只有 DOCKIND=="2" 才弹视频播放器；其余资料的动作是"下载"，
            # updateWatchtime 对它们是无效调用（回 Flag=1 但不记已读）。
            if dockind and dockind != "2":
                log(f"    资料 {did}: 文档类资料(DOCKIND={dockind})不走观看时长，跳过")
                continue
            try:
                det = self.c.study_doc_one(did)
            except Exception as e:                    # noqa: BLE001
                log(f"    资料 {did} 详情失败：{e}")
                continue
            if isinstance(det, dict) and "_raw" in det:
                log(f"    资料 {did} 详情被拒绝：{_raw_snippet(det)}")
                continue
            if needs_login(det):
                log(LOGIN_HINT)
                break
            dd = det.get("data")
            first = dd[0] if isinstance(dd, list) and dd and isinstance(dd[0], dict) else {}
            rid = str(first.get("RID") or "").strip()
            if not rid:
                log(f"    资料 {did}: 未取到学习记录ID(RID)，跳过")
                continue

            # 平台已经记为已读的资料不用再报（ISDOWN 是前端表格里的已读/已下载标志）
            isdown = str(first.get("ISDOWN") or first.get("IsDown") or "").strip()
            if isdown not in ("", "0"):
                log(f"    资料 {did}: 平台已标记已读(ISDOWN={isdown})，跳过")
                self._mark("watchtime", key)
                continue

            # 前端 PlayVideoNew 关闭时发的是「当前播放位置」的累计秒数，不是分段增量。
            # 早期版本按 300s 分块发送，服务端若只记最后一次/最大值，就只记到 300s。
            candidates = ("Times", "TIMES", "STUDYTIME", "SYSTIME",
                          "DURATION", "Duration", "VIDEOTIME", "TOTALTIME")
            raw_secs = next((first.get(k) for k in candidates
                             if first.get(k) not in (None, "", 0, "0")), None)
            try:
                secs = int(raw_secs or 0) or STUDY_FALLBACK_SECONDS
            except (TypeError, ValueError):
                secs = STUDY_FALLBACK_SECONDS
            if not dbg_fields:
                dbg_fields = True
                # 把所有字段打一次：便于发现平台真正用于时长的字段（如 PLAYTIME/FILEURL）
                detail_txt = ", ".join(f"{k}={v!r}" for k, v in first.items())
                log(f"    [字段] 资料 {did} 全部字段: {detail_txt[:500]}")
                log(f"    [字段] 时长候选: "
                    + ", ".join(f"{k}={first.get(k)!r}" for k in candidates)
                    + f" -> 使用 {secs}s")

            if self.dry:
                log(f"    [dry] {label}资料 {did} RID={rid} 上报 {secs}s")
                continue
            ok = True
            try:
                time.sleep(0.5)                       # 让服务端先建立学习记录会话
                rr: Dict[str, Any] = {}
                stopped = False
                last_sent = 0
                if STUDY_REALTIME and secs > STUDY_HEARTBEAT_SECONDS:
                    # 真实节奏：按心跳间隔发递增的累计位置，每段都慢于真实时间
                    t0 = time.monotonic()
                    t = STUDY_HEARTBEAT_SECONDS
                    while t <= secs:
                        wait = t - (time.monotonic() - t0)
                        while wait > 0:
                            time.sleep(min(wait, 5))
                            wait = t - (time.monotonic() - t0)
                        if self._stopped():
                            stopped = True
                            break
                        rr = self.c.watch_time(rid, t)
                        if str(rr.get("Flag", "")) != "1":
                            break
                        last_sent = t
                        pct = int(t * 100 / secs) if secs else 100
                        log(f"    资料 {did} RID={rid} 进度 {pct}% ({t}/{secs}s)")
                        t += STUDY_HEARTBEAT_SECONDS
                    if stopped:
                        ok = False
                        log("    已请求停止，中止该资料")
                    else:
                        if last_sent != secs:
                            rr = self.c.watch_time(rid, secs)
                        log(f"    资料 {did} RID={rid} 真实节奏上报完成 {secs}s")
                else:
                    rr = self.c.watch_time(rid, secs)
                if not dbg_resp:
                    dbg_resp = True
                    log(f"    [字段] updateWatchtime 响应: {str(rr)[:160]}")
                if ok and str(rr.get("Flag", "")) != "1":
                    ok = False
                    log(f"    RID {rid} 上报异常: {str(rr)[:100]}")
                elif ok:
                    log(f"    资料 {did} RID={rid} 上报 {secs}s"
                        + ("（真实节奏分段）" if STUDY_REALTIME else "（累计值，单次）"))
            except Exception as e:                    # noqa: BLE001
                ok = False
                log(f"    RID {rid} 上报失败：{e}")
            if ok:
                sent += 1
                marked_keys.append(key)
                self._mark("watchtime", key)

        after = self._study_progress()
        if sent and after == before:
            # 计数可能异步刷新，等几秒再看，别立刻下结论
            for _ in range(2):
                time.sleep(3)
                after = self._study_progress()
                if after != before:
                    break
        if sent and after == before and marked_keys:
            # 服务端接受了请求但已读计数没动：回滚断点，下次可以换参数重试
            for k in marked_keys:
                self._unmark("watchtime", k)
            log("  本次写入的学习断点已回滚（服务端未记入已读），下次会重新上报")
        self._report_study_delta(before, after, sent)

    def _report_study_delta(self, before: Dict[str, tuple],
                            after: Dict[str, tuple], sent: int) -> None:
        """上报后复核 getStudyinfo 的已读计数，别只信 Flag=1。"""
        log(f"已上报 {sent} 条资料的观看时长")
        if self.dry:
            log("（演练模式，未真正上报）")
            return
        if sent == 0:
            log("  本次没有实际上报任何资料，无法判断已读计数变化")
            log("学习阶段完成")
            return
        if not after:
            log("  ⚠ 复核失败：拿不到 getStudyinfo（登录可能已失效）")
            log("学习阶段完成")
            return
        moved = []
        for kid, a in sorted(after.items()):
            b = before.get(kid)
            if b and a != b:
                moved.append((kid, b, a))
        if moved:
            for kid, b, a in moved:
                log(f"  ✔ 类别 {kid} 已读计数变化：必修 {b[2]}→{a[2]}，选修 {b[3]}→{a[3]}")
        else:
            tot_m = sum((v[2] or 0) for v in after.values())
            tot_e = sum((v[3] or 0) for v in after.values())
            log(f"  ⚠ getStudyinfo 已读计数没有变化（当前合计：必修 {tot_m}，选修 {tot_e}）")
            log("    已上报累计播放秒数；若仍不增加，说明平台还校验了"
                "真实播放时长/视频进度，或这些资料不计入该账号的已读统计。")
            log("    下一步：先确认视频真实时长（日志里的 [字段] 全部字段 / 视频文件），"
                "再设 AQZRUI_STUDY_REALTIME=1 按真实节奏分段重试；"
                f"或用 AQZRUI_STUDY_SECONDS 指定时长（当前 {STUDY_FALLBACK_SECONDS}）。")
        log("学习阶段完成")

    # --- 3) 练习 ---
    def _practice_one(self, kind_id: str, paper_set_id: str) -> Optional[str]:
        key = f"{kind_id}:{paper_set_id}"
        if self._done("done_practice", key):
            log(f"  [skip] 练习已做 {key}")
            return "done"
        r = self.c.self_one_paper(kind_id, paper_set_id)
        if needs_login(r):
            return None
        flag, msg = flagged(r)
        if flag != "1":
            log(f"  取卷失败 kind={kind_id} set={paper_set_id}: [{flag}] {msg}")
            return "fail"
        first: Dict[str, Any] = {}
        d = r.get("data")
        if isinstance(d, list) and d and isinstance(d[0], dict):
            first = d[0]
        # 前端 model_TwoGradePage_practice.html: `Data[0].PaperID`
        # （早期版本只读 KSID/ID，导致所有类别都报“未取到练习卷 ID”）
        paper_id = str(first.get("PaperID") or first.get("KSID")
                       or first.get("ID") or "").strip()
        if not paper_id:
            log(f"  未取到练习卷 ID（kind={kind_id} set={paper_set_id}）"
                f" data={str(d)[:160]}")
            return "fail"

        exam_name = str(first.get("ExamName") or "")
        pass_count = str(first.get("PassCount") or "") if str(paper_set_id) != "0" else ""

        # 前端 selfPractice() 取题时传的是 SecurityKindID，不是 PaperID；
        # PaperID 只在交卷 tjtm 的 ksid 参数里用。
        pq = extract_questions(self.c.practice_questions(
            kind_id, "lx", str_top=pass_count))
        if pq:                       # 练习题接口自带参考答案，顺手回填
            self.bank.ingest(pq, "practice")
            self.bank.save()
        log(f"  练习卷 {paper_id}{('（' + exam_name + '）') if exam_name else ''}: {len(pq)} 题")
        if self.dry:
            log(f"  [dry] 不提交 kind={kind_id} set={paper_set_id}")
            return "dry"

        okc = 0
        for q in pq:
            if self._stopped():
                log("  已请求停止，中止本卷提交")
                break
            qid = str(q.get("ID") or "").strip()
            if not qid:
                continue
            ans = self.bank.lookup(q) or norm_answer(str(q.get("TxCode") or ""), q.get("AnSwer"))
            if not ans:
                self.missing.append(q)
                continue
            resp = self.c.submit_practice_answer(qid, paper_id, ans, paper_set_id)
            flag2, msg2 = flagged(resp)
            if flag2 == "1":
                okc += 1
                dd = resp.get("data")
                if isinstance(dd, list) and dd and isinstance(dd[0], dict):
                    tc = str(q.get("TxCode") or "")
                    ref = norm_answer(tc, dd[0].get("answer"))
                    if ref:
                        self.bank.add(BankEntry(qid, tc, ref, str(q.get("Caption") or ""), "practice"))
            elif needs_login(resp):
                return None
            else:
                log(f"    题 {qid} 提交失败: [{flag2}] {msg2}")
        self.bank.save()
        self._mark("done_practice", key)
        log(f"  练习完成 {key}: 提交 {okc}/{len(pq)}")
        return "ok"

    def run_practice(self, paper_sets: Optional[List[Tuple[str, str]]] = None) -> None:
        log("== 练习：取卷 -> 取题 -> 逐题提交 ==")
        jobs: List[Tuple[str, str]] = list(paper_sets or [])
        jobs += [(str(k.get("ID")), "0") for k in self.all_kinds()]
        seen = set()
        for kind_id, set_id in jobs:
            if self._stopped():
                log("已请求停止，中止练习阶段")
                return
            if (kind_id, set_id) in seen:
                continue
            seen.add((kind_id, set_id))
            if self._practice_one(kind_id, set_id) is None:
                log(LOGIN_HINT)
                return
        log("练习阶段完成")

    # --- 4) 考试 ---
    @staticmethod
    def _guess(tx_code: str) -> str:
        return {"A": "A", "B": "A", "C": "Y"}.get((tx_code or "").upper(), "A")

    def _answer_for(self, q: Dict[str, Any], section: str) -> Tuple[str, str]:
        """
        取一道题的答案，返回 (答案, 来源)。
        来源：bank=本地答案库 | ai=AI 兜底 | guess=本地猜测。
        """
        ans = self.bank.lookup(q)
        if ans:
            self._ai_stats["bank"] += 1
            return ans, "bank"
        if self.ai is not None:
            try:
                ans = self.ai.answer(q, section)
            except Exception as e:                    # noqa: BLE001
                log(f"  AI 兜底异常：{type(e).__name__}: {e}")
                ans = None
            if ans:
                self._ai_stats["ai"] += 1
                return ans, "ai"
        self._ai_stats["guess"] += 1
        return self._guess(section), "guess"

    def _submit_node(self, ks_id: str, labs: Dict[str, List[dict]], cfg: Dict[str, Any],
                     eid: str = "") -> Any:
        self._ai_stats = {"bank": 0, "ai": 0, "guess": 0}

        def build(section: str, field: str) -> str:
            parts = []
            for q in labs.get(section, []):
                ans, _src = self._answer_for(q, section)
                parts.append(f"{q.get('ID')}-{ans}")
            return f"{cfg.get(field, '0')}@" + ",".join(parts)

        radio = build("A", "RadioScore")
        check = build("B", "CheckScore")
        judge = build("C", "YorNScore")
        st = self._ai_stats
        log(f"  考试 {eid} 答案来源：题库 {st['bank']} / AI {st['ai']} / 猜测 {st['guess']}")
        if self.ai is not None:
            ast = self.ai.stats()
            log(f"  AI 统计：调用 {ast['calls']} 次，缓存命中 {ast['cache_hits']}，"
                f"失败 {ast['failures']}，tokens in/out {ast['tokens_in']}/{ast['tokens_out']}")
        return self.c.post_js("wxInterface/AQKS.asmx/PaperSubmit", {
            "PaperID": ks_id,
            "radio": radio,
            "check": check,
            "judge": judge,
            "type": 0,
            "RadioGroup": str(cfg.get("RadioGroup", "0")),
        })

    def _verify_exam_result(self, eid: str, exam: Dict[str, Any],
                            attempts: int = 3, wait: float = 2.0
                            ) -> Tuple[Optional[bool], Any, Optional[float], Any]:
        """
        交卷后回查考试列表，读 SCORE / PASSSCORE / ISPASS。
        返回 (是否通过 | None 未知, 得分, 通过线, ISPASS 原值)。
        """
        pass_score = num_or_none(exam.get("PASSSCORE"))
        last_score: Any = exam.get("SCORE")
        last_ispass: Any = exam.get("ISPASS")
        for i in range(max(1, attempts)):
            try:
                lst = self.c.exam_papers(1, 50)
            except Exception as e:                    # noqa: BLE001
                log(f"    成绩查询失败：{e}")
            else:
                row = next((x for x in rows_of(lst) if str(x.get("ID")) == eid), None)
                if row:
                    last_score = row.get("SCORE")
                    last_ispass = row.get("ISPASS")
                    ps = num_or_none(row.get("PASSSCORE"))
                    if ps is not None:
                        pass_score = ps
                    state = pass_state(last_ispass)
                    if state is None and last_score not in (None, "") and pass_score is not None:
                        sc = num_or_none(last_score)
                        if sc is not None:
                            state = sc >= pass_score
                    if state is not None:
                        return state, last_score, pass_score, last_ispass
                else:
                    # 有些账号通过后该场考试会从“可参加”列表消失，改查考试记录。
                    try:
                        srows = rows_of(self.c.exam_scores(1, 50))
                    except Exception:                 # noqa: BLE001
                        srows = []
                    want = str(exam.get("EXAMNAME") or exam.get("ExamName") or "")
                    srow = next((x for x in srows if str(x.get("EXAMNAME")) == want), None)
                    if srow:
                        last_score = srow.get("SCORE")
                        sc = num_or_none(last_score)
                        if sc is not None and pass_score is not None:
                            return sc >= pass_score, last_score, pass_score, None
            if i + 1 < attempts:
                time.sleep(wait)
        return None, last_score, pass_score, last_ispass

    def _exam_one(self, exam: Dict[str, Any]) -> Optional[str]:
        eid = str(exam.get("ID") or "").strip()
        name = exam.get("EXAMNAME") or exam.get("ExamName") or eid
        if not eid:
            return "skip"

        state = pass_state(exam.get("ISPASS"))
        if state is True:
            if not self._done("done_exam", eid):
                self._mark("done_exam", eid)
            log(f"  [skip] 考试已通过 {eid} {name}"
                f"（得分 {exam.get('SCORE')} / 通过线 {exam.get('PASSSCORE')}）")
            return "done"
        if self._done("done_exam", eid):
            if state is False:
                # 旧版本在“交卷成功”时就写 done_exam，未通过也会被跳过。
                self._unmark("done_exam", eid)
                log(f"  [retake] 考试 {eid} {name} 记录为未通过"
                    f"（得分 {exam.get('SCORE')} / 通过线 {exam.get('PASSSCORE')}），解除完成标记")
            else:
                log(f"  [skip] 考试已完成 {eid} {name}（通过状态未知，保留标记）")
                return "done"

        if needs_login(self.c.exam_queue(eid, "add")):      # 入场排队
            return None

        paper = self.c.get_one_paper(eid)
        if needs_login(paper):
            return None
        flag, msg = flagged(paper)
        if flag == "2":                                    # 需先完成练习
            d = paper.get("data")
            two = str(d[0].get("TwoCode") or "") if isinstance(d, list) and d and isinstance(d[0], dict) else ""
            log(f"  考试 {eid} 需要先做练习（SecurityKindID={two}）")
            if two:
                self._practice_one(two, eid)
                paper = self.c.get_one_paper(eid)
                flag, msg = flagged(paper)
        if flag == "3":
            log(f"  考试 {eid} 需要先学资料：{msg}")
            self.c.exam_queue(eid, "del")
            return "fail"
        if flag != "1":
            log(f"  取卷失败 {eid}: [{flag}] {msg}")
            self.c.exam_queue(eid, "del")
            return "fail"

        cfg = paper["data"][0]
        ks_id = str(cfg.get("KSID") or "").strip()
        two_code = str(cfg.get("TwoCode") or "0")
        if str(cfg.get("RegularType")) == "3":
            two_code = "0" + "".join(f",{p.get('SecurityKindID')}"
                                     for p in (cfg.get("PercentList") or []) if isinstance(p, dict))
        tx_count = f"A-{cfg.get('RadioCount', 0)}|B-{cfg.get('CheckCount', 0)}|C-{cfg.get('YorNCount', 0)}"

        labs_resp = self.c.post_js("wxInterface/AQKS.asmx/viewExamLab", {
            "setID": cfg.get("ID"), "KSID": ks_id, "RegularType": cfg.get("RegularType"),
            "txCount": tx_count, "TwoCode": two_code,
        })
        if needs_login(labs_resp):
            return None
        lflag, lmsg = flagged(labs_resp)
        if lflag != "1":
            log(f"  取题失败 {eid}: [{lflag}] {lmsg}")
            self.c.exam_queue(eid, "del")
            return "fail"

        labs: Dict[str, List[dict]] = {"A": [], "B": [], "C": []}
        d = labs_resp.get("data")
        if isinstance(d, list) and d and isinstance(d[0], dict):
            for k in labs:
                v = d[0].get(k)
                if isinstance(v, list):
                    labs[k] = [x for x in v if isinstance(x, dict)]
        n_all = sum(len(v) for v in labs.values())
        hit = sum(1 for k in labs for q in labs[k] if self.bank.lookup(q))
        for k in labs:
            for q in labs[k]:
                if not self.bank.lookup(q):
                    self.missing.append(q)
        log(f"  考试 {eid} {name}: 取题 {n_all}（命中 {hit}，需猜 {n_all - hit}）")

        if self.dry:
            log(f"  [dry] 不交卷 {eid}")
            self.c.exam_queue(eid, "del")
            return "dry"

        r = self._submit_node(ks_id, labs, cfg, eid)
        if needs_login(r):
            return None
        sflag, smsg = flagged(r)
        log(f"  交卷 {eid}: [{sflag}] {smsg or '已提交'}")
        self.c.exam_queue(eid, "del")
        if sflag != "1":
            return "fail"

        # ReturnFlag=1 只代表“服务端收卷”，不等于及格；回查成绩再决定是否标记完成。
        passed, score, pass_score, ispass = self._verify_exam_result(eid, exam)
        line = f"通过线 {pass_score:g}" if pass_score is not None else "通过线未知"
        if passed is True:
            log(f"  成绩 {eid}: {score} 分（{line}，ISPASS={ispass}）✔ 已通过")
            self._mark("done_exam", eid)
            return "ok"
        if passed is False:
            log(f"  成绩 {eid}: {score} 分（{line}，ISPASS={ispass}）✗ 未通过，"
                "不标记完成，重跑可重考")
            return "fail"
        log(f"  已交卷 {eid}，成绩尚未刷新（{line}，ISPASS={ispass}），本次不标记完成")
        return "submitted"

    def run_exam(self) -> Optional[List[Tuple[str, str]]]:
        log("== 考试：列表 -> 排队 -> 取卷 -> 取题 -> 交卷 ==")
        lst = self.c.exam_papers(1, 50)
        if needs_login(lst):
            log(LOGIN_HINT)
            return None
        exams = rows_of(lst)
        if not exams:
            # 空列表可能真的是“没有考试”，也可能是未登录；用 IsLogin 明确区分。
            try:
                st = self.c.is_login()
            except Exception:                         # noqa: BLE001
                st = {}
            if not SpeedClient.logged_in(st):
                log(LOGIN_HINT)
                return None
            log("考试列表为空（该账号暂无可参加的考试）")
            return []
        log(f"可参加考试 {len(exams)} 场")
        for e in exams:
            log(f"  {e.get('ID')} {e.get('EXAMNAME', '')}: 得分={e.get('SCORE', '-')} "
                f"通过线={e.get('PASSSCORE', '-')} 是否通过={e.get('ISPASS', '-')}")
        practice_jobs: List[Tuple[str, str]] = []
        for i, e in enumerate(exams):
            if self._stopped():
                log("已请求停止，中止考试阶段")
                break
            if self.max_exams is not None and i >= self.max_exams:
                break
            two = str(e.get("TWOCODE") or "")
            if two:
                practice_jobs.append((two, str(e.get("ID"))))
            if self._exam_one(e) is None:
                log(LOGIN_HINT)
                return practice_jobs
        return practice_jobs

    # --- 总控 ---
    def run_all(self) -> None:
        self.harvest_bank()
        self.run_study()
        self.run_practice()
        jobs = self.run_exam()
        if jobs:
            self.run_practice(paper_sets=jobs)
        log(f"全部完成。答案库 {len(self.bank.by_id)} 条；未命中题 {len(self.missing)} 条")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
# 全局参数规格：同时挂到主 parser 与各子命令，两个位置都能写
COMMON_SPECS = [
    (("--base",), {"default": os.environ.get("AQZRUI_BASE", BASE)}),
    (("--session",), {"default": os.environ.get("AQZRUI_SESSION")}),
    (("--cookie",), {"default": os.environ.get("AQZRUI_COOKIE")}),
    (("--token",), {"default": os.environ.get("AQZRUI_TOKEN")}),
    (("--delay",), {"type": float, "default": 0.5}),
    (("--timeout",), {"type": int, "default": 25}),
    (("--allow-host",), {"action": "append", "default": []}),
    (("--state-dir",), {"default": DEFAULT_STATE_DIR}),
    (("--dry-run",), {"action": "store_true"}),
    (("--kinds",), {"default": ""}),
    (("--max-exams",), {"type": int, "default": None}),
    (("--ai",), {"action": "store_true",
                 "help": "考试答案库未命中时启用 AI 兜底（需 AQZRUI_AI_KEY）"}),
    (("--ai-base",), {"default": os.environ.get("AQZRUI_AI_BASE", ""),
                      "help": "OpenAI 兼容接口 base，如 https://api.openai.com/v1"}),
    (("--ai-model",), {"default": os.environ.get("AQZRUI_AI_MODEL", ""),
                       "help": "模型名，如 gpt-4o-mini / deepseek-chat"}),
    (("--ai-max-calls",), {"type": int, "default": None,
                           "help": "本次运行 AI 调用上限（默认 30）"}),
]


def _add_common(ap: argparse.ArgumentParser, suppress: bool = False) -> None:
    """
    把全局参数挂到 parser 上。
    suppress=True（用于子命令）：缺省不写入命名空间，从而保留主 parser 已解析的值，
    使得 `script.py --dry-run exam` 与 `script.py exam --dry-run` 都能工作。
    """
    for flags, kw in COMMON_SPECS:
        k = dict(kw)
        if suppress:
            k["default"] = argparse.SUPPRESS
        ap.add_argument(*flags, **k)


def cmd_login(args) -> int:
    """
    账号密码登录。CAS 账号走统一认证 + 二次认证；本地账号加 --local。
    成功后打印可直接粘贴的 `set AQZRUI_SESSION=...`。
    """
    import getpass
    import aqzrui_login as L

    user = args.user or os.environ.get("AQZRUI_USER") or input("学号/工号: ").strip()
    pwd = (args.password or os.environ.get("AQZRUI_PASSWORD")
           or getpass.getpass("密码（不回显）: "))
    if not user or not pwd:
        log("缺少账号或密码")
        return 2

    if args.local:
        res = L.platform_local_login(user, pwd, log=log)
    else:
        cas = L.CasLogin(user, pwd, log=log)
        res = cas.step_main()
        if res.need_2fa:
            names = ", ".join(f"{c}={L.REAUTH_NAMES.get(c, c)}" for c in res.channels)
            log(f"二次认证可选方式：{names}")
            ok, msg = cas.step_request_code(args.channel)
            log(f"验证码：{msg}")
            if not ok and getattr(cas, "last_code_wait", 0):
                log(f"   冷却中，{cas.last_code_wait}s 后可以重发；仍可尝试已收到的旧码")

            # 交互模式允许重试 3 次；--code 提供时只提交一次
            attempts = 1 if args.code else 3
            for i in range(attempts):
                code = (args.code or input("请输入收到的验证码: ")).strip()
                if not code:
                    log("未输入验证码，中止")
                    return 1
                res = cas.step_submit_2fa(code)
                if not res.need_2fa:
                    break
                if res.stage == "reauth_expired":
                    log(f"✗ {res.msg}；需要重新执行 login 获取新的二次认证会话")
                    return 1
                if i + 1 < attempts:
                    log(f"✗ {res.msg}，请重新输入（剩余 {attempts - i - 1} 次）")
                    args.code = ""            # 第一轮之后走交互输入

    if res.ok and res.cookie:
        log(f"登录成功，平台会话：{res.cookie}")
        print()
        print("把下面这行贴进当前终端即可让后续命令复用登录态：")
        print(f'set AQZRUI_SESSION={res.cookie}')
        print()
        return 0
    log(f"登录未完成[{res.stage}]：{res.msg}")
    return 1


def main() -> int:
    global PATHS
    ap = argparse.ArgumentParser(
        description="CUMT aqzrui 速刷脚本（答案库 -> 学习/练习/考试）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：set AQZRUI_SESSION=xxx && python aqzrui_speedrun.py all --dry-run",
    )
    _add_common(ap)

    common = argparse.ArgumentParser(add_help=False)
    _add_common(common, suppress=True)

    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", parents=[common], help="会话存活 + 账号信息")
    p = sub.add_parser("login", parents=[common], help="账号密码登录（CAS + 二次认证）")
    p.add_argument("--user", default=os.environ.get("AQZRUI_USER"), help="学号/工号")
    p.add_argument("--password", default=os.environ.get("AQZRUI_PASSWORD"),
                   help="密码（建议用环境变量或交互输入，避免留在历史里）")
    p.add_argument("--channel", default="",
                   choices=["", "phone", "email", "dingtalk", "welink", "otp"],
                   help="二次认证取码渠道（空=按页面当前 reAuthType）")
    p.add_argument("--code", default="", help="验证码（非交互时用）")
    p.add_argument("--local", action="store_true", help="改用平台本地账号登录")
    p = sub.add_parser("bank", parents=[common], help="抓答案库")
    p.add_argument("--show", action="store_true")
    p = sub.add_parser("study", parents=[common], help="学习：时长上报")
    p.add_argument("--retry", action="store_true",
                   help="忽略已上报断点，重新上报全部资料（换参数重试时用）")
    sub.add_parser("practice", parents=[common], help="练习：逐题提交")
    sub.add_parser("exam", parents=[common], help="考试：自动答卷交卷")
    sub.add_parser("scores", parents=[common], help="查考试得分 / 通过线 / 是否通过")
    sub.add_parser("all", parents=[common], help="bank + study + practice + exam")

    args = ap.parse_args()

    try:
        PATHS = StateDir(args.state_dir)
    except (OSError, ValueError) as e:
        print(f"state 目录不可用: {e}")
        return 2

    try:
        client = SpeedClient(
            base=args.base,
            session_id=args.session,
            cookie=args.cookie,
            token=args.token,
            delay=args.delay,
            allowed_hosts=set(args.allow_host) | DEFAULT_ALLOWED_HOSTS,
            timeout=args.timeout,
        )
    except ValueError as e:
        log(f"目标校验失败：{e}")
        return 2

    bank = AnswerBank.load()

    if args.cmd == "check":
        try:
            st = client.is_login()
        except Exception as e:                        # noqa: BLE001
            log(f"无法连接平台：{e}")
            log("（服务端当前不可用或网络不通；该平台偶尔整段 API 超时，稍后重试即可）")
            return 3
        ok = SpeedClient.logged_in(st)
        print(json.dumps(st, ensure_ascii=False, indent=2))
        log(f"登录态：{'有效' if ok else '无效'}")
        if not ok:
            log(LOGIN_HINT)
        log(f"答案库：{bank.stats()}")
        return 0 if ok else 1

    if args.cmd == "login":
        return cmd_login(args)

    if args.cmd == "scores":
        try:
            lst = client.exam_papers(1, 50)
        except Exception as e:                        # noqa: BLE001
            log(f"查成绩失败：{e}")
            return 3
        if needs_login(lst):
            log(LOGIN_HINT)
            return 1
        rows = rows_of(lst)
        if not rows:
            try:
                st = client.is_login()
            except Exception:                         # noqa: BLE001
                st = {}
            if not SpeedClient.logged_in(st):
                log(LOGIN_HINT)
                return 1
            log("没有考试记录（该账号暂无可参加的考试）")
            return 0
        for e in rows:
            state = pass_state(e.get("ISPASS"))
            mark = "✔ 通过" if state is True else ("✗ 未通过" if state is False else "? 未知")
            log(f"{e.get('ID')} {e.get('EXAMNAME', '')}: "
                f"得分={e.get('SCORE', '-')} 通过线={e.get('PASSSCORE', '-')} {mark}")
        return 0

    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]

    ai = None
    if getattr(args, "ai", False):
        try:
            from ai_fallback import AIAnswerer
            ai = AIAnswerer.from_env(
                log=log,
                base=args.ai_base or None,
                model=args.ai_model or None,
                max_calls=args.ai_max_calls,
                cache=_read_json("ai_answers.json", {}),
                on_save=lambda d: _write_json("ai_answers.json", d),
            )
        except Exception as e:                        # noqa: BLE001
            log(f"AI 兜底初始化失败：{e}")
        if ai is None:
            log("AI 兜底已启用，但未找到 API Key"
                "（AQZRUI_AI_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY）")

    sr = SpeedRun(client, bank, args.dry_run, kinds, args.max_exams,
                  ai_answerer=ai, retry_study=getattr(args, "retry", False))

    if args.cmd == "bank":
        try:
            stats = sr.harvest_bank()
        except Exception as e:                        # noqa: BLE001
            log(f"抓库失败（服务端不可用或网络不通）：{e}")
            return 3
        if args.show:
            print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0
    # 各阶段内部已尽量自我容错；这里兜底，保证不把 traceback 甩给用户
    try:
        if args.cmd == "study":
            sr.run_study()
        elif args.cmd == "practice":
            sr.run_practice()
        elif args.cmd == "exam":
            sr.run_exam()
        elif args.cmd == "all":
            sr.run_all()
    except Exception as e:                            # noqa: BLE001
        log(f"阶段执行中断：{type(e).__name__}: {e}")
        log("（多为服务端临时不可用；稍后重试，或用 --delay 放慢节奏）")
        return 3

    if sr.missing:
        _write_json("missing_questions.json", sr.missing)
        log(f"未命中题清单：{PATHS.path('missing_questions.json')}")
    log(f"答案库总量 {len(bank.by_id)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
