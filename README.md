# Simple Coding Agent (SCA) 🧠💻

Simple Coding Agent (SCA) 是一个基于纯净 ReAct 架构的本地终端编码辅助智能体。它拒绝臃肿的重型框架，完全由原生 Python 构建。通过极其精简的四大底层工具（读、写、改、执行），让大语言模型（如 DeepSeek、GPT-4）直接在你的本地工作区中编写代码、运行测试并自我修复报错。

## ✨ 核心特性

- 🎯 **极简架构**：没有复杂的图状态流转，只有纯粹的 `思考 -> 行动 -> 观察` 循环。
- 🛠️ **四大原子工具**：仅提供 `read` (读)、`write` (写)、`edit` (局部精准修改)、`bash` (终端执行) 四个底层能力。
- 🛡️ **原生错误反刍 (Error Feeding)**：当代码运行报错或 Bash 脚本抛出异常时，SCA 不会崩溃，而是将 `stderr` 原封不动喂给模型，实现**自我纠错**。
- 📦 **物理隔离设计**：`core/` (大脑逻辑) 与 `cli/` (Rich 终端皮肤) 严格解耦，核心代码零 `print`，随时可接入 Web 或其他 UI 端。
- 🧠 **长文本记忆压缩**：内置 Token 监控，逼近上下文上限时自动触发旧对话摘要压缩，防止爆显存。

## 🚀 快速开始

### 环境要求
- **Python:** >= 3.13
- 推荐使用 DeepSeek API，也完全兼容所有标准 OpenAI API 格式的模型。

### 安装步骤

1. **克隆仓库并进入目录**
   ```bash
   git clone [https://github.com/huguotao7412/simple_coding_agent.git](https://github.com/huguotao7412/simple_coding_agent.git)
   cd simple_coding_agent
   ```
2. **创建虚拟环境并安装项目 (推荐)**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Windows: .venv\Scripts\activate
   pip install -e .
    ```
3. **设置环境变量**
   - 创建 `.env` 文件，添加你的模型 API Key：
   ```bash
   SCA_API_KEY=sk-your-api-key-here
   SCA_API_BASE=[https://api.deepseek.com](https://api.deepseek.com)
   SCA_MODEL=deepseek-v4-pro
   SCA_MAX_TOKENS=128000
    ```
4. **运行 SCA**
   ```bash
   # 在当前目录下启动 Agent
   sca
   
   # 指定工作目录启动
   sca --dir /path/to/your/project
   
   # 临时覆盖使用其他模型
   sca --model gpt-4o
    ```
   启动后，SCA 会进入交互式 REPL 终端。你可以直接用自然语言向它下达开发指令：

   示例指令：

   "帮我在当前目录下初始化一个 React 项目。"
   
   "读取 main.py，帮我把里面的冒泡排序改成快速排序。"
   
   "运行一下 pytest，如果报错了你就自己看报错信息修复它，直到测试全绿为止。"
   
   输入 exit 或 quit 即可退出。
5. **目录结构说明**
   ```bash
   simple_coding_agent/
   ├── pyproject.toml              # 项目依赖配置
   ├── core/                       # 🧠 【大脑层】纯逻辑处理
   │   ├── agent.py                # ReAct 核心执行大循环
   │   ├── context.py              # 对话历史与 Token 压缩
   │   ├── llm.py                  # 异步流式 API 客户端
   │   └── tools/                  # 🛠️ 基础工具实现 (bash, edit, read, write)
   └── cli/                        # 🖥️ 【皮肤层】终端交互
       ├── main.py                 # CLI 入口
       ├── ui.py                   # 基于 Rich 的 UI 渲染
       └── bridge.py               # 连接大脑与终端流的桥接器
   ```

6. **安全警告**
   SCA 拥有执行本地终端命令（bash）和修改本地文件的权限。虽然我们在代码中内置了高危命令黑名单（如 rm -rf /）并限制了目录逃逸，但请不要赋予该程序 sudo 权限。建议在版本控制（Git）健全的目录下或 Docker 容器中使用该工具。