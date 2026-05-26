import http.server
import socketserver
import json
import urllib.parse
import urllib.request
import os
import time
import uuid
import logging
import zipfile
import re
import subprocess
import shutil
import threading
import concurrent.futures
from datetime import datetime, timedelta

import config_manager
from utils import is_complex_password


class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class VideoPortalHandler(http.server.SimpleHTTPRequestHandler):
    engine = None  # Class variable assigned in main.py

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=config_manager.STORAGE_DIR, **kwargs)

    def has_valid_session(self):
        auth_header = self.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
            if token in config_manager.SESSIONS:
                self.current_user = config_manager.SESSIONS[token]['username']
                user_info = config_manager.USERS_DB.get(self.current_user)
                if user_info:
                    self.current_role = user_info.get('role', 'user')
                    return True

        parsed_path = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed_path.query)
        token = query.get('token', [''])[0]
        if token and token in config_manager.SESSIONS:
            self.current_user = config_manager.SESSIONS[token]['username']
            user_info = config_manager.USERS_DB.get(self.current_user)
            if user_info:
                self.current_role = user_info.get('role', 'user')
                return True

        return False

    def check_auth_or_401(self):
        if self.has_valid_session():
            return True
        self.send_error(401, "Unauthorized")
        return False

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0, post-check=0, pre-check=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed_path.path)

        if path == '/':
            self.serve_html_app()
            return

        if not self.check_auth_or_401(): return

        if path == '/api/whoami':
            self.api_whoami()
        elif path == '/api/cameras':
            self.api_cameras()
        elif path == '/api/users':
            self.api_users_get()
        elif path == '/api/groups':
            self.api_groups_get()
        elif path == '/api/schedule':
            self.api_schedule_get()
        elif path == '/api/logs':
            self.api_logs_get(parsed_path.query)
        elif path == '/api/log_download':
            self.api_log_download(parsed_path.query)
        elif path == '/api/search':
            self.api_search(parsed_path.query)
        elif path == '/api/export':
            self.api_export(parsed_path.query)
        elif path == '/api/batch_zip':
            self.api_batch_zip(parsed_path.query)
        elif path == '/api/live_start':
            self.api_live_start(parsed_path.query)
        elif path == '/api/live_heartbeat':
            self.api_live_heartbeat(parsed_path.query)
        elif path == '/api/live_stop':
            self.api_live_stop(parsed_path.query)
        elif path == '/api/batch_status':
            job_id = urllib.parse.parse_qs(parsed_path.query).get('job_id', [''])[0]
            job = self.engine.batch_jobs.get(job_id)
            if job:
                self.send_json(job)
            else:
                self.send_error(404, "Job not found")
        else:
            super().do_GET()

    def do_POST(self):
        parsed_path = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed_path.path)

        if path == '/api/login':
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                body = self.rfile.read(content_length).decode('utf-8')
                try:
                    data = json.loads(body)
                    u = data.get('username', '')
                    p = data.get('password', '')

                    user_info = config_manager.USERS_DB.get(u)
                    if user_info and str(user_info.get('password')) == str(p):
                        token_id = str(uuid.uuid4())
                        config_manager.SESSIONS[token_id] = {'username': u, 'time': time.time()}
                        logging.info(f"🛡️ [安全审计] 用户 [{u}] 已成功登录系统。")

                        self.send_json({"status": "ok", "token": token_id})
                        return
                except:
                    pass

            logging.warning(f"🛡️ [安全预警] 检测到异常登录尝试！")
            self.send_error(401, "Invalid credentials")
            return

        if not self.check_auth_or_401(): return

        content_length = int(self.headers.get('Content-Length', 0))
        post_data = {}
        if content_length > 0:
            body = self.rfile.read(content_length).decode('utf-8')
            try:
                post_data = json.loads(body)
            except Exception:
                pass

        if path == '/api/cameras':
            if self.current_role not in ['superadmin', 'admin']: return self.send_error(403)

            action = post_data.get('action', 'add')
            original_id = str(post_data.get('original_id', '')).strip()
            cam_group = str(post_data.get('group', '默认分组')).strip()
            cam_id = str(post_data.get('id', '')).strip()
            ip = str(post_data.get('ip', '')).strip()
            brand = str(post_data.get('brand', 'hikvision')).strip()
            user = str(post_data.get('user', 'admin')).strip()
            pwd = str(post_data.get('pwd', '')).strip()
            port = str(post_data.get('port', '554')).strip()
            channel = str(post_data.get('channel', '102')).strip()
            custom_url = str(post_data.get('custom_url', '')).strip()

            if not cam_group: cam_group = '默认分组'
            if not cam_id: return self.send_json({"error": "摄像头名称(ID)不能为空"}, status=400)

            # ID 防重校验机制
            if action == 'add' and any(str(cam["id"]) == cam_id for cam in self.engine.cameras):
                return self.send_json({"error": "您输入的摄像头名称(ID)已被占用，请换一个名称"}, status=400)
            if action == 'edit' and cam_id != original_id and any(
                    str(cam["id"]) == cam_id for cam in self.engine.cameras):
                return self.send_json({"error": "您修改的新名称(ID)与其他设备冲突，请更换"}, status=400)

            # 多品牌 RTSP 拉流地址智能生成工厂
            rtsp_url = ""
            safe_pwd = urllib.parse.quote(pwd)  # 极其关键：防止密码中的 @ / : 等特殊符号破坏URL结构！

            if brand == 'hikvision':
                rtsp_url = f"rtsp://{user}:{safe_pwd}@{ip}:{port}/Streaming/Channels/{channel}"
            elif brand == 'dahua':
                rtsp_url = f"rtsp://{user}:{safe_pwd}@{ip}:{port}/cam/realmonitor?channel={channel}&subtype=1"
            elif brand == 'uniview':
                rtsp_url = f"rtsp://{user}:{safe_pwd}@{ip}:{port}/media/{channel}"
            elif brand == 'custom':
                rtsp_url = custom_url
            else:
                rtsp_url = f"rtsp://{user}:{safe_pwd}@{ip}:{port}/Streaming/Channels/{channel}"

            # IP 连通性探测隔离带
            test_ip = ip
            test_port = port
            if brand == 'custom':
                try:
                    match = re.search(r'@([^:/]+)(?::(\d+))?', custom_url)
                    if match:
                        test_ip = match.group(1)
                        test_port = match.group(2) if match.group(2) else "554"
                    else:
                        match2 = re.search(r'rtsp://([^:/]+)(?::(\d+))?', custom_url)
                        if match2:
                            test_ip = match2.group(1)
                            test_port = match2.group(2) if match2.group(2) else "554"
                except:
                    pass

            if test_ip:
                try:
                    is_connected = self.engine.check_ip_connectivity(test_ip, int(test_port))
                    if not is_connected:
                        return self.send_json({
                                                  "error": f"连接受阻！系统无法连通设备 IP: {test_ip} 的 {test_port} 端口。请检查设备是否开机或网线是否通畅。"},
                                              status=400)
                except Exception as e:
                    logging.error(f"IP探测异常: {e}")

            # 组装超级属性节点
            new_cam = {
                "id": cam_id,
                "group": cam_group,
                "ip": ip,
                "brand": brand,
                "user": user,
                "pwd": pwd,
                "port": port,
                "channel": channel,
                "url": rtsp_url
            }

            if action == 'add':
                self.engine.cameras.append(new_cam)
                config_manager.save_cameras(self.engine.cameras)
                # 尝试热启动
                if self.engine.is_running:
                    try:
                        self.engine.root.after(0, self.engine.start_single, new_cam)
                    except:
                        pass
                logging.info(f"🛡️ [安全审计] 用户 [{self.current_user}] 新增入网并配对了摄像头: {cam_id} ({ip})")
            else:
                for i, c in enumerate(self.engine.cameras):
                    if str(c["id"]) == original_id:
                        self.engine.cameras[i] = new_cam
                        break
                config_manager.save_cameras(self.engine.cameras)

                # 如果用户修改了名称(ID)，自动跨目录转移旧的录像文件，防止孤儿文件泄露
                if original_id != cam_id:
                    old_dir = os.path.join(config_manager.STORAGE_DIR, original_id)
                    new_dir = os.path.join(config_manager.STORAGE_DIR, cam_id)
                    if os.path.exists(old_dir):
                        try:
                            os.rename(old_dir, new_dir)
                        except:
                            pass

                logging.info(
                    f"🛡️ [安全审计] 用户 [{self.current_user}] 修改并重载了设备信息: {original_id} -> {cam_id}")

            if self.engine.ui:
                self.engine.root.after(0, self.engine.ui.refresh_desktop_tree)

            self.send_json({"status": "ok", "action": action})

        elif path == '/api/groups':
            if self.current_role not in ['superadmin', 'admin']: return self.send_error(403)
            grp = str(post_data.get('group', '')).strip()
            if grp:
                parts = grp.split('/')
                curr_path = ""
                added_any = False
                for part in parts:
                    part = part.strip()
                    if not part: continue
                    curr_path = curr_path + "/" + part if curr_path else part
                    if curr_path not in config_manager.GROUPS_DB:
                        config_manager.GROUPS_DB.append(curr_path)
                        added_any = True
                if added_any:
                    config_manager.save_groups()
                    if self.engine.ui: self.engine.root.after(0, self.engine.ui.refresh_group_combobox)
                    logging.info(f"🛡️ [安全审计] 用户 [{self.current_user}] 创建了新的组织层级架构: {grp}")
            self.send_json({"status": "ok"})

        elif path == '/api/control':
            if self.current_role not in ['superadmin', 'admin']: return self.send_error(403)
            action = post_data.get('action')
            if action == 'start':
                logging.warning(f"🛡️ [安全审计] ⚠️ 用户 [{self.current_user}] 下发了 [强制全量拉流启动] 紧急指令！")
                self.engine.root.after(0, self.engine.start_all)
            elif action == 'stop':
                logging.warning(f"🛡️ [安全审计] ⚠️ 用户 [{self.current_user}] 下发了 [强制全部断流休眠] 紧急指令！")
                self.engine.root.after(0, self.engine.stop_all)
            elif action == 'restart_service':
                logging.warning(
                    f"🛡️ [安全审计] 🔄 用户 [{self.current_user}] 请求了 [热重启后台核心服务] 指令！守护进程将自动拉起。")
                self.send_json({"status": "ok"})
                self.engine.root.after(1000, lambda: os._exit(0))
                return
            self.send_json({"status": "ok"})

        elif path == '/api/schedule':
            if self.current_role not in ['superadmin', 'admin']: return self.send_error(403)
            self.engine.root.after(0, self.engine.web_api_save_schedule, post_data)
            self.send_json({"status": "ok"})

        elif path == '/api/web_batch_download':
            date_str = post_data.get('date', '')
            slots = post_data.get('slots', [])
            cams = post_data.get('cams', [])
            tasks = post_data.get('tasks', [])

            if not date_str or (not tasks and (not slots or not cams)):
                return self.send_error(400, "缺少必要的提取参数或时间段为空")

            logging.info(f"🛡️ [数据排队] 用户 [{self.current_user}] 提交了并发提取。开始列队处理...")

            job_id = str(uuid.uuid4())
            self.engine.batch_jobs[job_id] = {
                "total": len(tasks) if tasks else len(cams) * len(slots),
                "completed": 0,
                "failed": 0,
                "status": "running",
                "results": []
            }

            threading.Thread(target=self.engine.run_batch_job, args=(job_id, date_str, slots, cams, tasks),
                             daemon=True).start()
            self.send_json({"status": "ok", "job_id": job_id})

        elif path == '/api/web_batch_download_excel':
            tasks = post_data.get('tasks', [])

            if not tasks:
                return self.send_error(400, "缺少精确的课程提取任务清单！")

            logging.info(
                f"🛡️ [数据排队] 用户 [{self.current_user}] 提交了智能 Excel 检索过滤并发提取。任务总数: {len(tasks)}")

            job_id = str(uuid.uuid4())
            self.engine.batch_jobs[job_id] = {
                "total": len(tasks),
                "completed": 0,
                "failed": 0,
                "status": "running",
                "results": []
            }

            threading.Thread(target=self._run_excel_range_batch, args=(job_id, tasks), daemon=True).start()
            self.send_json({"status": "ok", "job_id": job_id})

        elif path == '/api/users':
            action = post_data.get('action', 'save')

            if action == 'change_pwd':
                new_pwd = str(post_data.get('password', '')).strip()
                if new_pwd in ['123456', 'admin'] or not is_complex_password(new_pwd):
                    return self.send_error(400, "密码太弱")
                config_manager.USERS_DB[self.current_user]['password'] = new_pwd
                config_manager.save_users()
                logging.info(f"🛡️ [安全审计] 用户 [{self.current_user}] 修改了自己的登录密码。")
                return self.send_json({"status": "ok"})

            if self.current_role != 'superadmin': return self.send_error(403)
            username = str(post_data.get('username', '')).strip()
            if not username: return self.send_error(400)

            if action == 'reset_pwd':
                new_pwd = str(post_data.get('password', '')).strip()
                if not new_pwd or username not in config_manager.USERS_DB: return self.send_error(400)
                if not is_complex_password(new_pwd): return self.send_error(400)
                config_manager.USERS_DB[username]['password'] = new_pwd
                config_manager.save_users()
                logging.warning(f"🛡️ [安全审计] 超级管理员 [{self.current_user}] 强制重置了账号 [{username}] 的密码！")
                return self.send_json({"status": "ok"})

            password = str(post_data.get('password', '')).strip()
            role = str(post_data.get('role', 'user')).strip()
            desc = str(post_data.get('desc', '')).strip()

            if username == 'admin' and role != 'superadmin': role = 'superadmin'

            if username in config_manager.USERS_DB:
                if not password:
                    password = config_manager.USERS_DB[username]['password']
                elif not is_complex_password(password):
                    return self.send_error(400, "密码过弱")
            else:
                if not password: password = '123456'
                if not is_complex_password(password): return self.send_error(400, "密码过弱")

            config_manager.USERS_DB[username] = {"password": password, "role": role, "desc": desc}
            config_manager.save_users()
            logging.info(f"🛡️ [安全审计] 超管 [{self.current_user}] 变动了账户权限数据：分配/修改了账号 [{username}]。")
            self.send_json({"status": "ok"})
        else:
            self.send_error(404)

    def do_DELETE(self):
        parsed_path = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed_path.path)

        if not self.check_auth_or_401(): return
        query = urllib.parse.parse_qs(parsed_path.query)

        if path == '/api/cameras':
            if self.current_role not in ['superadmin', 'admin']: return self.send_error(403)
            cam_id = query.get('id', [''])[0]

            self.engine.root.after(0, self.engine.web_api_delete_camera, cam_id)

            cam_dir = os.path.join(config_manager.STORAGE_DIR, cam_id)
            if os.path.exists(cam_dir):
                try:
                    shutil.rmtree(cam_dir)
                    logging.info(f"🗑️ [数据清理] 已彻底清除摄像头 [{cam_id}] 的所有底层录像文件与目录。")
                except Exception as e:
                    logging.error(f"清除历史录像失败: {e}")

            logging.warning(f"🛡️ [安全审计] 用户 [{self.current_user}] 删除了监控设备及其全部录像: {cam_id}")
            self.send_json({"status": "ok"})

        elif path == '/api/groups':
            if self.current_role not in ['superadmin', 'admin']: return self.send_error(403)
            grp = query.get('group', [''])[0]
            if grp == '默认分组': return self.send_error(400, "默认分组不可删")
            if grp in config_manager.GROUPS_DB:
                config_manager.GROUPS_DB.remove(grp)
                config_manager.save_groups()

                changed = False
                for c in self.engine.cameras:
                    if str(c.get('group', '')) == grp:
                        c['group'] = '默认分组'
                        changed = True
                if changed:
                    config_manager.save_cameras(self.engine.cameras)

                if self.engine.ui: self.engine.root.after(0, self.engine.ui.refresh_group_combobox)
                logging.warning(f"🛡️ [安全审计] 用户 [{self.current_user}] 废除了架构组: {grp}")
            self.send_json({"status": "ok"})

        elif path == '/api/users':
            if self.current_role != 'superadmin': return self.send_error(403)
            username = query.get('username', [''])[0]
            if username == 'admin': return self.send_error(400)
            if username in config_manager.USERS_DB:
                del config_manager.USERS_DB[username]
                config_manager.save_users()
                logging.warning(f"🛡️ [安全审计] 超级管理员 [{self.current_user}] 移除了账号: {username}")
            self.send_json({"status": "ok"})
        else:
            self.send_error(404)

    def _run_excel_range_batch(self, job_id, explicit_tasks):
        engine = self.engine
        export_folder_name = f"Excel_Batch_{int(time.time())}"
        export_dir = os.path.join(config_manager.STORAGE_DIR, "Web_Batch_Export", export_folder_name)
        os.makedirs(export_dir, exist_ok=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            future_to_task = {}
            for t in explicit_tasks:
                cam_id = t.get('cam')
                date_str = t.get('date')
                start_str = t.get('start')
                end_str = t.get('end')
                try:
                    req_start = datetime.strptime(f"{date_str} {start_str}", "%Y-%m-%d %H:%M")
                    req_end = datetime.strptime(f"{date_str} {end_str}", "%Y-%m-%d %H:%M")
                except ValueError:
                    engine.batch_jobs[job_id]["failed"] += 1
                    engine.batch_jobs[job_id]["completed"] += 1
                    continue

                future = executor.submit(engine._process_single_cam_batch, cam_id, req_start, req_end, export_dir,
                                         export_folder_name, date_str, start_str, end_str)
                future_to_task[future] = t

            for future in concurrent.futures.as_completed(future_to_task):
                res = future.result()
                if res["status"] == "ok":
                    engine.batch_jobs[job_id]["results"].append(res["result"])
                else:
                    engine.batch_jobs[job_id]["failed"] += 1
                engine.batch_jobs[job_id]["completed"] += 1

        if engine.batch_jobs[job_id]["results"]:
            zip_fname = f"Excel_Batch_{int(time.time())}.zip"
            zip_fpath = os.path.join(export_dir, zip_fname)
            try:
                with zipfile.ZipFile(zip_fpath, 'w', zipfile.ZIP_STORED, allowZip64=True) as zipf:
                    for res in engine.batch_jobs[job_id]["results"]:
                        mp4_phys_path = res.get("path")
                        mp4_filename = res.get("fname")
                        if mp4_phys_path and os.path.exists(mp4_phys_path):
                            zipf.write(mp4_phys_path, mp4_filename)

                engine.batch_jobs[job_id][
                    "zip_url"] = f"/Web_Batch_Export/{export_folder_name}/{urllib.parse.quote(zip_fname)}"
            except Exception as e:
                err_msg = str(e) or repr(e)
                logging.error(f"批量打包ZIP失败: {err_msg}")
                engine.batch_jobs[job_id]["zip_error"] = err_msg
                if os.path.exists(zip_fpath):
                    try:
                        os.remove(zip_fpath)
                    except:
                        pass

        engine.batch_jobs[job_id]["status"] = "done"
        logging.info(f"✅ [Excel智能跨日排队] Job {job_id} 圆满完工！")

    def send_json(self, obj, status=200):
        encoded = json.dumps(obj).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-type', 'application/json')
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def api_whoami(self):
        pwd = str(config_manager.USERS_DB.get(self.current_user, {}).get('password', ''))
        self.send_json({
            "username": self.current_user,
            "role": self.current_role,
            "must_change_pwd": (pwd in ['123456', 'admin'])
        })

    def api_cameras(self):
        cams = []
        running_cams = self.engine.go_running_cams if self.engine else []

        if self.engine:
            for c in self.engine.cameras:
                cam_id = str(c['id'])
                status = "就绪 (未启动)"
                if cam_id in running_cams:
                    status = "正在录制 ⏺"
                elif self.engine.is_running:
                    status = "断线重连中 ⚠️"

                cams.append({
                    "id": cam_id,
                    "status": status,
                    "group": str(c.get('group', '默认分组')),
                    "ip": str(c.get('ip', '')),
                    "brand": str(c.get('brand', 'hikvision')),
                    "user": str(c.get('user', 'admin')),
                    "pwd": str(c.get('pwd', '')),
                    "port": str(c.get('port', '554')),
                    "channel": str(c.get('channel', '102')),
                    "url": str(c.get('url', ''))
                })

        self.send_json({"cameras": cams})

    def api_users_get(self):
        if self.current_role != 'superadmin': return self.send_error(403)
        safe_users = [{"username": u, "role": i.get("role"), "desc": i.get("desc")} for u, i in
                      config_manager.USERS_DB.items()]
        self.send_json(safe_users)

    def api_groups_get(self):
        self.send_json(config_manager.GROUPS_DB)

    def api_schedule_get(self):
        if self.current_role not in ['superadmin', 'admin']: return self.send_error(403)
        self.send_json({
            "retention_days": self.engine.retention_days,
            "schedule_enabled": self.engine.schedule_enabled,
            "sch1_en": self.engine.sch1_en, "sch1_start": self.engine.sch1_start, "sch1_end": self.engine.sch1_end,
            "sch2_en": self.engine.sch2_en, "sch2_start": self.engine.sch2_start, "sch2_end": self.engine.sch2_end,
            "sch3_en": self.engine.sch3_en, "sch3_start": self.engine.sch3_start, "sch3_end": self.engine.sch3_end
        })

    def api_logs_get(self, query_string):
        if self.current_role != 'superadmin': return self.send_error(403)
        params = urllib.parse.parse_qs(query_string)
        date_str = params.get('date', [''])[0]

        log_content = "暂无系统运行日志记录。"
        target_file = config_manager.LOG_FILE

        if date_str:
            try:
                target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                if target_date < datetime.now().date():
                    target_file = f"{config_manager.LOG_FILE}.{date_str}"
            except ValueError:
                pass

        try:
            if os.path.exists(target_file):
                with open(target_file, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    if target_file == config_manager.LOG_FILE:
                        log_content = "".join(lines[-500:])
                    else:
                        log_content = "".join(lines)
            else:
                if target_file != config_manager.LOG_FILE:
                    log_content = f"⚠️ 未找到日期为 [{date_str}] 的历史归档日志文件。可能尚无操作记录，或已被自动滚动清理。"
        except Exception as e:
            log_content = f"读取日志文件失败: {e}"

        self.send_json({"logs": log_content})

    def api_log_download(self, query_string):
        if self.current_role != 'superadmin': return self.send_error(403)
        params = urllib.parse.parse_qs(query_string)
        date_str = params.get('date', [''])[0]

        target_file = config_manager.LOG_FILE
        if date_str:
            try:
                target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                if target_date < datetime.now().date():
                    target_file = f"{config_manager.LOG_FILE}.{date_str}"
            except ValueError:
                pass

        if os.path.exists(target_file):
            try:
                with open(target_file, "rb") as f:
                    file_data = f.read()
                self.send_response(200)
                self.send_header('Content-type', 'text/plain; charset=utf-8')
                filename = f"System_Audit_Log_{date_str or 'Today'}.txt"
                self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
                self.send_header('Content-Length', str(len(file_data)))
                self.end_headers()
                self.wfile.write(file_data)
            except Exception as e:
                self.send_error(500, f"Error reading log: {e}")
        else:
            self.send_error(404, "Log file not found")

    def api_search(self, query_string):
        params = urllib.parse.parse_qs(query_string)
        cam_id = params.get('cam', [''])[0]
        start_str = params.get('start', [''])[0]
        end_str = params.get('end', [''])[0]

        cam_dir = os.path.join(config_manager.STORAGE_DIR, cam_id)
        total_size = 0
        count = 0
        segments = []

        if os.path.isdir(cam_dir):
            try:
                start_dt = datetime.strptime(start_str, "%Y-%m-%dT%H:%M") if start_str else datetime.min
                end_dt = datetime.strptime(end_str, "%Y-%m-%dT%H:%M") if end_str else datetime.max
                for fname in os.listdir(cam_dir):
                    if fname.endswith(".mp4"):
                        match = re.search(r'_(\d{8}_\d{6})\.mp4$', fname)
                        if match:
                            try:
                                file_start_dt = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
                                file_end_dt = file_start_dt + timedelta(seconds=config_manager.SEGMENT_TIME)
                                if file_end_dt >= start_dt and file_start_dt <= end_dt:
                                    fpath = os.path.join(cam_dir, fname)
                                    fsize = os.path.getsize(fpath)
                                    total_size += fsize
                                    count += 1
                                    segments.append({
                                        "filename": fname,
                                        "time": file_start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                                        "size": round(fsize / (1024 * 1024), 1),
                                        "url": f"/{urllib.parse.quote(cam_id)}/{urllib.parse.quote(fname)}"
                                    })
                            except ValueError:
                                pass
            except Exception as e:
                logging.error(f"检索 API 处理异常: {e}")

        segments.sort(key=lambda x: x['time'])
        self.send_json({
            "count": count,
            "total_size_mb": round(total_size / (1024 * 1024), 1),
            "segments": segments
        })

    def api_batch_zip(self, query_string):
        params = urllib.parse.parse_qs(query_string)
        cam_id = params.get('cam', [''])[0]
        files_str = params.get('files', [''])[0]

        if not cam_id or not files_str:
            self.send_error(400, "缺少参数")
            return

        logging.info(
            f"🛡️ [数据查阅] 用户 [{self.current_user}] 执行了视频打包下载指令: 目标 [{cam_id}] 批处理片段数: {len(files_str.split(','))}")

        files_to_zip = files_str.split(',')
        cam_dir = os.path.join(config_manager.STORAGE_DIR, cam_id)
        export_dir = os.path.join(cam_dir, "Export_Download")
        os.makedirs(export_dir, exist_ok=True)

        zip_fname = f"NVR_Batch_{cam_id}_{int(time.time())}.zip"
        zip_fpath = os.path.join(export_dir, zip_fname)

        try:
            with zipfile.ZipFile(zip_fpath, 'w', zipfile.ZIP_STORED, allowZip64=True) as zipf:
                for fname in files_to_zip:
                    f_path = os.path.join(cam_dir, fname)
                    if os.path.exists(f_path):
                        zipf.write(f_path, fname)

            url = f"/{urllib.parse.quote(cam_id)}/Export_Download/{urllib.parse.quote(zip_fname)}"
            self.send_json({"url": url})
        except Exception as e:
            logging.error(f"打包 ZIP 失败: {e}")
            self.send_error(500, "内部打包异常")

    def api_export(self, query_string):
        params = urllib.parse.parse_qs(query_string)
        cam_id = params.get('cam', [''])[0]
        start_str = params.get('start', [''])[0]
        end_str = params.get('end', [''])[0]

        logging.info(
            f"🛡️ [数据查阅] 用户 [{self.current_user}] 请求生成长录像流/下载: 目标 [{cam_id}] 时间段: {start_str} 至 {end_str}")

        cam_dir = os.path.join(config_manager.STORAGE_DIR, cam_id)
        export_dir = os.path.join(cam_dir, "Export_Download")
        os.makedirs(export_dir, exist_ok=True)

        try:
            req_start = datetime.strptime(start_str, "%Y-%m-%dT%H:%M")
            req_end = datetime.strptime(end_str, "%Y-%m-%dT%H:%M")
        except ValueError:
            self.send_error(400, "Invalid time format")
            return

        files_to_merge = []
        first_file_dt = None

        if os.path.isdir(cam_dir):
            for fname in sorted(os.listdir(cam_dir)):
                if fname.endswith(".mp4"):
                    match = re.search(r'_(\d{8}_\d{6})\.mp4$', fname)
                    if match:
                        try:
                            file_start_dt = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
                            file_end_dt = file_start_dt + timedelta(seconds=config_manager.SEGMENT_TIME)
                            if file_end_dt >= req_start and file_start_dt <= req_end:
                                files_to_merge.append(fname)
                                if first_file_dt is None: first_file_dt = file_start_dt
                        except ValueError:
                            pass

        if not files_to_merge:
            self.send_error(404, "No video files found to merge")
            return

        list_file_name = f"concat_list_{int(time.time())}.txt"
        list_file_path = os.path.join(export_dir, list_file_name)
        with open(list_file_path, "w", encoding="utf-8") as f:
            for fname in files_to_merge:
                abs_path = os.path.abspath(os.path.join(cam_dir, fname)).replace("\\", "/")
                f.write(f"file '{abs_path}'\n")

        start_offset = max(0, (req_start - first_file_dt).total_seconds())
        duration = (req_end - req_start).total_seconds()
        out_fname = f"NVR_{cam_id}_{req_start.strftime('%Y%m%d_%H%M')}-{req_end.strftime('%H%M')}.mp4"
        out_fpath = os.path.join(export_dir, out_fname)

        if not os.path.exists(out_fpath):
            cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file_path, "-ss", str(start_offset), "-t",
                   str(duration), "-map", "0:v", "-map", "0:a?", "-c", "copy", out_fpath]
            try:
                logging.info(f"正在无损视频拼合: {' '.join(cmd)}")
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            except subprocess.CalledProcessError as e:
                logging.error(f"视频无损合并失败: {e.stderr}")
                self.send_error(500, "FFmpeg Merge Failed")
                return
            finally:
                if os.path.exists(list_file_path): os.remove(list_file_path)

        url = f"/{urllib.parse.quote(cam_id)}/Export_Download/{urllib.parse.quote(out_fname)}"
        self.send_json({"url": url})

    def api_live_start(self, query_string):
        params = urllib.parse.parse_qs(query_string)
        cam_id = params.get('cam', [''])[0]
        if not cam_id: return self.send_error(400, "Miss cam parameter")

        cam_info = next((c for c in self.engine.cameras if str(c["id"]) == cam_id), None)
        if not cam_info: return self.send_error(404, "Camera not found")

        logging.info(f"🌐 [在线直播] 用户 [{self.current_user}] 正在发起对设备 [{cam_id}] 的实时预览流请求...")
        m3u8_url = self.engine.start_live_stream(cam_info)

        m3u8_path = os.path.join(config_manager.STORAGE_DIR, "live_temp", f"{cam_id}.m3u8")
        for _ in range(10):
            if os.path.exists(m3u8_path): break
            time.sleep(0.5)

        self.send_json({"status": "ok", "url": m3u8_url})

    def api_live_heartbeat(self, query_string):
        params = urllib.parse.parse_qs(query_string)
        cam_id = params.get('cam', [''])[0]
        if cam_id in self.engine.live_processes:
            self.engine.live_processes[cam_id]['last_heartbeat'] = time.time()
        self.send_json({"status": "ok"})

    def api_live_stop(self, query_string):
        params = urllib.parse.parse_qs(query_string)
        cam_id = params.get('cam', [''])[0]
        self.engine.stop_live_stream(cam_id)
        logging.info(f"🌐 [在线直播] 用户 [{self.current_user}] 主动退出了设备 [{cam_id}] 的预览。")
        self.send_json({"status": "ok"})

    def serve_html_app(self):
        html_content = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <title>企业级监控拉流中控系统</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.4.12/hls.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js"></script>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f4f6f9; margin: 0; overflow: hidden; }

        /* 登录界面样式 */
        #login-view { background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%); width: 100vw; height: 100vh; display: flex; justify-content: center; align-items: center; }
        .login-box { background: white; padding: 40px; border-radius: 12px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); width: 100%; max-width: 380px; box-sizing: border-box; }
        .login-box h2 { margin-top: 0; color: #0f172a; text-align: center; font-size: 22px; margin-bottom: 30px; letter-spacing: 1px;}
        .icon-shield { text-align: center; font-size: 40px; margin-bottom: 15px; }
        .input-group { margin-bottom: 20px; }
        .input-group label { display: block; font-size: 13px; font-weight: 600; color: #475569; margin-bottom: 8px; }
        .input-group input { width: 100%; padding: 12px; border: 1px solid #cbd5e1; border-radius: 6px; box-sizing: border-box; font-size: 15px; outline: none; transition: all 0.2s; background: #f8fafc;}
        .input-group input:focus { border-color: #3b82f6; background: #ffffff; box-shadow: 0 0 0 3px rgba(59,130,246,0.2); }
        .btn-login { width: 100%; background: #3b82f6; color: white; border: none; padding: 14px; border-radius: 6px; font-size: 16px; font-weight: bold; cursor: pointer; transition: background 0.2s; margin-top: 10px;}
        .btn-login:hover { background: #2563eb; }
        .btn-login:disabled { opacity: 0.7; cursor: not-allowed; }
        .error-msg { color: #ef4444; font-size: 13px; margin-top: -10px; margin-bottom: 15px; display: none; text-align: center; font-weight: bold; background: #fef2f2; padding: 10px; border-radius: 6px; border: 1px solid #fecaca;}

        /* 主程序样式 */
        #app-view { display: none; height: 100vh; width: 100vw; display: flex; }
        .sidebar { width: 320px; background: #1e293b; color: #e2e8f0; padding: 20px 10px; overflow-y: auto; box-shadow: 2px 0 5px rgba(0,0,0,0.1); z-index: 10; display: flex; flex-direction: column;}
        .sidebar h2 { margin: 0 10px 15px 10px; padding-bottom: 15px; border-bottom: 1px solid #334155; font-size: 18px; color: #fff; display: flex; justify-content: space-between; align-items: center;}
        .role-badge { font-size: 12px; padding: 3px 6px; border-radius: 4px; background: #3b82f6; color: white; }
        .role-badge.superadmin { background: #ef4444; }
        .role-badge.admin { background: #f59e0b; }
        .folder-item { padding: 10px 15px; cursor: pointer; color: #cbd5e1; font-weight: 600; display: flex; align-items: center; border-radius: 4px; margin-bottom: 2px; transition: background 0.2s;}
        .folder-item:hover { background: #334155; color: white;}
        .folder-icon { margin-right: 8px; font-size: 15px; }
        .cam-item { padding: 10px 15px; margin-bottom: 2px; border-radius: 4px; cursor: pointer; transition: all 0.2s ease; display: flex; align-items: center; justify-content: space-between; color: #94a3b8;}
        .cam-item:hover { background: #334155; color: white;}
        .cam-item.active { background: #3b82f6; color: white; font-weight: bold; box-shadow: 0 2px 4px rgba(59,130,246,0.3); }
        .cam-status { font-size: 12px; opacity: 0.8; }
        .main { flex: 1; display: flex; flex-direction: column; background: #f8fafc; overflow: hidden;}
        .top-nav { background: white; padding: 0 30px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); display: flex; align-items: center; height: 60px; z-index: 5; flex-shrink: 0; overflow-x: auto;}
        .nav-btn { background: none; border: none; padding: 0 20px; height: 100%; font-size: 16px; font-weight: 600; color: #64748b; cursor: pointer; border-bottom: 3px solid transparent; transition: all 0.2s; white-space: nowrap;}
        .nav-btn:hover { color: #3b82f6; }
        .nav-btn.active { color: #3b82f6; border-bottom-color: #3b82f6; }

        .content-area { padding: 20px 30px; overflow-y: auto; flex: 1; }

        .tab-pane { display: none !important; }
        .tab-pane.active { display: block !important; }

        .header-title { color: #0f172a; margin-top: 0; font-size: 24px; margin-bottom: 20px; }
        .card { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.05); margin-bottom: 20px; border: 1px solid #e2e8f0; }
        .toolbar { display: flex; gap: 15px; align-items: center; flex-wrap: wrap; margin-bottom: 15px;}
        .toolbar label { font-weight: 600; color: #475569; font-size: 14px; }
        .toolbar input, .toolbar select { padding: 10px 12px; border: 1px solid #cbd5e1; border-radius: 6px; outline: none; font-family: inherit; }
        .btn { padding: 10px 20px; border-radius: 6px; cursor: pointer; font-weight: bold; border: none; color: white; transition: background 0.2s; }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .btn-primary { background: #3b82f6; } .btn-primary:hover { background: #2563eb; }
        .btn-success { background: #10b981; } .btn-success:hover { background: #059669; }
        .btn-danger { background: #ef4444; } .btn-danger:hover { background: #dc2626; }
        .btn-warning { background: #f59e0b; } .btn-warning:hover { background: #d97706; }
        .btn-live { background: #0ea5e9; padding: 5px 10px; font-size: 12px;} .btn-live:hover { background: #0284c7; }
        .player-container { background: #000; border-radius: 10px; margin-bottom: 20px; display: none; text-align: center; padding: 10px; }
        video { max-width: 100%; max-height: 450px; outline: none; border-radius: 6px; }
        .grid { width: 100%; border-collapse: collapse; text-align: left; }
        .grid th, .grid td { padding: 15px 20px; border-bottom: 1px solid #f1f5f9; }
        .grid th { background: #f8fafc; font-weight: 600; color: #475569; font-size: 13px; }
        .grid tr:hover td { background: #f8fafc; }
        .action-link { cursor: pointer; color: #3b82f6; font-weight: 600; font-size: 13px; text-decoration: none; margin-right: 10px;}
        .action-link:hover { text-decoration: underline; }
        .action-link.del { color: #ef4444; }
        .action-link.reset { color: #f59e0b; }
        .empty-msg { text-align: center; padding: 40px; color: #64748b; }
        .loading-spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid rgba(255,255,255,0.3); border-radius: 50%; border-top-color: #fff; animation: spin 1s ease-in-out infinite; vertical-align: middle; margin-right: 5px; }
        @keyframes spin { to { transform: rotate(360deg); } }
        .log-console { background: #1e1e1e; color: #10b981; padding: 15px; border-radius: 6px; height: 500px; overflow-y: auto; font-family: monospace; white-space: pre-wrap; word-wrap: break-word; font-size: 13px; line-height: 1.5; border: 1px solid #0f172a;}
        .log-console .alarm { color: #ef4444; font-weight: bold; }
        .log-console .recover { color: #3b82f6; font-weight: bold; }
        .modal-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(15,23,42,0.9); z-index: 9999; justify-content: center; align-items: center; backdrop-filter: blur(5px);}
        .live-modal { background: #1e293b; padding: 20px; border-radius: 12px; width: 800px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); position: relative; border: 1px solid #334155; }
        .live-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; color: white; border-bottom: 1px solid #334155; padding-bottom: 10px;}
        .close-btn { background: #ef4444; color: white; border: none; padding: 5px 15px; border-radius: 5px; cursor: pointer; font-weight: bold; }
        .close-btn:hover { background: #dc2626; }
        .live-video-box { background: black; width: 100%; height: 450px; border-radius: 8px; display: flex; justify-content: center; align-items: center; position: relative; }
        .live-video-box video { width: 100%; height: 100%; border-radius: 8px; object-fit: contain; }
        .live-status-badge { position: absolute; top: 10px; left: 10px; background: rgba(220,38,38,0.8); color: white; padding: 4px 8px; font-size: 12px; border-radius: 4px; font-weight: bold; z-index: 2; display: flex; align-items: center;}
        .live-status-badge .dot { width: 8px; height: 8px; background: #fff; border-radius: 50%; margin-right: 6px; animation: blink 1s infinite; }
        @keyframes blink { 50% { opacity: 0.3; } }
    </style>
</head>
<body>
    <!-- 登录界面 -->
    <div id="login-view">
        <div class="login-box">
            <div class="icon-shield">🛡️</div>
            <h2>贵阳市新世界学校拉流系统</h2>
            <div class="error-msg" id="error-msg">账号或密码错误，请重试。</div>
            <div class="input-group">
                <label>身份账号</label>
                <input type="text" id="username" placeholder="请输入您的账号" autocomplete="off" onkeydown="if(event.keyCode==13) document.getElementById('password').focus()">
            </div>
            <div class="input-group">
                <label>安全凭证</label>
                <input type="password" id="password" placeholder="请输入密码" onkeydown="if(event.keyCode==13) doLogin()">
            </div>
            <button class="btn-login" onclick="doLogin()" id="login-btn">验证身份并登入</button>
        </div>
    </div>

    <!-- 主控制台界面 -->
    <div id="app-view">
        <div class="sidebar" id="sidebar">
            <h2>
                <span>🎥 设备架构</span>
                <div style="display:flex; align-items:center;">
                    <span id="ui-role" class="role-badge">加载中</span>
                </div>
            </h2>
            <div id="cam-list" style="flex:1; overflow-y:auto; padding-right:5px;">
                <div class="empty-msg">正在加载树状通道...</div>
            </div>
        </div>

        <div class="main">
            <div class="top-nav">
                <button class="nav-btn active" id="nav-search" onclick="switchTab('search')">🎞️ 录像回放与检索</button>
                <button class="nav-btn" id="nav-batch" onclick="switchTab('batch')" style="display:none; color:#10b981;">📥 极简课表提取下载</button>
                <button class="nav-btn" id="nav-excel" onclick="switchTab('excel')" style="display:none; color:#8b5cf6;">📅 智能Excel课表解析下载</button>
                <button class="nav-btn" id="nav-cams" onclick="switchTab('cams')" style="display:none;">📹 远程设备与全局控制</button>
                <button class="nav-btn" id="nav-users" onclick="switchTab('users')" style="display:none;">👥 Web账号权限管理</button>
                <button class="nav-btn" id="nav-logs" onclick="switchTab('logs')" style="display:none;">📜 系统审计日志</button>
            </div>

            <div class="content-area">

                <!-- Tab: 录像检索 -->
                <div id="tab-search" class="tab-pane active">
                    <h2 class="header-title" id="cam-title">请先从左侧选择监控通道</h2>
                    <div class="card">
                        <div class="toolbar">
                            <label>检索开始时间:</label>
                            <input type="datetime-local" id="time-start">
                            <label>检索结束时间:</label>
                            <input type="datetime-local" id="time-end">
                            <button class="btn btn-primary" onclick="searchVideos()">🔍 立即检索</button>
                        </div>
                    </div>
                    <div class="player-container" id="player-container">
                        <video id="player" controls autoplay></video>
                    </div>
                    <div class="card" style="padding:0;">
                        <table class="grid" id="result-grid">
                            <thead><tr><th width="35%">检索时段</th><th width="30%">底层切片状态</th><th width="35%">操作选项</th></tr></thead>
                            <tbody id="result-body"><tr><td colspan="3" class="empty-msg">等待检索条件...</td></tr></tbody>
                        </table>
                    </div>
                </div>

                <!-- Tab: 智能课表提取下载 -->
                <div id="tab-excel" class="tab-pane">
                    <h2 class="header-title">智能 Excel 课表解析与自动化下载</h2>
                    <div class="card">
                        <h3 style="margin-top:0; color: #8b5cf6;">✅ 无需后台 Pandas 库，浏览器瞬间解析 Excel，自动根据日期（星期几）匹配所选日期范围内所有的课程录像。</h3>

                        <div class="toolbar" style="background:#f8fafc; padding:15px; border-radius:8px; border:1px solid #e2e8f0; align-items: flex-start; flex-direction: column;">
                            <div style="margin-bottom: 10px; width: 100%; display: flex; align-items: center; flex-wrap: wrap; gap: 10px;">
                                <label style="display:inline-block; font-weight:bold; color:#1e293b;">① 提取起止日期:</label> 
                                <input type="date" id="excel-date-start" style="padding: 8px; border: 1px solid #cbd5e1; border-radius: 4px;">
                                <span style="color: #64748b; font-weight:bold;">至</span>
                                <input type="date" id="excel-date-end" style="padding: 8px; border: 1px solid #cbd5e1; border-radius: 4px;">
                                <span style="margin-left:10px; color:#64748b; font-size:13px;">(系统将自动算出包含的每一天是星期几，并去表格里捞当天的课)</span>
                            </div>

                            <div style="width: 100%; display: flex; align-items: center;">
                                <label style="display:inline-block; font-weight:bold; color:#1e293b;">② 上传课表(Excel):</label> 
                                <input type="file" id="excel-file" accept=".xlsx, .xls, .csv" style="margin-left: 10px; padding: 6px; border: 1px solid #cbd5e1; background: white; border-radius: 4px; width: 300px;">
                                <button class="btn btn-primary" style="margin-left: 15px; background: #8b5cf6;" onclick="parseExcel()">📊 解析所选日期范围内的所有课程</button>
                            </div>
                        </div>

                        <!-- 课表二次检索工具栏 -->
                        <div id="excel-filter-bar" style="display:none; margin-top: 15px; padding: 15px; background: #e0e7ff; border-radius: 8px; border: 1px solid #c7d2fe; align-items: center; flex-wrap: wrap; gap: 10px;">
                            <span style="font-weight:bold; color:#4338ca; font-size: 15px;">🔍 课程二次检索:</span>
                            <input type="text" id="search-class" placeholder="班级 (例: 七(10)班)" style="padding:8px; border:1px solid #cbd5e1; border-radius:4px; width: 140px; outline: none;">
                            <input type="text" id="search-subject" placeholder="学科 (例: 英语)" style="padding:8px; border:1px solid #cbd5e1; border-radius:4px; width: 110px; outline: none;">
                            <input type="text" id="search-teacher" placeholder="任课教师 (例: 蒋春敏)" style="padding:8px; border:1px solid #cbd5e1; border-radius:4px; width: 140px; outline: none;">
                            <input type="text" id="search-time" placeholder="时间段 (例: 08:00)" style="padding:8px; border:1px solid #cbd5e1; border-radius:4px; width: 120px; outline: none;">
                            <button class="btn btn-primary" style="background:#4f46e5; margin-left:10px;" onclick="applyExcelFilters()">过滤列表</button>
                            <button class="btn btn-warning" style="background:#64748b;" onclick="resetExcelFilters()">重置条件</button>
                            <span style="width:100%; display:block; color:#6366f1; font-size:12px; margin-top:5px;">* 提示：检索后，下方的列表会自动更新。点击最下方的下载按钮，只会打包提取当前展示出来的这些课程。</span>
                        </div>

                        <div style="margin-top: 20px;">
                            <table class="grid" id="excel-grid">
                                <thead>
                                    <tr>
                                        <th width="8%"><input type="checkbox" id="excel-select-all" checked onclick="toggleExcelCams(this.checked)"> 全选</th>
                                        <th width="15%">提取日期</th>
                                        <th width="15%">班级 (关联监控)</th>
                                        <th width="10%">星期</th>
                                        <th width="17%">时间段</th>
                                        <th width="15%">学科</th>
                                        <th width="10%">任课教师</th>
                                        <th width="10%">提取状态</th>
                                    </tr>
                                </thead>
                                <tbody id="excel-result-body">
                                    <tr><td colspan="8" class="empty-msg">请先上传包含“班级, 星期, 开始时间, 结束时间”等表头的 Excel 并点击解析...</td></tr>
                                </tbody>
                            </table>
                        </div>

                        <!-- 进度条区 -->
                        <div id="excel-progress-container" style="display:none; margin-top:25px; padding: 15px; border-radius: 8px; border: 1px dashed #8b5cf6; background: #f5f3ff;">
                            <div style="font-weight:bold; margin-bottom:10px; color:#5b21b6; font-size:15px;" id="excel-progress-text">任务准备中...</div>
                            <div style="width:100%; background:#e2e8f0; border-radius:8px; overflow:hidden; height:24px; box-shadow: inset 0 1px 2px rgba(0,0,0,0.1);">
                                <div id="excel-progress-bar" style="width:0%; background: linear-gradient(90deg, #8b5cf6 0%, #6d28d9 100%); height:100%; transition:width 0.4s ease;"></div>
                            </div>
                        </div>

                        <div style="margin-top: 25px; border-top: 1px dashed #cbd5e1; padding-top: 20px;">
                            <button class="btn btn-success" id="btn-excel-batch" style="width:100%; font-size:16px; padding:15px; display:none; background: #8b5cf6;" onclick="doExcelBatchDownload()">🚀 立即把表格中勾选的课程送入后台排队提取ZIP包</button>
                        </div>

                        <div id="excel-batch-result" style="margin-top: 20px; display:none; background:#ecfdf5; padding:15px; border:1px solid #10b981; border-radius:6px;">
                            <div id="excel-batch-links"></div>
                        </div>
                    </div>
                </div>

                <!-- Tab: 极简课表提取下载 -->
                <div id="tab-batch" class="tab-pane">
                    <h2 class="header-title">极简课表视频批量提取与下载 (纯Web队列处理)</h2>
                    <div class="card">
                        <h3 style="margin-top:0; color: #059669;">✅ 架构已升级支持无限任务并发排队！系统将在后台按最高 3 路并发的模式自动排队处理，绝对不卡硬盘。</h3>

                        <div class="toolbar" style="background:#f8fafc; padding:15px; border-radius:8px; border:1px solid #e2e8f0; align-items: flex-start; flex-direction: column;">
                            <div>
                                <label>① 目标日期:</label> 
                                <input type="date" id="batch-date" style="padding: 8px; border: 1px solid #cbd5e1; border-radius: 4px;">
                            </div>

                            <div style="width: 100%; margin-top: 10px; border-top: 1px dashed #cbd5e1; padding-top: 10px;">
                                <label style="display:inline-block; margin-bottom:10px; color:#1e293b;">② 添加提取时间段 (可添加多节课):</label>
                                <button class="btn btn-success" style="padding: 4px 10px; font-size: 13px; margin-left: 15px;" onclick="addBatchSlot()">➕ 增加时间段</button>

                                <div id="batch-slots-container" style="margin-top: 10px;">
                                    <div class="batch-slot-item" style="margin-bottom: 8px; display: flex; align-items: center; gap: 10px;">
                                        开始: <input type="time" class="slot-start" value="08:00" style="padding: 6px; border: 1px solid #cbd5e1; border-radius: 4px;">
                                        结束: <input type="time" class="slot-end" value="08:45" style="padding: 6px; border: 1px solid #cbd5e1; border-radius: 4px;">
                                        <button class="btn btn-danger" style="padding: 4px 10px;" onclick="removeBatchSlot(this)">❌ 移除</button>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <div style="margin-top:20px; font-weight:bold; color:#1e293b; font-size:15px;">③ 勾选需要下载的教室目标：</div>
                        <div style="margin:10px 0;">
                            <button class="btn btn-live" onclick="toggleBatchCams(true)">☑ 全选所有教室</button>
                            <button class="btn btn-live" style="background:#64748b;" onclick="toggleBatchCams(false)">☐ 清空所有勾选</button>
                        </div>
                        <div id="batch-cam-list" style="display: flex; flex-wrap: wrap; gap: 15px; max-height: 250px; overflow-y: auto; padding: 15px; background: white; border: 1px solid #cbd5e1; border-radius: 6px;">
                            <div class="empty-msg">等待设备数据加载...</div>
                        </div>

                        <div id="batch-progress-container" style="display:none; margin-top:25px; padding: 15px; border-radius: 8px; border: 1px dashed #3b82f6; background: #eff6ff;">
                            <div style="font-weight:bold; margin-bottom:10px; color:#1e3a8a; font-size:15px;" id="batch-progress-text">任务准备中...</div>
                            <div style="width:100%; background:#e2e8f0; border-radius:8px; overflow:hidden; height:24px; box-shadow: inset 0 1px 2px rgba(0,0,0,0.1);">
                                <div id="batch-progress-bar" style="width:0%; background: linear-gradient(90deg, #3b82f6 0%, #2563eb 100%); height:100%; transition:width 0.4s ease;"></div>
                            </div>
                        </div>

                        <div style="margin-top: 25px; border-top: 1px dashed #cbd5e1; padding-top: 20px;">
                            <button class="btn btn-primary" id="btn-do-batch" style="width:100%; font-size:16px; padding:15px;" onclick="doBatchDownload()">🚀 立即把选中的教室送入后台排队并开始提取</button>
                        </div>

                        <div id="batch-result" style="margin-top: 20px; display:none; background:#ecfdf5; padding:15px; border:1px solid #10b981; border-radius:6px;">
                            <h3 style="color:#059669; margin-top:0;">✅ 该批次排队任务已全部提取完毕！</h3>
                            <div id="batch-links" style="display:flex; flex-direction:column; max-height:350px; overflow-y:auto; padding-right:10px;"></div>
                        </div>
                    </div>
                </div>

                <!-- Tab: 远程设备与全局拉流控制 -->
                <div id="tab-cams" class="tab-pane">
                    <h2 class="header-title">远程设备与全局拉流控制</h2>
                    <div class="card">
                        <h3>1. 手动全局启停引擎</h3>
                        <button class="btn btn-success" onclick="apiControl('start')">▶ 强制启动全量拉流</button>
                        <button class="btn btn-danger" style="margin-left:10px;" onclick="apiControl('stop')">■ 强制停止所有录像</button>
                        <button class="btn btn-warning" style="margin-left:10px;" onclick="if(confirm('热重启将会瞬间切断并重新拉起所有服务，确定继续吗？')) apiControl('restart_service')">🔄 重启核心后台服务</button>
                    </div>

                    <div class="card">
                        <h3>2. 系统全局配置与定时录像计划 (支持多时段与跨夜)</h3>
                        <div class="toolbar" style="margin-bottom: 10px; padding-bottom: 10px; border-bottom: 1px dashed #e2e8f0;">
                            <label>磁盘循环覆盖: 自动清理并保留最近 <input type="number" id="sys-retention" style="width: 70px; text-align: center; padding: 5px; border: 1px solid #cbd5e1; border-radius: 6px;" min="1" max="365" value="7"> 天录像</label>
                        </div>
                        <div class="toolbar" style="margin-bottom: 10px;">
                            <label style="cursor:pointer; font-size:15px; font-weight:bold; color:#0f172a;">
                                <input type="checkbox" id="sch-enabled" style="width:18px;height:18px;vertical-align:-3px;"> 启用全自动定时录像 (总开关)
                            </label>
                            <button class="btn btn-success" style="margin-left: auto;" onclick="apiSaveSchedule()">💾 保存并下发系统与排班配置</button>
                        </div>
                        <div style="display:flex; gap:20px; flex-wrap:wrap; background:#f8fafc; padding:15px; border-radius:8px; border:1px solid #e2e8f0;">
                            <div>
                                <label><input type="checkbox" id="sch1-en"> 时段 1: </label>
                                <input type="time" id="sch1-start" style="padding:5px;"> 至 <input type="time" id="sch1-end" style="padding:5px;">
                            </div>
                            <div>
                                <label><input type="checkbox" id="sch2-en"> 时段 2: </label>
                                <input type="time" id="sch2-start" style="padding:5px;"> 至 <input type="time" id="sch2-end" style="padding:5px;">
                            </div>
                            <div>
                                <label><input type="checkbox" id="sch3-en"> 时段 3: </label>
                                <input type="time" id="sch3-start" style="padding:5px;"> 至 <input type="time" id="sch3-end" style="padding:5px;">
                            </div>
                        </div>
                    </div>

                    <div class="card">
                        <h3>3. 预设组织架构 (支持智能无限层级创建)</h3>
                        <div class="toolbar">
                            <label>① 选择父级目录:</label>
                            <select id="parent-grp-select"><option value="">-- 作为根目录 (顶级) --</option></select>
                            <label>② 附加子级目录:</label>
                            <input type="text" id="add-grp-name" placeholder="例: 高中部/高一/1班" style="width: 250px;">
                            <button class="btn btn-success" onclick="apiAddGroup()">➕ 一键创建架构</button>
                        </div>
                        <div class="toolbar" style="margin-top: 10px; border-top: 1px dashed #e2e8f0; padding-top: 15px;">
                            <label>删除废弃分组:</label>
                            <select id="del-grp-select" style="min-width: 200px;"></select>
                            <button class="btn btn-danger" onclick="apiDelGroup()">❌ 删除选中</button>
                        </div>
                    </div>

                    <!-- 【核心功能升级】新版多品牌设备入网与修改表单 -->
                    <div class="card">
                        <h3 style="display:flex; justify-content:space-between; align-items:center; margin-top:0;">
                            4. 添加 / 编辑入网监控设备 
                            <button class="btn btn-warning" style="padding:4px 8px; font-size:12px; font-weight:bold; background:#f59e0b;" onclick="resetCamForm()">➕ 切换回新增模式</button>
                        </h3>
                        <div class="toolbar" style="background:#f8fafc; padding:15px; border-radius:8px; border:1px solid #e2e8f0; display:flex; flex-direction:column; align-items:flex-start;">
                            <input type="hidden" id="edit-cam-original-id" value="">
                            <div style="display:flex; gap:15px; width:100%; margin-bottom:10px;">
                                <div style="flex:1;">
                                    <label>设备品牌:</label><br>
                                    <select id="add-cam-brand" onchange="onBrandChange()" style="width:100%; padding:10px; margin-top:5px;">
                                        <option value="hikvision">海康威视 (Hikvision)</option>
                                        <option value="dahua">大华 (Dahua)</option>
                                        <option value="uniview">宇视科技 (Uniview)</option>
                                        <option value="custom">自定义流 (Custom RTSP)</option>
                                    </select>
                                </div>
                                <div style="flex:1;">
                                    <label>所属分组 (架构):</label><br>
                                    <select id="add-cam-group" style="width:100%; padding:10px; margin-top:5px;"></select>
                                </div>
                                <div style="flex:1;">
                                    <label>摄像头名称 (唯一ID):</label><br>
                                    <input type="text" id="add-cam-id" placeholder="例: 高一(1)班全景" style="width:100%; padding:10px; margin-top:5px; box-sizing:border-box;">
                                </div>
                                <div style="flex:1;" id="field-ip">
                                    <label>内网 IP 地址:</label><br>
                                    <input type="text" id="add-cam-ip" placeholder="例: 172.17.21.161" style="width:100%; padding:10px; margin-top:5px; box-sizing:border-box;">
                                </div>
                            </div>

                            <div style="display:flex; gap:15px; width:100%; margin-bottom:15px;" id="standard-auth-fields">
                                <div style="flex:1;">
                                    <label>RTSP 登录账号:</label><br>
                                    <input type="text" id="add-cam-user" value="admin" style="width:100%; padding:10px; margin-top:5px; box-sizing:border-box;">
                                </div>
                                <div style="flex:1;">
                                    <label>RTSP 登录密码:</label><br>
                                    <input type="text" id="add-cam-pwd" placeholder="系统自动转码防爆" style="width:100%; padding:10px; margin-top:5px; box-sizing:border-box;">
                                </div>
                                <div style="flex:1;">
                                    <label>RTSP 端口:</label><br>
                                    <input type="text" id="add-cam-port" value="554" style="width:100%; padding:10px; margin-top:5px; box-sizing:border-box;">
                                </div>
                                <div style="flex:1;">
                                    <label>码流/通道 (Channel):</label><br>
                                    <input type="text" id="add-cam-channel" value="102" placeholder="海康常用 101/102" style="width:100%; padding:10px; margin-top:5px; box-sizing:border-box;">
                                </div>
                            </div>

                            <div style="width:100%; margin-bottom:15px; display:none;" id="custom-url-field">
                                <label>完整 RTSP 拉流地址:</label><br>
                                <input type="text" id="add-cam-custom-url" placeholder="例: rtsp://admin:123456@192.168.1.100:554/stream1" style="width:100%; padding:10px; margin-top:5px; box-sizing:border-box;">
                                <span style="color:#64748b; font-size:12px; margin-top:5px; display:block;">* 自定义模式下，系统将直接使用该地址进行拉流。上方填写的IP仅用作简单的 Ping 连通性测试。</span>
                            </div>

                            <button class="btn btn-primary" id="btn-save-cam" onclick="apiSaveCamera()" style="width:100%; padding:15px; font-size:16px;">➕ 校验连通性并保存配置</button>
                        </div>
                    </div>

                    <div class="card">
                        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:15px;">
                            <h3 style="margin:0;">5. 监控设备列表与实时运行状态</h3>
                            <button class="btn btn-primary" style="padding: 6px 12px; font-size: 13px;" onclick="fetchCameras()">🔄 手动刷新状态</button>
                        </div>
                        <table class="grid">
                            <thead><tr><th>摄像头ID</th><th>所属分组</th><th>设备 IP</th><th>运行状态</th><th>操作</th></tr></thead>
                            <tbody id="cam-status-body"><tr><td colspan="5" class="empty-msg">等待数据加载...</td></tr></tbody>
                        </table>
                    </div>
                </div>

                <!-- Tab: 系统账号与权限分发 -->
                <div id="tab-users" class="tab-pane">
                    <h2 class="header-title">系统账号与权限分发</h2>
                    <div class="card">
                        <h3>添加账号 / 修改权限</h3>
                        <div class="toolbar">
                            <input type="text" id="add-usr-id" placeholder="登录账号" style="width:120px;">
                            <input type="password" id="add-usr-pwd" placeholder="登录密码 (留空默认不改)" style="width:160px;">
                            <select id="add-usr-role">
                                <option value="superadmin">超管 (所有权限)</option>
                                <option value="admin">普通管理员 (无账号管理权)</option>
                                <option value="user" selected>使用者 (仅浏览下载权)</option>
                            </select>
                            <input type="text" id="add-usr-desc" placeholder="备注姓名/部门">
                            <button class="btn btn-success" onclick="apiAddUser()">💾 保存/更新账号</button>
                        </div>
                    </div>
                    <div class="card" style="padding:0;">
                        <table class="grid">
                            <thead><tr><th>登录账号</th><th>角色权限</th><th>备注说明</th><th>操作</th></tr></thead>
                            <tbody id="users-tbody"></tbody>
                        </table>
                    </div>
                </div>

                <!-- Tab: 审计日志 -->
                <div id="tab-logs" class="tab-pane">
                    <h2 class="header-title">系统安全审计与运行日志中心</h2>
                    <div class="card">
                        <div class="toolbar" style="justify-content: space-between;">
                            <div>
                                <label style="font-weight: bold; color: #475569;">选择历史日期: </label>
                                <input type="date" id="log-date" style="padding: 6px; border: 1px solid #cbd5e1; border-radius: 6px; margin-right: 10px;">
                                <button class="btn btn-primary" onclick="fetchLogs()">🔍 查询指定日期日志</button>
                                <button class="btn btn-success" style="margin-left: 10px;" onclick="downloadLogFile()">⬇️ 导出查询结果为TXT</button>
                            </div>
                        </div>
                        <pre class="log-console" id="log-console">等待超管拉取数据...</pre>
                    </div>
                </div>

            </div>
        </div>
    </div>

    <!-- 首次登录密码修改拦截 -->
    <div id="pwd-modal" class="modal-overlay">
        <div style="background:white; padding:30px 40px; border-radius:12px; width:400px; box-shadow:0 10px 30px rgba(0,0,0,0.5);">
            <h3 style="margin-top:0; color:#ef4444; font-size:20px; text-align:center;">⚠️ 首次登录安全拦截</h3>
            <p style="color:#64748b; font-size:14px; margin-bottom:25px; line-height:1.5;">系统检测到您当前使用的是初始默认密码。为保障监控数据安全及符合等保规定，请必须<strong style="color:#0f172a;">修改密码</strong>后继续访问系统。</p>
            <div style="margin-bottom:15px;">
                <label style="display:block; font-size:13px; font-weight:bold; margin-bottom:8px; color:#1e293b;">设定新密码 <span style="color:#ef4444;">*</span> <br><span style="font-weight:normal; color:#94a3b8;">(必须包含字母和数字，且不少于6位)</span></label>
                <input type="password" id="new-pwd1" style="width:100%; padding:12px; border:1px solid #cbd5e1; border-radius:6px; box-sizing:border-box;">
            </div>
            <div style="margin-bottom:25px;">
                <label style="display:block; font-size:13px; font-weight:bold; margin-bottom:8px; color:#1e293b;">确认新密码 <span style="color:#ef4444;">*</span></label>
                <input type="password" id="new-pwd2" style="width:100%; padding:12px; border:1px solid #cbd5e1; border-radius:6px; box-sizing:border-box;">
            </div>
            <button onclick="submitNewPwd()" style="width:100%; background:#3b82f6; color:white; border:none; padding:14px; border-radius:6px; font-size:15px; font-weight:bold; cursor:pointer;">💾 确认修改并重新登录</button>
        </div>
    </div>

    <!-- 实时预览弹窗 -->
    <div id="live-modal" class="modal-overlay">
        <div class="live-modal">
            <div class="live-status-badge"><div class="dot"></div>实时直播流 (零转码)</div>
            <div class="live-header">
                <h3 style="margin:0;" id="live-title">正在观看</h3>
                <button class="close-btn" onclick="closeLivePreview()">✖ 结束预览</button>
            </div>
            <div class="live-video-box">
                <div id="live-loading" style="color:white; font-weight:bold; font-size:15px;">
                    <span class="loading-spinner"></span> 正在呼叫底层引擎拉取视频流，请稍候...
                </div>
                <video id="live-video" controls autoplay muted playsinline></video>
            </div>
        </div>
    </div>

    <script>
        const fetchNoCache = async (url, options = {}) => {
            const sep = url.includes('?') ? '&' : '?';
            const token = sessionStorage.getItem('nvr_token');
            let headers = options.headers || {};
            if (token) headers['Authorization'] = 'Bearer ' + token;
            const res = await fetch(url + sep + '_t=' + Date.now(), { ...options, headers: headers, cache: 'no-store' });
            if (res.status === 401 && !url.includes('/api/login')) {
                sessionStorage.removeItem('nvr_token');
                document.getElementById('app-view').style.display = 'none';
                document.getElementById('login-view').style.display = 'flex';
                throw new Error('鉴权失效，已踢回登录页');
            }
            return res;
        };

        const cnMap = {'零':'0', '一':'1', '二':'2', '三':'3', '四':'4', '五':'5', '六':'6', '七':'7', '八':'8', '九':'9', '十':'10'};
        const cn2num = (str) => {
            if (str === null || str === undefined) return '';
            let s = String(str).replace(/幼儿园/g, '0幼儿园').replace(/小学/g, '1小学').replace(/初中/g, '2初中').replace(/高中/g, '3高中').replace(/大学/g, '4大学');
            return s.replace(/[零一二三四五六七八九十]/g, m => cnMap[m]);
        };

        let currentCam = '';
        let currentUserRole = 'user';
        let expandedFolders = new Set(['默认分组']);
        let camPollTimer = null;
        window.allCamsList = []; 

        let liveHlsInstance = null;
        let liveHeartbeatTimer = null;
        let activeLiveCamId = null;
        let batchPollTimer = null;

        const now = new Date();
        const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0);
        const todayEnd = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 23, 59);
        const formatDt = (d) => d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')+'T'+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');

        document.addEventListener('DOMContentLoaded', () => {
            const elStart = document.getElementById('time-start');
            const elEnd = document.getElementById('time-end');
            if(elStart) elStart.value = formatDt(todayStart);
            if(elEnd) elEnd.value = formatDt(todayEnd);

            const logDateInput = document.getElementById('log-date');
            const batchDateInput = document.getElementById('batch-date');
            const excelDateStartInput = document.getElementById('excel-date-start');
            const excelDateEndInput = document.getElementById('excel-date-end');
            const today = new Date();
            const yyyy = today.getFullYear();
            const mm = String(today.getMonth() + 1).padStart(2, '0');
            const dd = String(today.getDate()).padStart(2, '0');
            if (logDateInput) logDateInput.value = `${yyyy}-${mm}-${dd}`;
            if (batchDateInput) batchDateInput.value = `${yyyy}-${mm}-${dd}`;
            if (excelDateStartInput) excelDateStartInput.value = `${yyyy}-${mm}-${dd}`;
            if (excelDateEndInput) excelDateEndInput.value = `${yyyy}-${mm}-${dd}`;
        });

        async function initApp() {
            const token = sessionStorage.getItem('nvr_token');
            if (!token) {
                document.getElementById('login-view').style.display = 'flex';
                document.getElementById('app-view').style.display = 'none';
                return;
            }

            try {
                await fetchWhoAmI();
                document.getElementById('login-view').style.display = 'none';
                document.getElementById('app-view').style.display = 'flex';
                switchTab('search');
                fetchCameras();
                if(currentUserRole === 'superadmin' || currentUserRole === 'admin') {
                    fetchGroups();
                    fetchSchedule(); 
                }
                if(currentUserRole === 'superadmin') fetchUsers();
            } catch (e) {}
        }

        function doLogin() {
            const u = document.getElementById('username').value.trim();
            const p = document.getElementById('password').value.trim();
            if(!u || !p) return;
            const btn = document.getElementById('login-btn');
            btn.disabled = true;
            btn.innerText = '正在验证安全凭证...';
            document.getElementById('error-msg').style.display = 'none';

            fetch('/api/login?_t=' + Date.now(), {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username: u, password: p}),
                cache: 'no-store'
            }).then(r => {
                if(r.ok) return r.json();
                throw new Error('login failed');
            }).then(data => {
                if(data.status === 'ok') {
                    sessionStorage.setItem('nvr_token', data.token); 
                    initApp();
                }
            }).catch(e => {
                document.getElementById('error-msg').style.display = 'block';
                btn.disabled = false;
                btn.innerText = '验证身份并登入';
            });
        }

        async function fetchWhoAmI() {
            const r = await fetchNoCache('/api/whoami');
            const data = await r.json();
            currentUserRole = data.role;

            if(data.must_change_pwd) document.getElementById('pwd-modal').style.display = 'flex';
            else document.getElementById('pwd-modal').style.display = 'none';

            let roleName = '👤 使用者';
            if(data.role === 'superadmin') roleName = '👑 超管';
            else if(data.role === 'admin') roleName = '👮 管理员';

            const badge = document.getElementById('ui-role');
            badge.innerText = `${data.username} | ${roleName}`;
            badge.className = `role-badge ${data.role}`;

            document.getElementById('nav-batch').style.display = 'inline-block';
            document.getElementById('nav-excel').style.display = 'inline-block'; 

            if(data.role === 'superadmin' || data.role === 'admin') document.getElementById('nav-cams').style.display = 'inline-block';
            if(data.role === 'superadmin') {
                document.getElementById('nav-users').style.display = 'inline-block';
                document.getElementById('nav-logs').style.display = 'inline-block';
            }
        }

        function submitNewPwd() {
            const p1 = document.getElementById('new-pwd1').value;
            const p2 = document.getElementById('new-pwd2').value;
            if(p1 !== p2) return alert('两次输入的密码不一致！');
            if(p1 === '123456' || p1 === 'admin') return alert('新密码不能为初始默认密码！');
            const regex = /^(?=.*[a-zA-Z])(?=.*\d).{6,}$/;
            if(!regex.test(p1)) return alert('密码太弱了！必须包含字母和数字，且不少于6位！');

            fetchNoCache('/api/users', { 
                method: 'POST', 
                body: JSON.stringify({action: 'change_pwd', password: p1}) 
            }).then(res => {
                if(res.ok) {
                    alert("✅ 密码修改成功！为了安全，系统将强制注销当前登录状态，请使用新密码重新登录。");
                    sessionStorage.removeItem('nvr_token');
                    window.location.reload();
                } else {
                    alert("修改失败，请检查网络后重试。");
                }
            });
        }

        function switchTab(tabId) {
            document.querySelectorAll('.nav-btn').forEach(btn => btn.classList.remove('active'));
            document.querySelectorAll('.tab-pane').forEach(pane => pane.classList.remove('active'));
            const targetBtn = document.getElementById('nav-' + tabId);
            if (targetBtn) targetBtn.classList.add('active');
            const targetPane = document.getElementById('tab-' + tabId);
            if (targetPane) targetPane.classList.add('active');
            if (camPollTimer) { clearInterval(camPollTimer); camPollTimer = null; }

            if(tabId === 'cams') { 
                fetchCameras(); 
                fetchGroups(); 
                fetchSchedule(); 
                camPollTimer = setInterval(fetchCameras, 3000);
            }
            if(tabId === 'batch' || tabId === 'excel') fetchCameras(); 
            if(tabId === 'users' && currentUserRole === 'superadmin') fetchUsers();
            if(tabId === 'logs' && currentUserRole === 'superadmin') fetchLogs();
        }

        // ================= 【全新升级】智能多品牌添加与修改核心逻辑 =================
        function onBrandChange() {
            const brand = document.getElementById('add-cam-brand').value;
            const stdFields = document.getElementById('standard-auth-fields');
            const customField = document.getElementById('custom-url-field');
            const chInput = document.getElementById('add-cam-channel');

            if (brand === 'custom') {
                stdFields.style.display = 'none';
                customField.style.display = 'block';
            } else {
                stdFields.style.display = 'flex';
                customField.style.display = 'none';
                if (brand === 'hikvision') { chInput.value = '102'; chInput.placeholder = "海康常用 101/102"; }
                else if (brand === 'dahua') { chInput.value = '1'; chInput.placeholder = "大华通常填 1"; }
                else if (brand === 'uniview') { chInput.value = 'video2'; chInput.placeholder = "宇视常用 video1/video2"; }
            }
        }

        function apiSaveCamera() {
            const originalId = document.getElementById('edit-cam-original-id').value;
            const brand = document.getElementById('add-cam-brand').value;
            const group = document.getElementById('add-cam-group').value || '默认分组';
            const id = document.getElementById('add-cam-id').value.trim();
            const ip = document.getElementById('add-cam-ip').value.trim();

            const user = document.getElementById('add-cam-user').value.trim();
            const pwd = document.getElementById('add-cam-pwd').value.trim();
            const port = document.getElementById('add-cam-port').value.trim() || '554';
            const channel = document.getElementById('add-cam-channel').value.trim();
            const customUrl = document.getElementById('add-cam-custom-url').value.trim();

            if (!id) return alert("操作受阻：摄像头名称(ID)是唯一标识，不能为空！");
            if (brand !== 'custom' && (!ip || !user || !pwd)) return alert("标准品牌的 IP、账号、密码均不能为空！");
            if (brand === 'custom' && !customUrl) return alert("自定义模式下，完整 RTSP 地址不能为空！");

            let payload = { action: originalId ? 'edit' : 'add', original_id: originalId, brand: brand, group: group, id: id, ip: ip, user: user, pwd: pwd, port: port, channel: channel, custom_url: customUrl };

            const btn = document.getElementById('btn-save-cam');
            const oldText = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = '⏳ 正在向设备下发网络握手校验...';

            fetchNoCache('/api/cameras', { method: 'POST', body: JSON.stringify(payload) })
                .then(r => r.json().then(data => ({status: r.status, ok: r.ok, data})))
                .then(res => {
                    if (res.ok) {
                        alert(originalId ? "✅ 设备修改成功！\n\n如果该设备当前正在录像，系统已为您在底层重启该路拉流以应用最新密码与地址。\n您可以点击左侧【实时预览】确认画面是否成功出流。" : "✅ 新设备校验网络畅通，成功入网！");
                        resetCamForm();
                        fetchCameras();
                    } else {
                        alert("❌ 操作失败：" + (res.data.error || '可能是网络不通或ID已存在'));
                    }
                })
                .catch(e => alert("网络请求异常：" + e))
                .finally(() => { btn.disabled = false; btn.innerHTML = oldText; });
        }

        function resetCamForm() {
            document.getElementById('edit-cam-original-id').value = '';
            document.getElementById('add-cam-id').value = '';
            document.getElementById('add-cam-ip').value = '';
            document.getElementById('add-cam-pwd').value = '';
            document.getElementById('add-cam-custom-url').value = '';
            document.getElementById('add-cam-brand').value = 'hikvision';
            onBrandChange();
            document.getElementById('btn-save-cam').innerHTML = '➕ 校验网络并保存设备';
            document.getElementById('btn-save-cam').classList.replace('btn-warning', 'btn-primary');
        }

        function editCam(camId) {
            const cam = window.allCamsList.find(c => c.id === camId);
            if (!cam) return;

            document.getElementById('edit-cam-original-id').value = cam.id;
            document.getElementById('add-cam-brand').value = cam.brand || 'hikvision';
            onBrandChange(); // 触发展示逻辑

            // 延迟渲染以避免DOM未刷新
            setTimeout(() => {
                document.getElementById('add-cam-group').value = cam.group || '默认分组';
                document.getElementById('add-cam-id').value = cam.id;
                document.getElementById('add-cam-ip').value = cam.ip || '';

                if (cam.brand === 'custom') {
                    document.getElementById('add-cam-custom-url').value = cam.url || '';
                } else {
                    document.getElementById('add-cam-user').value = cam.user || 'admin';
                    document.getElementById('add-cam-pwd').value = cam.pwd || '';
                    document.getElementById('add-cam-port').value = cam.port || '554';
                    document.getElementById('add-cam-channel').value = cam.channel || '102';
                }

                document.getElementById('btn-save-cam').innerHTML = '💾 保存设备信息修改 (将自动重启该路拉流)';
                document.getElementById('btn-save-cam').classList.replace('btn-primary', 'btn-warning');
                document.getElementById('add-cam-brand').scrollIntoView({ behavior: 'smooth', block: 'center' });
            }, 100);
        }
        // ================= 【结束】智能多品牌逻辑 =================

        window.parsedExcelData = [];

        function toggleExcelCams(state) {
            document.querySelectorAll('.excel-chk').forEach(cb => cb.checked = state);
        }

        function parseExcel() {
            const fileInput = document.getElementById('excel-file');
            const dateStartStr = document.getElementById('excel-date-start').value;
            const dateEndStr = document.getElementById('excel-date-end').value;

            if (!fileInput.files.length) return alert('请先选择要上传的课表 Excel 文件！');
            if (!dateStartStr || !dateEndStr) return alert('请选择需要提取录像的【起止日期】范围！');

            const startDate = new Date(dateStartStr);
            const endDate = new Date(dateEndStr);
            if (startDate > endDate) return alert('开始日期不能大于结束日期！');

            const weekdays = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六'];

            let targetDates = [];
            let curr = new Date(startDate);
            while (curr <= endDate) {
                let y = curr.getFullYear();
                let m = String(curr.getMonth() + 1).padStart(2, '0');
                let d = String(curr.getDate()).padStart(2, '0');
                targetDates.push({ dateStr: `${y}-${m}-${d}`, weekday: weekdays[curr.getDay()] });
                curr.setDate(curr.getDate() + 1);
            }

            const reader = new FileReader();
            reader.onload = function(e) {
                try {
                    const data = new Uint8Array(e.target.result);
                    const workbook = XLSX.read(data, {type: 'array'});
                    const sheet = workbook.Sheets[workbook.SheetNames[0]];
                    const rows = XLSX.utils.sheet_to_json(sheet, {defval: '', raw: false});

                    window.parsedExcelData = [];
                    targetDates.forEach(td => {
                        const matchedRows = rows.filter(r => (r['星期'] || '').toString().trim() === td.weekday);
                        matchedRows.forEach(mr => {
                            window.parsedExcelData.push({ ...mr, __target_date: td.dateStr });
                        });
                    });

                    if (window.parsedExcelData.length === 0) {
                        let tbody = document.getElementById('excel-result-body');
                        tbody.innerHTML = `<tr><td colspan="8" class="empty-msg" style="color:#ef4444;">未在 Excel 表格中匹配到该日期范围内的任何课程记录，请检查表格内容。</td></tr>`;
                        document.getElementById('btn-excel-batch').style.display = 'none';
                        document.getElementById('excel-filter-bar').style.display = 'none';
                        return;
                    }

                    document.getElementById('excel-filter-bar').style.display = 'flex';
                    renderExcelTable(window.parsedExcelData);
                    alert(`✅ 解析成功！共为您匹配到了分布在选中日期范围内的 ${window.parsedExcelData.length} 节课程。\n\n您可以使用下方的二次检索工具精确查找某位老师、班级或学科进行打包提取。`);

                } catch(err) {
                    alert('Excel 解析失败，请确认文件格式正确且未损坏！\n具体错误：' + err.message);
                }
            };
            reader.readAsArrayBuffer(fileInput.files[0]);
        }

        function renderExcelTable(dataToRender) {
            let tbody = document.getElementById('excel-result-body');
            if (dataToRender.length === 0) {
                tbody.innerHTML = `<tr><td colspan="8" class="empty-msg" style="color:#ef4444;">未找到符合检索条件的课程记录。</td></tr>`;
                document.getElementById('btn-excel-batch').style.display = 'none';
                return;
            }

            let html = '';
            dataToRender.forEach((r, idx) => {
                let cam = (r['班级'] || '').toString().trim();
                let start = (r['开始时间'] || '').toString().trim();
                let end = (r['结束时间'] || '').toString().trim();

                if (start && start.length === 4 && start.includes(':')) start = '0' + start;
                if (end && end.length === 4 && end.includes(':')) end = '0' + end;

                let subject = (r['学科'] || '').toString().trim() || '-';
                let teacher = (r['任课教师'] || '').toString().trim() || '-';

                html += `<tr>
                    <td><input type="checkbox" class="excel-chk" data-cam="${cam}" data-date="${r['__target_date']}" data-start="${start}" data-end="${end}" checked style="transform: scale(1.2);"></td>
                    <td style="font-weight:bold; color:#0f172a;">${r['__target_date']}</td>
                    <td style="font-weight:bold; color:#1e293b;">${cam}</td>
                    <td>${r['星期']}</td>
                    <td><span style="background:#e0e7ff; color:#4338ca; padding:3px 6px; border-radius:4px;">${start} - ${end}</span></td>
                    <td>${subject}</td>
                    <td>${teacher}</td>
                    <td style="color:#64748b;">待提取</td>
                </tr>`;
            });
            tbody.innerHTML = html;
            document.getElementById('btn-excel-batch').style.display = 'block';
            const selectAllCb = document.getElementById('excel-select-all');
            if (selectAllCb) selectAllCb.checked = true;
        }

        function applyExcelFilters() {
            const sClass = document.getElementById('search-class').value.trim().toLowerCase();
            const sSubject = document.getElementById('search-subject').value.trim().toLowerCase();
            const sTeacher = document.getElementById('search-teacher').value.trim().toLowerCase();
            const sTime = document.getElementById('search-time').value.trim().toLowerCase();

            const filteredData = window.parsedExcelData.filter(r => {
                let cam = (r['班级'] || '').toString().trim().toLowerCase();
                let subject = (r['学科'] || '').toString().trim().toLowerCase();
                let teacher = (r['任课教师'] || '').toString().trim().toLowerCase();
                let start = (r['开始时间'] || '').toString().trim().toLowerCase();
                let end = (r['结束时间'] || '').toString().trim().toLowerCase();

                let isMatch = true;
                if (sClass && !cam.includes(sClass)) isMatch = false;
                if (sSubject && !subject.includes(sSubject)) isMatch = false;
                if (sTeacher && !teacher.includes(sTeacher)) isMatch = false;
                if (sTime && !start.includes(sTime) && !end.includes(sTime)) isMatch = false;
                return isMatch;
            });
            renderExcelTable(filteredData);
        }

        function resetExcelFilters() {
            document.getElementById('search-class').value = '';
            document.getElementById('search-subject').value = '';
            document.getElementById('search-teacher').value = '';
            document.getElementById('search-time').value = '';
            renderExcelTable(window.parsedExcelData);
        }

        function doExcelBatchDownload() {
            const checks = document.querySelectorAll('.excel-chk:checked');
            if(checks.length === 0) return alert('请在上方列表中至少勾选一节需要提取的课程！');

            const tasks = [];
            checks.forEach(chk => {
                tasks.push({
                    cam: chk.getAttribute('data-cam'),
                    date: chk.getAttribute('data-date'),
                    start: chk.getAttribute('data-start'),
                    end: chk.getAttribute('data-end')
                });
            });

            const btn = document.getElementById('btn-excel-batch');
            btn.disabled = true;
            document.getElementById('excel-batch-result').style.display = 'none';
            document.getElementById('excel-progress-container').style.display = 'block';
            document.getElementById('excel-progress-bar').style.width = '0%';
            document.getElementById('excel-progress-text').innerText = `已接管！系统正在将这 ${tasks.length} 节横跨多日的课送入并发队列提取...`;

            fetchNoCache('/api/web_batch_download_excel', {
                method: 'POST',
                body: JSON.stringify({tasks: tasks})
            }).then(r => r.json()).then(data => {
                if(data.status === 'ok') {
                    if(window.excelPollTimer) clearInterval(window.excelPollTimer);
                    window.excelPollTimer = setInterval(() => { checkExcelBatchStatus(data.job_id); }, 2000);
                } else {
                    alert('提交任务失败: ' + (data.error || '未知错误'));
                    btn.disabled = false;
                    document.getElementById('excel-progress-container').style.display = 'none';
                }
            }).catch(e => {
                alert('网络请求超时！');
                btn.disabled = false;
                document.getElementById('excel-progress-container').style.display = 'none';
            });
        }

        function checkExcelBatchStatus(jobId) {
            fetchNoCache(`/api/batch_status?job_id=${jobId}`).then(r => r.json()).then(data => {
                const total = data.total;
                const completed = data.completed;
                const failed = data.failed;
                const pct = total === 0 ? 0 : Math.floor((completed / total) * 100);

                document.getElementById('excel-progress-text').innerText = `[任务排队与提取中] 正在打包课程: ${total} | 已完成: ${completed} (内含无录像失败的空提取: ${failed})`;
                document.getElementById('excel-progress-bar').style.width = pct + '%';

                if(data.status === 'done') {
                    clearInterval(window.excelPollTimer);
                    document.getElementById('btn-excel-batch').disabled = false;

                    document.getElementById('excel-batch-result').style.display = 'block';
                    const linksDiv = document.getElementById('excel-batch-links');
                    const tokenStr = "token=" + sessionStorage.getItem('nvr_token');

                    if(data.results.length === 0) {
                        linksDiv.innerHTML = '<div style="color:#b91c1c; font-weight:bold;">⚠️ 提取失败：底层硬盘中没找到任何对应的录像记录（可能服务器日期不对或当天没录像）！</div>';
                    } else {
                        let html = '';
                        if (data.zip_url) {
                            html += `<div style="margin-bottom:15px; padding:25px; background:#e0f2fe; border:2px dashed #8b5cf6; border-radius:8px; text-align:center;">
                                <div style="color:#5b21b6; font-weight:bold; margin-bottom:15px; font-size:18px;">📦 您提取的所有课表录像已全部为您打包好啦！</div>
                                <a href="${data.zip_url}?${tokenStr}" download class="btn btn-primary" style="background:#8b5cf6; text-decoration:none; padding:15px 30px; font-size:18px; display:inline-block; font-weight:bold; box-shadow: 0 4px 6px rgba(139,92,246,0.3);">⬇️ 一键下载完整 ZIP 压缩包 (内含 ${data.results.length} 节课)</a>
                            </div>`;
                        }

                        html += `<div style="max-height: 200px; overflow-y: auto; background: white; border: 1px solid #cbd5e1; border-radius: 6px; padding: 10px;">`;
                        html += data.results.map(f => `
                            <div style="padding: 8px 10px; border-bottom: 1px dashed #e2e8f0; color: #475569; font-size: 13px; display: flex; justify-content: space-between; align-items: center;">
                                <span>✅ <b>教室:</b> ${f.cam} &nbsp;&nbsp;|&nbsp;&nbsp; <b>时段:</b> ${f.time}</span>
                                <a href="${f.url}?${tokenStr}" download class="btn btn-success" style="padding: 4px 10px; font-size: 12px; text-decoration: none;">⬇ 单独下载</a>
                            </div>
                        `).join('');
                        html += `</div>`;
                        linksDiv.innerHTML = html;
                    }
                }
            });
        }

        function renderBatchCams() {
            const list = document.getElementById('batch-cam-list');
            if(!list) return;
            list.innerHTML = window.allCamsList.map(c => `
                <label style="cursor:pointer; width:180px; padding:8px 10px; background:#f1f5f9; border:1px solid #cbd5e1; border-radius:6px; font-weight:bold;">
                    <input type="checkbox" class="batch-cam-chk" value="${c.id}" style="transform: scale(1.2); margin-right:8px;"> ${c.id}
                </label>
            `).join('');
        }

        function toggleBatchCams(state) {
            document.querySelectorAll('.batch-cam-chk').forEach(cb => cb.checked = state);
        }

        function addBatchSlot() {
            const container = document.getElementById('batch-slots-container');
            const div = document.createElement('div');
            div.className = 'batch-slot-item';
            div.style.marginBottom = '8px';
            div.style.display = 'flex';
            div.style.alignItems = 'center';
            div.style.gap = '10px';

            div.innerHTML = `
                开始: <input type="time" class="slot-start" value="09:00" style="padding: 6px; border: 1px solid #cbd5e1; border-radius: 4px;">
                结束: <input type="time" class="slot-end" value="09:45" style="padding: 6px; border: 1px solid #cbd5e1; border-radius: 4px;">
                <button class="btn btn-danger" style="padding: 4px 10px;" onclick="removeBatchSlot(this)">❌ 移除</button>
            `;
            container.appendChild(div);
        }

        function removeBatchSlot(btn) {
            const container = document.getElementById('batch-slots-container');
            if (container.children.length <= 1) {
                alert("必须至少保留一个提取时间段！");
                return;
            }
            btn.parentElement.remove();
        }

        function doBatchDownload() {
            const dateVal = document.getElementById('batch-date').value;
            if(!dateVal) return alert('请先选择目标日期！');

            const checkedCams = Array.from(document.querySelectorAll('.batch-cam-chk:checked')).map(cb => cb.value);
            if(checkedCams.length === 0) return alert('请至少勾选一个需要提取视频的教室！');

            const slotElements = document.querySelectorAll('.batch-slot-item');
            const slots = [];
            for(let i=0; i<slotElements.length; i++) {
                const st = slotElements[i].querySelector('.slot-start').value;
                const ed = slotElements[i].querySelector('.slot-end').value;
                if(st && ed) slots.push({start: st, end: ed});
            }

            if(slots.length === 0) return alert('必须提供至少一个提取时间段！');

            const totalTasks = checkedCams.length * slots.length;
            if(totalTasks > 20) {
                if(!confirm(`⚠️ 安全警告 ⚠️\n\n您勾选了 ${checkedCams.length} 个教室，并设定了 ${slots.length} 个时间段，将提取共计 ${totalTasks} 个独立视频文件！\n后台并发提取非常消耗服务器 CPU 和磁盘资源，强行执行可能导致监控系统完全卡死！\n\n您确定要强行继续吗？`)) return;
            }

            const btn = document.getElementById('btn-do-batch');
            btn.disabled = true;
            document.getElementById('batch-result').style.display = 'none';

            document.getElementById('batch-progress-container').style.display = 'block';
            document.getElementById('batch-progress-bar').style.width = '0%';
            document.getElementById('batch-progress-text').innerText = `已接管！服务器正在将 ${checkedCams.length} 个任务列队处理...`;

            fetchNoCache('/api/web_batch_download', {
                method: 'POST',
                body: JSON.stringify({date: dateVal, slots: slots, cams: checkedCams})
            }).then(r => r.json()).then(data => {
                if(data.status === 'ok') {
                    if(batchPollTimer) clearInterval(batchPollTimer);
                    batchPollTimer = setInterval(() => { checkBatchStatus(data.job_id); }, 2000);
                } else {
                    alert('提交任务失败: ' + (data.error || '未知错误'));
                    btn.disabled = false;
                    document.getElementById('batch-progress-container').style.display = 'none';
                }
            }).catch(e => {
                alert('网络请求超时或后端无响应！');
                btn.disabled = false;
                document.getElementById('batch-progress-container').style.display = 'none';
            });
        }

        function checkBatchStatus(jobId) {
            const dateVal = document.getElementById('batch-date').value;
            fetchNoCache(`/api/batch_status?job_id=${jobId}`).then(r => r.json()).then(data => {
                const total = data.total;
                const completed = data.completed;
                const failed = data.failed;
                const pct = total === 0 ? 0 : Math.floor((completed / total) * 100);

                document.getElementById('batch-progress-text').innerText = `[任务排队与提取中] 总计: ${total} | 已完成: ${completed} (内含无记录失败: ${failed})`;
                document.getElementById('batch-progress-bar').style.width = pct + '%';

                if(data.status === 'done') {
                    clearInterval(batchPollTimer);
                    document.getElementById('btn-do-batch').disabled = false;

                    document.getElementById('batch-result').style.display = 'block';
                    const linksDiv = document.getElementById('batch-links');
                    const tokenStr = "token=" + sessionStorage.getItem('nvr_token');

                    if(data.results.length === 0) {
                        linksDiv.innerHTML = '<div style="color:#b91c1c; background:#fef2f2; border:1px solid #fca5a5; padding:15px; border-radius:6px;">' +
                            '<span style="font-weight:bold; font-size:15px;">⚠️ 提取失败：在您选择的【' + dateVal + '】时段内，底层硬盘中没有任何对应的录像记录！</span><br><br>' +
                            '<b>💡 核心排查建议：</b><br>' + 
                            '1. 请前往左上角【🎞️ 录像回放与检索】页面，搜索一下刚才的教室，核实底层的<b>真实录像日期</b>到底是哪一天。<br>' +
                            '2. 极大概率是因为您的<b>电脑日期</b>与<b>服务器系统日期</b>不一致（服务器时区可能不对或差了一天），导致您选错日期了！' +
                            '</div>';
                    } else {
                        let html = '';
                        if (data.zip_url) {
                            html += `<div style="margin-bottom:15px; padding:25px; background:#e0f2fe; border:2px dashed #3b82f6; border-radius:8px; text-align:center;">
                                <div style="color:#1e3a8a; font-weight:bold; margin-bottom:15px; font-size:18px;">📦 所有录像已为您打包完毕！</div>
                                <a href="${data.zip_url}?${tokenStr}" download class="btn btn-primary" style="text-decoration:none; padding:15px 30px; font-size:18px; display:inline-block; font-weight:bold; box-shadow: 0 4px 6px rgba(59,130,246,0.3);">⬇️ 一键下载完整 ZIP 压缩包 (内含 ${data.results.length} 个视频)</a>
                            </div>`;
                        } else {
                            html += `<div style="color:#b91c1c; font-weight:bold; margin-bottom:15px; border: 1px solid #fca5a5; padding: 15px; border-radius: 6px; background: #fef2f2;">
                                <span style="font-size: 15px;">⚠️ 致命错误：打包 ZIP 压缩包失败！服务器底层报错信息如下：</span><br>
                                <span style="font-family: monospace; display: block; margin-top: 10px; color: #991b1b; background: #fee2e2; padding: 10px; border-radius: 4px;">${data.zip_error || '磁盘空间不足、文件系统权限被拒绝或未知I/O异常'}</span>
                            </div>`;
                        }

                        html += `<div style="color:#64748b; font-size:14px; font-weight:bold; margin-bottom:10px;">📋 本次提取的视频明细：</div>`;
                        html += `<div style="max-height: 200px; overflow-y: auto; background: white; border: 1px solid #cbd5e1; border-radius: 6px; padding: 10px;">`;

                        html += data.results.map(f => `
                            <div style="padding: 8px 10px; border-bottom: 1px dashed #e2e8f0; color: #475569; font-size: 13px; display: flex; justify-content: space-between; align-items: center;">
                                <span>✅ <b>教室:</b> ${f.cam} &nbsp;&nbsp;|&nbsp;&nbsp; <b>时段:</b> ${f.time}</span>
                                <a href="${f.url}?${tokenStr}" download class="btn btn-success" style="padding: 4px 10px; font-size: 12px; text-decoration: none;">⬇ 单独下载</a>
                            </div>
                        `).join('');

                        html += `</div>`;
                        linksDiv.innerHTML = html;
                    }
                }
            }).catch(e => {
                console.error('进度查询失败，不中断查询等下次重试', e);
            });
        }

        function openLivePreview(camId) {
            document.getElementById('live-modal').style.display = 'flex';
            document.getElementById('live-title').innerText = `正在观看: ${camId}`;
            document.getElementById('live-loading').style.display = 'block';
            document.getElementById('live-video').style.display = 'none';
            activeLiveCamId = camId;

            fetchNoCache(`/api/live_start?cam=${encodeURIComponent(camId)}`)
                .then(r => r.json())
                .then(data => {
                    if (data.status === 'ok') {
                        initHlsPlayer(data.url);
                        liveHeartbeatTimer = setInterval(() => {
                            fetchNoCache(`/api/live_heartbeat?cam=${encodeURIComponent(camId)}`).catch(e=>{});
                        }, 5000);
                    } else {
                        alert('启动直播流失败！可能设备离线。');
                        closeLivePreview();
                    }
                }).catch(e => {
                    alert(`请求直播服务异常: ${e.message}`);
                    closeLivePreview();
                });
        }

        function initHlsPlayer(m3u8Url) {
            const video = document.getElementById('live-video');
            document.getElementById('live-loading').style.display = 'none';
            video.style.display = 'block';

            const tokenStr = "token=" + sessionStorage.getItem('nvr_token');

            if (typeof Hls !== 'undefined' && Hls.isSupported()) {
                if (liveHlsInstance) { liveHlsInstance.destroy(); }
                liveHlsInstance = new Hls({
                    liveSyncDurationCount: 2, 
                    liveMaxLatencyDurationCount: 5,
                    enableWorker: true,
                    xhrSetup: function(xhr, url) {
                        xhr.setRequestHeader('Authorization', 'Bearer ' + sessionStorage.getItem('nvr_token'));
                    }
                });
                liveHlsInstance.loadSource(m3u8Url + "?" + tokenStr + "&_t=" + Date.now()); 
                liveHlsInstance.attachMedia(video);
                liveHlsInstance.on(Hls.Events.MANIFEST_PARSED, function () {
                    video.play().catch(e => console.log("自动播放可能被拦截，需手动点击"));
                });
                liveHlsInstance.on(Hls.Events.ERROR, function (event, data) {
                    if (data.fatal) {
                        switch (data.type) {
                            case Hls.ErrorTypes.NETWORK_ERROR:
                                liveHlsInstance.startLoad();
                                break;
                            case Hls.ErrorTypes.MEDIA_ERROR:
                                liveHlsInstance.recoverMediaError();
                                break;
                            default:
                                liveHlsInstance.destroy();
                                break;
                        }
                    }
                });
            } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
                video.src = m3u8Url + "?" + tokenStr + "&_t=" + Date.now();
                video.addEventListener('loadedmetadata', function() { video.play(); });
            } else {
                alert("您的浏览器不支持视频直播流，请使用 Chrome、Edge 或 Safari。");
            }
        }

        function closeLivePreview() {
            document.getElementById('live-modal').style.display = 'none';
            if (typeof liveHlsInstance !== 'undefined' && liveHlsInstance) {
                liveHlsInstance.destroy();
                liveHlsInstance = null;
            }
            const video = document.getElementById('live-video');
            video.pause();
            video.removeAttribute('src');
            video.load();

            if (liveHeartbeatTimer) {
                clearInterval(liveHeartbeatTimer);
                liveHeartbeatTimer = null;
            }
            if (activeLiveCamId) {
                fetchNoCache(`/api/live_stop?cam=${encodeURIComponent(activeLiveCamId)}`).catch(e=>{});
                activeLiveCamId = null;
            }
        }

        function fetchSchedule() {
            fetchNoCache('/api/schedule').then(r => r.json()).then(data => {
                if(data) {
                    document.getElementById('sys-retention').value = data.retention_days || 7;
                    document.getElementById('sch-enabled').checked = data.schedule_enabled;
                    document.getElementById('sch1-en').checked = data.sch1_en;
                    document.getElementById('sch1-start').value = data.sch1_start;
                    document.getElementById('sch1-end').value = data.sch1_end;
                    document.getElementById('sch2-en').checked = data.sch2_en;
                    document.getElementById('sch2-start').value = data.sch2_start;
                    document.getElementById('sch2-end').value = data.sch2_end;
                    document.getElementById('sch3-en').checked = data.sch3_en;
                    document.getElementById('sch3-start').value = data.sch3_start;
                    document.getElementById('sch3-end').value = data.sch3_end;
                }
            }).catch(e=>{});
        }

        function apiSaveSchedule() {
            const data = {
                retention_days: parseInt(document.getElementById('sys-retention').value) || 7,
                schedule_enabled: document.getElementById('sch-enabled').checked,
                sch1_en: document.getElementById('sch1-en').checked,
                sch1_start: document.getElementById('sch1-start').value,
                sch1_end: document.getElementById('sch1-end').value,
                sch2_en: document.getElementById('sch2-en').checked,
                sch2_start: document.getElementById('sch2-start').value,
                sch2_end: document.getElementById('sch2-end').value,
                sch3_en: document.getElementById('sch3-en').checked,
                sch3_start: document.getElementById('sch3-start').value,
                sch3_end: document.getElementById('sch3-end').value
            };
            fetchNoCache('/api/schedule', { method: 'POST', body: JSON.stringify(data) }).then(r => {
                if(r.ok) alert('系统保留天数与定时录像计划已成功下发至服务器生效！');
                else alert('保存失败，请检查时间格式或无权限。');
            });
        }

        function fetchGroups() {
            fetchNoCache('/api/groups').then(r => r.json()).then(groups => {
                groups.sort((a, b) => cn2num(a).localeCompare(cn2num(b), 'zh-CN', {numeric: true}));
                const selParent = document.getElementById('parent-grp-select');
                const selAddCam = document.getElementById('add-cam-group');
                const selDel = document.getElementById('del-grp-select');

                if(selParent && selAddCam && selDel) {
                    selParent.innerHTML = '<option value="">-- 作为根目录 (顶级) --</option>';
                    selAddCam.innerHTML = '';
                    selDel.innerHTML = '';

                    groups.forEach(g => {
                        selParent.innerHTML += `<option value="${g}">${g}</option>`;
                        selAddCam.innerHTML += `<option value="${g}">${g}</option>`;
                        selDel.innerHTML += `<option value="${g}">${g}</option>`;
                    });
                }
            }).catch(e=>{});
        }

        function apiAddGroup() {
            const parent = document.getElementById('parent-grp-select').value;
            const child = document.getElementById('add-grp-name').value.trim();
            if(!child) return alert("子级名称不能为空");

            const newGrp = (parent && parent !== "-- 作为根目录 (顶级) --") ? (parent + '/' + child) : child;

            fetchNoCache('/api/groups', {method:'POST', body:JSON.stringify({group:newGrp})}).then(r=>{
                if(r.ok) { document.getElementById('add-grp-name').value=''; fetchGroups(); alert("分组架构创建成功！");}
                else alert("操作失败");
            });
        }

        function apiDelGroup() {
            const grp = document.getElementById('del-grp-select').value;
            if(!grp) return;
            if(grp === '默认分组') return alert("默认分组不可删");
            if(confirm(`确定删除分组模板 [${grp}] 吗？\n(已归属于该分组的摄像头会自动安全降级转移至默认分组，且不会影响录像)`)) {
                fetchNoCache('/api/groups?group=' + encodeURIComponent(grp), {method:'DELETE'}).then(r=>{
                    if(r.ok) { fetchGroups(); alert("分组删除成功！"); }
                });
            }
        }

        function fetchCameras() {
            fetchNoCache('/api/cameras').then(r => r.json()).then(data => {
                const cams = data.cameras || [];

                cams.sort((a, b) => {
                    let grpA = cn2num(a.group || '默认分组');
                    let grpB = cn2num(b.group || '默认分组');
                    if (grpA !== grpB) return grpA.localeCompare(grpB, 'zh-CN', {numeric: true});
                    return cn2num(a.id).localeCompare(cn2num(b.id), 'zh-CN', {numeric: true});
                });

                window.allCamsList = cams; 
                renderBatchCams(); 

                const list = document.getElementById('cam-list');
                const statusBody = document.getElementById('cam-status-body');

                if (cams.length === 0) { 
                    list.innerHTML = '<div class="empty-msg">暂无设备，请联系管理员添加</div>'; 
                    if (statusBody) statusBody.innerHTML = '<tr><td colspan="5" class="empty-msg">暂无设备入网，请在上方添加。</td></tr>';
                    return; 
                }

                if(statusBody) {
                    let tableHtml = '';
                    cams.forEach(cam => {
                        let actionHtml = '-';
                        if (currentUserRole !== 'user') {
                            actionHtml = `
                                <span class="action-link reset" data-cam="${cam.id}" onclick="editCam(this.getAttribute('data-cam'))">✏️ 编辑</span>
                                <span class="action-link del" data-cam="${cam.id}" onclick="apiDelCamera(this.getAttribute('data-cam'))">❌ 删除</span>
                            `;
                        }
                        let statusColor = '#64748b'; 
                        if (cam.status.includes('正在录制')) statusColor = '#10b981'; 
                        if (cam.status.includes('重连') || cam.status.includes('断线')) statusColor = '#f59e0b'; 

                        let previewBtn = `<button class="btn btn-live" data-cam="${cam.id}" onclick="openLivePreview(this.getAttribute('data-cam'))">👁️ 预览</button>`;

                        tableHtml += `<tr>
                            <td><b>${cam.id}</b></td>
                            <td>${cam.group || '默认分组'}</td>
                            <td>${cam.ip || '-'}</td>
                            <td style="color: ${statusColor}; font-weight: bold;">${cam.status}</td>
                            <td>${previewBtn} &nbsp;&nbsp; ${actionHtml}</td>
                        </tr>`;
                    });
                    statusBody.innerHTML = tableHtml;
                }

                const tree = { _cams: [], _children: {} };
                cams.forEach(cam => {
                    const groupStr = (cam.group && cam.group.trim() !== '') ? cam.group : '默认分组';
                    const parts = groupStr.split('/');
                    let curr = tree;
                    parts.forEach(part => {
                        if (!curr._children[part]) curr._children[part] = { _cams: [], _children: {} };
                        curr = curr._children[part];
                    });
                    curr._cams.push(cam);
                });

                list.innerHTML = renderTreeHTML(tree, 0, '');
            }).catch(e => {
                document.getElementById('cam-list').innerHTML = '<div class="empty-msg" style="color:red;">加载失败，请检查网络</div>';
            });
        }

        function toggleFolder(el, event, path) {
            event.stopPropagation();
            const content = el.nextElementSibling;
            const icon = el.querySelector('.folder-icon');
            if (content.style.display === 'none') {
                content.style.display = 'block'; icon.innerText = '📂'; expandedFolders.add(path);
            } else {
                content.style.display = 'none'; icon.innerText = '📁'; expandedFolders.delete(path);
            }
        }

        function renderTreeHTML(node, level, currentPath) {
            let html = '';
            const folders = Object.keys(node._children).sort((a, b) => cn2num(a).localeCompare(cn2num(b), 'zh-CN', {numeric: true}));

            folders.forEach(folderName => {
                const folderPath = currentPath ? currentPath + '/' + folderName : folderName;
                const isExpanded = expandedFolders.has(folderPath);
                const displayStyle = isExpanded ? 'block' : 'none';
                const iconStr = isExpanded ? '📂' : '📁';
                const paddingL = level * 15 + 5;

                html += `<div class="folder-item" style="padding-left: ${paddingL}px" onclick="toggleFolder(this, event, '${folderPath}')">
                            <span class="folder-icon">${iconStr}</span> ${folderName}
                         </div>
                         <div class="folder-content" style="display:${displayStyle};">
                            ${renderTreeHTML(node._children[folderName], level + 1, folderPath)}
                         </div>`;
            });

            node._cams.forEach(cam => {
                let rightHtml = `<div class="cam-status">${cam.status || ''}</div>`;
                const paddingL = level * 15 + 10;
                html += `<div class="cam-item ${currentCam === cam.id ? 'active' : ''}" style="padding-left: ${paddingL}px" data-cam="${cam.id}" onclick="selectCam(this.getAttribute('data-cam'), this)">
                            <div>📹 ${cam.id}</div>
                            <div style="display:flex; align-items:center; gap:10px;">${rightHtml}</div>
                         </div>`;
            });

            return html;
        }

        function selectCam(camId, el) {
            document.querySelectorAll('.cam-item').forEach(e => e.classList.remove('active'));
            if(el) el.classList.add('active');
            currentCam = camId;
            document.getElementById('cam-title').innerText = '当前查看: ' + camId;
            document.getElementById('player-container').style.display = 'none';
            const player = document.getElementById('player');
            if(player) player.pause();
            searchVideos();
        }

        function apiControl(action) {
            fetchNoCache('/api/control', { method: 'POST', body: JSON.stringify({action: action}) }).then(r => {
                if(action === 'restart_service') {
                    alert('重启指令已下发！系统将在3秒后自动恢复，请稍后刷新页面。');
                    setTimeout(()=> window.location.reload(), 3000);
                } else {
                    alert(action === 'start' ? '拉流指令已下发！' : '停止指令已下发！');
                    setTimeout(fetchCameras, 1500);
                }
            });
        }

        function apiAddCamera() {
            // 这个是旧的本地备用兼容函数，真实点击现在由 apiSaveCamera() 托管了
            alert("已升级！请使用高级面板操作");
        }

        function apiDelCamera(id) {
            if(confirm(`确定要从服务器彻底删除设备 [${id}] 吗？\n\n【极其重要】：此操作将同时彻底清空该设备在服务器上的所有历史录像档案，释放磁盘空间，且无法恢复！`)) {
                fetchNoCache('/api/cameras?id=' + encodeURIComponent(id), {method: 'DELETE'}).then(() => setTimeout(fetchCameras, 500));
            }
        }

        function fetchUsers() {
            fetchNoCache('/api/users').then(r => r.json()).then(users => {
                const tbody = document.getElementById('users-tbody');
                tbody.innerHTML = '';
                users.forEach(u => {
                    let roleTxt = u.role === 'superadmin' ? '👑 超管' : (u.role === 'admin' ? '👮 管理员' : '👤 使用者');
                    tbody.innerHTML += `<tr>
                        <td><b>${u.username}</b></td>
                        <td>${roleTxt}</td>
                        <td>${u.desc || '-'}</td>
                        <td>
                            <span class="action-link" onclick="editUser('${u.username}', '${u.role}', '${u.desc || ''}')">✏️ 编辑</span>
                            <span class="action-link reset" onclick="apiResetPwd('${u.username}')">🔑 重置密码</span>
                            <span class="action-link del" onclick="apiDelUser('${u.username}')">❌ 删除</span>
                        </td>
                    </tr>`;
                });
            }).catch(e=>{});
        }

        function editUser(u, r, d) {
            document.getElementById('add-usr-id').value = u;
            document.getElementById('add-usr-role').value = r;
            document.getElementById('add-usr-desc').value = d;
            document.getElementById('add-usr-pwd').value = '';
            document.getElementById('add-usr-pwd').placeholder = "留空代表不修改密码";
        }

        function apiAddUser() {
            const u = document.getElementById('add-usr-id').value.trim();
            const p = document.getElementById('add-usr-pwd').value.trim();
            const r = document.getElementById('add-usr-role').value;
            const d = document.getElementById('add-usr-desc').value.trim();

            if(!u) return alert("账号必填");

            fetchNoCache('/api/users', { method: 'POST', body: JSON.stringify({action: 'save', username: u, password: p, role: r, desc: d}) }).then(res => {
                if(res.ok) { 
                    alert("保存成功"); 
                    document.getElementById('add-usr-id').value = ''; 
                    document.getElementById('add-usr-pwd').value = ''; 
                    document.getElementById('add-usr-desc').value = '';
                    document.getElementById('add-usr-pwd').placeholder = "登录密码 (留空默认不改)";
                    fetchUsers(); 
                }
                else {
                    if(p && p !== '123456') alert(`操作失败！请检查：\n1. 密码是否包含字母和数字且至少6位\n2. 是否拥有对应权限`);
                    else alert("操作失败，检查输入或权限");
                }
            });
        }

        function apiResetPwd(u) {
            let newPwd = prompt(`请输入账号 [${u}] 的新密码：\n(若留空或输入 123456，下次登录将被强制要求修改)\n\n* 自定义密码需包含字母和数字，至少6位`);
            if (newPwd !== null) {
                newPwd = newPwd.trim() || '123456';
                if(newPwd !== '123456' && !/^(?=.*[a-zA-Z])(?=.*\d).{6,}$/.test(newPwd)) {
                    return alert("密码太弱！必须包含字母和数字，且不少于6位。");
                }
                fetchNoCache('/api/users', { 
                    method: 'POST', 
                    body: JSON.stringify({action: 'reset_pwd', username: u, password: newPwd}) 
                }).then(res => {
                    if(res.ok) alert(`账号 [${u}] 的密码已成功重置！`);
                    else alert("操作失败，请检查权限。");
                });
            }
        }

        function apiDelUser(u) {
            if(confirm(`确定删除账号 [${u}] 吗？`)) {
                fetchNoCache('/api/users?username=' + encodeURIComponent(u), {method: 'DELETE'}).then(r => { if(r.ok) fetchUsers(); else alert("删除失败(默认admin不可删)"); });
            }
        }

        function fetchLogs() {
            const consoleDiv = document.getElementById('log-console');
            const dateVal = document.getElementById('log-date') ? document.getElementById('log-date').value : '';
            consoleDiv.innerHTML = '<span class="loading-spinner"></span> 正在拉取底层日志档案...';

            let url = '/api/logs';
            if (dateVal) {
                url += '?date=' + encodeURIComponent(dateVal);
            }

            fetchNoCache(url)
                .then(r => r.json())
                .then(data => {
                    let formattedLogs = data.logs || '✅ 暂无任何日志记录。';
                    formattedLogs = formattedLogs.replace(/🚨(.*)/g, '<span class="alarm">🚨$1</span>');
                    formattedLogs = formattedLogs.replace(/✅(.*故障恢复.*)/g, '<span class="recover">✅$1</span>');

                    consoleDiv.innerHTML = formattedLogs;
                    consoleDiv.scrollTop = consoleDiv.scrollHeight;
                })
                .catch(err => {
                    consoleDiv.innerHTML = '❌ 拉取日志失败，请检查网络或刷新页面。';
                });
        }

        function downloadLogFile() {
            const dateVal = document.getElementById('log-date') ? document.getElementById('log-date').value : '';
            const tokenStr = "token=" + sessionStorage.getItem('nvr_token');
            let url = '/api/log_download?' + tokenStr;
            if (dateVal) {
                url += '&date=' + encodeURIComponent(dateVal);
            }

            const a = document.createElement('a');
            a.href = url;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
        }

        function searchVideos() {
            if (!currentCam) return alert('请先选择需要查看的摄像头通道！');
            const start = document.getElementById('time-start').value; const end = document.getElementById('time-end').value;
            const tbody = document.getElementById('result-body'); tbody.innerHTML = '<tr><td colspan="3" class="empty-msg">正在努力检索底层录像碎片中，请稍候...</td></tr>';
            fetchNoCache(`/api/search?cam=${encodeURIComponent(currentCam)}&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`)
                .then(r => r.json()).then(data => {
                    if (data.count === 0) { tbody.innerHTML = '<tr><td colspan="3" class="empty-msg" style="color:#ef4444;">该时间段内未找到任何录像文件。</td></tr>'; return; }

                    let segmentsHTML = '<div style="margin-top:15px; padding: 15px; background: #f8fafc; border-radius: 8px; border: 1px solid #e2e8f0;">';
                    segmentsHTML += '<div style="font-size:14px; font-weight: bold; margin-bottom: 10px; color: #1e293b;">底层原始切片明细 (可勾选打包下载)：</div>';
                    segmentsHTML += '<button class="btn btn-warning" style="margin-bottom: 10px; padding: 6px 12px;" onclick="batchDownloadZip(this)">📦 批量打包下载选中切片 (ZIP格式)</button>';
                    segmentsHTML += '<table class="grid" style="width:100%; font-size:13px; background: white;">';
                    segmentsHTML += '<tr><th style="padding:10px;width:60px;"><input type="checkbox" onclick="toggleAll(this)"> 全选</th><th style="padding:10px;">开始时间</th><th style="padding:10px;">大小</th><th style="padding:10px;">操作</th></tr>';

                    const tokenStr = "token=" + sessionStorage.getItem('nvr_token');
                    data.segments.forEach(seg => {
                        segmentsHTML += `<tr>
                            <td style="padding:8px 10px;"><input type="checkbox" class="seg-check" value="${seg.filename}"></td>
                            <td style="padding:8px 10px;">${seg.time}</td>
                            <td style="padding:8px 10px;">${seg.size} MB</td>
                            <td style="padding:8px 10px;"><a href="${seg.url}?${tokenStr}" download class="action-link">⬇ 提取</a></td>
                        </tr>`;
                    });
                    segmentsHTML += '</table></div>';

                    tbody.innerHTML = `<tr>
                        <td style="font-weight:500; vertical-align:top;">⏱️ ${start.replace('T', ' ')} <br>至<br>⏱️ ${end.replace('T', ' ')}</td>
                        <td style="color:#64748b; font-size:13px; vertical-align:top;">跨越 <b>${data.count}</b> 个底层录像切片<br>总预估大小: <b>${data.total_size_mb} MB</b><br><span style="color:#10b981">连贯就绪</span></td>
                        <td style="vertical-align:top;">
                            <button class="btn btn-primary" id="btn-preview" onclick="doExportAndAction('preview')">▶ 无缝预览</button>
                            <button class="btn btn-success" style="margin-top:5px;" id="btn-download" onclick="doExportAndAction('download')">⬇ 合并下载</button>
                        </td>
                    </tr>
                    <tr><td colspan="3" style="padding: 10px 20px;">${segmentsHTML}</td></tr>`;
                }).catch(err => { tbody.innerHTML = '<tr><td colspan="3" class="empty-msg" style="color:#ef4444;">检索失败。</td></tr>'; });
        }

        function toggleAll(source) {
            const checkboxes = document.querySelectorAll('.seg-check');
            checkboxes.forEach(cb => cb.checked = source.checked);
        }

        function batchDownloadZip(btn) {
            const checkboxes = document.querySelectorAll('.seg-check:checked');
            const files = Array.from(checkboxes).map(cb => cb.value);
            if(files.length === 0) return alert("请先勾选需要打包下载的视频切片！");

            const oldText = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = '<span class="loading-spinner"></span>正在后台压制 ZIP...';

            fetchNoCache(`/api/batch_zip?cam=${encodeURIComponent(currentCam)}&files=${encodeURIComponent(files.join(','))}`)
                .then(r => r.json())
                .then(data => {
                    const a = document.createElement('a');
                    const tokenStr = "token=" + sessionStorage.getItem('nvr_token');
                    a.href = data.url.includes('?') ? data.url + '&' + tokenStr : data.url + '?' + tokenStr;
                    a.download = data.url.split('/').pop();
                    document.body.appendChild(a); a.click(); document.body.removeChild(a);
                })
                .catch(e => alert("打包失败"))
                .finally(() => { btn.disabled = false; btn.innerHTML = oldText; });
        }

        function doExportAndAction(actionType) {
            const start = document.getElementById('time-start').value; const end = document.getElementById('time-end').value;
            const btnPreview = document.getElementById('btn-preview'); const btnDownload = document.getElementById('btn-download');
            btnPreview.disabled = true; btnDownload.disabled = true;
            if(actionType === 'download') btnDownload.innerHTML = '<span class="loading-spinner"></span>合并中...';
            else btnPreview.innerHTML = '<span class="loading-spinner"></span>组装流...';

            fetchNoCache(`/api/export?cam=${encodeURIComponent(currentCam)}&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`)
                .then(r => { if (!r.ok) throw new Error('合并处理失败'); return r.json(); })
                .then(data => {
                    const tokenStr = "token=" + sessionStorage.getItem('nvr_token');
                    const finalUrl = data.url.includes('?') ? data.url + '&' + tokenStr : data.url + '?' + tokenStr;

                    if (actionType === 'download') {
                        const a = document.createElement('a'); 
                        a.href = finalUrl; 
                        a.download = data.url.split('/').pop(); 
                        document.body.appendChild(a); 
                        a.click(); 
                        document.body.removeChild(a);
                    } else {
                        const container = document.getElementById('player-container'); 
                        const player = document.getElementById('player');
                        container.style.display = 'block'; 
                        player.src = finalUrl; 
                        player.play(); 
                        container.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    }
                }).catch(err => alert("底层合成失败！"))
                .finally(() => {
                    btnPreview.disabled = false; btnDownload.disabled = false;
                    btnPreview.innerHTML = '▶ 无缝预览'; btnDownload.innerHTML = '⬇ 合并下载';
                });
        }

        window.onload = initApp;
    </script>
</body>
</html>
"""
        encoded = html_content.encode('utf-8')
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)