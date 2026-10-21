# ComfyUI Workflow-Models-Downloader 插件修复进度

> 最后更新：2026-10-19 约 00:30
> 工作目录：`E:\AI\ComfyUI-aki-v3.2\ComfyUI\custom_nodes\ComfyUI-Workflow-Models-Downloader`
> ComfyUI：`E:\AI\ComfyUI-aki-v3.2\ComfyUI\`，python：`E:\AI\ComfyUI-aki-v3.2\python\python.exe`
> 当前分支：`main`，origin 已改为自己的仓库 `https://github.com/yezhangdeng01/ComfyUI-Workflow-Models-Downloader.git`
> **尚未 commit，尚未 push**（按 JCH 习惯：修完本地攒着，实测通过再统一推）

---

## 一、本阶段目标（用户原话归档）

1. 「下载模型总是失败而且不能续传」→ 修续传
2. 「设置里没有镜像开关」→ 改用环境变量方案（比 GUI 开关更普适）
3. 「下载速度非常慢，没有速度显示和预估剩余时间」→ 加 aria2 + 镜像 + 速度/ETA 显示
4. 「我comfy的秋叶启动器里是开了镜像开关的，好像在这里用不上？」→ 查证启动器不注入代理
5. 「如果有其它镜像的话，能都加上然后自动选择最优吗」→ 多源 + 真实测速选优
6. 「魔搭我下过模型，非常快，可加入，下载时自动选择最优」→ 接入 ModelScope
7. 「保持混合版，下载时匹配工作流模型名，没有一致名称模型可选时给推荐让用户选择」→ 精确匹配 + 推荐列表
8. 「我说加魔搭不是为这次下载，而是作为以后下载模型的一个自动选优源」→ 做成通用架构

---

## 二、已完成的代码改动（全部在本地，未提交）

### server.py（约 15 处）

| 改动 | 位置/函数 | 作用 |
|---|---|---|
| 精确匹配查找 | `lookup_url_in_model_list()` | 原先是子串模糊匹配（`filename_base in model_base or ...`），导致 taeh3 被错配到 `black-forest-labs/FLUX.1-schnell/resolve/main/ae.safetensors`（gated 库 → 401 死循环）。改成仅精确匹配 |
| 精选源表 | `KNOWN_GOOD_SOURCES`（约 780 行起） | 11 条已验证直链，优先级最高。键为 lowercase basename。hybrid 两条带 `size: 19530379632` |
| 续传下载核心 | `_resume_download_core(url, dest_path, download_id, headers, max_retries=5)` | `.partial` + HTTP Range 续传 + 指数退避重试 + 可取消。已接入三个下载路径（`_download_model_thread`、`_download_from_url_thread`、队列下载） |
| 镜像重写 | `apply_hf_mirror()` + `_hf_mirror_endpoint_from_env()` + `_normalize_endpoint()` | 优先级：插件设置 `use_hf_mirror`/`hf_mirror_domain` → 环境变量 `HF_ENDPOINT`/`HF_MIRROR`/`HF_HUB_ENDPOINT` → 原 URL。aria2 命令、HF API 搜索 URL 都套用 |
| 速度/ETA | `_resume_download_core` 进度块 | 2 秒窗口算 `speed_bps` 和 `avg_speed_bps`，经 `/workflow-models/progress` 推送 |
| 设置接口 | GET/POST `/workflow-models/settings` | 新增 `use_hf_mirror`、`hf_mirror_domain`（GET 时掩码） |
| aria2 命令 | `_download_with_aria2` 相关 | **加了 `--check-certificate=false`**。不加会因 Windows 证书吊销检查脱机导致每次 TLS 失败（实测撞到） |
| 魔搭源（**本阶段核心新增**） | `MODELSCOPE_FILE_API` / `MODELSCOPE_RESOLVE` / `MODELSCOPE_REPO_HINTS` / `_modelscope_headers` / `_modelscope_list_dir` / `modelscope_list_files` / `_modelscope_file_to_dir` / `search_modelscope` | 约 860–1020 行。递归列举魔搭仓库文件（内存缓存），**仅精确文件名匹配**，路径→ComfyUI 目录映射表覆盖 20 种 |
| 测速选优 | `probe_url_speed()` / `collect_source_candidates()` / `pick_best_candidate()` / `find_model_url_with_sources()` | 1049–1118 行。拉 1MB 实测速度，按 MB/s 降序，选最快。候选不足或全失败返回 None |
| 推荐列表 | `suggest_alternative_models()` + `_name_tokens()` + `_SUGGEST_IGNORE_TOKENS` | 1121–1213 行。token 重叠匹配同族，忽略量化/包装词；排序 = 相似度 → 覆盖度 → **体积接近度** |
| 接口 | POST `/workflow-models/source-candidates`、POST `/workflow-models/suggest-alternatives` | 2160–2220 行。无精确匹配时 `best=null` + `suggestions` 列表 |

