# 部署与发版

这个项目有两份，**改代码只改第一份**，第二份是 ComfyUI 实际加载的运行副本。

| 角色 | 路径 |
|---|---|
| 开发库（唯一真源，带 git 历史） | `E:\Docs\workbuddy\ComfyUI-Workflow-Models-Downloader` |
| 插件目录（ComfyUI 实机加载） | `E:\AI\ComfyUI-aki-v3.2\ComfyUI\custom_nodes\ComfyUI-Workflow-Models-Downloader` |
| 远端仓库 | https://github.com/yezhangdeng01/ComfyUI-Workflow-Models-Downloader |

开发库放在 workbuddy 目录里，是为了不丢进度——插件目录会被 ComfyUI-Manager 更新、被重装覆盖，开发库不会。

## 一轮完整流程

### 1. 改

在开发库里改 `server.py` / `js/*.js`。改代码前先看 [PROGRESS.md](PROGRESS.md) 的「动代码前必须知道的三件事」和「硬性约定」。

### 2. 部署到插件

```bash
cd E:\Docs\workbuddy\ComfyUI-Workflow-Models-Downloader
python deploy.py            # 先看会改哪些文件，确认后再执行
python deploy.py --dry-run  # 只报告，不写盘
```

`deploy.py` 只复制代码和文档，**不碰** `.git/`、`__pycache__/`、`_backup_*/`，也不碰插件自己生成的运行数据（`model_metadata.json`、`download_history.json`、`usage_cache.json`、`wmd.log` 等）——那些复制过去会覆盖用户的扫描缓存和下载历史。

### 3. 验证

```bash
# 从 ComfyUI 根目录另起第二实例，不打扰正在用的那个
python main.py --port 8189 --cpu --disable-auto-launch --dont-print-server
```

- 改 `server.py` → **必须重启 ComfyUI**（路由在 import 时注册）
- 改 `js/` → 浏览器 **Ctrl+F5** 硬刷新（没有版本号查询串，普通刷新会吃缓存）
- 前端结论只认 DOM 读数（`widget.value`、`scrollWidth`）和接口返回，不认截图肉眼

详细验证手法见 [PROGRESS.md](PROGRESS.md) 第二节。

### 4. 发版

版本号要改**三个地方**，漏一个就会出现「界面显示 1.10、Registry 是 1.11」：

1. `pyproject.toml` 的 `version`
2. `js/workflow-models-downloader.js` 里的 `const VERSION`
3. `CHANGELOG.md` 新增一段

```bash
git add -A
git commit -m "feat: ..."
git push
git tag v1.11.1 && git push origin v1.11.1
gh release create v1.11.1 --title "工作流模型管理 v1.11.1" --notes-file -   # heredoc 传 notes
gh workflow run publish.yml -f version=1.11.1                              # 推 Comfy Registry
```

Registry 拉取成功后版本状态是 `NodeVersionStatusPending`，等 Comfy-Org 审核变 `Approved` 属常态。

## 一个必须知道的风险

ComfyUI-Manager 更新插件时（`manager_core.py` 的 `git_pull`）：工作区脏 → `git stash`（不自动恢复）→ `git pull --ff-only` → 快进失败则 `git reset --hard origin/main`。

也就是说 **deploy 进去的改动会被抹回远端版本**。这不是故障：从开发库重新跑一次 `deploy.py` 就回来了。同理，开发库里的提交**一定要 push**——本地未推的提交遇到 reset 会从工作区消失（reflog 能救，但没必要）。
