# YunAndroid

Windows 云手机管理桌面程序，建议使用 Python 3.11。

## 安装与启动

在项目根目录打开 PowerShell，创建独立虚拟环境并安装运行依赖：

```powershell
py -3.11 -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

配置好本机数据后，运行 `start.bat`；也可以运行 `.\venv\Scripts\python.exe app.py`。

## 本机配置

1. 将 `config/monitor.example.ini` 复制为 `config/monitor.ini`。启用监控或日报汇总前，填写自己的飞书机器人地址、安全关键词及随机生成的控制令牌，并按实际目录调整路径和标签。
2. 将设备清单放在 `config/cloud_phone_queue.xlsx`，SSH 私钥放在 `config/keys/atm-phone.pem`。使用 SSH 连接功能时，系统还需要可用的 OpenSSH 客户端。
3. 使用 APK 下载功能时，将下载地址写入 `config/oss_url.txt`，每行一个地址。
4. 使用 SSH 连接或自动登录时，在启动程序前设置本机环境变量：

```powershell
$env:YUNANDROID_SSH_USER_HOST = "你的SSH用户名@服务器地址"
$env:YUNANDROID_PUBLIC_IPS = "服务器地址1,服务器地址2"
$env:YUNANDROID_LOGIN_ACCOUNT = "你的登录账号"
$env:YUNANDROID_LOGIN_PASSWORD = "你的登录密码"
```

请将示例值替换为实际值。自动登录需要同时设置账号和密码；不使用对应功能时，无需设置其变量。

仓库包含程序运行使用的 scrcpy/adb、Airtest 图片模板和 Lua 文件。设备清单、私钥、本机配置、运行输出及缓存不提交到 GitHub。
