#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
labsafe.cumt.edu.cn（实验室安全学习平台 · JeecgBoot）客户端
============================================================

页面与接口均从线上前端包逐字还原：

    http://labsafe.cumt.edu.cn/students/courseDetail?graphId=<G>&id=<C>

    GET  /jcedutec/courseSource/getById?id=<C>&graphId=<G>
         -> {result:{url,name,remark,q1,anum,unCorrect}}
    GET  /jcedutec/courseSource/queryCourseQuestionRelaByMainId?id=<C>&graphId=<G>
         -> 视频弹题列表（ejectTime/stem/optiona..d/kind）
    GET  /jcedutec/courseSource/myCourseTypeTree?id=<C>&graphId=<G>
    POST /jcedutec/courseSource/updateVisits    {id,graphId}
    POST /jcedutec/courseSource/finishRate      {id,graphId,watchDuration}
    POST /jcedutec/courseSource/finish          {id,graphId}
    POST /jcedutec/courseSource/submitAnswer    {id,graphId,questionId,option}

鉴权链：
    1. CAS 登录 service=http://labsafe.cumt.edu.cn/（复用 aqzrui_login 的
       密码加密与二次认证）
    2. GET /jeecg-boot/sys/cas/client/validateLogin?ticket=...&service=...
       -> result.token
    3. 其余接口带 X-Access-Token: <token>

所有接口还带 JeecgBoot 的请求签名：
    X-Sign      = MD5(JSON.stringify(sorted_merge(query, body)) + SECRET).upper()
    X-TIMESTAMP = 毫秒时间戳
签名算法取自前端 `a88b` 模块（SECRET 见常量），GET 的签名在追加 `_t` 之前计算。
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests

from aqzrui_login import CasLogin, LoginError, LoginResult, UA
from aqzrui_speedrun import validate_target

LABSAFE_BASE = "http://labsafe.cumt.edu.cn/"
LABSAFE_HOST = "labsafe.cumt.edu.cn"
API_BASE = "http://202.119.198.73:8080/jeecg-boot"
API_HOST = "202.119.198.73"
CAS_BASE = "https://authserver.cumt.edu.cn/authserver"

# 前端 a88b 模块里的固定盐
SIGN_SECRET = "dd05f1c54d63749eda95f9fa6d49v442a"

ALLOWED_HOSTS = {LABSAFE_HOST, API_HOST, "authserver.cumt.edu.cn"}

COURSE_STATIC = "/jcedutec/courseSource/static/"


class LabsafeError(Exception):
    pass


class LabsafeAuthError(LabsafeError):
    """token 失效 / 未登录。"""


def _norm(v: Any) -> Any:
    """对齐前端 mergeObject：number 转字符串，其余类型原样参与 JSON 序列化。"""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return str(v)
    return v


def sign_params(params: Optional[Dict[str, Any]]) -> str:
    data = {k: _norm(v) for k, v in (params or {}).items()}
    ordered = {k: data[k] for k in sorted(data)}
    raw = json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)
    return hashlib.md5((raw + SIGN_SECRET).encode("utf-8")).hexdigest().upper()


