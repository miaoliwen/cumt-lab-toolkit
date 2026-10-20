#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国矿业大学 实验室安全准入平台（aqzrui）登录模块
=================================================

支持两条登录路径：

**1. `cas` 统一身份认证（学号/工号账号走这条）**

    GET  /authserver/login?service=<平台回跳地址>      → 取 pwdEncryptSalt / execution
    POST /authserver/login                             → AES-CBC 加密密码，主认证
         成功返回 302 + CASTGC；若账号开了二次认证，302 指向 reAuthCheck
    (二次认证)
         POST /authserver/dynamicCode/getDynamicCodeByReauth.do   → 短信/邮箱/钉钉/WeLink 动态码
              data={userName: reAuthUserId, authCodeTypeName: reAuthDynamicCodeType}
         POST /authserver/otp/getDynamicCodeByReAuthOtp.do       → 仅 type 10（OTP）绑定手机/邮箱
              data={showPhoneOrEmail: phone|email}，errCode==1 表示成功
         POST /authserver/reAuthCheck/changeReAuthType.do        → 切换二次认证方式
         POST /authserver/reAuthCheck/reAuthSubmit.do            → 校验动态码 / 账号密码 / OTP
    最后 GET /authserver/login?service=...（带 CASTGC）→ 302 到 caslogin.aspx
         → 平台下发 ASP.NET_SessionId

**2. `local` 平台本地账号（`LoginNew`，DES）** —— 适用于在平台本地库里有独立
   密码的账号；CAS 账号在本地库没有密码，走这条会返回"用户名或密码错误"。

密码加密算法（从前端 `cas_encrypt.js` 逐字还原）：

    plain = randomString(64) + password
    key   = salt（页面 #pwdEncryptSalt 的值，16 字节 → AES-128）
    iv    = randomString(16)
    AES-128-CBC / PKCS7，输出 Base64（**不含 IV**）

注意 IV 不在密文里：CBC 下 IV 只影响第一个分组，而真正的口令在第 64 字节之后，
所以服务端用固定 IV 解密也能拿到口令 —— 前 64 字节的随机串正好吸收了这个偏差。

二次认证类型（`reAuthType`）：
    2=账号密码(保底)  3=手机动态码  10=OTP 令牌  11=邮箱验证码
    12=钉钉  13=WeLink  14=扫码  6/18=人脸
