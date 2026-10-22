# ComfyUI 工作流模型管理

> 英文名：ComfyUI Workflow Model Manager
> ComfyUI Registry node id：`comfyui-workflow-model-manager`

[![ComfyUI Registry](https://img.shields.io/badge/ComfyUI-Registry-green)](https://registry.comfy.org/publishers/yezhangdeng/nodes/comfyui-workflow-model-manager)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![GitHub stars](https://img.shields.io/github/stars/yezhangdeng01/ComfyUI-Workflow-Models-Downloader.svg)](https://github.com/yezhangdeng01/ComfyUI-Workflow-Models-Downloader/stargazers)

自动扫描工作流里引用的模型，从 **HuggingFace、ModelScope（魔搭）、CivitAI** 等来源解析出可用下载地址，实测测速选出最快的源，并把缺失的模型下载到 ComfyUI 真正会去读的目录里。

**界面为中文，定位是上游节点的中文优化版。**

> 想直接用 → 往下看「安装」「使用」；想改代码 → [`docs/DEV-NOTES.md`](docs/DEV-NOTES.md)。

---

## 这是什么：上游版本的两个致命问题

本项目 fork 自 [slahiri/ComfyUI-Workflow-Models-Downloader](https://github.com/slahiri/ComfyUI-Workflow-Models-Downloader)（MIT 协议，原作者 Siddhartha Lahiri）。

上游 1.8.1 有两个在真实使用中几乎让人放弃的问题：

### 1. 扫不出模型 —— 搜不到来源

工作流里明明写着 `moss\qwen3.5_9b_int8_convrot.safetensors`，插件却报告"未找到来源"。原因：

- 带**子目录前缀**的工作流引用（`moss\xxx.safetensors`）查表时匹配不上
- 同一个模型在表格里**出现两行**，其中一行是"幽灵行"，目标目录还判错成 `checkpoints`
- 缺失模型的来源解析**完全依赖用户手动点"搜索"**，插件自己不会去后台找

结果就是：扫出来一堆"缺链接 / 未知"，用户只能一个一个手动搜。对新手来说基本等于不可用。

### 2. 下载不能续传 —— 大文件从头再来

下载一断，缓冲区被丢弃，**从 0 开始重下**。9GB 的模型断一次就是 9GB 的流量和耐心。加上进度循环里还有一个 `NameError`（`progress_window` 未定义），会让**每一次下载都异常中断并陷入无限重试**。

对一个主要用途就是下大模型的插件来说，这两点是致命的。

---

## 我们做了什么改进

### 搜索：自动找源，且能找到

| 改进 | 说明 |
|---|---|
| **后台自动解析来源** | 扫出缺失模型后自动在后台并发查询 HuggingFace 候选，**不需要用户手动点搜索** |
| **带目录前缀的引用能查表** | `moss\xxx.safetensors` 这类引用现在能正确匹配到模型 |
| **消除幽灵行** | 同一模型不再显示两行，之前那条目录判错的"重复行"已移除 |
| **来源依据可见** | 每个模型标注 `✓ 已校验`（客观校验通过）或 `≈ 最佳猜测`（只是最佳候选），并展示判定依据：远端文件大小匹配、目标目录匹配、官方发布者命名空间 |
| **解析结果持久化** | 选定的来源跨重启保留，重新扫描不再显示"无链接" |
| **可手动覆盖** | 任何模型都可以用自己的 URL 覆盖自动选择 |
| **扫描纯本地** | 扫描不再联网，模型表格**几乎瞬间出现**（上游冷启动要等约 10 秒） |

实测：扫描 10.01s → **0.071s**（热缓存 0.008s）；单模型找源 3.14s → 候选全量 3.12s。

### 续传：真的能续

| 改进 | 说明 |
|---|---|
| **断点续传保留每个字节** | 下载中断不再丢弃缓冲，从断点继续，而不是从头再来 |
| **修掉中断下载的 NameError** | 删除了导致每次下载异常中断、陷入无限重试的 `progress_window` 未定义错误 |
| **按 safetensors header 校验大小** | 期望大小从文件头读取，半下载状态也能正确比对，不会因为"当前大小"误判 |
| **正确的目标目录** | 模型被保存到**加载节点实际读取的 ComfyUI 目录**（如 `diffusion_models/`、`text_encoders/`），不是拍脑袋猜的分类 |

实测：9.75GB 的 qwen 模型从 61% 平滑续传到 100%，最终字节数与 safetensors header 推导值完全一致。

### 表格：先看清代价再动手

| 改进 | 说明 |
|---|---|
| **体积列** | 每个模型标出大小，下载前就知道是 644MB 还是 20GB。在盘上的读 safetensors header 声明的总字节（截断的下载也准）；缺失的由后台 HEAD 探测，前面带 `≈` 表示"这是服务器报的体积" |
| **备选真的会切换节点** | 备选弹窗里的「使用该版本」直接改写用着该文件的加载节点（含子图内部），同步下拉框选项与工作流元数据——不是把文件名丢给你自己去粘贴 |
| **同名不同写能认出来** | 磁盘上是 `Qwen- image-2.1_int8_convrot.safetensors` 而工作流要 `qwen_image_2.1_int8_convrot.safetensors`（**同一份字节**）时，ComfyUI 的下拉框按完整路径精确匹配，节点里就是空的。这类行会带一枚「本地已有·名字不同」徽章：可以让节点改用它（只动图），也可以原地改名（不复制、不下载） |
| **📂 每行都有** | 已在盘上的打开并选中文件；缺失的打开"该落到哪个目录"，方便先看看再决定 |

### 网络适应：多源测速选优

国内直连 HuggingFace 不稳，这一层做了四件事（实现细节见 [`docs/DEV-NOTES.md`](docs/DEV-NOTES.md)）：

| 能力 | 说明 |
|---|---|
| **ModelScope（魔搭）常驻第二源** | 不是为某个工作流临时加的：内置精选源表 → 魔搭 → HuggingFace / CivitAI 依次尝试。**只接受精确文件名匹配**，不猜相似名；路径→目录映射覆盖 23 种模型类型 |
| **HuggingFace 镜像** | 设置里可开关，也读环境变量 `HF_ENDPOINT` / `HF_MIRROR` / `HF_HUB_ENDPOINT`（优先级：插件设置 > 环境变量 > 直连）。同时作用于来源搜索与实际下载，存的仍是规范 URL |
| **aria2 加速** | 检测到 `aria2c` 自动启用（4 连接分片 + 续传），未安装则回落 Python 下载；下载面板实时显示速度与剩余时间 |
| **真实测速选优** | 每个候选实际拉 1MB 计时，按速度降序取最快；全部失败时给出同族推荐（相似度 → 覆盖度 → 体积接近度） |

实测：同一模型魔搭 2.13 MB/s 胜出，测速后自动被选中。

> ⚠️ Windows 上 `curl` 直连镜像可能报 `CRYPT_E_REVOCATION_OFFLINE`，那是 schannel 的证书吊销检查问题，不是镜像挂了。插件用 `requests` 不受影响，aria2 走 `--check-certificate=false`。

### 高分屏画布错位修复

Windows 显示缩放 125% + 浏览器缩放 90%/100% 时，ComfyUI 画布会出现**节点与背景网格错位、画布缩在一角**。

根因不在前端，而在 `comfyui-mixlab-nodes`：它覆盖了 `LGraphCanvas.drawBackCanvas`，用"在压缩后的源码里检索 `window.devicePixelRatio` 字面量"这种脆弱方式探测设备像素比。新版前端压缩后该字面量已不存在，于是背景网格被**永久锁定为 1 倍绘制**，而节点按真实 DPR 渲染。

插件内置的 web 扩展会修正背景画布的缩放倍率，使其与前景一致，并跟随浏览器缩放实时变化——**不需要改 MixLab，也不需要改前端包**。删除 `js/wmd_dpr_fix.js` 即可完全回滚。

### 其他

- **中文界面**：界面、提示、帮助页全部中文
- **注册表合规**：移除运行时 `pip install`（Registry 禁止），依赖由 ComfyUI-Manager 统一安装，缺依赖只提示不擅自安装

---

## 安装

### 方式一：ComfyUI-Manager（推荐）

在 ComfyUI-Manager 中搜索 **"Workflow Models Downloader"** 或 **"工作流模型管理"** 并安装。

> 本节点在 Registry 上的发布者为 [`@yezhangdeng`](https://registry.comfy.org/publishers/yezhangdeng)，node id 为 `comfyui-workflow-model-manager`。
> 上游原版（`slahiri` / `comfyui-workflow-models-downloader`）仍独立存在，继续服务 `1.8.1`。

### 方式二：手动安装

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/yezhangdeng01/ComfyUI-Workflow-Models-Downloader.git
pip install -r ComfyUI-Workflow-Models-Downloader/requirements.txt
```

安装后重启 ComfyUI。

---

## 使用

1. 在 ComfyUI 中打开任意工作流
2. 点击顶部菜单的 **「工作流模型管理」** 按钮
3. 看表格：每个模型带着**类型 / 大小 / 该存到哪 / 状态 / 来源**
4. 决定要下的，点 **下载**；全都要，点 **下载全部缺失**

### 「本地已有·名字不同」是什么意思

你磁盘上有这个模型，但文件名跟工作流写的不完全一致（多一个空格、横线换成下划线、多个 `_v1`）。ComfyUI 的模型下拉框按完整路径精确匹配，所以**节点里是空的，面板也报缺失**——但字节是一样的，不必重下。

点这枚蓝色徽章，二选一：

| 选项 | 效果 | 什么时候用 |
|---|---|---|
| **让节点改用它** | 只改工作流里的节点指向，不动磁盘 | 想保留现有文件名 |
| **重命名为标准名** | 同目录改名（`os.rename`，不复制不下载） | 想让工作流原样就能跑、方便分享 |

### 找不到来源的模型

- 插件会**自动在后台解析来源**，稍等片刻即可看到结果
- 标注 `≈ 最佳猜测` 的表示尚未通过客观校验，可点 **模糊匹配** 找相似模型，或 **高级搜索**（Tavily）扩大范围
- 仍然拿不到时，在 **工作流** 页签可直接粘贴来源链接覆盖

### 直链下载

1. 进入 **下载** 页签
2. 粘贴模型 URL（HuggingFace、CivitAI 或直链）
3. 点 **检查链接** 自动识别文件名与类型
4. 按需调整文件名 / 目录
5. 点 **下载**

---

## 界面说明

| 页签 | 用途 |
|---|---|
| **工作流模型** | 扫描当前工作流所需模型，显示大小、状态、来源与依据 |
| **下载** | 直链下载、进行中的任务、历史记录 |
| **本地模型** | 按目录浏览已安装模型，查看路径、修改时间、删除 |
| **工作流** | 三栏布局：工作流列表 → 该工作流的模型 → 模型详情与来源管理 |
| **帮助** | 内置 FAQ 与排障说明 |

---

## 支持的模型类型

Checkpoints（SD1.5 / SDXL / SD3 / Flux）、Diffusion Models（Wan / LTX / HunyuanVideo / MOSS…）、VAE、LoRA / LoHa / LoKr、Text Encoders（T5 / CLIP / Qwen）、CLIP Vision、ControlNet、IP-Adapter、Upscalers（ESRGAN 等）、SAM / SAM2，以及按扩展名扫描到的其它 `.safetensors` / `.ckpt` / `.pt` / `.gguf` 模型。

---

## 环境要求

- ComfyUI
- Python 3.10+
- Python 依赖见 [`requirements.txt`](requirements.txt)，由 ComfyUI-Manager 负责安装
- 可选：[aria2](https://aria2.github.io/) —— 约 4 倍速下载，支持自动续传

---

## 设置项

在 ComfyUI **原生设置面板** →「工作流模型管理」中配置（共 7 项，改完重启 ComfyUI 生效）：

| 设置项 | 用途 | 说明 |
|---|---|---|
| **HuggingFace 令牌** | 下载门控模型（Flux、SD3 等）**必需** | [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) |
| **CivitAI API 密钥** | 下载 CivitAI 模型**必需** | [civitai.com/user/account](https://civitai.com/user/account) |
| **Tavily API 密钥** | 可选，开启 AI 高级搜索 | [tavily.com](https://tavily.com) |
| **启用镜像** | 可选，大陆网络直连 HF 不稳时开启 | 改写 HF API 与下载请求到镜像域名 |
| **镜像域名** | 镜像地址 | 默认 `hf-mirror.com` |
| **最大并行下载数** | 同时下载几个模型 | 默认 8 |
| **启用高级搜索** | 开启 Tavily 搜索 | 默认开启 |

---

## 后续维护

这是一个**持续维护的中文优化分支**。使用中遇到问题会继续修复更新。

| 想看什么 | 去哪 |
|---|---|
| 每个版本改了什么 | [`CHANGELOG.md`](CHANGELOG.md) |
| 自己改这个插件 | [`docs/DEV-NOTES.md`](docs/DEV-NOTES.md)（架构地图、硬性约定、踩坑清单、怎么验证） |
| 参与贡献 | [`CONTRIBUTING.md`](CONTRIBUTING.md) |

## 致谢与许可

- 上游作者：[slahiri/ComfyUI-Workflow-Models-Downloader](https://github.com/slahiri/ComfyUI-Workflow-Models-Downloader) —— MIT 协议，原版权声明保留
- 本分支改进与维护：[yezhangdeng01](https://github.com/yezhangdeng01)

MIT License
