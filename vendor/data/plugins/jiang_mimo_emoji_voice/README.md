# 姜小妹表情语音桥

这是一个很薄的适配插件，不负责实现 TTS，也不复制 MiMo 音色管理逻辑。它依赖
`astrbot_plugin_mimo_tts_clone` 的公开 `synthesize_text()` 方法：当
`meme_manager` 已经实际选中表情包后，把当前回复中的 `Plain` 替换为一条 `Record`，
表情包继续由 `meme_manager` 发送。

## 行为

- 普通回复：不触发语音。
- 没有实际表情包：不触发语音。
- 有表情包且文字长度合适：移除文字，发送 MiMo 语音；表情包保留。
- TTS、音色、ffmpeg 或接口失败：保留原文字，表情包流程不受影响。
- 长文本、代码和链接不适合朗读时：保留原文字。

## 触发方式

### 直接测试 MiMo TTS

MiMo TTS Clone 自带显式朗读命令，不依赖表情包：

```text
/tts 你好，这是语音测试
/tts -e happy 今天心情不错
/朗读 这是一条朗读测试
```

执行前需要在 MiMo TTS Clone 的 Pages 中配置 API Key、上传已授权音色，并设置默认音色。

### 表情包联动语音

这是本适配插件的主流程：

1. LLM 回复中出现有效情绪标记，例如 `&&happy&&`、`&&sad&&`、`&&angry&&`。
2. `meme_manager` 根据标记或情绪匹配，实际找到对应分类下的图片。
3. 回复文字长度在 `min_text_chars` 到 `max_text_chars`（默认 1–120）之间。
4. MiMo TTS 合成成功后，原 `Plain` 文字被替换为 `Record` 语音条，表情包继续发送。

没有实际图片、情绪概率未命中、音色/API/转码失败或文本过长时，插件会保留原文字，不影响原有回复。

首次联调可以让机器人回复一条带 `&&happy&&` 的短句，并临时将 `meme_manager` 的情绪触发概率调为 100%；确认语音正常后再恢复原概率。日志中出现
`[jiang_mimo_emoji_voice] 表情触发语音` 即表示联动成功。

## 依赖与配置

1. 安装并启用 `astrbot_plugin_mimo_tts_clone`。
2. 在 MiMo TTS Clone 的 Pages 中填写 API Key。
3. 上传本人或已获授权的 MP3/WAV 参考音频，登记至少一个音色，并设置默认音色或情绪音色。
4. 确认 `auto_tts_enabled=false`，避免普通回复被另一条自动 TTS 链路处理。
5. 本适配插件默认最多朗读 120 个字符；MiMo Clone 只输出 WAV，本插件用系统 `ffmpeg` 转成 24kHz 单声道 PCM，再用 `pilk` 编码为 SILK，交给微信语音接口发送。这样旧 HOOK 和新 HOOK 都会显示为语音条，而不是 WAV 文件卡片。

## 维护约定

- MiMo Clone 版本通过其自身 Git 仓库维护；不要把其源码复制到本插件中。
- 本插件只依赖 `synthesize_text()`、`meme_manager_pending_images` 和表情图片路径这几个稳定边界。
- 更新 MiMo Clone 后，先运行静态检查和本插件回归测试，再手动验证一条开心表情和一条失败回退场景。
- 不在日志中输出 API Key、参考音频内容或音频 base64。
