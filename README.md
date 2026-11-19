Agent Console Bridge v0.2.0 发布说明
简介
Agent Console Bridge 是一个 Blender 侧栏插件 + 本地桥接服务。它让你可以用一句中文指令驱动本地 AI agent 在 Blender 里建模、改材质、调场景。模型每想一步就立刻执行一步，思考过程和执行结果实时显示在对话列表里，每一步都带独立撤销点。

项目地址：[待填写 GitHub 仓库地址]

v0.2.0 更新亮点
通用本地 agent 适配器：不再绑定 CodeBuddy CLI。通过 command_template 可接入 Claude Code、Cursor CLI 或任何能从命令行调用、按约定格式输出的本地 agent。
CodeBuddy CLI 一键安装：安装器加 --install-cli 即可调用官方安装脚本；Windows 用户也可双击 install_cli.bat。首次登录仍需用户自己完成。
避免命令行长度限制：完整提示词写入 prompt.txt 再交给 agent，彻底绕开 Windows 的命令行长度上限。
纯文本输出模式：output_format: text 支持不提供流式事件的 agent，同样能拆成多步执行。
稳定性修复：心跳改为独立线程，模型长时间生成时不再误报离线；单实例锁防止重复启动桥接。
可滚动对话面板：固定 5 行高度，最多保留 200 条消息，越用越长的问题彻底解决。
完整包结构：安装脚本、卸载脚本、Blender Extension manifest、README、MIT 许可、单元测试和 GitHub Actions CI 一应俱全。
功能一览
自然语言指令控制 Blender：建模、网格编辑、材质节点、灯光相机、动画、场景设置等
边思考边执行：模型每输出一个 # 思考: + python 代码块，Blender 立刻应用一步
实时进度流：桥接控制台和侧栏对话区都能看到模型的思考与操作
连接状态指示灯：基于每秒写入的心跳文件判断是否在线
场景上下文自动附带：选中物体、整场景物体清单、材质、集合、视口截图
每步独立 undo，改错一键 Ctrl+Z
环境要求
Blender 4.2 或更高版本（已在 5.3 上测试）
Python 3.9+（用于安装器和桥接启动器；Blender 自带 Python 也可）
一个已登录的本地 agent CLI，例如 CodeBuddy CLI；其他 agent 也可通过配置接入
Windows / macOS / Linux
快速开始
bash
复制
python install.py --install-cli    # 安装插件，并自动安装 CodeBuddy CLI（可选）
或者在 Blender 里通过 Edit > Preferences > Add-ons > Install from Disk 安装本压缩包。

首次使用：

在终端运行一次 codebuddy，完成登录（仅首次）
在 Blender 侧栏打开 Agent Console
点击 Start / Restart 启动桥接，或运行 python start_bridge.py
选中物体，输入一句中文指令，点击 Send
示例指令：

生成一个红色立方体，放在坐标 (2,0,0)，再加一个倒角修改器
把场景里所有灯光删掉
给中间的模型做一个金属质感的材质
使用其他 agent
编辑 bridge/config.json：

json
复制
{
  "agent": {
    "provider": "generic",
    "command_template": "my-agent --prompt-file \"{prompt_file}\" --system \"{system_prompt_file}\"",
    "output_format": "text",
    "stdin_mode": "none"
  }
}
只要对方 agent 按 # 思考: + python 代码块的协议输出，就能逐步执行。stream-json 模式可复用 CodeBuddy 的流式事件格式。

安全与隐私
本插件会生成并执行任意 bpy 代码，不是沙箱环境。建议只在副本或已备份的工程上使用。
场景数据、视口截图会发送给所配置的模型服务，请勿用于保密项目。
每次操作都有独立撤销点；出错时 Ctrl+Z 即可回退。
桥接拦截常见危险调用，但这是防护栏而非安全边界。
已知限制
目前适配的是命令行 agent，不直接支持 GUI-only 的 agent。
流式思考可视化需要 agent 输出兼容的流式事件；普通文本 agent 只能看到分步结果。
自然语言理解质量取决于所接 agent 的模型能力，复杂任务建议拆成多条指令。
反馈
遇到问题请提交 Issue，并附上：

操作系统、Blender 版本、agent 类型
bridge.log 的最后 50 行
result.json 和 progress.json 的内容
