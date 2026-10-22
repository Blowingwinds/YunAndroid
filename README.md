# YunAndroid

Windows desktop manager for cloud Android devices. Requires Python 3.10 or newer.

## Setup

1. Install dependencies: `python -m pip install -r requirements.txt`.
2. Copy `config/monitor.example.ini` to `config/monitor.ini` and fill in your own Feishu webhook, security keyword and random control tokens before enabling monitoring.
3. Keep the device workbook at `config/cloud_phone_queue.xlsx` and the SSH private key at `config/keys/atm-phone.pem`. These files contain local data and are intentionally excluded from Git.
4. Set `YUNANDROID_SSH_USER_HOST` to the SSH `user@host`, `YUNANDROID_PUBLIC_IPS` to two comma-separated server addresses, and `YUNANDROID_LOGIN_ACCOUNT` / `YUNANDROID_LOGIN_PASSWORD` if using automatic login. Set these in your local environment; never commit their values.
5. Put APK download URLs in `config/oss_url.txt` when using the APK download action.
6. Run `start.bat` or `python app.py`.

The repository includes the bundled scrcpy/adb tools, Airtest image templates and Lua payloads required by the corresponding UI actions. Output, logs, workbooks, private keys and local settings stay on each machine.
