#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国矿业大学 实验室安全准入平台（aqzrui）接口客户端

覆盖：
  - 在线学习：学习类别、学习题目、学习资料、观看时长上报
  - 考前模拟/练习：练习试卷、练习题、逐题提交
  - 在线考试：考试列表、取卷、取题、交卷、成绩/查卷、考试队列

用法（登录态）：
  set AQZRUI_SESSION=<浏览器 DevTools 里的 ASP.NET_SessionId>
  python aqzrui_api.py kinds
  python aqzrui_api.py papers
  python aqzrui_api.py learn --kind 11
  python aqzrui_api.py practice --kind 11
  python aqzrui_api.py paper --id <试卷ID>
  python aqzrui_api.py scores

也支持：
  python aqzrui_api.py --cookie "ASP.NET_SessionId=xxx" papers
  python aqzrui_api.py --token <localStorage.Token> practice --kind 11

注意：平台 Web 登录走 CAS + 二次认证（短信/邮箱/钉钉/令牌）。
      本客户端不自带绕过；请先在浏览器完成登录，再把会话 Cookie 交给本脚本。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from typing import Any, Dict, Optional

import requests

BASE = "http://dygx.cumt.edu.cn:8080/aqzrui/"
AUTH_HEADER = {"Authorization": "WanXinKey2019"}
DEFAULT_STR_JSON = '{"data":[{}]}'


