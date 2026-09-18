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
files = []          # [(仓库内路径, 字节内容)]  —— 走 PREFIX 前缀
root_files = []     # [(仓库内完整路径, 字节内容)] —— 不走 PREFIX（如 .github）

# 本地真实文件（含 exe）
for local_name in ("netswitch.py", "windivert_throttle.py", "使用说明.md",
                   "NetSwitch.exe", "build_mac.sh", "create_dmg.sh",
                   "netswitch_cli.sh"):
    p = os.path.join(SRC_DIR, local_name)
    if os.path.exists(p):
        files.append((rpath(local_name), open(p, "rb").read()))
    else:
        print("  跳过（本地不存在）:", local_name)

# README / .gitignore 直接读本地文件，避免与磁盘内容不一致
for local_name in ("README.md", ".gitignore"):
    p = os.path.join(SRC_DIR, local_name)
    if os.path.exists(p):
        files.append((rpath(local_name), open(p, "rb").read()))
    else:
        print("  跳过（本地不存在）:", local_name)

# 把本脚本自身也一并归档（便于以后在新机器上同步）
try:
    files.append((rpath("upload_to_github.py"), open(os.path.abspath(__file__), "rb").read()))
except Exception:
    pass

# CI 工作流必须放在仓库根目录 .github/workflows/（不受 netswitch 前缀影响）
wf = os.path.join(SRC_DIR, ".github", "workflows", "build-mac.yml")
if os.path.exists(wf):
    root_files.append((".github/workflows/build-mac.yml", open(wf, "rb").read()))
else:
    print("  跳过（本地不存在）: .github/workflows/build-mac.yml")

# 注意：含 .github/workflows/* 的提交需要 PAT 具备 "Workflows: Read and write" 权限，
# 否则 git/trees 会 403。先单独推送源码/文档/exe，工作流待授权后另行推送。
all_files = files + (root_files if os.environ.get("NS_INCLUDE_WORKFLOW") else [])

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
for path, data in all_files:
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
