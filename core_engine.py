import os
import time
import threading
import subprocess
import queue
import re
import shutil
import uuid
import concurrent.futures
import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
import logging
import logging.handlers
import socket

import config_manager
from utils import QueueHandler


class CoreEngine:
    def __init__(self, root):
        self.root = root
        self.ui = None  # 稍后从 main.py 注入
        self.httpd = None
        self.app_running = True
        self.is_running = False
        self.processes = {}
        self.go_running_cams = []
        self.log_queue = queue.Queue()
        self.cameras = []

        self.last_running_cams = set()
        self.first_poll_done = False

        self.live_processes = {}
        self.batch_jobs = {}

        self.retention_days = 7
        self.schedule_enabled = False
        self.sch1_en, self.sch1_start, self.sch1_end = False, "08:00", "12:00"
        self.sch2_en, self.sch2_start, self.sch2_end = False, "14:00", "18:00"
        self.sch3_en, self.sch3_start, self.sch3_end = False, "19:00", "22:00"

        # 初始化配置与目录
        config_manager.load_users()
        config_manager.load_groups()
        self.cameras = config_manager.load_cameras()
        self.load_sys_config()

        os.makedirs(config_manager.STORAGE_DIR, exist_ok=True)
        if os.path.exists(config_manager.LIVE_TEMP_DIR):
            shutil.rmtree(config_manager.LIVE_TEMP_DIR, ignore_errors=True)
        os.makedirs(config_manager.LIVE_TEMP_DIR, exist_ok=True)

        self.setup_logging()

    def start_threads(self):
        self.cleanup_thread = threading.Thread(target=self.cleanup_worker, daemon=True)
        self.cleanup_thread.start()

        self.scheduler_thread = threading.Thread(target=self.scheduler_worker, daemon=True)
        self.scheduler_thread.start()

        self.go_poll_thread = threading.Thread(target=self.poll_go_engine, daemon=True)
        self.go_poll_thread.start()

        self.live_watchdog_thread = threading.Thread(target=self.live_watchdog_worker, daemon=True)
        self.live_watchdog_thread.start()

    def setup_logging(self):
        logger = logging.getLogger()
        logger.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

        gui_handler = QueueHandler(self.log_queue)
        gui_handler.setFormatter(formatter)
        logger.addHandler(gui_handler)

        file_handler = logging.handlers.TimedRotatingFileHandler(
            config_manager.LOG_FILE, when='midnight', interval=1, backupCount=180, encoding='utf-8'
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    def load_sys_config(self):
        if os.path.exists(config_manager.SYS_CONFIG_FILE):
            try:
                with open(config_manager.SYS_CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    self.retention_days = config.get("retention_days", 7)
                    self.schedule_enabled = config.get("schedule_enabled", False)
                    self.sch1_en = config.get("sch1_en", False)
                    self.sch1_start = config.get("sch1_start", "08:00")
                    self.sch1_end = config.get("sch1_end", "12:00")
                    self.sch2_en = config.get("sch2_en", False)
                    self.sch2_start = config.get("sch2_start", "14:00")
                    self.sch2_end = config.get("sch2_end", "18:00")
                    self.sch3_en = config.get("sch3_en", False)
                    self.sch3_start = config.get("sch3_start", "19:00")
                    self.sch3_end = config.get("sch3_end", "22:00")
            except Exception:
                pass

    # ---------------- 核心：批量排队拼合子引擎 ----------------
    def run_batch_job(self, job_id, date_str, slots, cams, explicit_tasks=None):
        export_folder_name = f"Web_Batch_{date_str}_{int(time.time())}"
        export_dir = os.path.join(config_manager.STORAGE_DIR, "Web_Batch_Export", export_folder_name)
        os.makedirs(export_dir, exist_ok=True)

        all_tasks = []
        if explicit_tasks:
            # 【新增兼容】如果是从智能课表解析页面发来的，直接提取精准的任务
            for t in explicit_tasks:
                all_tasks.append((t.get('cam'), t.get('start'), t.get('end')))
        else:
            # 否则走旧版极简提取的叉乘逻辑
            for cam_id in cams:
                for slot in slots:
                    all_tasks.append((cam_id, slot.get('start'), slot.get('end')))

        import zipfile  # Ensure zipfile is available here
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            future_to_task = {}
            for task in all_tasks:
                cam_id, start_str, end_str = task
                try:
                    req_start = datetime.strptime(f"{date_str} {start_str}", "%Y-%m-%d %H:%M")
                    req_end = datetime.strptime(f"{date_str} {end_str}", "%Y-%m-%d %H:%M")
                except ValueError:
                    self.batch_jobs[job_id]["failed"] += 1
                    self.batch_jobs[job_id]["completed"] += 1
                    continue

                future = executor.submit(self._process_single_cam_batch, cam_id, req_start, req_end, export_dir,
                                         export_folder_name, date_str, start_str, end_str)
                future_to_task[future] = task

            for future in concurrent.futures.as_completed(future_to_task):
                res = future.result()
                if res["status"] == "ok":
                    self.batch_jobs[job_id]["results"].append(res["result"])
                else:
                    self.batch_jobs[job_id]["failed"] += 1
                self.batch_jobs[job_id]["completed"] += 1

        if self.batch_jobs[job_id]["results"]:
            zip_fname = f"Batch_Export_{date_str}_{int(time.time())}.zip"
            zip_fpath = os.path.join(export_dir, zip_fname)
            try:
                with zipfile.ZipFile(zip_fpath, 'w', zipfile.ZIP_STORED, allowZip64=True) as zipf:
                    for res in self.batch_jobs[job_id]["results"]:
                        mp4_phys_path = res.get("path")
                        mp4_filename = res.get("fname")
                        if mp4_phys_path and os.path.exists(mp4_phys_path):
                            zipf.write(mp4_phys_path, mp4_filename)

                self.batch_jobs[job_id][
                    "zip_url"] = f"/Web_Batch_Export/{export_folder_name}/{urllib.parse.quote(zip_fname)}"
            except Exception as e:
                err_msg = str(e) or repr(e)
                logging.error(f"批量打包ZIP失败: {err_msg}")
                self.batch_jobs[job_id]["zip_error"] = err_msg
                if os.path.exists(zip_fpath):
                    try:
                        os.remove(zip_fpath)
                    except:
                        pass

        self.batch_jobs[job_id]["status"] = "done"
        logging.info(f"✅ [批量并发调度] Job {job_id} 圆满完工！")

    def _process_single_cam_batch(self, cam_id, req_start, req_end, export_dir, export_folder_name, date_str, start_str,
                                  end_str):
        cam_dir = os.path.join(config_manager.STORAGE_DIR, cam_id)
        if not os.path.isdir(cam_dir):
            return {"status": "error"}

        files_to_merge = []
        first_file_dt = None
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
            return {"status": "error"}

        list_file_path = os.path.join(export_dir, f"list_{cam_id}_{start_str.replace(':', '')}.txt")
        with open(list_file_path, "w", encoding="utf-8") as f:
            for fname in files_to_merge:
                abs_path = os.path.abspath(os.path.join(cam_dir, fname)).replace("\\", "/")
                f.write(f"file '{abs_path}'\n")

        start_offset = max(0, (req_start - first_file_dt).total_seconds())
        duration = (req_end - req_start).total_seconds()

        out_fname = f"{cam_id}_{date_str}_{start_str.replace(':', '')}-{end_str.replace(':', '')}.mp4"
        out_fpath = os.path.join(export_dir, out_fname)

        if not os.path.exists(out_fpath):
            cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file_path,
                   "-ss", str(start_offset), "-t", str(duration),
                   "-map", "0:v", "-map", "0:a?", "-c", "copy", out_fpath]
            try:
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            except subprocess.CalledProcessError:
                pass
            finally:
                if os.path.exists(list_file_path): os.remove(list_file_path)

        if os.path.exists(out_fpath):
            url = f"/Web_Batch_Export/{export_folder_name}/{urllib.parse.quote(out_fname)}"
            return {"status": "ok",
                    "result": {"cam": cam_id, "time": f"{start_str} 至 {end_str}", "url": url, "path": out_fpath,
                               "fname": out_fname}}
        else:
            return {"status": "error"}

    # -------------- 实时直播生命周期管理 --------------
    def start_live_stream(self, cam_info):
        cam_id = cam_info["id"]
        rtsp_url = cam_info["url"]

        if cam_id in self.live_processes and self.live_processes[cam_id]['process'].poll() is None:
            self.live_processes[cam_id]['last_heartbeat'] = time.time()
            return f"/live_temp/{urllib.parse.quote(cam_id)}.m3u8"

        m3u8_path = os.path.join(config_manager.LIVE_TEMP_DIR, f"{cam_id}.m3u8").replace("\\", "/")
        ts_pattern = os.path.join(config_manager.LIVE_TEMP_DIR, f"{cam_id}_%03d.ts").replace("\\", "/")

        if os.path.exists(m3u8_path): os.remove(m3u8_path)
        for f in os.listdir(config_manager.LIVE_TEMP_DIR):
            if f.startswith(f"{cam_id}_") and f.endswith(".ts"):
                os.remove(os.path.join(config_manager.LIVE_TEMP_DIR, f))

        cmd = [
            "ffmpeg", "-rtsp_transport", "tcp", "-i", rtsp_url,
            "-c:v", "copy", "-c:a", "aac", "-f", "hls",
            "-hls_time", "2", "-hls_list_size", "3",
            "-hls_flags", "delete_segments",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", ts_pattern,
            m3u8_path
        ]

        try:
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.live_processes[cam_id] = {
                "process": p,
                "last_heartbeat": time.time()
            }
        except Exception as e:
            logging.error(f"启动在线直播失败: {e}")

        return f"/live_temp/{urllib.parse.quote(cam_id)}.m3u8"

    def stop_live_stream(self, cam_id):
        if cam_id in self.live_processes:
            p = self.live_processes[cam_id]['process']
            if p.poll() is None:
                p.terminate()
            del self.live_processes[cam_id]

        m3u8_path = os.path.join(config_manager.LIVE_TEMP_DIR, f"{cam_id}.m3u8")
        if os.path.exists(m3u8_path):
            try:
                os.remove(m3u8_path)
            except:
                pass
        for f in os.listdir(config_manager.LIVE_TEMP_DIR):
            if f.startswith(f"{cam_id}_") and f.endswith(".ts"):
                try:
                    os.remove(os.path.join(config_manager.LIVE_TEMP_DIR, f))
                except:
                    pass

    def live_watchdog_worker(self):
        while self.app_running:
            now = time.time()
            cams_to_kill = []
            for cam_id, info in self.live_processes.items():
                if now - info['last_heartbeat'] > 15:
                    cams_to_kill.append(cam_id)

            for cam_id in cams_to_kill:
                logging.info(f"🔄 [资源回收] 频道 [{cam_id}] 无用户观看，已自动切断在线推流。")
                self.stop_live_stream(cam_id)
            time.sleep(5)

    def poll_go_engine(self):
        logging.info("🔗 Golang引擎状态同步监听已启动...")
        while self.app_running:
            if self.is_running:
                try:
                    req = urllib.request.Request("http://127.0.0.1:8081/status")
                    with urllib.request.urlopen(req, timeout=2) as response:
                        data = json.loads(response.read().decode())
                        self.go_running_cams = data.get("running", [])
                        current_running = set(self.go_running_cams)

                        if not self.first_poll_done:
                            self.last_running_cams = current_running
                            self.first_poll_done = True
                        else:
                            dropped = self.last_running_cams - current_running
                            for cid in dropped:
                                logging.warning(
                                    f"🚨 [网络/设备异常告警] 监控通道 [{cid}] 拉流意外中断！可能是网络波动或设备离线，引擎已介入并持续尝试自动重连...")

                            recovered = current_running - self.last_running_cams
                            for cid in recovered:
                                logging.info(f"✅ [故障恢复] 监控通道 [{cid}] 故障已排除，底层视频流重新连接成功！")

                            self.last_running_cams = current_running

                        for cam in self.cameras:
                            cid = cam["id"]
                            if cid in self.go_running_cams:
                                if self.ui: self.root.after(0, self.ui.update_tree_status, cid, "正在录制 ⏺")
                            else:
                                if self.ui: self.root.after(0, self.ui.update_tree_status, cid, "断线重连中 ⚠️")
                except Exception:
                    self.go_running_cams = []
                    if self.first_poll_done:
                        logging.error(
                            "🚨 [系统级致命告警] 无法连接到底层 Golang 拉流引擎！引擎可能发生异常崩溃，等待系统守护进程尝试拉起...")
                        self.first_poll_done = False
            else:
                self.first_poll_done = False
                self.last_running_cams = set()

            time.sleep(3)

    def scheduler_worker(self):
        logging.info("⏱️ 多时段自动定时录像看门狗已启动...")
        while self.app_running:
            if self.schedule_enabled:
                now_str = datetime.now().strftime("%H:%M")
                should_run = False

                slots = [
                    (self.sch1_en, self.sch1_start, self.sch1_end),
                    (self.sch2_en, self.sch2_start, self.sch2_end),
                    (self.sch3_en, self.sch3_start, self.sch3_end)
                ]

                triggered_slot = ""
                for idx, (en, st, ed) in enumerate(slots):
                    if en and st and ed:
                        if st <= ed:  # 同一天内
                            if st <= now_str < ed:
                                should_run = True
                                triggered_slot = f"时段{idx + 1} ({st}-{ed})"
                                break
                        else:  # 跨夜场景
                            if now_str >= st or now_str < ed:
                                should_run = True
                                triggered_slot = f"时段{idx + 1} ({st}-{ed})"
                                break

                if should_run and not self.is_running:
                    logging.info(f"⏰ 触发排班计划 [{triggered_slot}]，正在自动唤醒全量拉流...")
                    self.root.after(0, self.start_all)
                elif not should_run and self.is_running:
                    logging.info(f"⏰ 当前时间不在任何排班计划内，正在自动切断拉流休眠...")
                    self.root.after(0, self.stop_all)

            for _ in range(15):
                if not self.app_running: return
                time.sleep(1)

    def cleanup_worker(self):
        logging.info("♻️ 系统自动清理看门狗已启动，防硬盘打满...")
        while self.app_running:
            try:
                self.perform_cleanup()
            except Exception as e:
                logging.error(f"执行自动清理时发生异常: {e}")
            for _ in range(600):
                if not self.app_running: return
                time.sleep(1)

    def perform_cleanup(self):
        if self.retention_days <= 0: return
        now = time.time()
        main_cutoff_time = now - (self.retention_days * 24 * 3600)
        export_cutoff_time = now - (60 * 60)
        deleted_count = deleted_size = 0

        for sid in list(config_manager.SESSIONS.keys()):
            if now - config_manager.SESSIONS[sid].get('time', now) > 24 * 3600:
                del config_manager.SESSIONS[sid]

        if not os.path.exists(config_manager.STORAGE_DIR): return

        for root_dir, dirs, files in os.walk(config_manager.STORAGE_DIR, topdown=False):
            is_export_dir = "Web_Batch_Export" in root_dir or "Export_Download" in root_dir
            for file in files:
                if file.endswith(".mp4") or file.endswith(".txt") or file.endswith(".zip"):
                    file_path = os.path.join(root_dir, file)
                    try:
                        file_mtime = os.path.getmtime(file_path)
                        target_cutoff = export_cutoff_time if is_export_dir else main_cutoff_time
                        if file_mtime < target_cutoff:
                            size = os.path.getsize(file_path)
                            os.remove(file_path)
                            deleted_count += 1
                            deleted_size += size
                            logging.info(f"🗑️ [看门狗] 已自动清理过期/临时文件: {file}")
                    except Exception:
                        pass

            if is_export_dir and root_dir not in [os.path.join(config_manager.STORAGE_DIR, "Web_Batch_Export"),
                                                  os.path.join(config_manager.STORAGE_DIR, "Export_Download")]:
                try:
                    if not os.listdir(root_dir):
                        os.rmdir(root_dir)
                except Exception:
                    pass

        if deleted_count > 0:
            logging.info(
                f"✨ [清理完成] 共释放 {deleted_count} 个过期文件，腾出 {deleted_size / (1024 * 1024):.1f} MB 硬盘空间。")

    def web_api_save_schedule(self, data):
        self.retention_days = int(data.get('retention_days', self.retention_days))
        self.schedule_enabled = data.get('schedule_enabled', False)
        self.sch1_en = data.get('sch1_en', False)
        self.sch1_start = data.get('sch1_start', '08:00')
        self.sch1_end = data.get('sch1_end', '12:00')
        self.sch2_en = data.get('sch2_en', False)
        self.sch2_start = data.get('sch2_start', '14:00')
        self.sch2_end = data.get('sch2_end', '18:00')
        self.sch3_en = data.get('sch3_en', False)
        self.sch3_start = data.get('sch3_start', '19:00')
        self.sch3_end = data.get('sch3_end', '22:00')

        if self.ui:
            self.ui.sync_sys_config_to_ui()
            self.ui.save_sys_config(from_code=True)
        logging.info(f"🌐 [Web远程操作] 成功更新系统多时段定时排班计划！")

    def web_api_add_camera(self, cam_group, cam_id, ip):
        full_url = f"rtsp://{config_manager.RTSP_USER}:{config_manager.RTSP_PWD}@{ip}:{config_manager.RTSP_PORT}/Streaming/Channels/{config_manager.RTSP_CHANNEL}"
        new_cam = {"id": str(cam_id), "group": str(cam_group), "ip": str(ip), "url": full_url}
        self.cameras.append(new_cam)
        config_manager.save_cameras(self.cameras)

        if self.ui:
            self.ui.refresh_desktop_tree()

        if self.is_running:
            try:
                data = json.dumps(new_cam).encode('utf-8')
                req = urllib.request.Request("http://127.0.0.1:8081/start_single", data=data,
                                             headers={'Content-Type': 'application/json'})
                urllib.request.urlopen(req, timeout=2)
            except Exception as e:
                logging.error(f"通知 Golang 引擎拉流失败: {e}")

    def web_api_delete_camera(self, cam_id):
        self.cameras = [cam for cam in self.cameras if str(cam["id"]) != str(cam_id)]
        config_manager.save_cameras(self.cameras)
        if self.ui:
            self.ui.refresh_desktop_tree()

    def check_ip_connectivity(self, ip, port=554, timeout=2):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            result = s.connect_ex((ip, port))
            s.close()
            return result == 0
        except Exception:
            return False

    def start_all(self):
        if not self.cameras:
            if self.ui: self.ui.show_warning("提示", "列表为空，请先添加！")
            return
        self.is_running = True
        if self.ui: self.ui.set_control_buttons_state(running=True)

        os.makedirs(config_manager.STORAGE_DIR, exist_ok=True)
        logging.info("=== 集中启动所有拉流任务 (移交 Golang 引擎) ===")
        try:
            data = json.dumps({"cameras": self.cameras}).encode('utf-8')
            req = urllib.request.Request("http://127.0.0.1:8081/start_all", data=data,
                                         headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            logging.error(f"❌ 无法连接到 Golang 引擎，请确认其已启动: {e}")

    def stop_all(self):
        self.is_running = False
        if self.ui: self.ui.set_control_buttons_state(running=False)
        try:
            req = urllib.request.Request("http://127.0.0.1:8081/stop_all", method="POST")
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass
        self.go_running_cams = []
        logging.info("=== 所有拉流任务已被安全停止 ===")
        if self.ui: self.ui.refresh_desktop_tree()