# 标准 Windows 安装器

`WeChatReBot.iss` 是面向最终用户的 Inno Setup 7 安装外壳。它提供系统原生的安装范围选择、安装目录、进度、开始菜单、桌面快捷方式、升级和卸载体验；实际文件校验与事务式部署仍复用现有 Python 安装后端。

## 两种产物

- 在线安装器：只包含约 40 MB 的管理器引导程序；安装时通过 HTTPS 下载 `payload.wrb`，并校验固定 SHA-256。适合作为默认分发包。
- 离线安装器：内嵌完整 `payload.wrb`，无需联网，但体积仍接近完整运行时载荷。适合离线环境。

在线安装器需要先把同版本的完整载荷放在稳定的 HTTPS 地址上；仅仅缩小安装器下载体积，不会缩小实际安装后的运行时占用。`prepare_native_test_payload.py` 和 `create_native_smoke_payload.py` 生成的载荷只供本地烟测，不可用于客户交付。

安装器不会绕过载荷清单校验。升级前只停止安装目录内的相关进程；检测到比安装器更高的已安装版本时会阻止降级。同版本可修复安装，旧版本可直接升级。

## 本地轻量校验

以下命令只检查参数并输出将要执行的编译命令，不调用 Inno Setup，也不生成安装包：

```powershell
python .\scripts\build_native_installer.py `
  --manager-exe 'E:\path\to\WeChatReBot.exe' `
  --payload-url 'https://example.invalid/payload.wrb' `
  --payload-sha256 '<64位SHA-256>' `
  --output '.\artifacts\native-dry-run' `
  --dry-run
```

正式生成安装器需要预先安装 Inno Setup 7，并去掉 `--dry-run`。输出目录必须位于当前工作树的 `artifacts` 下。

若机器上已有通过 `build_manager.py` 创建的工具环境，可用其 `--build-python` 参数复用 PyInstaller，避免每次重新下载依赖。

当前按非商用用途分发标准 Inno Setup 安装器，默认不做 Windows Authenticode 代码签名；商用用途发生变化时须重新核对 Inno Setup 授权和签名方案。省略 Windows 代码签名不等于省略 `payload.wrb` 的固定 SHA-256 校验，也不改变远程更新清单的 Ed25519 签名要求。本地烟测载荷及其安装器不属于可正式分发的产物。
