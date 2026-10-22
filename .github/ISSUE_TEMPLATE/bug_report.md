---
name: 问题反馈
about: 报告 bug 或异常行为
title: '[BUG] '
labels: bug
assignees: ''
---

## 现象

一句话说清发生了什么。截图可以有，但**模型名请另贴文字**——截图 OCR 会把 `ref2va` 看成 `rel2va`、`taeh3` 看成 `taehi3`。

## 复现步骤

1.
2.
3.

## 期望 vs 实际

- 期望：
- 实际：

## 环境

- ComfyUI 版本：
- 操作系统：
- Python 版本：
- 浏览器：

## 日志

插件日志末尾几十行（`ComfyUI/custom_nodes/ComfyUI-Workflow-Models-Downloader/wmd.log`）：

```
粘贴在这里
```

前端相关问题请附浏览器控制台（F12）里的 `[WMD]` 输出：

```
粘贴在这里
```

## 已排除

- [ ] 改过代码后**重启过 ComfyUI**（`server.py` / 路由改动必须重启才生效）
- [ ] **Ctrl+F5 硬刷新**过浏览器（插件 JS 没有版本号查询串，普通刷新会吃缓存）
- [ ] 核对过模型文件的**完整文件名**（大小写、空格、下划线都可能让 ComfyUI 认不出）

## 工作流

与特定工作流相关时请附 JSON（可脱敏），比截图有用得多。
