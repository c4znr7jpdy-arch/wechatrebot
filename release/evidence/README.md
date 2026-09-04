# 发布证据目录

本目录只存放已脱敏、可归档的验收记录。模板本身不是验收证据，也不得在 `acceptance-evidence.json` 中标记为 `approved`。

填写原则：

- 一项证据只对应明确版本和明确 SHA-256；
- 写明机器、系统/软件版本、执行人、复核人、时间、步骤和实际结果；
- 图片和日志删除账号、群名、联系人、消息、路径中的用户名及所有凭据；
- 禁止保存私钥、PFX/P12、证书密码、API Key、Cookie 或客户数据库；
- 完成后计算文件 SHA-256，将相对路径和哈希写入 `release/acceptance-evidence.json`。

推荐文件名：`<版本>-<检查项ID>-<日期>.md/json/pdf`。

HOOK 权利人和法务审核人允许是同一自然人，但必须分别完成权利事实声明与审核结论，并披露同一人兼任；使用 `HOOK-RIGHTS-DECLARATION-TEMPLATE.md`。安全自审使用 `HOOK-SECURITY-REVIEW-TEMPLATE.md`，不得用作者身份代替实际检查记录。

模板填写、删除占位符并签署后，应另存为新文件。模板本身不得改名冒充证据；建议保留受控签字 PDF 原件，在本目录仅保存脱敏 PDF 或 Markdown 副本。