def _loads_js(text: str) -> Any:
    """解析服务端返回的“类 JSON”：可能键无引号，也可能用单引号字符串。"""
    text = (text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        import ast

        return ast.literal_eval(text)
    except Exception:
        pass
    fixed = re.sub(r'([{,])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:', r'\1"\2":', text)
    return json.loads(fixed)


def _lenient_json(text: str) -> Any:
    try:
        return _loads_js(text)
    except Exception:
        # 非 JSON（例如 IIS 的“访问禁止”HTML），原样返回便于排查
        return {"_raw": (text or "").strip()}


def _unwrap(obj: Any) -> Any:
    """拆 ASMX 的 {"d": "<json string>"} 信封。"""
    if isinstance(obj, dict) and set(obj.keys()) == {"d"}:
        inner = obj["d"]
        return _loads_js(inner) if isinstance(inner, str) else inner
    return obj


class AqzruiClient:
    def __init__(
        self,
        base: str = BASE,
        session_id: Optional[str] = None,
        cookie: Optional[str] = None,
        token: Optional[str] = None,
        timeout: int = 25,
    ) -> None:
        self.base = base.rstrip("/") + "/"
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/155.0.0.0 Safari/537.36"
                ),
                "Referer": self.base,
            }
        )
        if session_id:
            self.s.cookies.set(
                "ASP.NET_SessionId", session_id, domain="dygx.cumt.edu.cn", path="/"
            )
        if cookie:
            for part in cookie.split(";"):
                if "=" in part:
                    k, v = part.strip().split("=", 1)
                    self.s.cookies.set(k, v)
        if token:
            self.s.headers["Token"] = token

    # ---------- 底层请求 ----------

    def post_json(self, path: str, payload: Dict[str, Any], auth: bool = True) -> Any:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if auth:
            headers.update(AUTH_HEADER)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        r = self.s.post(self.base + path, data=body, headers=headers, timeout=self.timeout)
        r.encoding = "utf-8"
        return _unwrap(_lenient_json(r.text))

    def post_form(self, path: str, data: Dict[str, Any], auth: bool = True) -> Any:
        headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
        if auth:
            headers.update(AUTH_HEADER)
        r = self.s.post(self.base + path, data=data, headers=headers, timeout=self.timeout)
        r.encoding = "utf-8"
        return _unwrap(_lenient_json(r.text))

    def paged(self, path: str, page: int = 1, size: int = 20, str_json: str = DEFAULT_STR_JSON) -> Any:
        return self.post_form(
            path,
            {
                "PageIndex": page,
                "PageSize": size,
                "strOrder": "",
                "strJson": str_json,
            },
        )

    # ---------- 在线学习 ----------

    def security_kinds(self, page: int = 1, size: int = 50) -> Any:
        """学习/练习类别列表（匿名可访问，但必须带 Authorization 头）。"""
        return self.paged("wxInterface/index.asmx/getSecurityKind", page, size)

    def security_kinds_by_type(self, type_: str = "1", lx: str = "AQLB") -> Any:
        """按类型取安全类别（AQLB=安全类别）。"""
        return self.post_json(
            "wxInterface/index.asmx/getSecurityKindByType", {"type": type_, "lx": lx}
        )

    def learn_questions(self, kind_id: str, type_: str = "xx") -> Any:
        """在线学习：取某安全类别下的学习题目（type=xx 学习，lx 练习）。"""
        return self.post_json(
            "wxInterface/index.asmx/selfStudy", {"ID": str(kind_id), "Type": type_}
        )

    def learn_questions_new(
        self, kind_id: str, type_: str = "xx", page: int = 1, size: int = 100
    ) -> Any:
        """新版分页取学习题目。"""
        return self.post_json(
            "wxInterface/index.asmx/selfStudyNew",
            {
                "ID": str(kind_id),
                "Type": type_,
                "PageIndex": page,
                "PageSize": size,
            },
        )

    def watch_time(self, study_id: str, times: str) -> Any:
        """学习页心跳：上报观看时长（studyid=资料ID，times=秒）。"""
        return self.post_json(
            "wxInterface/index.asmx/updateWatchtime",
            {"studyid": str(study_id), "times": str(times)},
        )

    def study_docs(
        self,
        page: int = 1,
        size: int = 20,
        security_kind_id: Optional[str] = None,
        doc_kind: str = "0",
        ismust: Optional[str] = None,
        isdown: Optional[str] = None,
        pre_name: Optional[str] = None,
    ) -> Any:
        """学习资料列表。ismust: 1=必修 0=选修；isdown: 1=已下载。"""
        item: Dict[str, str] = {"1": "1", "DocKind": doc_kind}
        if security_kind_id:
            item["SecurityKindID"] = str(security_kind_id)
        if ismust is not None:
            item["MustStudy"] = str(ismust)
        if isdown is not None:
            item["isdown"] = str(isdown)
        if pre_name:
            item["PreName"] = pre_name
        str_json = json.dumps({"data": [item]}, ensure_ascii=False, separators=(",", ":"))
        return self.paged(
            "wxInterface/update/index.asmx/getPreStudyDoc", page, size, str_json
        )

    def study_doc_index(self, top: int = 10) -> Any:
        """首页学习资料（视频/文档）列表。"""
        return self.post_json(
            "wxInterface/index.asmx/getPreStudyDocIndex", {"top": str(top)}
        )

    def study_doc_one(self, doc_id: str) -> Any:
        return self.post_json(
            "wxInterface/index.asmx/getPreStudyDocOne", {"ID": str(doc_id)}
        )

    def study_info(self) -> Any:
        return self.post_json("wxInterface/index.asmx/getStudyinfo", {})

    # ---------- 考前模拟 / 在线练习 ----------

    def self_one_paper(self, security_kind_id: str, paper_set_id: str = "0") -> Any:
        """取练习/模拟试卷。PaperSetID=0 为普通练习；非 0 为考前模拟套卷。"""
        return self.post_json(
            "wxInterface/indexJdz.asmx/selfOnePaper",
            {
                "SecurityKindID": str(security_kind_id),
                "PaperSetID": str(paper_set_id),
            },
        )

    def practice_questions(
        self,
        paper_id: str,
        type_: str = "lx",
        is_xj: str = "0",
        bmid: int = 0,
        str_top: str = "",
    ) -> Any:
        """取练习题（含参考答案/解析）。"""
        return self.post_json(
            "wxInterface/indexJdz.asmx/selfStudy",
            {
                "ID": str(paper_id),
                "Type": type_,
                "IsXj": is_xj,
                "bmid": bmid,
                "strTop": str_top,
            },
        )

    def submit_practice_answer(
        self, lab_id: str, paper_id: str, answer: str, paper_set_id: str = "0"
    ) -> Any:
        """练习逐题提交：id=题目ID，ksid=试卷ID，ksanswer=答案。"""
        return self.post_json(
            "wxInterface/indexJdz.asmx/tjtm",
            {
                "id": str(lab_id),
                "ksid": str(paper_id),
                "papersetid": str(paper_set_id),
                "ksanswer": str(answer),
            },
        )

    # ---------- 在线考试 ----------

    def exam_papers(self, page: int = 1, size: int = 20) -> Any:
        """可参加的考试/试卷列表（需登录）。"""
        return self.paged("wxInterface/indexJdz.asmx/getExamPaperSetList", page, size)

    def exam_scores(self, page: int = 1, size: int = 20) -> Any:
        """考试记录/成绩列表（需登录）。"""
        return self.paged("wxInterface/index.asmx/getExamScoreList", page, size)

    def get_one_paper(self, paper_id: str) -> Any:
        """考试取卷：返回试卷配置、题型数量、分值、是否逐题等。"""
        return self.post_json(
            "wxInterface/indexJdz.asmx/GetOnePaper", {"ID": str(paper_id)}
        )

    def exam_labs(
        self,
        set_id: str,
        ks_id: str,
        regular_type: str = "0",
        tx_count: str = "A-0|B-0|C-0",
        two_code: str = "0",
    ) -> Any:
        """取考试题目。txCount 形如 A-10|B-5|C-5（单选/多选/判断数量）。"""
        return self.post_json(
            "wxInterface/AQKS.asmx/viewExamLab",
            {
                "setID": str(set_id),
                "KSID": str(ks_id),
                "RegularType": str(regular_type),
                "txCount": tx_count,
                "TwoCode": str(two_code),
            },
        )

    def submit_paper(
        self,
        paper_id: str,
        radio: str = "",
        check: str = "",
        judge: str = "",
        type_: str = "1",
        radio_group: str = "0",
    ) -> Any:
        """
        交卷。
        radio: "<单选总分>@<题目ID>-<答案>,..."
        check: "<多选总分>@<题目ID>-<A|B>,..."
        judge: "<判断总分>@<题目ID>-<答案>,..."
        type_ : 1（默认，与前端手动/自动交卷一致）
        """
        return self.post_json(
            "wxInterface/AQKS.asmx/PaperSubmit",
            {
                "PaperID": str(paper_id),
                "radio": radio,
                "check": check,
                "judge": judge,
                "type": str(type_),
                "RadioGroup": str(radio_group),
            },
        )

    def get_user_paper(self, paper_id: str) -> Any:
        """查卷：取用户已考试卷。"""
        return self.post_json(
            "wxInterface/index.asmx/GetUserPaper", {"ID": str(paper_id)}
        )

    def view_user_exam_lab(self, ks_id: str) -> Any:
        """查卷：逐题查看用户答案/正确答案。"""
        return self.post_json(
            "wxInterface/index.asmx/viewUserExamLab", {"KSID": str(ks_id)}
        )

    def exam_queue(self, paper_id: str, type_: str = "add") -> Any:
        """考试生成队列：add 开始排队，del 离开页面时移除。"""
        return self.post_json(
            "wxInterface/update/index.asmx/getonebyone",
            {"Type": type_, "ID": str(paper_id)},
        )

    def submit_exam_question(
        self,
        ks_id: str,
        tm_id: str,
        tm_type: str,
        answer: str,
        radio_group: str = "0",
        is_change: int = 0,
    ) -> Any:
        """逐题保存（update/index.asmx/tjks）。"""
        return self.post_json(
            "wxInterface/update/index.asmx/tjks",
            {
                "ksid": str(ks_id),
                "tmid": str(tm_id),
                "tmtype": str(tm_type),
                "ksanswer": str(answer),
                "RadioGroup": str(radio_group),
                "ischange": int(is_change),
            },
        )

    # ---------- 平台本地登录（可选） ----------

    @staticmethod
    def _des_b64(plain: str, key: str = "31113001") -> str:
        from Crypto.Cipher import DES
        from Crypto.Util.Padding import pad

        cipher = DES.new(key.encode("utf-8"), DES.MODE_CBC, key.encode("utf-8"))
        return base64.b64encode(cipher.encrypt(pad(plain.encode("utf-8"), 8))).decode()

    def login_new(
        self,
        username: str,
        password: str,
        checkcode: str = "",
        usertype: str = "0",
        is_wei_xin: str = "0",
        is_check_login: str = "0",
        is_ok: str = "true",
    ) -> Any:
        """
        平台本地账号登录（LoginNew，DES 加密）。
        说明：CAS 统一认证账号在本平台本地库中通常没有独立密码，
              该接口对本账号返回“登录失败，用户名或密码错误”。
              CAS 账号请走浏览器登录 + 会话 Cookie。
        """
        pw = base64.b64encode(password.encode("utf-8")).decode()
        raw = (
            "{username:'%s',password:'%s',usertype:'%s',checkcode:'%s',"
            "isWeiXin:'%s',IsCheckLogin:'%s',isOk:'%s'}"
            % (username, pw, usertype, checkcode, is_wei_xin, is_check_login, is_ok)
        )
        return self.post_json(
            "wxInterface/update/index.asmx/LoginNew", {"DESJson": self._des_b64(raw)}
        )


