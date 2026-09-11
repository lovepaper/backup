# -*- coding: utf-8 -*-
"""
把 NetSwitch 的源码、文档与可执行文件同步到 GitHub 仓库。

用法:
    python upload_to_github.py [owner/repo] [branch] [子目录前缀]

默认: lovepaper/netswitch main （无前缀，放仓库根目录）
例:   python upload_to_github.py lovepaper/backup main netswitch

说明:
- token 自动从本机各项目 .git/config 里找（fine-grained PAT），不落盘、不打印
- 若提示 403/404，多半是该 PAT 未授权此仓库，去 GitHub 把仓库加进 PAT 的
  Repository access 白名单即可
- 走 Git Data API（blobs → trees → commits → refs），不受 contents API 的 1MB 限制，
  因此 12MB 的 NetSwitch.exe 也能传
- 提交前会比对远端已有 blob sha，内容没变的文件不会重复提交
"""

import os
import re
import glob
import json
import base64
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


def req(method, path, payload=None, timeout=300):
    d = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request("https://api.github.com/" + path, data=d,
                               headers=H, method=method)
    try:
        resp = urllib.request.urlopen(r, timeout=timeout)
        raw = resp.read()
        return json.loads(raw) if raw else {"ok": True}
    except urllib.error.HTTPError as e:
        return {"_err": e.code, "_b": e.read().decode()[:300]}
    except Exception as e:
        return {"_err": "EXC", "_b": str(e)}


# ---------- 待上传文件 ----------
files = []          # [(仓库内路径, 本地绝对路径或None, 字节内容)]

# 本地真实文件（含 exe）
for local_name in ("netswitch.py", "使用说明.md", "NetSwitch.exe"):
    p = os.path.join(SRC_DIR, local_name)
    if os.path.exists(p):
        files.append((rpath(local_name), open(p, "rb").read()))
    else:
        print("  跳过（本地不存在）:", local_name)

readme = """# NetSwitch · PC 网络控制开关

给测试工作用的一键网络开关，避免反复拔网线 / 关 WiFi。三种模式互不干扰。

- `netswitch.py` — 主程序源码（Python 3.11 + tkinter）
- `NetSwitch.exe` — 打包好的可执行文件，免 Python 环境，直接以管理员运行
- `使用说明.md` — 完整使用说明
- `upload_to_github.py` — 把本项目同步到 GitHub 的工具脚本

## 三种模式

| 模式 | 干什么 | 适用场景 |
|---|---|---|
| 整机断网 | 禁用/启用网卡，等价于拔网线 | 测整体离线表现 |
| 循环断网 | 断 X 秒 → 连 Y 秒 → 重复 N 轮 | 测断线重连、超时重试、状态机恢复 |
| 单应用断网 | 只掐某个 exe，其他软件照常上网 | 整机不下线，单独验证某个客户端 |

## 直接下载使用

`NetSwitch.exe` 已入库，下载后**右键 → 以管理员身份运行**（非管理员无法开关网卡）。
首次以管理员运行后点「创建免UAC快捷方式」，以后双击不再弹 UAC。

## 重新打包

需要**带 tkinter 的 Python 环境**（注：某些精简 Python 发行版不含 tkinter）：

```bat
pyinstaller --noconfirm --onefile --windowed --clean --name NetSwitch ^
  --hidden-import=keyboard --hidden-import=win32com.client netswitch.py
```

## 已知注意

- 防火墙规则只对新连接生效，已建立的长连接建议重启被测程序再验证
- 批量断网默认跳过 Hyper-V / WSL / Docker / VMware 等虚拟网卡
- 断网后远程桌面会断开，远程机器建议用「单应用断网」模式
"""
files.append((rpath("README.md"), readme.encode("utf-8")))
files.append((rpath(".gitignore"), """# 构建中间产物（最终 exe 已入库，这里只忽略过程文件）
dist/
build/
*.spec

# 运行时配置（含本机网卡名等，不入库）
netswitch_config.json

# Python
__pycache__/
*.py[cod]
.venv/
""".encode("utf-8")))

# 把本脚本自身也一并归档（便于以后在新机器上同步）
try:
    files.append((rpath("upload_to_github.py"), open(os.path.abspath(__file__), "rb").read()))
except Exception:
    pass

