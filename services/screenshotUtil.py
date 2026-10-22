import os
import re
import ast
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ====================== 配置区 ======================
INPUT_FILE = str(PROJECT_ROOT / "data" / "input" / "result1.txt")   #你的设备文本文件路径
SAVE_DIR = str(PROJECT_ROOT / "data" / "screenshots")
# ====================================================

def safe_filename(name: str) -> str:
    """清理Windows文件名非法字符 \ / : * ? " < > |"""
    return re.sub(r'[\\/:*?"<>|]', "_", name)


def build_output_identifier(app_name: str, group: str = "", remote_ip: str = "") -> str:
    """Build the shared application/group/remote-IP output identifier."""
    app_value = str(app_name).strip()
    group_value = str(group).strip()
    remote_ip_value = str(remote_ip).strip()
    if not group_value and not remote_ip_value:
        return safe_filename(app_value) or "unknown_app"
    parts = (
        app_value or "unknown_app",
        group_value or "unknown_group",
        remote_ip_value or "unknown_ip",
    )
    return safe_filename("_".join(parts))


def adb_screenshot(serial: str, save_img_path: str) -> bool:
    """修复Windows下截图损坏问题：先存手机，再pull拉取"""
    tmp_android_path = "/sdcard/tmp_screenshot.png"
    # 1.手机内部截图保存
    cmd1 = ["adb", "-s", serial, "shell", "screencap", tmp_android_path]
    r1 = subprocess.run(cmd1, capture_output=True)
    if r1.returncode != 0:
        return False
    # 2.pull拉到电脑本地
    cmd2 = ["adb", "-s", serial, "pull", tmp_android_path, save_img_path]
    r2 = subprocess.run(cmd2, capture_output=True)
    # 3.清理手机临时文件
    subprocess.run(["adb", "-s", serial, "shell", "rm", "-f", tmp_android_path], capture_output=True)

    if r2.returncode == 0 and os.path.exists(save_img_path):
        # 简单校验文件大小，小于1KB视为截图失败
        size = os.path.getsize(save_img_path)
        if size > 1024:
            return True
    return False


def read_device_file(filepath: str):
    """读取文件，每行是字典字符串，解析返回设备列表"""
    device_list = []
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"输入文件不存在：{filepath}")

    with open(filepath, "r", encoding="utf-8-sig") as f:
        for idx, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            # 兼容每行末尾带多余逗号
            if line.endswith(","):
                line = line[:-1]
            try:
                data = ast.literal_eval(line)
                if isinstance(data, tuple):
                    data = data[0]
                if not isinstance(data, dict):
                    print(f"⚠️第{idx}行不是字典，跳过：{line}")
                    continue

                item = {
                    "appName": data["appName"].strip(),
                    "group": str(data.get("group", "")).strip(),
                    "remoteIp": str(
                        data.get("remoteIp")
                        or data.get("remote_ip")
                        or data.get("remoteIP")
                        or ""
                    ).strip(),
                    "packageName": data["packageName"],
                    "serialno": data["serialno"]
                }
                device_list.append(item)
            except Exception as e:
                print(f"❌解析失败，第{idx}行：{line}")
                print(f"错误信息：{e}")
    return device_list


def main():
    os.makedirs(SAVE_DIR, exist_ok=True)
    devices = read_device_file(INPUT_FILE)
    print(f"一共读取到 {len(devices)} 台设备\n")

    success_count = 0
    fail_count = 0

    for dev in devices:
        app_name = dev["appName"]
        output_identifier = build_output_identifier(
            app_name,
            dev.get("group", ""),
            dev.get("remoteIp", ""),
        )
        serial = dev["serialno"]
        img_full_path = os.path.join(SAVE_DIR, f"{output_identifier}.png")

        print(f"【设备】appName:{app_name} serial:{serial}")
        try:
            ok = adb_screenshot(serial, img_full_path)
            if ok:
                print(f"✅ 截图成功：{img_full_path}")
                success_count += 1
            else:
                print(f"❌ 截图失败")
                fail_count +=1
        except Exception as e:
            print(f"❌ 异常：{str(e)}")
            fail_count +=1
        print("-"*60)

    print(f"\n=====执行结束=====")
    print(f"成功：{success_count}  失败：{fail_count}")


if __name__ == "__main__":
    main()
