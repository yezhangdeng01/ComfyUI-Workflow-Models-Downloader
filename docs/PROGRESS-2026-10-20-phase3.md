# WMD 插件第三阶段 — UI 中文化 + 📂 按钮归位

> 更新：2026-10-20
> 改动文件：`js/workflow-models-downloader.js`、`server.py`、`pyproject.toml`
> 全部本地未 commit（惯例：实测通过后统一提交）
> 前两阶段：本目录 `PROGRESS-2026-10-19-phase1.md` / `phase2.md`

## 一、本阶段两项改动（用户需求原话）

1. 「打开文件夹按钮放的参差不齐，放在 Directory 列后更合适」
2. 「把这个插件菜单语言全换成中文」

## 二、📂 按钮（openModelFolder）

- **根因**：原按钮只挂在 Action 列 `wmd-action-buttons` 组内、且只有「有下载链接」分支生成——无链接行没有它，位置随按钮组个数漂移，所以参差不齐。
- **改法**：
  - 移出 Action 列（主渲染 + `updateRowWithUrl` 局部刷新两处都删）；
  - Directory 列改为 `<div class="wmd-dir-cell">` = 目录下拉框 + `#wmd-dir-open-${index}` 容器（每行位置恒定）；
  - 新增 `refreshDirOpenButton(index)`，接入 `onDirectoryChange`、`recheckModelStatus` 两个状态变化点，保证换目录/下载完成后按钮即时出现或消失；
  - 新 CSS `.wmd-dir-cell`（flex 对齐 + 6px 间距）。
- **顺带修复**：文件已存在但无下载链接的行，原来看不到 📂，现在能看到。

## 三、全中文化

实现方式：批量替换脚本 `profiles\coder\cache\scratch\wmd_translate.py`（355+48 条规则，逐条断言命中数，跑前备份到 `%TEMP%\wmd_js_backup_*.js` / `wmd_server_backup_*.py`）。

覆盖范围：
- 顶栏按钮/弹窗标题/5 个页签/页脚按钮；
- 模型表全部：表头、状态文案（已存在/缺失/未完整/检查中）、按钮（下载/重新下载/模糊匹配/搜索链接/高级搜索/哈希查询/一键下载/下载全部缺失）、筛选下拉、统计卡、tooltip、alt 徽章、来源徽章（热门/管理器/缓存）；
- 下载页、本地模型页、工作流页、帮助页（FAQ 全文）、备选格式弹窗、模型详情弹窗、全部 showNotification/alert/confirm；
- ComfyUI 设置面板：7 个设置项 name+tooltip、分类路径「模型下载」、顶栏菜单按钮「模型下载」；
- `pyproject.toml` DisplayName → 「模型下载」（最终版，阶段一遗留待拍板项已落实）；
- `server.py` 回给前端的 48 条 error/message 字符串。

**未翻译（故意保留）**：品牌名 HuggingFace / CivitAI / Tavily / GitHub、技术词 URN / SHA256 / GGUF / safetensors、Manager 界面原生英文（Security Level 等，保留原文并加中文注释）、`hf-mirror.com` 等字面量、文件名/路径/URL。

## 四、📂 点击只闪任务栏不弹窗（本阶段追加修复）

- **根因**：`explorer /select,` 由后台的 ComfyUI 服务进程 spawn，Windows 11 前台锁定不允许非前台应用激活新窗口，于是资源管理器窗口被压到任务栏闪烁。（实测本机 Win11 build 26300 必现。）
- **修法**：新增 `_win_open_in_explorer(path)`，改用 Shell API `SHOpenFolderAndSelectItems`（激活经由 shell 自身，不受前台锁定限制），原 explorer /select 保留为降级路径。
- **踩坑记录（重要）**：
  1. `ILCloneFull`/`ILCloneLastID` 在 Win11 的 shell32 里**已导出不可用**（getattr 抛错），必须用经典三件套 `ILClone + ILRemoveLastID + ILFindChild`（child PIDL 指向 pidl 内部，不拥有、勿 ILFree）。
  2. aiohttp 工作线程没有 COM 初始化，直接调用返回 `0x800401f0 CO_E_NOTINITIALIZED`。函数内先 `CoInitializeEx(None, 0)`（STA），返回 S_OK 才负责对称 `CoUninitialize`；S_FALSE=已初始化不重复清；再失败重试 MTA。
- **验证**（scratch\wmd_openfolder_test.py，从 server.py 提取真实函数进程外执行）：
  ```
  real file -> True   （窗口弹出并选中文件）
  folder    -> True
  missing   -> False  （不抛异常）
  PASSED
  ```
- `py_compile` PY_OK。

## 五、文档归档约定（本阶段确定）

进度文档统一放插件仓库 `docs/`（phase1/2/3），随 git 走；`E:\Docs\Hermes\` 下那份 phase1 为历史存档副本。

## 六、下一步

1. 重启 ComfyUI 实测：界面全中文、📂 在保存目录列每行对齐、点 📂 资源管理器窗口**前台弹出并选中文件**（不再闪任务栏）、换目录后按钮刷新
2. 通过后与阶段一、二改动一起 commit（文案建议：`Fix resume & directory consistency, unify UI to Chinese, move folder button to Directory column, foreground Explorer via shell API`）
3. 按指令再 push + tag + release

（阶段二验证记录：node --check JS_OK、py_compile PY_OK、残留英文扫描无用户可见残留）

## 七、备份位置

- 改前 JS：`C:\Users\JCH\AppData\Local\Temp\wmd_js_backup_135126.js`
- 改前 server.py：`C:\Users\JCH\AppData\Local\Temp\wmd_server_backup_135126.py`
