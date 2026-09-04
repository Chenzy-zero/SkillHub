# 扫描器官方制品

本目录保存扫描节点无法直接访问 GitHub 时仍需安装的官方发布制品。

## SkillSpector 2.5.1

- 文件：`skillspector-2.5.1-py3-none-any.whl`
- 来源：NVIDIA SkillSpector 官方 `v2.5.1` GitHub Release
- 官方下载地址：<https://github.com/NVIDIA/SkillSpector/releases/download/v2.5.1/skillspector-2.5.1-py3-none-any.whl>
- SHA-256：`56196f2f8689cc6e7f565181f06db5e489ba010ef0e5da19855d99043a5f6415`
- 文件类型：Python 3 通用 wheel；其 `yara-python` 依赖目前使整套扫描环境限定为 Python 3.12 或 3.13
- 许可证：Apache-2.0，许可文本已包含在 wheel 内

安装器在使用前会核对 SHA-256；文件丢失或内容变化时安装会立即停止。

### 运行依赖清单

- `skillspector-runtime.in`：从 NVIDIA v2.5.1 官方 wheel 元数据提取的实际扫描运行依赖范围
- 解析来源：只使用当前配置的公司 pip 源，不使用公网解析结果固定公司源中不存在的传递版本

官方 wheel 将 `langgraph-cli[inmem]` 声明成必装项，但 SkillSpector 扫描代码没有引用
`langgraph_cli`。该组件用于 LangGraph Studio 开发服务，并会额外引入 Windows 无 wheel 的
`forbiddenfruit`。安装器保持官方 SkillSpector wheel 原文件不变，先安装并校验上述真实运行依赖，
再以 `--no-deps` 安装官方 wheel，因此不会下载 `langgraph-cli`、`blockbuster` 或
`forbiddenfruit`。如果未来官方 wheel 的运行依赖或代码引用发生变化，安装器会停止并要求重新审核清单。

## Python 3.13.15 Windows 64 位

- 文件：`python-3.13.15-amd64.exe`
- 来源：Python.org 官方 Python 3.13.15 Release
- 官方下载地址：<https://www.python.org/ftp/python/3.13.15/python-3.13.15-amd64.exe>
- SHA-256：`edec09c4853aeae9ac36efb8c9f95b6b8e2fee65eee56d9767a8b7c69c574403`
- 用途：当 Windows 机器只有 Python 3.14 时，为 SkillSpector 提供项目专用的 3.13 运行环境
- 安装边界：仅当前用户，目标为 `.scanner-tools/_python313`；不添加 PATH、不更改文件关联、不安装新启动器或快捷方式
- 许可证：Python Software Foundation License

本制品同样在使用前校验 SHA-256；非 Windows 64 位环境不会执行它。
