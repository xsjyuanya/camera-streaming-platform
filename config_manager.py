import os
import json
import logging

# ================= 配置区域 =================
STORAGE_DIR = "./video_storage"
LIVE_TEMP_DIR = os.path.join(STORAGE_DIR, "live_temp")
CONFIG_FILE = "./cameras_config.json"
SYS_CONFIG_FILE = "./system_config.json"
USERS_CONFIG_FILE = "./users_config.json"
GROUPS_CONFIG_FILE = "./groups_config.json"
LOG_FILE = "system_operate.log"
SEGMENT_TIME = 600

# 摄像头统一拉流凭证配置 (通过环境变量读取，杜绝硬编码明文密码)
RTSP_USER = os.getenv("RTSP_USER", "admin")
RTSP_PWD = os.getenv("RTSP_PWD", "")   # 留空，强制用户配置，防止泄露
RTSP_PORT = os.getenv("RTSP_PORT", "554")
RTSP_CHANNEL = os.getenv("RTSP_CHANNEL", "102")

# 内存数据存储
USERS_DB = {}
GROUPS_DB = ["默认分组"]
SESSIONS = {}  # 内存 Token 存储: token_id -> {"username": "xxx", "time": timestamp}

def load_users():
    global USERS_DB
    if os.path.exists(USERS_CONFIG_FILE):
        try:
            with open(USERS_CONFIG_FILE, 'r', encoding='utf-8') as f:
                USERS_DB.update(json.load(f))
        except Exception as e:
            logging.error(f"加载用户配置失败: {e}")
    if not USERS_DB:
        USERS_DB.update({"admin": {"password": "123456", "role": "superadmin", "desc": "系统默认超管"}})
        save_users()

def save_users():
    try:
        with open(USERS_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(USERS_DB, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logging.error(f"保存用户配置失败: {e}")

def load_groups():
    global GROUPS_DB
    if os.path.exists(GROUPS_CONFIG_FILE):
        try:
            with open(GROUPS_CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                GROUPS_DB.clear()
                GROUPS_DB.extend(data)
        except Exception as e:
            logging.error(f"加载分组配置失败: {e}")
    if "默认分组" not in GROUPS_DB:
        GROUPS_DB.insert(0, "默认分组")

def save_groups():
    try:
        with open(GROUPS_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(GROUPS_DB, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logging.error(f"保存分组配置失败: {e}")

def load_cameras():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_cameras(cameras):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cameras, f, ensure_ascii=False, indent=4)
    except Exception:
        pass