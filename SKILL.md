---
name: xiaomi-run-repair
description: 修复小米运动健康跑步 TCX，结合截图近似重建心率、步频、配速和步长曲线，生成 RQrun 兼容 TCX 与含标准步幅字段的 FIT，并核对平均配速的整数显示差异。适用于导入失败或指标缺失；截图重建不是原始传感器数据。
---

# 小米跑步数据修复

用户上传小米 TCX（可附运动详情截图）时，先检查原文件，再选择下列模式。该用户的目标平台为国内 RQrun；保留这个已知偏好，不重复询问。保留原件和已交付文件，新的活动或修正版输出到当前任务的交付目录。

- **截图曲线模式**：用户已有心率、配速、步频、步长图并要求补全曲线时，优先提取图像形状，按实际活动时长对齐后插值，生成标准 FIT，并提供 TCX 兼容版。详细处理和命令见 [references/curves-and-fit.md](references/curves-and-fit.md)。
- **平均值模式**：只有汇总截图或 TCX 时，运行 `scripts/repair_tcx.py`。它补固定平均心率、固定步频及 GPS 估算速度。明确告知固定指标的图会接近水平线；不能称为曲线恢复。

脚本处理一个活动、一个圈、一个 Track。多圈、暂停、大间隔、坐标跳点、时区异常，以及已有真实逐点指标时先分析，不用平均值或截图估算覆盖真实数据。

## 来源与补全

- 原文件的 UTC 时间、GPS、精确距离和时长优先于截图的舍入显示。不要把 `Z` 时间重新当作本地时间。
- 读取截图中的平均心率、平均步频、平均步长、最高值、步数和热量。模糊数字不可猜测；截图不足时仅询问影响输出的缺失值。
- 用户要求曲线且截图包含图表时，根据原图提取颜色曲线；不能仍交付固定平均值冒充曲线。平均值模式才使用固定值，注明代填。
- 步频优先用同一活动截图的平均步频；没有截图时用 `步数 × 60 / 时长`，说明是估算。不得沿用上一次跑步的 149、179 等数值。
- 配速在平均值模式下从 GPS 累计距离计算、按原总距离校准，使用 10 秒窗口估算速度；截图曲线模式下提取图形轮廓，并校验速度积分等于原总距离。两者分别注明来源。不要随机生成变化。
- 逐秒 CSV 的步长为 `速度 × 60 / 双脚步频`，与截图平均步长分别保留，不能强行匹配。
- 截图均值和峰值可辅助校准近似曲线的幅度，说明这种校准不是原始传感器数据。平均值模式中，截图最大值只保存到 Notes/JSON，不随机插入一个峰值。海拔、功率、触地时间等缺失字段不猜。

## 平均配速的整数显示

始终以原精确总距离和时长计算 `平均配速（秒/公里）=时长秒数×1000/距离米数`，不要用逐秒配速的算术平均代替它，也不要用界面舍入后的公里数反算距离。

本次实际案例：2665 秒 / 6005 米得到 443.7968 秒/公里，即 7:23.797。用户确认小米显示 **7:23**、RQrun 显示 **7:24**，与截断和四舍五入的差异相符。这个案例仅说明显示差异与不同取整方式一致，不能推定两家平台所有活动都采用此规则。

`scripts/repair_tcx.py` 的 `average_pace_report()` 会记录精确值、截断值、四舍五入值，并与当次 `display_average_pace`、`target_average_pace` 比较。两者若只差 1 秒且符合整数显示范围，解释显示差异，**保留距离和时长，不统一加减 1 秒、不篡改 AvgSpeed、不整体平移配速曲线**。其他差异检查暂停/运动时间、有效点区间、距离单位和设备统计口径；不猜测平台算法。

## 运行

将截图数据写入临时 JSON，例如（数值需来自当次活动）：

```json
{"average_hr_bpm":149,"average_cadence_spm":179,"average_step_length_cm":74,"maximum_hr_bpm":160,"maximum_cadence_spm":190,"maximum_step_length_cm":81,"total_calories_kcal":372}
```

```powershell
python <skill目录>/scripts/repair_tcx.py <输入.tcx> --output <输出.tcx> --summary <截图汇总.json>
```

无截图可省略 `--summary`。需要明确指定时使用 `--heart-rate`、`--cadence`。脚本不覆盖已有输出文件。

TCX 使用 Garmin v2 顺序与心率 Value、Sport="Running"、StartTime、Intensity、TriggerMethod。RQrun 兼容版在每个 Trackpoint 的 HeartRateBpm 之后、Extensions 之前写入基础 **Cadence（双脚步/分）**，圈中同样写平均 Cadence；另保留 **TPX/RunCadence（整数单脚步频）**。不要只写 RunCadence 后便声称 RQrun 可读取。用常见 ns3 前缀保持兼容，命名空间 URI 不变。速度为 m/s。双脚 179 在单脚整数字段中可量化为 89/90，不把 90 当成真实双脚 180。

TCX 没有标准逐点步长字段：自定义扩展只能保存数据，不能声称 RQrun 会显示它。需要步幅曲线时，优先交付 FIT：原生 record.stepLength 和 session/lap.avgStepLength 的单位是 **毫米**，74 厘米写成 740 毫米。FIT cadence 为单脚周期/分；同时写 cadence、fractionalCadence 和 cadence256。用 Garmin 官方 SDK 编码并回读验证。TCX 供用户选择使用；提醒同一记录两种文件二选一导入。

逐秒补点仅填 Time 和派生指标，不伪造 GPS。原样保留全部原坐标和时间；新补的无 GPS 点标记在 CSV。对于暂停或长缺口，脚本停止而非填满整段。

## 验证和交付

检查：原 GPS 和时间保留、距离单调且最终等于原总距离、总时长未变、心率覆盖输出点、基础 Cadence 与 RunCadence 均写入、整数配速显示检查记录、曲线模式确实有变化。FIT 需回读检查 CRC、解码错误、逐点 cadence/stepLength、单位、GPS 个数和 session 汇总。FIT 坐标须转换为 semicircle，说明其格式量化误差。若 lxml 与官方 XSD 可用，验证核心 TCX 和所有 TPX/LX。官方定义：

- https://www8.garmin.com/xmlschemas/TrainingCenterDatabasev2.xsd
- https://www8.garmin.com/xmlschemas/ActivityExtensionv2.xsd

交付最终 FIT/TCX、逐秒 CSV 和简短说明；曲线模式提供可核对的预览。区分原始值、截图近似、固定代填和 GPS 估算。图像分辨率限制写在说明中，不把插值的 2666 点等同于 2666 次真实测量。仅报告实际通过的检查；RQrun 展示哪些字段必须以实际导入反馈为准。用户反馈已交付后不擅自重写那次记录。不会连接小米账户或自行上传运动记录。