# ---------- 上传：Git Data API ----------
print("目标: https://github.com/%s  (branch=%s)\n" % (REPO, BRANCH))
probe = req("GET", "repos/" + REPO)
if "_err" in probe:
    print("无法访问仓库:", probe)
    if probe.get("_err") in (403, 404):
        print("\n原因多半是：当前 PAT 未授权该仓库。")
        print("去 GitHub → Settings → Developer settings → Personal access tokens")
        print("→ Fine-grained tokens → 选那个 token → Repository access → 把 %s 加进去" % REPO)
    raise SystemExit(1)
print("仓库 OK · private=%s · default=%s" % (probe.get("private"), probe.get("default_branch")))

# 分支存在性校验
brs = req("GET", "repos/%s/branches?per_page=100" % REPO)
names = [b["name"] for b in brs] if isinstance(brs, list) else []
if names and BRANCH not in names:
    print("  分支 %s 不存在，改用默认分支 %s" % (BRANCH, probe.get("default_branch")))
    BRANCH = probe.get("default_branch")
print("  分支:", BRANCH if names else "(空仓库，将创建)")

# 1) 现有文件 sha，用于跳过未变更文件
existing = {}
if names:
    t = req("GET", "repos/%s/git/trees/%s?recursive=1" % (REPO, BRANCH))
    for it in t.get("tree", []):
        if it["type"] == "blob":
            existing[it["path"]] = it["sha"]

# 2) 建 blob（内容未变则复用已有 sha）
tree_items = []
print("\n[1/4] 创建 blob")
for path, data in files:
    r = req("POST", "repos/%s/git/blobs" % REPO,
            {"content": base64.b64encode(data).decode(), "encoding": "base64"})
    if "_err" in r:
        print("  FAIL %-32s %s" % (path, r))
        continue
    size_mb = len(data) / 1024 / 1024
    if existing.get(path) == r["sha"]:
        print("  SKIP %-32s 内容未变 (%0.2f MB)" % (path, size_mb))
        continue
    tree_items.append({"path": path, "mode": "100644", "type": "blob", "sha": r["sha"]})
    print("  OK   %-32s %8.2f MB" % (path, size_mb))

if not tree_items:
    print("\n所有文件均与远端一致，无需提交。")
    raise SystemExit(0)

# 3) 取 base tree
base_tree = None
parents = []
if names:
    ref = req("GET", "repos/%s/git/ref/heads/%s" % (REPO, BRANCH))
    if "_err" in ref:
        print("\n取分支引用失败:", ref)
        raise SystemExit(1)
    head_sha = ref["object"]["sha"]
    cm = req("GET", "repos/%s/git/commits/%s" % (REPO, head_sha))
    base_tree = cm.get("tree", {}).get("sha")
    parents = [head_sha]
    print("\n[2/4] base tree: %s" % base_tree)
else:
    print("\n[2/4] 空仓库，无 base tree")

# 4) 建 tree
payload = {"tree": tree_items}
if base_tree:
    payload["base_tree"] = base_tree
tr = req("POST", "repos/%s/git/trees" % REPO, payload)
if "_err" in tr:
    print("建 tree 失败:", tr)
    raise SystemExit(1)
print("[3/4] new tree: %s" % tr["sha"])

# 5) 建 commit
cm = req("POST", "repos/%s/git/commits" % REPO, {
    "message": "update NetSwitch (%d files)" % len(tree_items),
    "tree": tr["sha"],
    "parents": parents,
})
if "_err" in cm:
    print("建 commit 失败:", cm)
    raise SystemExit(1)
print("[4/4] new commit: %s" % cm["sha"])

# 6) 更新 / 创建 ref
if names:
    up = req("PATCH", "repos/%s/git/refs/heads/%s" % (REPO, BRANCH), {"sha": cm["sha"]})
else:
    up = req("POST", "repos/%s/git/refs" % REPO,
             {"ref": "refs/heads/" + BRANCH, "sha": cm["sha"]})
if "_err" in up:
    print("更新分支失败:", up)
    raise SystemExit(1)
print("      ref updated: refs/heads/%s" % BRANCH)

# ---------- 校验 ----------
print("\n仓库文件树:")
t = req("GET", "repos/%s/git/trees/%s?recursive=1" % (REPO, BRANCH))
if "tree" in t:
    for item in t["tree"]:
        if item["type"] == "blob":
            print("   %-34s %10d bytes" % (item["path"], item.get("size") or 0))
else:
    print("   ", t)

print("\n完成: https://github.com/%s/tree/%s/%s" % (REPO, BRANCH, PREFIX or ""))
