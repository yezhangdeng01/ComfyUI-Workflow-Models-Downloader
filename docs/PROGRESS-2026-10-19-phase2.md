# WMD 插件修复进度 — 第二阶段（下载中断 + 目录不一致）

> 更新：2026-10-19 深夜
> 插件：`E:\AI\ComfyUI-aki-v3.2\ComfyUI\custom_nodes\ComfyUI-Workflow-Models-Downloader`
> 第一阶段文档见会话附件《WMD-插件修复进度_2026-10-19.md》
> 本阶段改动全部本地未 commit（按惯例实测通过后统一提交）

## 一、本阶段修复的两个问题（用户原话）

1. 「大文件下载途中总是莫名中断，需要手动重启下载」
2. 「有些下载的文件夹和显示的文件夹不同，导致已经下载完成的模型下载完成后显示还要下载」

## 二、根因（日志实证）

### 问题 1：中断后放弃
- 日志：`retry 5/5 → ERROR`。`_resume_download_core` 的 `max_retries=5` 在网络频繁断流下必然耗尽
- 你的网络每次连接约只能传 ~600MB，19.5GB 文件需要 ~35 次续传，5 次重试根本不够
- 续传机制本身有效（partial 从 14.4→15.0→15.6→16.3→17.58GB 单调推进），只是重试预算太小

### 问题 2：目录判定不一致
- 日志铁证：同一分钟内
  - `Target directory: ...\models\checkpoints`
  - `Forced directory for minimax_h3_hybrid...: diffusion_models`
- 扫描侧用文件名规则猜目录（猜 checkpoints），下载侧被 `KNOWN_GOOD_SOURCES` 强制到 diffusion_models
- 结果：文件落在 diffusion_models，扫描去 checkpoints 找 → 永远「还要下载」
- `add_to_download_history` 完成后只清 `checkpoints` 的 folder cache，不清真实目录

## 三、修复内容（server.py，均已验证）

### 问题 1（下载中断）
| 改动 | 说明 |
|---|---|
| `max_retries=5 → 0` | 0 = 无限重试；循环条件从 `while attempt <= max_retries` 改为 `while True`（原条件在 max_retries=0 时只跑 1 次！首版补丁的真 bug，测试抓出来的） |
| 零进展看护 | 连续 5 次失败且 partial 零增长才放弃（死链/服务器拒绝 Range 的逃生口）；任何真实进展都会清零计数，慢链路永不放弃；前 120s 宽限期 |
| 快速重试 | 有进展时 backoff=1s（原来涨到 30s，10 次断线白等 4 分钟）；无进展才指数退避 |
| KeyError 加固 | `download_progress[download_id]` 16 处裸下标改为 `prog = download_progress.setdefault(download_id, {})`（直接测试调用时 KeyError 会打挂整次下载） |
| aria2 降级 | aria2 失败后自动切续传引擎（原本 aria2 一崩就整单报错）；无 .partial 时用 aria2 已写文件播种 |
| KG 表尺寸修正 | `19530379632 → 20970379632`（HF Content-Range 实测；差 1.4GB，影响推荐排序） |

### 问题 2（目录不一致）
| 改动 | 说明 |
|---|---|
| `resolve_model_directory()` | 新函数 = 目录判定唯一真相源：调用方显式目录 → KNOWN_GOOD_SOURCES 表 → 文件名规则 |
| 扫描侧接入 | `scan_workflow_for_models` 无提示时先查 KG 表再退文件名规则（原逻辑跳过 KG 表直接猜） |
| `check_model_exists` 全局兜底 | 新增 `_find_file_by_basename`：按文件名在所有 models 目录（含子目录）索引查找（10s 缓存），文件在任何地方都判定已安装 |
| 缓存失效修正 | `add_to_download_history` 完成后清「记录目录 + 文件实际落盘目录」两处 folder cache |

## 四、测试（7/7 全过）

`profiles\coder\cache\scratch\wmd_fix_verify.py`（真实函数级测试，非机制复刻）：

```
PASS  无限重试自动跑完（10次断线无人工干预）  ok=True err=None bytes=6291456/6291456
PASS  内容完整 + partial 已清理
PASS  resolve_model_directory 归到 diffusion_models
PASS  扫描猜 checkpoints、文件在 diffusion_models → 仍判定已安装  exists=True size=19.53 GB
PASS  不存在的文件返回 False
PASS  跨目录查找不受 folder_type 限制
PASS  有界重试可正常退出（不抛异常）
```

- 测试 1 用本地 HTTP 服务器按绝对位置每 600KB 断一次线（模拟你的 ~35 次续传场景），6MB 全量下载 md5 一致
- 测试环境：stub 了 ComfyUI `PromptServer.instance`（RouteTableDef 真实类型）；插件 server.py 与 ComfyUI 根 server.py 同名，必须用 importlib 按路径加载（别名 wmd_server），插件目录绝不能进 sys.path 前列
- 踩坑记录：`import server` 拿到的是 ComfyUI 的 server.py；`PromptServer.instance` 只有 `__init__` 才设，测试进程要手动挂

## 五、下一步

1. **重启 ComfyUI**（当前进程还在跑旧 server.py，必须重启才加载新逻辑）
2. 实测：混合版（20.97GB）下载是否能在断流后自动续传跑完；下载完成后面板是否立即显示「已安装」
3. 实测通过 → commit（「Fix unlimited resume retries, unify directory resolution between scan and download, add global basename lookup」）
4. 按指令再 push + tag + release
