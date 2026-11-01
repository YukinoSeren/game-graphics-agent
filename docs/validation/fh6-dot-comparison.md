# Dot Esports 原型提取与冻结样本对照摘要

记录日期：2026-11-01。对应本地运行 `runs/fh6-final-seeded`；原始运行产物不随仓库推送。以下为首次真实运行的已见样本回归摘要，不是独立准确率或完整推荐验收。

选定固定项：24/24 一致。

| 样本项 | 原文名称 | 样本值 | 一致 |
|---|---|---|---|
| 预设 | Preset | Custom | 是 |
| 车辆细节 | Car Level of Detail | Extreme | 是 |
| 环境纹理 | Environment Texture Quality | High | 是 |
| 环境几何 | Environment Geometry Quality | High | 是 |
| 车身反射 | Car Reflection Quality | Extreme | 是 |
| 屏幕空间反射 | Screen Space Reflections Quality | Off | 是 |
| 光追反射 | Raytraced Reflections Quality | Low | 是 |
| 阴影 | Shadow Quality | Extreme | 是 |
| 夜间阴影 | Night Shadows | Ultra | 是 |
| 屏幕空间全局光照 | Screen Space GI Quality | Off | 是 |
| 光追全局光照 | Raytraced GI Quality | Low | 是 |
| 着色器 | Shader Quality | High | 是 |
| 音频 | Audio Quality | Ultra | 是 |
| 可变形地形 | Deformable Terrain Quality | Extreme | 是 |
| 粒子效果 | Particle Effects Quality | Ultra | 是 |
| 体积雾 | Volumetric Fog Quality | High | 是 |
| 镜头效果 | Lens Effects | High | 是 |
| 运动模糊质量 | Motion Blur Quality | Medium | 是 |
| 垂直同步 | Vertical Synchronization | Off | 是 |
| 全屏 | Full Screen | On | 是 |
| 运动模糊 | Motion Blur | Short | 是 |
| DLAA | NVIDIA DLAA | On | 是 |
| DLSS | NVIDIA DLSS | Off | 是 |
| Reflex | NVIDIA Reflex Low Latency | On + Boost | 是 |

三项语义保留检查通过：HDR保留为未检测到的观察；分辨率保留为条件策略；70—85 FPS保持区间，未提升为平均FPS。

FPS区间本身保留正确，但本次运行引用的测试配置结构不合法，因此测量仍被隔离。配置模板为 needs_followup；固定值一致不代表整份方案已适用于玩家。

原始冻结样本见 [fh6-new-source-extraction.json](../../fh6-new-source-extraction.json)，验证局限见 [实施报告](../../collector-implementation-report.md)。
