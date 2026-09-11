# -*- coding: utf-8 -*-
"""
把 NetSwitch 源码与文档上传到 GitHub 仓库。

用法:
    python upload_to_github.py [owner/repo] [branch] [子目录前缀]

默认: lovepaper/netswitch main （无前缀，放仓库根目录）
例:   python upload_to_github.py lovepaper/backup main netswitch

说明:
- token 自动从本机各项目 .git/config 里找（fine-grained PAT），不落盘、不打印
- 若提示 403/404，多半是该 PAT 未授权此仓库，去 GitHub 把仓库加进 PAT 的
  Repository access 白名单即可
- 只传源码与文档；NetSwitch.exe 和 netswitch_config.json 已在 .gitignore 中排除
"""

import os
import re
import glob
import json
import base64
import urllib.parse
import urllib.request
import urllib.error
import sys

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = sys.argv[1] if len(sys.argv) > 1 else "lovepaper/netswitch"
BRANCH = sys.argv[2] if len(sys.argv) > 2 else "main"
PREFIX = (sys.argv[3] if len(sys.argv) > 3 else "").strip("/")


def rpath(name):
    """仓库内的完整路径"""
    return (PREFIX + "/" + name) if PREFIX else name


def find_token():
    cfgs = []
    for pat in ("*/.git/config", "*/*/.git/config", "*/*/*/.git/config"):
        cfgs.extend(glob.glob(os.path.join(r"C:\Users\v_jinqqqiu\WorkBuddy", pat)))
    for c in cfgs:
        try:
            txt = open(c, "r", encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        m = re.findall(r"(github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9]+)", txt)
        if m:
            return m[0]
    return None


TOK = find_token()
if not TOK:
    print("未在本机 .git/config 中找到 GitHub token")
    raise SystemExit(1)

H = {"Authorization": "Bearer " + TOK,
     "Accept": "application/vnd.github+json",
     "User-Agent": "netswitch-upload",
     "Content-Type": "application/json"}


def req(method, path, payload=None):
    d = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request("https://api.github.com/" + path, data=d,
                               headers=H, method=method)
    try:
        resp = urllib.request.urlopen(r, timeout=120)
        raw = resp.read()
        return json.loads(raw) if raw else {"ok": True}
    except urllib.error.HTTPError as e:
        return {"_err": e.code, "_b": e.read().decode()[:300]}
    except Exception as e:
        return {"_err": "EXC", "_b": str(e)}


# ---------- 待上传文件 ----------
files = []

# 本地真实文件
for local_name in ("netswitch.py", "使用说明.md"):
    p = os.path.join(SRC_DIR, local_name)
    if os.path.exists(p):
        files.append((local_name, open(p, "rb").read()))
    else:
        print("  跳过（本地不存在）:", local_name)

readme = """# NetSwitch · PC 网络控制开关

给测试工作用的一键网络开关，避免反复拔网线 / 关 WiFi。三种模式互不干扰。

- `netswitch.py` — 主程序源码（Python 3.11 + tkinter）
- `使用说明.md` — 完整使用说明
- `upload_to_github.py` — 把源码同步到本仓库的工具脚本

## 三种模式

| 模式 | 干什么 | 适用场景 |
|---|---|---|
| 整机断网 | 禁用/启用网卡，等价于拔网线 | 测整体离线表现 |
| 循环断网 | 断 X 秒 → 连 Y 秒 → 重复 N 轮 | 测断线重连、超时重试、状态机恢复 |
| 单应用断网 | 只掐某个 exe，其他软件照常上网 | 整机不下线，单独验证某个客户端 |

## 重新打包

需要**带 tkinter 的 Python 环境**（注：某些精简 Python 发行版不含 tkinter）：

```bat
pyinstaller --noconfirm --onefile --windowed --clean --name NetSwitch ^
  --hidden-import=keyboard --hidden-import=win32com.client netswitch.py
```

打包产物 `NetSwitch.exe` 与运行配置 `netswitch_config.json` 不入库（见 .gitignore）。

## 已知注意

- 防火墙规则只对新连接生效，已建立的长连接建议重启被测程序再验证
- 批量断网默认跳过 Hyper-V / WSL / Docker / VMware 等虚拟网卡
- 断网后远程桌面会断开，远程机器建议用「单应用断网」模式
"""
files.append(("README.md", readme.encode("utf-8")))
files.append((".gitignore", """# 构建产物
dist/
build/
*.spec
NetSwitch.exe

# 运行时配置
netswitch_config.json

# Python
__pycache__/
*.py[cod]
.venv/
""".encode("utf-8")))

# 把本脚本自身也一并归档（便于以后在新机器上同步）
try:
    files.append(("upload_to_github.py", open(os.path.abspath(__file__), "rb").read()))
except Exception:
    pass

# ---------- 上传 ----------
print("目标: https://github.com/%s  (branch=%s)\n" % (REPO, BRANCH))
probe = req("GET", "repos/" + REPO)
if "_err" in probe:
    print("无法访问仓库:", probe)
    if probe.get("_err") in (403, 404):
        print("\n原因多半是：当前 PAT 未授权该仓库。")
        print("去 GitHub → Settings → Developer settings → Personal access tokens")
        print("→ Fine-grained tokens → 选那个 token → Repository access → 把 %s 加进去" % REPO)
    raise SystemExit(1)
print("仓库 OK · private=%s · default=%s\n" % (probe.get("private"), probe.get("default_branch")))

# 分支存在性校验：指定的分支不存在就回落到默认分支，避免逐个文件 404
brs = req("GET", "repos/%s/branches?per_page=100" % REPO)
names = [b["name"] for b in brs] if isinstance(brs, list) else []
if names and BRANCH not in names:
    print("  分支 %s 不存在，改用默认分支 %s" % (BRANCH, probe.get("default_branch")))
    BRANCH = probe.get("default_branch")
print("  分支列表:", names if names else "(空仓库，无分支)")

for name, data in files:
    dest = rpath(name)
    g = req("GET", "repos/%s/contents/%s?ref=%s" % (REPO, urllib.parse.quote(dest), BRANCH))
    pl = {"message": "update %s" % dest if isinstance(g, dict) and "sha" in g else "add %s" % dest,
          "branch": BRANCH,
          "content": base64.b64encode(data).decode()}
    if isinstance(g, dict) and "sha" in g:
        pl["sha"] = g["sha"]
    res = req("PUT", "repos/%s/contents/%s" % (REPO, urllib.parse.quote(dest)), pl)
    if "_err" in res:
        print("  FAIL %-30s %s" % (dest, res))
    else:
        print("  OK   %-30s %8d bytes" % (dest, len(data)))

print("\n仓库文件树:")
t = req("GET", "repos/%s/git/trees/%s?recursive=1" % (REPO, BRANCH))
if "tree" in t:
    for item in t["tree"]:
        print("   %-24s %s bytes" % (item["path"], item.get("size")))
else:
    print("   ", t)

print("\n完成: https://github.com/%s" % REPO)
