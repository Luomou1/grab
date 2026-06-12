# 实现记录

## PZT 行为源

PZT 控制以 `camera2\PZT_Camera_GUI.m` 当前主版本为行为源：

- 地址固定 `0x01`
- 连接后对 `0..2` 通道发送闭环命令
- 串口默认 `115200`，保留旧 GUI 可选波特率
- 位移范围按旧 GUI 限制为 `0..270 um`
- 写位移包：`AA 01 0B 01 00 CH D0 D1 D2 D3 XOR`
- 读位移包：`AA 01 07 06 00 CH XOR`
- 读回只按旧逻辑校验 `AA` 和 XOR，并从第 7-10 字节解码位移

数值编码采用十进制字符串计算小数部分，避免 Python 二进制浮点把 `10.001` 编成 `0x0009`；协议单测已覆盖厂家/旧程序示例 `AA 01 0B 01 00 00 00 0A 00 0A A1`。

## 相机实现

相机继续使用 `mvsdk.py` 的 ctypes 封装，但在纯 Python 层做了三点加固：

- 启动时把 `D:\HuaTengVision\SDK\X64` 和 `D:\HuaTengVision\SDK` 加入 DLL 搜索路径。
- 当前 HT-GE134GM-T1 能力表只枚举 `MONO8` 和 `MONO12_PACKED`；程序移除 24bit RGB 路径，保留 `8bit` 与 `12bit Packed` 两档。
- `12bit Packed` 采集时先设置 `CameraSetMediaType(... MONO12_PACKED)`，再让 ISP 输出 `MONO16`，Python 侧右移 4 位得到真实 `0..4095` 灰度值；保存 PNG/TIFF 前再左移 4 位写入 16bit 容器，匹配常见看图软件和官方软件的显示习惯。
- 软触发模式设置 `TriggerCount=1`，并等待采集计数递增后返回图像。

## 扫描策略

- UI 线程只负责显示和参数输入。
- 相机后台线程持续取最新帧。
- 扫描线程执行 PZT 移动、稳定等待、读回位移、采图、保存。
- 软触发扫描启动前停止预览，避免缓存帧和显示线程竞争。

## 已完成验证

- `python -m compileall grab_app`
- `python -m pytest tests`
- PySide6 offscreen 主窗口构造测试

## 尚需硬件验证

- 相机枚举和连续预览 FPS
- 软触发模式是否每步图像时间戳/帧号递增
- PZT 串口实际连接、闭环命令、读回位移
- 完整扫描保存 100 到 1000 张图的稳定性