"""

from __future__ import annotations

import base64
import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import requests

AES_CHARS = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"

AUTH_BASE = "https://authserver.cumt.edu.cn/authserver"
CAS_HOST = "authserver.cumt.edu.cn"
PLATFORM_SERVICE = "http://dygx.cumt.edu.cn:8080/aqzrui/caslogin.aspx?rz=0"
PLATFORM_ROOT = "http://dygx.cumt.edu.cn:8080/aqzrui/"
PLATFORM_HOST = "dygx.cumt.edu.cn"

ALLOWED_HOSTS = {CAS_HOST, PLATFORM_HOST}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")

# reAuthType -> 人话
REAUTH_NAMES = {
    "2": "账号密码", "3": "手机动态码", "4": "企业微信动态码", "5": "今日校园动态码",
    "6": "人脸认证", "7": "账号密码", "8": "微信", "9": "QQ", "10": "OTP 安全令牌",
    "11": "邮箱验证码", "12": "钉钉验证码", "13": "WeLink 验证码", "14": "扫码认证",
    "18": "人脸认证",
}
# 需要用「动态码」输入框的类型
CODE_TYPES = {"3", "4", "5", "11", "12", "13"}

# reAuthType -> 发码接口的 authCodeTypeName（取自 reAuth.js:sendDynamicCodeByPhone）
CODE_ENDPOINTS = {
    "3": "reAuthDynamicCodeType",
    "4": "reAuthWChatDynamicCodeType",
    "5": "reAuthCpdailyDynamicCodeType",
    "11": "reAuthEmailDynamicCodeType",
    "12": "reAuthDingTalkDynamicCodeType",
    "13": "reAuthWeLinkDynamicCodeType",
}

# --channel 参数 -> reAuthType（前端「切换认证方式」走 changeReAuthType.do）
CHANNEL_TO_TYPE = {
    "phone": "3",
    "email": "11",
    "dingtalk": "12",
    "welink": "13",
    "otp": "10",
}

AJAX_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}


def _ajax_headers(referer: str = "") -> Dict[str, str]:
    h = dict(AJAX_HEADERS)
    if referer:
        h["Referer"] = referer
    return h


def _looks_expired(msg: str) -> bool:
    """二次认证页的会话/验证码是否已经过期（需要重新走一遍主认证）。"""
    return any(k in (msg or "") for k in
               ("过期", "失效", "超时", "重新登录", "重新认证", "expired",
                "timeout", "session expired"))


class LoginError(Exception):
    pass


def _rand(n: int) -> str:
    return "".join(secrets.choice(AES_CHARS) for _ in range(n))


def encrypt_password(password: str, salt: str) -> str:
    """
    还原前端 encryptPassword(password, pwdEncryptSalt)。
    salt 为空时前端原样返回（此处保持同样行为）。
    """
    salt = (salt or "").strip()
    if not salt:
        return password
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
    except ImportError as e:                       # pragma: no cover
        raise LoginError(
            "缺少 pycryptodome，无法加密密码：pip install pycryptodome"
        ) from e

    key = salt.encode("utf-8")
    if len(key) not in (16, 24, 32):               # AES 只接受这三种长度
        key = (salt * 4).encode("utf-8")[:16]
    iv = _rand(16).encode("utf-8")
    plain = (_rand(64) + password).encode("utf-8")
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return base64.b64encode(cipher.encrypt(pad(plain, 16))).decode("ascii")


@dataclass
class LoginResult:
    """一次登录阶段的结果。stage 说明卡在哪一步。"""
    ok: bool
    stage: str                      # cas_main | need_2fa | reauth_expired | done | failed
    msg: str = ""
    cookie: str = ""                # ASP.NET_SessionId 的值
    reauth_type: str = ""
    channels: List[str] = field(default_factory=list)   # 可用的验证方式

    @property
    def need_2fa(self) -> bool:
        # reauth_expired 仍然展示验证码输入行：页面会话过期时用户应该重新点「登录」，
        # 但旧输入框留着不会误导；是否重开由界面按 stage 提示。
        return self.stage in ("need_2fa", "reauth_expired")


def _extract_js_object(html: str, varname: str) -> Dict[str, str]:
    """
    抠出 `var xxx = { ... }` 或 `"xxx":{...}` 这类对象。

    实测该页面的 reAuthParams 是**合法 JSON**（键带双引号），所以先按 JSON 解，
    失败再退回正则（兼容 JS 字面量的无引号键）。括号匹配会跳过字符串内的
    花括号与转义，避免被值里的 `{`/`}` 带偏。
    """
    m = re.search(re.escape(varname) + r"\s*[:=]\s*\{", html)
    if not m:
        return {}
    start = html.index("{", m.start())
    depth, i, quote, esc = 0, start, "", False
    while i < len(html):
        c = html[i]
        if quote:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    if depth != 0:
        return {}
    blob = html[start:i + 1]
    try:
        obj = json.loads(blob)
        if isinstance(obj, dict):
            return {k: ("" if v is None else str(v)) for k, v in obj.items()}
    except ValueError:
        pass
    out: Dict[str, str] = {}
    for mm in re.finditer(r"([A-Za-z_]\w*)\s*:\s*(?:'([^']*)'|\"([^\"]*)\"|([-\w.]+))", blob):
        out[mm.group(1)] = mm.group(2) or mm.group(3) or mm.group(4) or ""
    return out


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()


class CasLogin:
    """
    一次登录会话。分步调用以便 GUI 在两步之间向用户取验证码：

        cas = CasLogin(user, pwd)
        r1 = cas.step_main()            # 可能直接 ok，也可能 need_2fa
        if r1.need_2fa:
            cas.step_request_code()     # 发验证码
            r2 = cas.step_submit_2fa(code)
    """

    def __init__(self, username: str, password: str,
                 service: str = PLATFORM_SERVICE, timeout: int = 25,
                 log: Callable[[str], None] = print,
                 session: Optional[requests.Session] = None,
                 finish_handler: Optional[Callable[["CasLogin"], LoginResult]] = None) -> None:
        self.username = username.strip()
        self.password = password
        self.service = service
        self.timeout = timeout
        self.log = log
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self.reauth: Dict[str, str] = {}
        self.reauth_url = ""
        self.salt = ""
        self.platform_cookie = ""
        self.last_code_wait = 0
        self.last_location = ""
        # 登录成功后如何把 CAS 票据换成业务会话。None = 平台默认（dygx 的
        # caslogin.aspx + ASP.NET_SessionId）；其他平台传入自己的处理器。
        self.finish_handler = finish_handler

    # ---------------- 底层 ----------------
    def _get(self, url: str, **kw) -> requests.Response:
        kw.setdefault("timeout", self.timeout)
        kw.setdefault("allow_redirects", True)
        return self.s.get(url, **kw)

    def _post(self, url: str, data: dict, **kw) -> requests.Response:
        kw.setdefault("timeout", self.timeout)
        kw.setdefault("allow_redirects", False)
        return self.s.post(url, data=data, **kw)

    # ---------------- 第 1 步：主认证 ----------------
    def step_main(self) -> LoginResult:
        try:
            page = self._get(f"{AUTH_BASE}/login", params={"service": self.service})
        except requests.RequestException as e:
            return LoginResult(False, "failed", f"无法访问统一认证：{e}")
        if page.status_code != 200:
            return LoginResult(False, "failed",
                               f"统一认证返回 {page.status_code}")
        html = page.text

        m = re.search(r'id="pwdEncryptSalt"[^>]*value="([^"]*)"', html)
        self.salt = m.group(1) if m else ""
        ex = re.search(r'name="execution"[^>]*value="([^"]*)"', html)
        execution = ex.group(1) if ex else "e1s1"
        self.log(f"登录页已取：salt={'有' if self.salt else '无'} execution={execution}")

        data = {
            "username": self.username,
            "password": encrypt_password(self.password, self.salt),
            "_eventId": "submit",
            "cllt": "userNameLogin",
            "dllt": "generalLogin",
            "lt": "",
            "execution": execution,
            "captcha": "",
        }
        try:
            r = self._post(f"{AUTH_BASE}/login", data=data)
        except requests.RequestException as e:
            return LoginResult(False, "failed", f"提交登录失败：{e}")

        loc = r.headers.get("Location", "")
        self.last_location = loc
        if r.status_code in (301, 302, 303, 307) and loc:
            if "reAuthCheck" in loc or "reAuth" in loc:
                self.log("主认证通过，进入二次认证")
                return self._enter_reauth(loc)
            self.log(f"主认证通过，跳转 {_strip_html(loc)[:90]}")
            return self._finish()

        # 200 = 没跳转，多半是账号密码错/需要验证码
        err = self._extract_error(html=r.text)
        return LoginResult(False, "failed",
                           err or f"主认证未通过（HTTP {r.status_code}）")

    @staticmethod
    def _extract_error(html: str = "", text: str = "") -> str:
        for blob in (text or "", html or ""):
            if not blob:
                continue
            m = re.search(r'id="showErrorTip"[^>]*>(.*?)</', blob, re.S)
            if m:
                v = _strip_html(m.group(1))
                if v:
                    return v
            m = re.search(r'class="[^"]*error[^"]*"[^>]*>(.*?)</', blob, re.S)
            if m:
                v = _strip_html(m.group(1))
                if v and len(v) < 120:
                    return v
        for kw in ("用户名或密码错误", "密码错误", "验证码", "账号被锁定", "用户不存在"):
            if kw in (text or "") or kw in (html or ""):
                return kw
        return ""

    def _enter_reauth(self, loc: str) -> LoginResult:
        url = loc if loc.startswith("http") else AUTH_BASE.rsplit("/authserver", 1)[0] + loc
        try:
            page = self._get(url)
        except requests.RequestException as e:
            return LoginResult(False, "failed", f"打开二次认证页失败：{e}")
        self.reauth_url = page.url or url
        self.reauth = _extract_js_object(page.text, "reAuthParams")
        rt = self.reauth.get("reAuthType", "") or "3"
        channels = self._channels()
        if not channels:                       # 页面没列出来就给个常规候选
            channels = ["3", "11", "12", "10"]
        names = [f"{c}({REAUTH_NAMES.get(c, c)})" for c in channels]
        self.log(f"二次认证方式 reAuthType={rt}({REAUTH_NAMES.get(rt, rt)})，可选 {names}")
        return LoginResult(False, "need_2fa",
                           f"需要二次认证：{REAUTH_NAMES.get(rt, rt)}",
                           reauth_type=rt, channels=channels)

    def _service(self) -> str:
        return self.reauth.get("service") or self.service

    def _channels(self) -> List[str]:
        """页面里 reAuthType3 / reAuthType11 这类键 = 可切换的认证方式。"""
        rt = self.reauth.get("reAuthType", "")
        return sorted({v for k, v in self.reauth.items()
                       if k.startswith("reAuthType") and v.isdigit() and v != rt})

    # ---------------- 第 2 步：二次认证 ----------------
    def change_reauth_type(self, reauth_type: str) -> Tuple[bool, str]:
        """
        切换二次认证方式（前端 reAuth.js:changeReAuthType 的等价实现）。
        reAuthType=3 的账号可选 3/11/12/10，切到 11 才是邮箱取码。
        """
        rt = str(reauth_type)
        data = {
            "isMultifactor": self.reauth.get("isMultifactor", "true"),
            "reAuthType": rt,
            "service": self._service(),
        }
        try:
            r = self._post(f"{AUTH_BASE}/reAuthCheck/changeReAuthType.do", data=data,
                           headers=_ajax_headers(self.reauth_url))
        except requests.RequestException as e:
            return False, f"切换二次认证方式失败：{e}"
        try:
            j = r.json()
        except ValueError:
            return False, f"切换认证方式返回异常：HTTP {r.status_code} {_strip_html(r.text)[:100]}"
        if str(j.get("code")) != "1":
            return False, str(j.get("message") or j.get("msg") or "切换二次认证方式被拒绝")
        d = j.get("data")
        effective = rt
        if isinstance(d, dict):
            for k in ("reAuthType", "reAuthTypeName", "reAuthUserId",
                      "reAuthUserNameInput", "isMultifactor", "pwdEncryptSalt",
                      "reAuthOtpDec1", "reAuthOtpDec2", "_isBindOtp", "_otpUrl"):
                if k in d and d[k] is not None:
                    self.reauth[k] = str(d[k])
            effective = str(d.get("reAuthType") or rt)
        self.reauth["reAuthType"] = effective
        return True, f"已切换二次认证方式：{REAUTH_NAMES.get(effective, effective)}"

    def step_request_code(self, channel: str = "") -> Tuple[bool, str]:
        """
        发送动态验证码。

        channel：'' 按页面当前 reAuthType 决定；'phone'/'email'/'dingtalk'/
        'welink'/'otp' 会先切到对应 reAuthType 再发码。

        注意两条接口完全不同（取自 reAuth.js）：
          - 短信/邮箱/钉钉/WeLink 动态码 → /dynamicCode/getDynamicCodeByReauth.do
            data={userName: reAuthUserId, authCodeTypeName: reAuthDynamicCodeType...}
          - 安全令牌（type 10）绑定手机/邮箱 → /otp/getDynamicCodeByReAuthOtp.do
            data={showPhoneOrEmail: phone|email}，返回 errCode==1 为成功
        """
        rt = str(self.reauth.get("reAuthType") or "3")
        ch = (channel or "").strip().lower()
        target_rt = CHANNEL_TO_TYPE.get(ch, "")
        if target_rt and target_rt != rt:
            ok, msg = self.change_reauth_type(target_rt)
            if not ok:
                return False, msg
            rt = target_rt
            self.log(msg)

        if rt == "10":
            return self._request_otp_code(ch or "phone")
        return self._request_dynamic_code(rt)

    def _request_dynamic_code(self, rt: str) -> Tuple[bool, str]:
        auth_name = CODE_ENDPOINTS.get(rt)
        if not auth_name:
            return False, f"reAuthType={rt} 没有对应的动态码接口"
        user = self.reauth.get("reAuthUserId") or self.username
        data = {"userName": user, "authCodeTypeName": auth_name}
        try:
            r = self._post(f"{AUTH_BASE}/dynamicCode/getDynamicCodeByReauth.do", data=data,
                           headers=_ajax_headers(self.reauth_url))
        except requests.RequestException as e:
            return False, f"请求验证码失败：{e}"
        try:
            j = r.json()
        except ValueError:
            return False, f"返回异常：HTTP {r.status_code} {_strip_html(r.text)[:120]}"

        res = str(j.get("res") or "")
        msg = str(j.get("returnMessage") or j.get("msg") or "")
        mobile = str(j.get("mobile") or "")
        if mobile:
            msg += mobile                      # 前端把脱敏号码拼在提示后面
        wait = int(j.get("codeTime") or 0)
        self.last_code_wait = wait

        if res == "code_time_fail":
            return False, f"{msg or '发送过于频繁'}（请 {wait}s 后再试）"
        if res in ("success", "wechat_success", "cpdaily_success"):
            return True, msg or "验证码已发送"
        return False, msg or f"发送失败（res={res or '?'}）"

    def _request_otp_code(self, channel: str) -> Tuple[bool, str]:
        want_email = (channel or "").strip().lower() == "email"
        data = {"showPhoneOrEmail": "email" if want_email else "phone"}
        try:
            r = self._post(f"{AUTH_BASE}/otp/getDynamicCodeByReAuthOtp.do", data=data,
                           headers=_ajax_headers(self.reauth_url))
        except requests.RequestException as e:
            return False, f"请求验证码失败：{e}"
        try:
            j = r.json()
        except ValueError:
            return False, f"返回异常：HTTP {r.status_code} {_strip_html(r.text)[:120]}"

        # 该接口 errCode == 1 表示成功（前端 reAuth.js:getReAuthOtpPhoneCode）
        try:
            err_code = int(j.get("errCode") or 0)
        except (TypeError, ValueError):
            err_code = 0
        msg = str(j.get("errMsg") or "")
        try:
            wait = int(j.get("data") or 0)
        except (TypeError, ValueError):
            wait = 0
        self.last_code_wait = wait
        if err_code == 1:
            return True, msg or "验证码已发送"
        return False, msg or f"发送失败（errCode={err_code}）"

    def step_submit_2fa(self, code: str, reauth_type: str = "",
                        password: str = "", trust_device: bool = False) -> LoginResult:
        """
        提交二次认证。
        reauth_type=2/7（账号密码）时用 password（默认取登录密码，用 pwdEncryptSalt 加密）；
        其他类型用 code（动态码 / OTP）。
        trust_device=False 对应前端的「仅本次登录」（不写入长期可信设备）。
        """
        rt = str(reauth_type or self.reauth.get("reAuthType") or "3")
        code = (code or "").strip()
        channels = self._channels()

        # 本地先做形状校验，避免把空串/带空格的码发给服务端换一个笼统的失败
        if rt in CODE_TYPES and not code:
            return LoginResult(False, "need_2fa", "验证码为空", reauth_type=rt,
                               channels=channels)
        if rt == "10" and (not code.isdigit() or len(code) != 6):
            return LoginResult(False, "need_2fa", "OTP 安全令牌应为 6 位数字",
                               reauth_type=rt, channels=channels)

        enc_pwd = ""
        if rt in ("2", "7"):
            if not password:
                password = self.password
            enc_pwd = encrypt_password(password, self.reauth.get("pwdEncryptSalt", ""))
        data = {
            "service": self._service(),
            "reAuthType": rt,
            "isMultifactor": self.reauth.get("isMultifactor", "true"),
            "password": enc_pwd,
            "dynamicCode": code if rt in CODE_TYPES else "",
            "uuid": "",
            "answer1": "",
            "answer2": "",
            "otpCode": code if rt == "10" else "",
        }
        # isSleepAccount=='0' 时前端会弹「可信设备」确认，两个按钮都会带上
        # skipTmpReAuth（信任=true / 仅本次=false）；不带该参数服务端会缺条件。
        if self.reauth.get("isSleepAccount") == "0":
            data["skipTmpReAuth"] = "true" if trust_device else "false"
        try:
            r = self._post(f"{AUTH_BASE}/reAuthCheck/reAuthSubmit.do", data=data,
                           headers=_ajax_headers(self.reauth_url))
        except requests.RequestException as e:
            return LoginResult(False, "need_2fa", f"提交二次认证失败：{e}",
                               reauth_type=rt, channels=channels)

        if r.status_code >= 400:
            return LoginResult(False, "need_2fa",
                               f"二次认证接口 HTTP {r.status_code}：{_strip_html(r.text)[:100]}",
                               reauth_type=rt, channels=channels)
        try:
            j = r.json()
        except ValueError:
            return LoginResult(False, "need_2fa",
                               f"返回异常（HTTP {r.status_code}）：{_strip_html(r.text)[:120]}",
                               reauth_type=rt, channels=channels)

        code_j = str(j.get("code") or "")
        msg = str(j.get("msg") or j.get("message") or "")
        if code_j in ("reAuth_failed", "reAuth_unauthorized"):
            stage = "reauth_expired" if _looks_expired(msg) else "need_2fa"
            return LoginResult(False, stage, msg or "验证码校验失败",
                               reauth_type=rt, channels=channels)
        self.log(f"二次认证通过（{code_j or 'ok'}）")
        return self._finish()

    def _finish(self) -> LoginResult:
        if self.finish_handler is not None:
            return self.finish_handler(self)
        return self.step_platform()

    # ---------------- 第 3 步：换取平台会话 ----------------
    def step_platform(self) -> LoginResult:
        # 前端二次认证成功后跳 /login?service=...；CAS 用 CASTGC 签发 ticket，
        # 302 到 service（caslogin.aspx），平台随后下发 ASP.NET_SessionId。
        service = self._service()
        try:
            r = self._get(f"{AUTH_BASE}/login", params={"service": service})
        except requests.RequestException as e:
            return LoginResult(False, "failed", f"建立平台会话失败：{e}")

        final_url = r.url or ""
        if "reAuthCheck" in final_url or "reAuthParams" in (r.text or ""):
            # 2FA 在本会话里没有真正落定（常见于提交后会话过期）
            rt = str(self.reauth.get("reAuthType") or "")
            return LoginResult(False, "reauth_expired",
                               "二次认证状态未保持，请重新登录", reauth_type=rt,
                               channels=self._channels())

        sid = self.s.cookies.get("ASP.NET_SessionId", domain=PLATFORM_HOST)
        if not sid:
            for c in self.s.cookies:
                if c.name == "ASP.NET_SessionId":
                    sid = c.value
                    break
        if not sid:
            return LoginResult(
                False, "failed",
                f"CAS 已通过，但平台未下发 ASP.NET_SessionId（最终地址 {final_url or '?'}，"
                f"HTTP {r.status_code}）")
        self.platform_cookie = sid
        self.log(f"已取得平台会话 ASP.NET_SessionId={sid[:8]}…")
        return LoginResult(True, "done", "登录成功", cookie=sid)

    # ---------------- 便捷封装 ----------------
    def login(self, code_provider: Optional[Callable[[LoginResult], str]] = None) -> LoginResult:
        """
        一步到位。需要二次认证时调用 code_provider(中间结果) 取验证码；
        返回空字符串视为放弃。
        """
        res = self.step_main()
        if res.ok or not res.need_2fa:
            return res
        if code_provider is None:
            return res
        self.step_request_code()
        code = (code_provider(res) or "").strip()
        if not code:
            return LoginResult(False, "failed", "未提供验证码，登录中止",
                               reauth_type=res.reauth_type)
        return self.step_submit_2fa(code)


# ---------------------------------------------------------------------------
# 平台本地账号登录（LoginNew，DES）
# ---------------------------------------------------------------------------
def platform_local_login(username: str, password: str,
                         timeout: int = 25,
                         log: Callable[[str], None] = print) -> LoginResult:
    """
    平台本地库账号登录。CAS 账号在本地库没有密码，会返回"用户名或密码错误"。
    """
    from aqzrui_api import AqzruiClient, _unwrap

    try:
        from Crypto.Cipher import DES
        from Crypto.Util.Padding import pad
    except ImportError as e:
        raise LoginError("缺少 pycryptodome：pip install pycryptodome") from e

    client = AqzruiClient(timeout=timeout)
    pw_b64 = base64.b64encode(password.encode("utf-8")).decode()
    raw = ("{username:'%s',password:'%s',usertype:'0',checkcode:'',"
           "isWeiXin:'0',IsCheckLogin:'0',isOk:'true'}" % (username, pw_b64))
    # DES-CBC / key=IV="31113001" 是平台 LoginNew 接口自己规定的报文格式
    # （前端 aes_encrypt 同款实现），不是本模块的安全选择 —— 换算法服务端就不认了。
    key = b"31113001"
    cipher = DES.new(key, DES.MODE_CBC, key)
    des = base64.b64encode(cipher.encrypt(pad(raw.encode("utf-8"), 8))).decode()

    resp = client.post_json("wxInterface/update/index.asmx/LoginNew", {"DESJson": des})
    msg = ""
    if isinstance(resp, dict):
        m = resp.get("message")
        if isinstance(m, list) and m and isinstance(m[0], dict):
            flag = str(m[0].get("ReturnFlag", ""))
            msg = str(m[0].get("ReturnMsg", ""))
            if flag == "1":
                sid = client.s.cookies.get("ASP.NET_SessionId", domain=PLATFORM_HOST)
                return LoginResult(True, "done", "本地登录成功", cookie=sid or "")
            return LoginResult(False, "failed", msg or "本地登录失败")
    return LoginResult(False, "failed", f"本地登录返回异常：{str(resp)[:120]}")
