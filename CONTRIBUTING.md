# 参与贡献

本仓库是「希儿桌宠」的 AstrBot 桥接插件，非营利同人项目的配套组件。

## 反馈渠道

| 渠道 | 适用 |
| --- | --- |
| GitHub Issue | **推荐**，可附日志与 ack |
| B 站评论 / 私信、QQ 群 | 轻量反馈，由作者整理成 Issue |

提 Issue 前请确认：插件目录名是 `astrbot_plugin_seele_desktop`、插件已启用、已**完全重启** AstrBot（不是刷新网页）。

## 本地开发

1. 把本仓库目录放到 `<AstrBot数据目录>\plugins\astrbot_plugin_seele_desktop\`；
2. 依赖：`aiohttp` 必需；`openai` 仅在主动录屏分析时需要（懒加载，不装不影响启动）；
3. 修改代码后**重启 AstrBot** 才生效，插件不支持热重载；
4. 验证平台是否注册成功，日志中应出现：

```text
[SeeleDesktop] added platform entry seele_desktop on port 9080
```

## 代码约定

- 全部异步：I/O 用 `aiohttp` / `asyncio`，不要在 `__init__` 或属性访问里做阻塞调用；
- 日志统一用 `from astrbot.api import logger`，前缀 `[SeeleDesktop]`；
- 配置读取走 `self._cfg(key, default)` 的形式，不要硬编码默认值；
- 不在模块顶层 import 重依赖（如 `openai`），保持懒加载，避免影响插件启动；
- 新增或修改配置项时，必须同时更新：`_conf_schema.json`（含默认值与说明）+ `README.md` 的对应章节；
- 不要写入本机绝对路径、API Key 或用户 ID。

## 提交 Pull Request

```bash
python -m py_compile *.py
```

CI 还会校验 `_conf_schema.json` 可解析、`metadata.yaml` 含 `name/desc/version/author`，并拒绝任何素材或模型文件。

## 素材政策

本仓库**只包含代码**，不接收任何美术、Spine 模型、序列帧、语音或模型权重文件；角色与素材版权归米哈游。

## 版本与发版

- 版本号写在 `metadata.yaml` 的 `version`；
- 非发版 PR 请勿改版本号；
- 发版时由维护者打 tag，并在 Release 附 `astrbot_plugin_seele_desktop.zip`。

## 许可

代码以 MIT 授权（见 `LICENSE`）。提交 PR 即表示同意以相同方式授权你的代码贡献；MIT 不覆盖任何角色素材。
