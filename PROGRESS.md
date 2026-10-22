# 进度与开发笔记

> 面向要改这个插件的人。用户向文档见 [README.md](README.md)，版本记录见 [CHANGELOG.md](CHANGELOG.md)。
> **这份代码有两处**：开发库 `E:\Docs\workbuddy\ComfyUI-Workflow-Models-Downloader`（真源，带 git 历史），
> 插件目录 `E:\AI\ComfyUI-aki-v3.2\ComfyUI\custom_nodes\ComfyUI-Workflow-Models-Downloader`（ComfyUI 实机加载的运行副本）。
> 改完跑 `python deploy.py` 同步过去，流程见 [DEPLOY.md](DEPLOY.md)。
> 本文件合并自 2026-10-19 ~ 10-23 的四份 `PROGRESS-*.md`，只保留**现在仍然成立**的部分：过程性的逐条 patch 清单、临时脚本路径、当时的网速数字都已删掉。
>
> **改完请立刻 commit。** ComfyUI-Manager 更新节点时会对脏工作区先 `git stash` 再 `git pull --ff-only`，
> 快进失败还会 `git reset --hard`；未提交的改动会从工作区消失（stash 里还在，但不会自动恢复）。

---

## 一、动代码前必须知道的三件事

1. **`server.py` 改动 → 必须重启 ComfyUI 才生效**（路由在 import 时注册）。
2. **`js/` 改动 → 浏览器 `Ctrl+F5` 硬刷新**。`WEB_DIRECTORY` 加载是 `/extensions/<name>/js/xxx.js`，**没有版本号查询串**，普通刷新很可能吃缓存，表现为"改了没反应"。
3. **判断"正在跑的是哪一版"，看它自己的日志**（`wmd.log` 的 `File logging initialized` / 前端 console 的 `v1.x.x loaded`），不要看文件 mtime，也不要猜进程启动时间。

## 二、怎么验证（不打扰正在用的实例）

**永远不要重启用户正在跑的那个 ComfyUI。** 另起第二实例，用同一份 `models`（只读，安全）：

```bash
# 从 ComfyUI 根目录
python main.py --port 8189 --cpu --disable-auto-launch --dont-print-server
```

三种验证手段，按成本从低到高：

| 手段 | 适用 | 要点 |
|---|---|---|
| `py_compile` / `node --check` | 任何改动，先过这关 | 必做 |
| 离线函数级 | 后端逻辑（扫描、选源、改名、体积） | 用 `importlib` 按路径加载 `server.py`；ComfyUI 根 `server.py` 同名，**不能用 `import server`** |
| 第二实例 + 无头 Chrome CDP | 前端/端到端 | 结论只认 DOM 读数（`widget.value`、`scrollWidth`）与接口返回，不认截图肉眼 |

离线加载后端的最小骨架（`PromptServer.instance` 只有 `__init__` 才设，测试进程要手动挂）：

```python
import types, sys, importlib.util
from aiohttp import web
fake = types.ModuleType("server")
class P:
    instance = None
    def __init__(self): self.routes = web.RouteTableDef()
P.instance = P(); fake.PromptServer = P; sys.modules["server"] = fake
spec = importlib.util.spec_from_file_location("wmd", r"...\server.py")
wmd = importlib.util.module_from_spec(spec); spec.loader.exec_module(wmd)
# @routes.post 装饰器返回原函数，所以端点可以直接 await 调用：
# asyncio.run(wmd.remote_sizes(FakeReq({...})))
```

## 三、想改什么，去哪里

| 需求 | 位置 |
|---|---|
| 扫描工作流、组装表格数据 | `scan_workflow_for_models()` |
| 类型徽章 / 目标目录 | `FILENAME_TYPE_PATTERNS` → `NODE_TYPE_DIRECTORIES` → `NODE_TYPE_MODEL_LABELS`（后者最权威） |
| 判断"这个模型在不在盘上" | `check_model_exists()` → `check_model_integrity()`（含 safetensors header 校验） |
| 跨目录/跨文件名找本地文件 | `_find_file_by_basename()`、`find_local_name_variants()`、`_normalize_model_name()` |
| 解析下载来源 | `collect_source_candidates()` → `score_candidates()` → `select_reliable_source()` |
| 体积 | 本地 `_expected_model_size()`；远端 `_hf_remote_size()`（HEAD） |
| 下载/续传 | `_resume_download_core()`、aria2 分支、`invalidate_folder_cache()` |
| 前端表格 | `renderModels()` 及其配套 `buildSizeCell` / `buildOpenFolderButton` / `refreshDirOpenFolderButton` |
| 改图（换模型、改节点） | `applyModelToGraph()`、`_collectGraphNodes()` |
| 高分屏网格修复 | `js/wmd_dpr_fix.js`（删除即回滚） |

数据流一句话：**扫描（纯本地）→ 缺失项后台 `resolve-source` 并发解析 → 用户点击才下载 → 续传引擎/aria2**。

## 四、硬性约定（都是踩出来的，别推翻）

