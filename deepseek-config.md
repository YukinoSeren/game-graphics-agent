# 为采集原型配置 DeepSeek

日期：2026-11-01。仅配置本项目采集程序的模型接口，不修改 Codex 自身模型。

## 1. 创建密钥

打开 [DeepSeek 开放平台](https://platform.deepseek.com/)创建 API Key，并确认账户有可用 API 余额。密钥不要发到聊天或写入研究 Markdown。

## 2. 在 PowerShell 保存用户环境变量

```powershell
$fh6Key = Read-Host "粘贴 DeepSeek API Key" -AsSecureString
$fh6PlainKey = [System.Net.NetworkCredential]::new("", $fh6Key).Password
[Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY", $fh6PlainKey, "User")
Remove-Variable fh6Key, fh6PlainKey

[Environment]::SetEnvironmentVariable("COLLECTOR_API_KEY_ENV", "DEEPSEEK_API_KEY", "User")
[Environment]::SetEnvironmentVariable("COLLECTOR_API_BASE", "https://api.deepseek.com", "User")
[Environment]::SetEnvironmentVariable("COLLECTOR_MODEL", "deepseek-v4-flash", "User")
```

程序优先读当前进程变量，再读 Windows 用户环境变量，因此本次已有会话通常无需重启。若当前终端曾设置同名 `$env:COLLECTOR_*`，它会优先于用户设置；改配置后可在新 PowerShell 中运行。

`deepseek-v4-flash` 是你指定的名称。官方目前仍接受它，但会路由到 V4.1 Flash；当前官方常用入口为 `deepseek-flash`。本项目保留指定请求名，并在运行文件记录接口实际返回的名称，不把兼容别名当成版本锁定。[官方说明](https://api-docs.deepseek.com/quick_start/agent_integrations/)

## 3. 检查连接

```powershell
Set-Location E:\code\game
py -3.13 -m game_collector --check-model
```

成功结果包含 `credential_check: passed`、请求模型与可用模型列表，不输出密钥。此检查不生成文本。

常见返回：401 检查密钥与服务地址；402 检查 API 余额；403 检查账户/服务权限；429 为限流；连接超时则检查网络。错误内容脱敏，原始响应中的敏感信息不写进日志。

## 4. 启动采集

复制 [README 中的指定来源或自动发现命令](./README.md)。默认启用 DeepSeek 思考模式；仅做快速调试时可在当前终端设置：

```powershell
$env:COLLECTOR_THINKING = "disabled"
```

取消当前覆盖可执行 `Remove-Item Env:COLLECTOR_THINKING`。模型调用会消耗 API 额度；程序按预算记录调用和实际 token 用量，不估算未计量金额。

本次已验证密钥连接通过，实际提取的返回模型为 `deepseek-flash`。网页提取是否通过校验仍须看对应运行报告。
