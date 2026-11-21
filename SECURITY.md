# 安全问题报告

## 影响范围

插件在 AstrBot 进程内运行，直接关系到本机文件与本地服务的访问，以下问题请勿公开提交 Issue：

- 平台令牌泄露、未鉴权的 WebSocket 接入
- 通过 inbox / 视频路径实现任意文件读取或写入
- API Key、令牌被写入 ack、日志或错误信息
- 能让 AstrBot 进程崩溃或死锁的输入

## 报告方式

1. 仓库 **Security → Report a vulnerability**（GitHub Security Advisory）；
2. 或通过 B 站私信联系作者。

请附：插件版本、AstrBot 版本、复现步骤、影响面（是否需用户交互）。

## 处理方式

- 7 天内初步确认；
- 修复后在 Release notes 说明；
- 报告中请移除你自己的 Key、令牌与个人路径。

## 非漏洞的已知行为

- 默认端口 `9080`、默认令牌 `seele-desktop-local-token-2026` 是本地占位值，部署在局域网或多用户环境时应自行修改；
- 主动录屏会读取用户配置的 inbox 目录，这是设计行为；
- 未配置 ffmpeg 时只处理无需转码的 MP4，属于预期降级。