def _dump(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description="CUMT aqzrui API client")
    ap.add_argument("--base", default=os.environ.get("AQZRUI_BASE", BASE))
    ap.add_argument("--session", default=os.environ.get("AQZRUI_SESSION"))
    ap.add_argument("--cookie", default=os.environ.get("AQZRUI_COOKIE"))
    ap.add_argument("--token", default=os.environ.get("AQZRUI_TOKEN"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("kinds", help="学习/练习类别列表（匿名可用）")
    sub.add_parser("docs", help="首页学习资料列表（匿名可用）")

    p = sub.add_parser("papers", help="考试列表（需登录）")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--size", type=int, default=20)

    p = sub.add_parser("scores", help="考试记录（需登录）")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--size", type=int, default=20)

    p = sub.add_parser("learn", help="取学习题目（需登录）")
    p.add_argument("--kind", required=True)
    p.add_argument("--type", default="xx", choices=["xx", "lx"])

    p = sub.add_parser("practice", help="取练习/模拟试卷（需登录）")
    p.add_argument("--kind", required=True)
    p.add_argument("--set", dest="paper_set", default="0")

    p = sub.add_parser("paper", help="考试取卷（需登录）")
    p.add_argument("--id", required=True)

    p = sub.add_parser("labs", help="取考试题目（需登录）")
    p.add_argument("--set", dest="set_id", required=True)
    p.add_argument("--ks", required=True)
    p.add_argument("--regular", default="0")
    p.add_argument("--tx", default="A-0|B-0|C-0")
    p.add_argument("--two", dest="two_code", default="0")

    p = sub.add_parser("submit", help="交卷（需登录）")
    p.add_argument("--paper", required=True)
    p.add_argument("--radio", default="")
    p.add_argument("--check", default="")
    p.add_argument("--judge", default="")
    p.add_argument("--type", dest="type_", default="1")
    p.add_argument("--radiogroup", default="0")

    p = sub.add_parser("login", help="平台本地账号登录（CAS 账号通常不适用）")
    p.add_argument("--user", required=True)
    p.add_argument("--pass", dest="password", required=True)
    p.add_argument("--code", default="")
    p.add_argument("--usertype", default="0")

    args = ap.parse_args()
    client = AqzruiClient(
        base=args.base,
        session_id=args.session,
        cookie=args.cookie,
        token=args.token,
    )

    if args.cmd == "kinds":
        _dump(client.security_kinds())
    elif args.cmd == "docs":
        _dump(client.study_doc_index())
    elif args.cmd == "papers":
        _dump(client.exam_papers(args.page, args.size))
    elif args.cmd == "scores":
        _dump(client.exam_scores(args.page, args.size))
    elif args.cmd == "learn":
        _dump(client.learn_questions(args.kind, args.type))
    elif args.cmd == "practice":
        _dump(client.self_one_paper(args.kind, args.paper_set))
    elif args.cmd == "paper":
        _dump(client.get_one_paper(args.id))
    elif args.cmd == "labs":
        _dump(client.exam_labs(args.set_id, args.ks, args.regular, args.tx, args.two_code))
    elif args.cmd == "submit":
        _dump(
            client.submit_paper(
                args.paper, args.radio, args.check, args.judge, args.type_, args.radiogroup
            )
        )
    elif args.cmd == "login":
        _dump(client.login_new(args.user, args.password, args.code, args.usertype))
    return 0


if __name__ == "__main__":
    sys.exit(main())
