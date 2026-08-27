# 中文字幕字体运行要求

视频合成时必须使用覆盖 CJK 字形的字体。运行时按以下顺序解析：

1. `SUBTITLE_FONT` 指定的字体文件；
2. Windows 的微软雅黑或黑体；
3. macOS 的苹方或华文黑体；
4. Linux 的 Noto Sans CJK 或文泉驿正黑。

Windows 个人电脑通常无需额外操作。Linux 服务器建议安装 `fonts-noto-cjk`，或将字体文件放入持久化目录并通过 `SUBTITLE_FONT` 指定绝对路径。找不到可显示中文的字体时，系统会在生成前停止并给出可行动提示，不会继续生成乱码字幕。