class LabsafeClient:
    """带签名与 token 的 JeecgBoot 客户端。"""

    def __init__(self, token: str = "", timeout: int = 25,
                 log: Callable[[str], None] = print) -> None:
        self.token = (token or "").strip()
        self.timeout = timeout
        self.log = log
        self.user: Dict[str, Any] = {}
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
        })

    # ---------------- 底层 ----------------
    def _headers(self, params: Optional[Dict[str, Any]]) -> Dict[str, str]:
        h = {
            "X-Sign": sign_params(params),
            "X-TIMESTAMP": str(int(time.time() * 1000)),
            "tenant-id": "0",
            "Content-Type": "application/json;charset=UTF-8",
        }
        if self.token:
            h["X-Access-Token"] = self.token
        return h

    @staticmethod
    def _parse(r: requests.Response) -> Dict[str, Any]:
        try:
            j = r.json()
        except ValueError as e:
            raise LabsafeError(
                f"HTTP {r.status_code} 返回非 JSON：{r.text[:200]}") from e
        if not isinstance(j, dict):
            raise LabsafeError(f"返回结构异常：{str(j)[:200]}")
        j["_http_status"] = r.status_code
        return j

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = dict(params or {})
        headers = self._headers(params)          # 签名在加 _t 之前算（同前端拦截器）
        query = dict(params)
        query["_t"] = str(int(time.time()))
        url = path if path.startswith("http") else API_BASE + path
        validate_target(url, allowed_hosts=ALLOWED_HOSTS)
        r = self.s.get(url, params=query, headers=headers, timeout=self.timeout)
        return self._parse(r)

    def _post(self, path: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = dict(data or {})
        url = path if path.startswith("http") else API_BASE + path
        validate_target(url, allowed_hosts=ALLOWED_HOSTS)
        r = self.s.post(url, json=data, headers=self._headers(data), timeout=self.timeout)
        return self._parse(r)

    @staticmethod
    def _ok(j: Dict[str, Any], what: str) -> Dict[str, Any]:
        # JeecgBoot 的 token 失效响应是 success:true + code:401 + "Token失效"，
        # 不能只看 success，否则会把 401 当成成功。
        code = j.get("code")
        msg = str(j.get("message") or j.get("msg") or "")
        if code in (401, "401") or "Token失效" in msg:
            raise LabsafeAuthError(f"{what}失败：{msg or 'Token 失效，请重新登录'}")
        if j.get("success") is not True:
            raise LabsafeError(f"{what}失败：{msg or j}")
        return j

    # ---------------- 登录 ----------------
    def validate_login(self, ticket: str, service: str = LABSAFE_BASE) -> "LabsafeClient":
        j = self._get("/sys/cas/client/validateLogin",
                      {"ticket": ticket, "service": service})
        if not j.get("success"):
            raise LabsafeError(j.get("message") or "CAS 票据校验失败")
        result = j.get("result") or {}
        token = str(result.get("token") or "")
        if not token:
            raise LabsafeError(f"票据校验成功但未返回 token：{str(result)[:200]}")
        self.token = token
        self.user = result.get("userInfo") or {}
        return self

    # ---------------- 诊断用原始请求 ----------------
    def raw_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """签名 GET，原样返回响应体（不因 success=false 抛错，只拦 401）。

        诊断脚本需要看到服务端真实的 success/code/message，因此不能复用 _ok()。
        """
        j = self._get(path, params or {})
        code = j.get("code")
        msg = str(j.get("message") or j.get("msg") or "")
        if code in (401, "401") or "Token失效" in msg:
            raise LabsafeAuthError(f"Token 失效，请重新登录：{msg}")
        return j

    # ---------------- 课程接口 ----------------
    def course_detail(self, course_id: str, graph_id: str) -> Dict[str, Any]:
        j = self._get("/jcedutec/courseSource/getById",
                      {"id": course_id, "graphId": graph_id})
        return self._ok(j, "读取课程")

    def course_tree(self, course_id: str, graph_id: str) -> Dict[str, Any]:
        j = self._get("/jcedutec/courseSource/myCourseTypeTree",
                      {"id": course_id, "graphId": graph_id})
        return self._ok(j, "读取课程树")

    def course_questions(self, course_id: str, graph_id: str) -> Dict[str, Any]:
        j = self._get("/jcedutec/courseSource/queryCourseQuestionRelaByMainId",
                      {"id": course_id, "graphId": graph_id})
        return self._ok(j, "读取弹题")

    def update_visits(self, course_id: str, graph_id: str) -> Dict[str, Any]:
        return self._ok(self._post("/jcedutec/courseSource/updateVisits",
                                   {"id": course_id, "graphId": graph_id}), "updateVisits")

    def finish_rate(self, course_id: str, graph_id: str, seconds: int) -> Dict[str, Any]:
        # 前端 a88b.mergeObject 会把数字统一 toString 后再 JSON.stringify，
        # 所以线上真实请求体是 {"watchDuration":"10"}，这里保持一致。
        return self._ok(self._post("/jcedutec/courseSource/finishRate",
                                   {"id": course_id, "graphId": graph_id,
                                    "watchDuration": str(int(seconds))}),
                        f"finishRate({seconds})")

    def finish(self, course_id: str, graph_id: str) -> Dict[str, Any]:
        return self._ok(self._post("/jcedutec/courseSource/finish",
                                   {"id": course_id, "graphId": graph_id}), "finish")

    def submit_answer(self, course_id: str, graph_id: str,
                      question_id: str, option: Any) -> Dict[str, Any]:
        return self._ok(self._post("/jcedutec/courseSource/submitAnswer",
                                   {"id": course_id, "graphId": graph_id,
                                    "questionId": question_id, "option": option}),
                        "submitAnswer")

    # ---------------- 业务封装 ----------------
    @staticmethod
    def video_url(relative: str) -> str:
        return API_BASE + COURSE_STATIC + (relative or "").lstrip("/")

    def watch(self, course_id: str, graph_id: str, duration: int,
              step: int = 10, interval: float = 0.4,
              on_report: Optional[Callable[[int, Dict[str, Any]], None]] = None) -> Dict[str, Any]:
        """
        按 watchDuration 推进并最终 finish。
        duration 是视频总时长（秒）；step 是每次上报的秒数增量。
        服务端是否校验真实播放时长未在真实会话上验证，先以小步长 + 间隔上报。
        """
        if duration <= 0:
            raise LabsafeError("duration 必须大于 0")
        self.update_visits(course_id, graph_id)
        reports = 0
        for t in range(step, int(duration) + 1, step):
            j = self.finish_rate(course_id, graph_id, t)
            if not j.get("success"):
                raise LabsafeError(f"finishRate({t}) 失败：{j.get('message') or j}")
            reports += 1
            if on_report:
                on_report(t, j)
            time.sleep(interval)
        return {"reports": reports, "finish": self.finish(course_id, graph_id)}


# ---------------------------------------------------------------------------
# CAS 登录 -> JeecgBoot token
# ---------------------------------------------------------------------------
def login(username: str, password: str,
          code_provider: Optional[Callable[[LoginResult], str]] = None,
          timeout: int = 25,
          log: Callable[[str], None] = print) -> tuple[LoginResult, Optional[LabsafeClient]]:
    """
    走 CAS（含二次认证）拿票据，再用 validateLogin 换 JeecgBoot token。
    返回 (LoginResult, client)；需要二次认证且未提供 code_provider 时 client 为 None。
    """
    holder: Dict[str, LabsafeClient] = {}

    def finish(cas: CasLogin) -> LoginResult:
        try:
            r = cas._get(f"{CAS_BASE}/login", params={"service": LABSAFE_BASE})
        except requests.RequestException as e:
            return LoginResult(False, "failed", f"换取 CAS 票据失败：{e}")

        ticket = parse_qs(urlparse(r.url or "").query).get("ticket", [""])[0]
        if not ticket:
            return LoginResult(
                False, "failed",
                f"CAS 未返回 ticket（最终地址 {r.url or '?'}，HTTP {r.status_code}）")
        client = LabsafeClient(timeout=timeout, log=log)
        try:
            client.validate_login(ticket, LABSAFE_BASE)
        except LabsafeError as e:
            return LoginResult(False, "failed", f"票据校验失败：{e}")
        holder["client"] = client
        who = (client.user.get("realname") or client.user.get("username")
               or client.user.get("userName") or "")
        return LoginResult(True, "done", f"登录成功 {who}".strip(), cookie=client.token)

    cas = CasLogin(username, password, service=LABSAFE_BASE, timeout=timeout,
                   log=log, finish_handler=finish)
    res = cas.step_main()
    if res.need_2fa:
        if code_provider is None:
            return res, None
        ok, msg = cas.step_request_code()
        log(f"验证码：{msg}")
        if not ok and getattr(cas, "last_code_wait", 0):
            log(f"   冷却中，{cas.last_code_wait}s 后可重发")
        code = (code_provider(res) or "").strip()
        if not code:
            return LoginResult(False, "failed", "未提供验证码"), None
        res = cas.step_submit_2fa(code)
    return res, holder.get("client")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _client_from_args(args) -> LabsafeClient:
    token = (args.token or os.environ.get("LABSAFE_TOKEN") or "").strip()
    if not token:
        raise LabsafeError("缺少 token：用 --token 或环境变量 LABSAFE_TOKEN")
    return LabsafeClient(token=token, timeout=args.timeout)


def cmd_login(args) -> int:
    user = args.user or os.environ.get("LABSAFE_USER") or os.environ.get("AQZRUI_USER") or ""
    pwd = (args.password or os.environ.get("LABSAFE_PASSWORD")
           or os.environ.get("AQZRUI_PASSWORD") or "")
    if not user:
        user = input("学号/工号: ").strip()
    if not pwd:
        pwd = getpass.getpass("密码（不回显）: ")
    if not user or not pwd:
        print("缺少账号或密码")
        return 2

    def provider(_res: LoginResult) -> str:
        return args.code or input("请输入收到的验证码: ").strip()

    res, client = login(user, pwd, code_provider=provider, timeout=args.timeout)
    if res.ok and client:
        print(f"✔ {res.msg}")
        print(f"LABSAFE_TOKEN={client.token}")
        return 0
    print(f"✗ 登录未完成[{res.stage}]：{res.msg}")
    return 1


def cmd_course(args) -> int:
    c = _client_from_args(args)
    detail = c.course_detail(args.id, args.graph)
    r = detail.get("result") or {}
    print(f"课程：{r.get('name','')}")
    print(f"视频：{c.video_url(r.get('url',''))}")
    print(f"已答：{r.get('q1','')}  总题：{r.get('anum','')}  答错：{r.get('unCorrect','')}")
    print(f"简介：{r.get('remark','')}")
    if args.questions:
        q = c.course_questions(args.id, args.graph).get("result") or []
        print(f"\n弹题 {len(q)} 道：")
        print(json.dumps(q, ensure_ascii=False, indent=2)[:8000])
    if args.tree:
        t = c.course_tree(args.id, args.graph).get("result") or []
        print(f"\n课程树：\n{json.dumps(t, ensure_ascii=False, indent=2)[:8000]}")
    return 0


def cmd_watch(args) -> int:
    c = _client_from_args(args)
    if args.dry_run:
        d = c.course_detail(args.id, args.graph)
        r = d.get("result") or {}
        print(f"[dry-run] {r.get('name','')} -> {c.video_url(r.get('url',''))}")
        print(f"[dry-run] 将上报 watchDuration 10..{args.duration}，步长 {args.step}")
        return 0

    def report(t: int, _j: Dict[str, Any]) -> None:
        print(f"  上报 watchDuration={t}")

    out = c.watch(args.id, args.graph, args.duration,
                  step=args.step, interval=args.interval, on_report=report)
    fin = out["finish"]
    print(f"完成：上报 {out['reports']} 次；finish success={fin.get('success')} {fin.get('message','')}")
    return 0 if fin.get("success") else 1


def cmd_answer(args) -> int:
    c = _client_from_args(args)
    option: Any = args.option
    if args.multi:
        option = [x for x in args.option.split(",") if x]
    j = c.submit_answer(args.id, args.graph, args.question, option)
    print(json.dumps(j, ensure_ascii=False, indent=2))
    return 0 if j.get("success") else 1


def cmd_report(args) -> int:
    c = _client_from_args(args)
    j = c.finish_rate(args.id, args.graph, args.seconds)
    print(json.dumps(j, ensure_ascii=False, indent=2))
    return 0 if j.get("success") else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="labsafe.cumt.edu.cn 课程客户端（CAS 登录 + 进度上报）")
    ap.add_argument("--timeout", type=int, default=25)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", help="CAS 登录并输出 LABSAFE_TOKEN")
    p.add_argument("--user", default="")
    p.add_argument("--password", default="")
    p.add_argument("--code", default="", help="非交互时直接给验证码")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("course", help="读取课程详情 / 弹题 / 课程树")
    p.add_argument("--token", default="")
    p.add_argument("--id", required=True, help="课程 id")
    p.add_argument("--graph", required=True, help="graphId")
    p.add_argument("--questions", action="store_true")
    p.add_argument("--tree", action="store_true")
    p.set_defaults(func=cmd_course)

    p = sub.add_parser("watch", help="上报观看时长并 finish")
    p.add_argument("--token", default="")
    p.add_argument("--id", required=True)
    p.add_argument("--graph", required=True)
    p.add_argument("--duration", type=int, required=True, help="视频总时长（秒）")
    p.add_argument("--step", type=int, default=10)
    p.add_argument("--interval", type=float, default=0.4)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("answer", help="提交单道弹题")
    p.add_argument("--token", default="")
    p.add_argument("--id", required=True)
    p.add_argument("--graph", required=True)
    p.add_argument("--question", required=True, help="questionId")
    p.add_argument("--option", required=True, help="单选 A / 判断 Y|N / 多选 A,B")
    p.add_argument("--multi", action="store_true", help="按逗号拆成数组")
    p.set_defaults(func=cmd_answer)

    p = sub.add_parser("report", help="只上报一次 watchDuration")
    p.add_argument("--token", default="")
    p.add_argument("--id", required=True)
    p.add_argument("--graph", required=True)
    p.add_argument("--seconds", type=int, required=True)
    p.set_defaults(func=cmd_report)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (LabsafeError, LoginError, ValueError) as e:
        print(f"✗ {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