### js/workflow-models-downloader.js

- 注册原生设置项：`UseHuggingFaceMirror`（checkbox）+ `HuggingFaceMirrorDomain`（text），在 `MaxParallelDownloads` 之后 → ComfyUI **Settings 对话框**里，不是插件面板内
- 速度/ETA 渲染：`formatSpeed()` / `formatDuration()`，Downloads 页和主面板行都显示 `· x.xx MB/s` + `· 剩余 xx`
- 推荐 UI：`buildSuggestionsInfoIcon()`（3081 行）、`addSuggestionsInfoIcon()`（2790 行）、`showAlternatives()`（2547 行），读 `metadata.suggestions`

### 其他文件

- `model_metadata.json`：清掉 6 条指向 FLUX.1-schnell 的污染缓存
- `pyproject.toml`：9 处身份改为 `yezhangdeng01`（PublisherId、DisplayName、URL、icon）
- `README.md`：badge 和 clone URL 改为 fork
- `.circleci/config.yml`：删除（git 已记录 D 状态）

---

## 三、实测验证记录

### 已通过的测试

| 测试 | 内容 | 结果 |
|---|---|---|
| `wmd_fix_test.py` | 真实下载 taeh3（22,709,752 B），3MB 和 7MB 两处模拟断线，Range 续传，md5/大小校验 | **PASSED** |
| `wmd_hf_mirror_test2.py` | 7 场景：env 重写、裸域名补 scheme、设置优先于 env、空环境穿透、Civitai 不受影响、镜像真实 Range GET | 全部 exit 0 |
| aria2 实跑 | `hf-mirror.com` + `--continue=true --split=4 --check-certificate=false` | exit 0，实测 CN:1 |
| 混合版保持 HF | `collect_source_candidates("minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors","diffusion_models")` | `['known_good']` 唯一起源 |
| 魔搭命中 | 3 个文件名实测：vae_int8_convrot → HF+魔搭双候选；controlnet_union_2.0_pruned_bf16 / ref2va_pruned_w6a8 → 仅魔搭 | PASS |
| 真实测速选优 | `find_model_url_with_sources(auto_select=True)` 对 vae_int8_convrot | 魔搭 2.13 MB/s 胜出被选中 |
| 推荐列表 | 混合版无同名 → 12 个同族推荐，Top4 为 fp8_scaled/int8_convrot(~20.9GB)、w6a8(15.98GB)，体积最接近 19.53GB 排前 | PASS |
| py_compile / node --check | server.py、js | PY_OK / JS_OK |

### 网速结论（重要，影响后续判断）

| 时点 | 环境 | 结果 |
|---|---|---|
| 约 21:30 | 旧节点 | HF/镜像/代理全部 **0.09–0.13 MB/s**，19.5GB 需 ~45 小时 |
| 约 23:00 | **用户换了节点** | 原站 8.27 / 镜像 8.13 / 走代理 9.91 / 大文件 12.92 MB/s |

**换节点是质变，不要再用早期「全网都慢」的结论。**

---

## 四、关键事实与踩过的坑（下次别再踩）

1. **绘世启动器不注入代理、不注入 HF_ENDPOINT**。反编译 `.launcher/StableDiffusionWebUILauncher.dll` 确认无 `proxy_model_download`/`HF_ENDPOINT`/`HTTP_PROXY` 任何逻辑。`preference.json` 里那些开关是启动器自用（pip/git 换源）。用户说"启动器不可能有代理"是对的。
2. **HF 原站和 hf-mirror 落地同一个 CDN**。跟踪重定向：`huggingface.co` 和 `hf-mirror.com` 最终都到 `https://us.aws.cdn.hf.co/xet-bridge-us/...`（Xet 存储）。"镜像"只是换入口域名，落地一样。所以同速同命是必然，不是配置错。
3. **Clash Mi 的配置是订阅生成 + 内核 patch 合成**，直接改 `service_core_runtime_profile.yaml` 会被订阅更新覆盖。正确改法是 profilePatchs 覆写。当前 HF 无专项规则，掉到 `MATCH → 🐟漏网之鱼 → 🚀节点选择 → ♻️自动选择`，实测已能拿到 9.91 MB/s（走代理），**不需要改规则**。用户要求"讨论出结论前不动代理"——全程未改任何 Clash 文件和设置。
4. **aria2 必须加 `--check-certificate=false`**，否则本机证书吊销检查脱机导致 TLS 握手失败。
5. **aria2 多连接对 hf-mirror 无效**：实测 `CN:1 DL:115KiB`，服务端只给单连接。瓶颈在网络，不在下载器。
6. **Windows PATH 传播**：新加的用户 PATH 只对新进程生效。aria2 在 `C:\Users\JCH\bin\aria2c.exe`（从 `.launcher/aria2c.exe` 复制），ComfyUI 必须重启才认。
7. **视觉识别的 OCR 坑（反复栽过）**：`ref2va` 被看成 `rel2va`、`taeh3` 看成 `taehi3`、`dareties` 看成 `darelites`。**涉及文件名一律去读 workflow JSON 原文，不要信截图 OCR。**
8. **切片测 server.py 函数时的 NameError 会被 except 吞掉**，导致原样返回、测试假通过。切片段必须包含被调用的前置函数。
9. **`KNOWN_GOOD_SOURCES` 曾经被一次太短的 old_string patch 整段删掉**，已恢复。改这张表务必用小步 patch 并 `py_compile`。

