 摄像头流媒体集中拉流管控平台

企业级多品牌监控设备统一拉流、录像存储、Web 回放与定时任务系统。

## 主要功能

- 支持海康、大华、宇视等主流 RTSP 摄像头，也支持自定义 RTSP 地址
- 底层 Golang 高并发拉流引擎，每路摄像头独立协程
- 自动循环覆盖旧录像，可配置保留天数；物理断网自动重连
- Web 全功能管控：设备分组管理、录像检索与在线预览、课表批量下载
- 多时段定时录像计划（支持跨夜）
- 实时 HLS 直播预览，无需插件
- 基于角色的账号权限控制，操作日志审计

## 快速部署

### 依赖环境
- Python 3.9+
- Golang 1.22+
- FFmpeg

### 启动命令
```bash
# 编译 Go 引擎
go build go_engine.go

# 安装 Python 依赖
pip install -r requirements.txt

# 启动服务
python main.py

访问 http://服务器IP:8080，默认账号 admin / 123456（首次登录强制修改密码）

详细文档
请参考仓库中的《服务器部署指南2026.05.26.pdf》

技术栈
拉流核心：Golang + FFmpeg

Web 框架：Python http.server

前端：原生 HTML/CSS/JS + HLS.js + XLSX

存储：本地文件系统 + 自动清理策略
