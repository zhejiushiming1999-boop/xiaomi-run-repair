# 从截图曲线到 RQrun 文件

## 提取

分析当次原图的实际尺寸、每条图的横轴起止时间、曲线区域及颜色。用 Pillow/numpy 提取彩色笔画，每列取颜色对比度加权中心；配速面积图应取上边缘，不能取填充区域的中心。排除平均值虚线、网格、图标和边框。低分辨率图宜使用亚像素加权中心，避免把整像素高度放大成明显台阶。截图区域会变化，不沿用旧截图的裁剪坐标。

保留原图中的上升、下降、起伏和稳定段，不随机生成高频噪声。散点图同一列可能含多次采样，需要说明取代表值的方法。均值、峰值可辅助校准幅度，但不能随意指定一个峰值发生时刻。数字或坐标轴不清晰时降低精度，或要求更清楚的原图；不假装秒级精度。

生成截图曲线覆盖层核对提取位置，再生成最终曲线预览。用活动原时长进行横向对齐，插值到每秒。CSV 锚点格式为：

```csv
elapsed_s,heart_rate_bpm,cadence_spm,pace_s_km,step_length_cm
0,100,178,500,65
...
```

脚本要求至少两个锚点，覆盖 0 至原活动时长，时间严格递增；四列数据必须都是有限值。这里 pace 是秒/公里，cadence 是双脚步/分，step_length 是厘米。不要将示例数值带到实际跑步。

## 生成文件

先用平均值脚本在 **work** 生成基础 TCX。它只接受 GPS-only 小米原件，防止覆盖真实逐点指标：

```powershell
python <skill>/scripts/repair_tcx.py <原件.tcx> --output <work/base.tcx> --summary <work/summary.json>
python <skill>/scripts/apply_curves.py <work/base.tcx> <work/curve_anchors.csv> --output <outputs/curves.tcx> --summary <work/summary.json> --fit-input <work/fit-input.json>
```

`apply_curves.py` 仅接受本技能生成的基础文件；写入基础 Cadence 与 RunCadence，保留 GPS 和时间，将截图速度积分按原距离校准，生成逐秒 CSV、来源 JSON 和 FIT 编码输入。校准比例应核对，较大差异先检查截图纵轴标尺和时间对应。不同来源的步长/步频/配速不能强行全部满足同一运动学关系；CSV 保留截图步长和根据速度/步频计算的步长两列。

summary 可包含 `display_average_pace: "7:23"` 及用户报告的 `target_average_pace: "7:24"`。这两个显示值仅用于检查和解释，不用于改变速度、时间或距离。没有目标显示反馈时省略该字段。

## FIT 编码

使用 [Garmin 官方 JavaScript SDK](https://github.com/garmin/fit-javascript-sdk)。优先复用已可用的 SDK；缺失时依赖和 npm 缓存均安装到当前任务 work 目录，避免写入用户其他目录：

```powershell
npm install --prefix <work/fit-runtime> --cache <work/npm-cache> --no-audit --no-fund --ignore-scripts @garmin/fitsdk
node <skill>/scripts/encode_fit.mjs --input <work/fit-input.json> --output <outputs/curves.fit> --sdk <work/fit-runtime/node_modules/@garmin/fitsdk/src/index.js>
```

编码器写标准 record.cadence、fractionalCadence、cadence256、stepLength，以及 session/lap 的平均指标；不要只写自定义开发字段。cadence 是单脚周期/分，双脚指标需乘 2；stepLength 和 avgStepLength 是毫米（74 cm → 740 mm），SDK负责 scale=10，调用时不要再手动乘一次编码 scale。

每次回读验证 CRC、字段覆盖、曲线变化、时间、总量和量化误差。FIT 位置信息是整数 semicircle，按 `度 × 2^31 / 180` 转换。编码器的 manufacturer 为 development，标注 Screenshot approximation，不冒充原硬件测量。

## 平台反馈

RQrun 官方支持 FIT；其 [上传说明](https://rq.run/article/content?id=406) 描述了 FIT 分享到 APP 与 TCX/GPX 官网上传。对于包含步幅曲线的记录，优先给用户测试 FIT，同时保留 TCX 兼容选择，不自行上传。

仅通过 XML/FIT 校验不能保证平台显示每个字段。若 RQrun 仍缺字段，先核对实际导入的文件和活动是否正确，再根据导入结果做有证据的兼容修正。用户此前遇到过仅写 RunCadence 时步频/步幅不显示，因此始终检查基础 Cadence 是否存在；不要无限添加未验证的字段或不断换格式猜测。