1. **扫描里不许有网络调用。** 扫描曾经因为每个缺失模型都现场查 HF 而卡 10 秒。远端信息一律走后台端点（`resolve-source` / `remote-sizes`），表先出来。
2. **类型和目录必须一致。** 文件名规则只是启发式；加载节点自己的选项列表（`folder_paths.get_filename_list()`）才是真相，冲突时以节点为准。
3. **ComfyUI 的下拉框按完整相对路径精确匹配。** 任何"换模型"的操作必须同时改 `widget.value`、补 `widget.options.values`、标记画布 dirty，否则画布上会显示成红色的"不存在"。
4. **只接受精确文件名匹配。** 上游用子串模糊匹配，把 `taeh3` 错配到 FLUX 私有库，换来 401 死循环。
5. **不删用户的文件。** 唯一会碰磁盘的操作是 `rename-variant`：同目录 `os.rename`、只允许"规范化后同名"、目标已存在直接 409、必须落在 models 目录内。
6. **不在运行时 `pip install`。** Comfy Registry 明令禁止；缺依赖只打印提示。
7. **改 `KNOWN_GOOD_SOURCES` 这类大表用小步 patch**，并且改完立刻 `py_compile`——这张表曾被一次过短的 patch 整段删掉过。

## 五、踩过的坑（现象 → 真相）

| 现象 | 真相 |
|---|---|
| 节点里模型是空的、面板说「缺失」，但文件明明在 | 文件名差一个空格/横线/大小写。ComfyUI 精确匹配路径，同一份字节也认不出 |
| 修好了却又多出一行「缺失」 | 旧文件名残留在节点记录的下载 URL 文本里，正则又捞回来了 → `_drop_satisfied_ghost_rows()` |
| 改了 widget，验证却显示没生效 | `node.widgets_values` 数组**不跟着** `widget.value` 变，必须读 `node.widgets[].value` |
| 改了模型后断言全错位 | 重扫后表格按文件名重排，不能按行索引断言，要按文件名 |
| `git push` 报 `unable to write credential store` | 凭据助手写不进去，**推送其实已成功**，用 `git ls-remote origin refs/heads/main` 核对 |
| 测后端函数"通过"了，其实没跑 | 片段里的 `NameError` 被 `except` 吞掉 → 原样返回。切片要包含被调用的前置函数 |
| 半下载的模型被判「大小不对」 | `.partial` 的当前字节数不是期望值，要读 safetensors header 声明的总长 |
| 大文件下载动辄中断 | 早先 `max_retries=5`，而弱网一次连接只能传几百 MB。现在无限重试 + 零进展看护（连续 5 次零增长才放弃） |
| aria2 每次 TLS 失败 | Windows 证书吊销检查脱机，必须 `--check-certificate=false` |
| 资源管理器只闪任务栏 | 后台进程 spawn `explorer /select` 被 Win11 前台锁压制 → 改用 Shell API `SHOpenFolderAndSelectItems`；`ILCloneFull` 在 Win11 已不可用，要用 `ILClone + ILRemoveLastID + ILFindChild` |
| 截图 OCR 认错模型名 | `ref2va`→`rel2va`、`taeh3`→`taehi3`。**涉及文件名一律读工作流 JSON 原文** |
| `wmic` 不存在、`tasklist /V` 的"启动时间"其实是 CPU 时间 | 本机环境限制，别拿它做判断依据 |

## 六、历史阶段（时间线）

- **2026-10-19**：续传（HTTP Range + `.partial`）、HF 镜像（设置 > 环境变量 > 直连）、接入 ModelScope 作为常驻第二源、`aria2` 加速、真实测速选优、同族推荐列表。配套：修掉子串匹配导致的 FLUX 401。
  结论：**换网络节点是质变**，早期"全网都慢"的判断作废；绘世/Aki 启动器不注入代理也不注入 `HF_ENDPOINT`，所以插件自己读环境变量。
- **2026-10-19 夜**：无限重试 + 零进展看护；`resolve_model_directory()` 成为目录判定的唯一真相源（此前扫描猜 `checkpoints`、下载被精选源强制到 `diffusion_models`，文件落了却永远显示"未下载"）；`_find_file_by_basename()` 做全局兜底。
- **2026-10-20**：全中文化（界面/提示/帮助/设置/`pyproject.toml` 的 DisplayName）；📂 按钮从"操作列里漂移"挪到保存目录列固定位置；Shell API 前台弹窗修复。
- **2026-10-22**：体积列（本地读 header + 后台 HEAD 探测）、类型徽章跟随节点、`applyModelToGraph()` 让"使用该版本"真正换节点、同名变体识别（徽章 + 改图/改名两条路）、📂 恢复为每行都有。
- **2026-10-23**：文档整理（四份进度合并为本文件，CHANGELOG 中英合并，CONTRIBUTING 重写为中文）；本文件由 `docs/DEV-NOTES.md` 改名为根目录 `PROGRESS.md`。

## 七、已知遗留

- 表格 7 列后在 1080px 弹窗里偏挤，超长文件名会换 3 行。没动弹窗宽度策略，觉得难看再说。
- 旧的 `docs/PROGRESS-*.md` 流水账已合并进本文件（`.gitignore` 仍忽略 `docs/PROGRESS-*.md` 这个命名，新写的临时流水别再进仓库；根目录这份 `PROGRESS.md` 是正式文档，入库）。
- `metadata/custom-node-list.json`、`extension-node-map.json` 是运行时节点数据库，**不是文档，别删**。
