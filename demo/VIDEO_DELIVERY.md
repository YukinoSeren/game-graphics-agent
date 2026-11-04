# 45 秒视频交付记录

> 仅用于本目录临时演示。正式开发不得以这里的设计、代码、数据或结果作为参考或验收依据。片中性能数据来自固定虚拟日志，不是实际游戏测量。

已交付：[game-assistant-45s.mp4](./runtime/video/game-assistant-45s.mp4)。

- 时长：45.000 秒，共 1,350 帧。
- 格式：1920×1080、30 fps、H.264 / MP4、yuv420p，包含 faststart 元数据。
- 声音：无音频轨，无配音、背景音乐或点击音。
- 文件大小：2,470,255 字节，约 2.36 MiB。
- 画面：实际浏览器录屏，覆盖首次使用、设备检查、需求输入、资料建卡、分组设置、确认采用、日志分析和体验反馈。
- 动态引导：鼠标移动、点击圆环、原生思考和进度动画、页面滚动；另加中文短字幕、章节编号和进度线。
- 剪辑：压缩输入、滚动与重复填写，省略约一分钟资料等待，画面显示“资料整理等待已省略”。没有修改网站正常的回复和建卡时间。

录制使用隔离服务 `8767`，录制后服务与录制浏览器已关闭。实际网站 `8765` 保持运行。旧数据库及关联文件保存在 `runtime/backups/before-video-20261103-184956/`，浏览器存储备份为 `runtime/video/browser-before-reset.json`。

最终验证主工作区 epoch 为 5，`ConversationMessage`、`GameIntent`、`GameCard`、`Summary` 均为 0，主浏览器显示空白首次使用首页。

## 验证材料

- [逐场景预览](./runtime/video/storyboard-preview.jpg)
- [视频验证结果](./runtime/video/video-verification.json)：完整解码退出码为 0，无解码错误，无音频流。
- [媒体信息](./runtime/video/media-info.txt)
- [剪辑时间表](./runtime/video/edit-decision-list.json)
- [录制脚本](./runtime/video/record-flow.js)及[开场补录脚本](./runtime/video/record-opening.js)
- [剪辑脚本](./runtime/video/edit_video.py)及[验证脚本](./runtime/video/verify_video.py)

原始素材保存在 `runtime/video/raw-recording.webm` 和 `runtime/video/raw-opening.webm`。开场补录修正了首次录制启动时序造成的开场素材缺失，成片使用完整开场；成片没有包含该启动缺失。

执行 `py -3.13 -B demo/runtime/video/verify_video.py` 可重新检查媒体参数与完整解码；该脚本同时核对主网页仍为空白，用户重新使用后其空白断言将自然不再成立。
