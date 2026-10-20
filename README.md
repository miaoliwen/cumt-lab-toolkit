# CUMT 实验室安全平台自动化工具

中国矿业大学实验室安全学习平台（`aqzrui` / `labsafe`）的命令行与图形界面工具：
登录、学习、练习、考试、课程视频播放进度上报。

> **声明**：本项目仅用于账号持有人本人的学习辅助与接口研究。请遵守所在学校与平台的
> 使用条款，不要用于代替他人操作、批量注册或任何未经授权的用途。仓库**不包含**任何
> 课程视频、题库答案、平台前端源码或他人账号信息。

---

## 功能

### aqzrui（实验室安全准入平台）

- **登录**：CAS 统一身份认证 + 短信/邮箱/钉钉/OTP 二次认证，密码按前端 AES-CBC 规则加密。
- **答案库**：从公开的学习题接口抓取题目与答案，累积入库、按题干/ID 匹配。
- **学习**：只对视频类资料（`DOCKIND=2`）上报观看时长；遇到 WAF 拒绝自动退避；
  可选 `AQZRUI_STUDY_REALTIME=1` 按真实节奏分段上报。
- **练习 / 考试**：取卷、取题、自动作答、交卷；考试支持先演练（`--dry-run`）。
- **成绩复核**：只读查询每场考试的得分、通过线、是否通过。
- **AI 兜底**（可选）：答案库未命中时调用 OpenAI 兼容接口，Key 只从环境变量读取。

### labsafe（JeecgBoot 课程平台）

- **登录**：CAS + 二次认证换取 `X-Access-Token`。
- **课程枚举**：知识树 → 课程 → 视频源，读取真实时长、完成状态、答题数。
- **虚拟播放器**：按 1x 真实时钟每 10 秒上报 `finishRate`，到弹题时间自动
  `submitAnswer`，播完 `finish`，最后回读 `q1/anum` 验证。
- **断点续跑**：`isFinish=1` 的视频默认跳过；可多线程并行。
- **实时进度**：终端原地刷新面板（百分比、进度条、剩余时间、最慢剩余），
  重定向到文件时自动退回逐行日志。
- **颜色分级**：进度/成功绿、错误红、调试黄；`--color` 可控制。
- **诊断**：只读盘点课程完成计数，或做上报前后计数对照。

---

## 环境

- Python 3.9+
- 依赖：

```bash
pip install -r requirements.txt
```

- GUI 使用 Python 标准库 `tkinter`（Windows 官方安装包自带）。

---

## 快速开始

### 1. 命令行：aqzrui

```bash
# 登录（密码交互输入；开了二次认证会提示验证码）
python aqzrui_speedrun.py login --user <学号>

# 抓答案库（匿名接口，约 2 秒）
python aqzrui_speedrun.py bank

# 学习：只对视频资料上报
python aqzrui_speedrun.py study

# 考试：先演练
python aqzrui_speedrun.py exam --dry-run

# 正式执行
python aqzrui_speedrun.py all
```

### 2. 命令行：labsafe

```bash
# 登录并打印 LABSAFE_TOKEN
python labsafe_player.py --login --user <学号>

# 只读演练：枚举课程、显示真实时长、跳过已完成
python labsafe_player.py --token <TOKEN> --all --threads 3 --dry-run

# 正式播放
python labsafe_player.py --token <TOKEN> --all --threads 3

# 播放单个视频（graphId + 视频源 ID + 真实秒数）
python labsafe_player.py --token <TOKEN> \
  --graph <GRAPH_ID> --id <SOURCE_ID> --duration 293

# 只读盘点课程完成计数
python labsafe_diag.py --token <TOKEN>
```

### 3. 图形界面

```bash
python aqzrui_gui.py
```

界面包含：登录 / 参数 / 执行 / **labsafe 视频** / 日志。labsafe 区块支持
token 登录、枚举、播放全部/单个、线程数、时长、答案 JSON、演练与停止。

---

## 打包 EXE

```bash
# GUI 主程序
python -m PyInstaller --noconfirm aqzrui-speedrun.spec

# 独立的 labsafe 控制台播放器
python -m PyInstaller --noconfirm labsafe-player.spec
```

单文件 EXE 的状态目录在 exe 同级的 `.speedrun/`；答案库可随目录一起拷贝，
或在界面里点「抓答案库」重建。

---

## 环境变量

| 变量 | 用途 |
|---|---|
| `AQZRUI_USER` / `AQZRUI_PASSWORD` | aqzrui 登录账号（非交互场景） |
| `AQZRUI_SESSION` | aqzrui 的 `ASP.NET_SessionId` 会话 |
| `LABSAFE_USER` / `LABSAFE_PASSWORD` | labsafe 登录账号 |
| `LABSAFE_TOKEN` | labsafe 的 `X-Access-Token` |
| `AQZRUI_STUDY_SECONDS` | 资料缺省上报秒数（默认 600） |
| `AQZRUI_STUDY_REALTIME` | `1` = 按真实节奏分段上报 |
| `AQZRUI_STUDY_HEARTBEAT` | 真实节奏的分段间隔秒数（默认 30） |
| `AQZRUI_AI_KEY` / `AQZRUI_AI_BASE` / `AQZRUI_AI_MODEL` | 可选 AI 兜底接口 |

> 所有凭据只从环境变量或交互输入读取，源码与测试中**没有**任何可用凭据字面量。

---

## 目录结构

```
aqzrui_api.py          aqzrui 底层 HTTP 客户端（ASMX）
aqzrui_login.py        CAS 登录 + 二次认证（AES 密码加密）
aqzrui_speedrun.py     学习 / 练习 / 考试编排（命令行）
aqzrui_gui.py          tkinter 图形界面
ai_fallback.py         可选 AI 兜底作答
labsafe_client.py      labsafe JeecgBoot 客户端（X-Sign + token）
labsafe_diag.py        labsafe 只读诊断
labsafe_player.py      labsafe 虚拟播放器（CLI + GUI 共用）
*.spec                 PyInstaller 打包配置
test_*.py              离线冒烟测试（不发起真实请求）
```

---

## 测试

```bash
python test_study_skip.py     # 学习阶段只对视频上报
python test_enumerate.py      # labsafe 源列表解析 duration / isFinish
python test_gui_ls.py         # GUI labsafe 集成（替换网络调用）
python aqzrui_gui.py --selftest
```

---

## 安全与隐私

- 仓库**不包含**账号、密码、Cookie、Token、验证码、手机号或任何个人数据。
- 运行时凭据仅保存在内存或环境变量中，不落盘。
- 不附带题库答案、课程视频或平台前端抓取文件。
- 使用前请确认你有权操作对应账号，并遵守平台使用条款。

## License

MIT © 2026 miaoliwen
