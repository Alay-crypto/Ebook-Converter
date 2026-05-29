这是一个基于 Python 和 Calibre 的轻量级电子书格式转换工具，支持将 Word/PDF 转换为 EPUB 等格式。
 前置环境准备（重要！）
由于本工具依赖 Calibre 的底层转换引擎 `ebook-convert`，请务必完成以下配置：
1. 下载安装软件：
前往 [Calibre 官网](https://calibre-ebook.com/download_windows) 下载并安装软件。
 记录你的安装路径（通常默认在 `C:\Program Files\Calibre2\`）。
2. 配置后端路径：
打开后端代码中的 `main.py`。
找到 `EBOOK_CONVERT_BIN` 配置项。
将其修改为你电脑上的实际安装路径（请确保路径指向 `ebook-convert.exe`）。
示例：`r"C:\Program Files\Calibre2\ebook-convert.exe"`
🚀 启动项目
1. 确保已安装 Python 环境。
2. 安装依赖：`pip install -r requirements.txt`
3. 启动后端：`python -m uvicorn main:app --reload`
4. 在浏览器打开前端页面即可开始转换。
---
本项目由 Alay 开发，感谢 Calibre 提供的强大内核支持。
