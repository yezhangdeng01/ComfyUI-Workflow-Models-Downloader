## 这个 PR 做了什么

一句话说清。相关 issue：#

## 类型

- [ ] 修 bug
- [ ] 新功能
- [ ] 破坏性变更（说明影响）
- [ ] 只改文档

## 改动点

-
-

## 怎么验证的

- ComfyUI 版本 / 系统：
- [ ] `py_compile server.py` 通过
- [ ] `node --check js/workflow-models-downloader.js` 通过
- [ ] 在**第二实例**上实测（`--port 8189`，没有重启用户正在用的实例）
- 用的工作流：

## 自检

- [ ] 没有往扫描路径里加网络调用
- [ ] 没有删用户文件；如动了磁盘，只做了 `rename-variant` 式的同目录改名
- [ ] 改了前端就补了 `widget.options.values` 并标记画布 dirty
- [ ] 同步了 README / CHANGELOG
- [ ] 没引入新的警告或报错

> 约定细节见 [`docs/DEV-NOTES.md`](docs/DEV-NOTES.md)。