---

## 五、当前状态快照

### 模型下载现状（用户重启后实测）

- 14 个 workflow 模型中，**3 个已能正常下载且不再中断**（taeh3、video_vae_int8_convrot、hybrid）
- 剩余 11 个：已全部找到并验证 URL
- `models/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors` 只下了 108MB（中断的 partial，约 20.97GB 总量）
- `.partial` 续传文件都在，断点不丢
- ComfyUI 进程（python.exe）可能仍在跑**旧版 server.py**，需重启才加载全部新逻辑

### 魔搭 Inventory（`Comfy-Org/MiniMax-H3`，525.19GB，44 文件）

| 目录 | 关键文件 |
|---|---|
| `diffusion_models` | fl2va/ref2va 各 6 档：bf16(66.28GB)、int8_convrot(34.04GB)、pruned_bf16(40.23GB)、pruned_fp8_scaled(20.96GB)、pruned_int8_convrot(20.97GB)、**pruned_w6a8(15.98GB)** |
| `vae` | audio_vae_fp32(605MB)、video_vae_fp16(5.21GB)、video_vae_int8_convrot(2.81GB) |
| `text_encoders` | qwen3vl_32b：bf16(51.5GB)、int8_convrot(27.1GB)、nvfp4_awq(15.7GB) |
| `loras` | fl2v_turbo_4step/8step、ref2v_turbo_4step（均 1.96GB） |
| `embeddings` | 10 个 minimaxh3_*.safetensors（0.5–1.5MB）— **HF 上没有，魔搭独有** |
| `model_patches` | fun_controlnet_union 2.0/1.0 的 bf16 和 int8_convrot（4 个） |

魔搭下载 URL 格式：`https://modelscope.cn/models/{repo}/resolve/master/{path}`
文件列举 API（实测可用）：`https://modelscope.cn/api/v1/models/{repo}/repo/files?Revision=master[&Root=dir]`

---

## 六、下一步（等用户实测后执行）

1. **等用户重启 ComfyUI 并实测**：14 个模型扫描结果、下载是否带速度/ETA、能否续传
2. **用户实测通过后**：`git add` + commit（文案如「Fix resume, add HF mirror env support, add ModelScope source with auto-select, add speed/ETA display」）**先不 push**
3. **之后按用户指令才 push + tag + release**
4. 待用户决策项：
   - 魔搭上的 19.5GB 混合版没有原文件，只有官方分体版。用户若想改用官方版（w6a8 15.98GB 最小），需要确认 workflow 支持
   - `minimax_h3_latent_upscaler_3d_fp16_V2`、5 个社区 LoRA、2 个 Civitai 模型**魔搭上没有**，只能走 HF/Civitai（现在 10–13 MB/s 可接受）
   - Registry 发布前置（用户操作）：registry.comfy.org 创建 Publisher `yezhangdeng01` → 生成 Registry Access Token → 加为 GitHub repo secret `REGISTRY_ACCESS_TOKEN`
   - DisplayName 用户已定「**模型下载**」，待最终落进 pyproject.toml

---

## 七、相关文件位置

| 用途 | 路径 |
|---|---|
| 测试脚本 | `profiles\coder\cache\scratch\wmd_fix_test.py`、`wmd_hf_mirror_test2.py` |
| URL 验证证据 | `profiles\coder\minimax_h3_3models_downloads.md`、`minimax_h3_6models_downloads.md` |
| 魔搭文件清单 | （本次 agent_helpers 工作区 `ms_files.json`） |
| workflow | `E:\AI\ComfyUI-aki-v3.2\ComfyUI\user\default\workflows\minimaxH3\YZ金鱼-MiniMax+H3+终极篇-文武双全.json` |
| aria2 | `C:\Users\JCH\bin\aria2c.exe`（已在用户 PATH） |
| 备份 | `_backup_20261018/` 已按用户确认删除 |
