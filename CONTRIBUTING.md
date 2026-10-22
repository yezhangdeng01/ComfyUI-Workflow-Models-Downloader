# 参与贡献

这是 [slahiri/ComfyUI-Workflow-Models-Downloader](https://github.com/slahiri/ComfyUI-Workflow-Models-Downloader) 的中文优化分支。欢迎 issue 和 PR，麻烦先花两分钟看完下面几条。

## 报告问题

在 [Issues](https://github.com/yezhangdeng01/ComfyUI-Workflow-Models-Downloader/issues) 里提，请带上：

- ComfyUI 版本、操作系统、Python 版本
- 复现步骤（哪个工作流、点了什么按钮）
- **相关日志**：`ComfyUI/custom_nodes/ComfyUI-Workflow-Models-Downloader/wmd.log` 末尾几十行；前端问题请附浏览器控制台（F12）里的 `[WMD]` 输出
- 出问题前**是否重启过 ComfyUI**、**是否 Ctrl+F5 过浏览器**——这两条能排掉一大半"改了没反应"

问题和工作流相关时，附上工作流 JSON（可脱敏）比截图有用得多：截图里的模型名会被 OCR 认错，我们坚持读 JSON 原文。

## 提交代码

```bash
git checkout -b fix/your-topic
# 改完先自测（见下）
git commit -m "fix: 简述改了什么"
```

- 一个 PR 一件事
- commit 首行用动词开头，中文即可：`feat:` / `fix:` / `docs:` / `chore:`
- UI 改动请附改前/改后截图，或贴 DOM 读数

## 改动前必读

[`docs/DEV-NOTES.md`](docs/DEV-NOTES.md) 里有一节**硬性约定**，都是踩坑换来的，别推翻。最要紧的四条：

1. **扫描里不许有网络调用**——表要先出来，远端信息走后台端点
2. **类型和目录必须一致**——文件名规则只是启发式，加载节点自己的选项列表才是真相
3. **ComfyUI 的下拉框按完整相对路径精确匹配**——换模型必须同时改 `widget.value`、补 `options.values`、标记画布 dirty
4. **不删用户文件**——唯一的磁盘写操作是 `rename-variant`，且只允许"规范化后同名"

## 本地验证

```bash
python -m py_compile server.py            # 后端
node --check js/workflow-models-downloader.js   # 前端
```

端到端**不要重启你自己正在用的 ComfyUI**，另起第二实例：

```bash
cd ComfyUI
python main.py --port 8189 --cpu --disable-auto-launch
```

前端改动建议用无头浏览器读 DOM 判据（`widget.value`、`scrollWidth`），而不是看截图猜。完整方法（含离线加载 `server.py` 的最小骨架）在 [`docs/DEV-NOTES.md`](docs/DEV-NOTES.md) 第二节。

## 新增模型来源

1. 在 `server.py` 的 `collect_source_candidates()` 里接上候选
2. 已知可靠的直链优先写进 `KNOWN_GOOD_SOURCES`（键为小写文件名，务必小步 patch 并 `py_compile`——这张表被误删过）
3. 用真实工作流验证：扫描 → 解析来源 → 下载 → 落盘目录正确
4. 同步 README 的「网络适应」与 CHANGELOG

## 行为准则

就事论事，别攻击人。技术分歧拿日志和工作流说话。

---

Thanks for contributing.
