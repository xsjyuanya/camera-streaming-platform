import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
import logging
import queue
import subprocess
import os

import config_manager
from utils import natural_keys, is_complex_password

class StreamManagerUI:
    def __init__(self, root, engine):
        self.root = root
        self.engine = engine
        self.engine.ui = self

        self.root.title("100路监控拉流集中管控平台 - 纯无头Web支持版")
        self.root.geometry("400x200")

        self.setup_ui()
        self.sync_sys_config_to_ui()
        self.check_ffmpeg()
        
        self.root.after(100, self.update_logs)

    def setup_ui(self):
        sys_frame = ttk.LabelFrame(self.root, text="系统底层运行与安全设置", padding=(10, 5))
        sys_frame.pack(fill=tk.X, padx=10, pady=5)

        f_sys_top = tk.Frame(sys_frame)
        f_sys_top.pack(fill=tk.X, pady=2)

        ttk.Label(f_sys_top, text="自动循环覆盖: 保留最近").grid(row=0, column=0, padx=5, pady=5)
        self.entry_retention = ttk.Entry(f_sys_top, width=5, justify='center')
        self.entry_retention.grid(row=0, column=1, padx=5, pady=5)
        ttk.Label(f_sys_top, text="天").grid(row=0, column=2, padx=5, pady=5)
        ttk.Button(f_sys_top, text="👥 本地账号权限管理", command=self.open_user_manager).grid(row=0, column=3, padx=30, pady=5)

        f_sch = tk.Frame(sys_frame)
        f_sch.pack(fill=tk.X, pady=5)

        self.var_schedule = tk.BooleanVar()
        ttk.Checkbutton(f_sch, text="启用全自动定时录像 (总开关)", variable=self.var_schedule).grid(row=0, column=0, columnspan=4, sticky=tk.W, pady=2)

        self.var_sch1 = tk.BooleanVar()
        ttk.Checkbutton(f_sch, text="时段 1:", variable=self.var_sch1).grid(row=1, column=0, sticky=tk.E, padx=5, pady=2)
        self.entry_sch1_start = ttk.Entry(f_sch, width=6, justify='center')
        self.entry_sch1_start.grid(row=1, column=1)
        ttk.Label(f_sch, text="至").grid(row=1, column=2, padx=2)
        self.entry_sch1_end = ttk.Entry(f_sch, width=6, justify='center')
        self.entry_sch1_end.grid(row=1, column=3)

        self.var_sch2 = tk.BooleanVar()
        ttk.Checkbutton(f_sch, text="时段 2:", variable=self.var_sch2).grid(row=2, column=0, sticky=tk.E, padx=5, pady=2)
        self.entry_sch2_start = ttk.Entry(f_sch, width=6, justify='center')
        self.entry_sch2_start.grid(row=2, column=1)
        ttk.Label(f_sch, text="至").grid(row=2, column=2, padx=2)
        self.entry_sch2_end = ttk.Entry(f_sch, width=6, justify='center')
        self.entry_sch2_end.grid(row=2, column=3)

        self.var_sch3 = tk.BooleanVar()
        ttk.Checkbutton(f_sch, text="时段 3:", variable=self.var_sch3).grid(row=3, column=0, sticky=tk.E, padx=5, pady=2)
        self.entry_sch3_start = ttk.Entry(f_sch, width=6, justify='center')
        self.entry_sch3_start.grid(row=3, column=1)
        ttk.Label(f_sch, text="至").grid(row=3, column=2, padx=2)
        self.entry_sch3_end = ttk.Entry(f_sch, width=6, justify='center')
        self.entry_sch3_end.grid(row=3, column=3)

        ttk.Button(f_sch, text="💾 保存系统与定时设置", command=self.save_sys_config).grid(row=0, column=5, rowspan=4, padx=30, sticky=tk.NS)

        web_frame = ttk.LabelFrame(self.root, text="局域网 Web 视频检索引擎 (包含远程全功能管控)", padding=(10, 5))
        web_frame.pack(fill=tk.X, padx=10, pady=5)
        self.btn_web = ttk.Button(web_frame, text="🌐 局域网 Web 客户端由引擎自动托管", state=tk.DISABLED)
        self.btn_web.pack(side=tk.LEFT, padx=5)

        # 【核心修改点】隐藏了复杂的本地添加界面，导流用户至 Web
        add_frame = ttk.LabelFrame(self.root, text="高级多品牌设备入网与管理 (新架构)", padding=(10, 15))
        add_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(add_frame, text="💡 系统底层已升级至高级多品牌解耦协议！(原生兼容 海康/大华/宇视/自定义流)\n为保障安全并使用多品牌账号单独配置功能，请使用浏览器登录系统专属 Web 控制台操作。").pack(pady=5)
        
        def open_browser():
            import webbrowser
            webbrowser.open("http://127.0.0.1:8080")
            
        row1 = tk.Frame(add_frame)
        row1.pack(fill=tk.X, pady=5)
        ttk.Button(row1, text="🌐 立即前往 Web 控制台新增/编辑设备", command=open_browser).pack(side=tk.LEFT, padx=5)
        
        self.btn_delete = ttk.Button(row1, text="❌ 本地删除选中", command=self.delete_camera)
        self.btn_delete.pack(side=tk.LEFT, padx=5)
        self.btn_preview = ttk.Button(row1, text="👁️ 本机实时预览选中", command=self.preview_camera)
        self.btn_preview.pack(side=tk.LEFT, padx=5)

        control_frame = ttk.LabelFrame(self.root, text="全局录像拉流控制", padding=(10, 10))
        control_frame.pack(fill=tk.X, padx=10, pady=5)
        self.btn_start = ttk.Button(control_frame, text="▶ 手动启动全部录像", command=self.engine.start_all)
        self.btn_start.pack(side=tk.LEFT, padx=5)
        self.btn_stop = ttk.Button(control_frame, text="■ 手动停止全部录像", command=self.engine.stop_all, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=5)

        list_frame = ttk.LabelFrame(self.root, text="摄像头列表与运行状态 (自动按字母拼音排序)", padding=(10, 10))
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        columns = ("ID", "所属分组", "IP", "状态")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=5)
        self.tree.heading("ID", text="摄像头ID")
        self.tree.heading("所属分组", text="所属分组")
        self.tree.heading("IP", text="摄像头IP")
        self.tree.heading("状态", text="当前状态")
        self.tree.column("ID", width=120)
        self.tree.column("所属分组", width=180)
        self.tree.column("IP", width=250)
        self.tree.column("状态", width=120)
        self.tree.pack(fill=tk.BOTH, expand=True)

        log_frame = ttk.LabelFrame(self.root, text="服务器后台实时日志", padding=(10, 10))
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.log_text = tk.Text(log_frame, state='disabled', wrap='word', height=10)
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def sync_sys_config_to_ui(self):
        self.entry_retention.delete(0, tk.END)
        self.entry_retention.insert(0, str(self.engine.retention_days))
        self.var_schedule.set(self.engine.schedule_enabled)
        self.var_sch1.set(self.engine.sch1_en)
        self.entry_sch1_start.delete(0, tk.END)
        self.entry_sch1_start.insert(0, self.engine.sch1_start)
        self.entry_sch1_end.delete(0, tk.END)
        self.entry_sch1_end.insert(0, self.engine.sch1_end)
        self.var_sch2.set(self.engine.sch2_en)
        self.entry_sch2_start.delete(0, tk.END)
        self.entry_sch2_start.insert(0, self.engine.sch2_start)
        self.entry_sch2_end.delete(0, tk.END)
        self.entry_sch2_end.insert(0, self.engine.sch2_end)
        self.var_sch3.set(self.engine.sch3_en)
        self.entry_sch3_start.delete(0, tk.END)
        self.entry_sch3_start.insert(0, self.engine.sch3_start)
        self.entry_sch3_end.delete(0, tk.END)
        self.entry_sch3_end.insert(0, self.engine.sch3_end)
        self.refresh_desktop_tree()

    def save_sys_config(self, from_code=False):
        try:
            val = int(self.entry_retention.get().strip())
            sch_en = self.var_schedule.get()

            s1_en, s1_st, s1_ed = self.var_sch1.get(), self.entry_sch1_start.get().strip(), self.entry_sch1_end.get().strip()
            s2_en, s2_st, s2_ed = self.var_sch2.get(), self.entry_sch2_start.get().strip(), self.entry_sch2_end.get().strip()
            s3_en, s3_st, s3_ed = self.var_sch3.get(), self.entry_sch3_start.get().strip(), self.entry_sch3_end.get().strip()

            from datetime import datetime
            if sch_en:
                try:
                    if s1_en: datetime.strptime(s1_st, "%H:%M"); datetime.strptime(s1_ed, "%H:%M")
                    if s2_en: datetime.strptime(s2_st, "%H:%M"); datetime.strptime(s2_ed, "%H:%M")
                    if s3_en: datetime.strptime(s3_st, "%H:%M"); datetime.strptime(s3_ed, "%H:%M")
                except ValueError:
                    if not from_code: messagebox.showerror("错误", "启用的时段格式错误，必须为 HH:MM (如 08:00)！")
                    return

            self.engine.retention_days = val
            self.engine.schedule_enabled = sch_en
            self.engine.sch1_en, self.engine.sch1_start, self.engine.sch1_end = s1_en, s1_st, s1_ed
            self.engine.sch2_en, self.engine.sch2_start, self.engine.sch2_end = s2_en, s2_st, s2_ed
            self.engine.sch3_en, self.engine.sch3_start, self.engine.sch3_end = s3_en, s3_st, s3_ed

            import json
            with open(config_manager.SYS_CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump({
                    "retention_days": val,
                    "schedule_enabled": sch_en,
                    "sch1_en": s1_en, "sch1_start": s1_st, "sch1_end": s1_ed,
                    "sch2_en": s2_en, "sch2_start": s2_st, "sch2_end": s2_ed,
                    "sch3_en": s3_en, "sch3_start": s3_st, "sch3_end": s3_ed
                }, f, ensure_ascii=False, indent=4)

            import threading
            threading.Thread(target=self.engine.perform_cleanup, daemon=True).start()
        except ValueError:
            pass

    def check_ffmpeg(self):
        logging.info("⚙️ FFmpeg 环境检测将由 Golang 底层引擎负责。")

    def show_warning(self, title, msg):
        messagebox.showwarning(title, msg)

    def set_control_buttons_state(self, running):
        if running:
            self.btn_start.config(state=tk.DISABLED)
            self.btn_stop.config(state=tk.NORMAL)
            self.btn_delete.config(state=tk.DISABLED)
        else:
            self.btn_start.config(state=tk.NORMAL)
            self.btn_stop.config(state=tk.DISABLED)
            self.btn_delete.config(state=tk.NORMAL)

    def update_logs(self):
        try:
            while True:
                msg = self.engine.log_queue.get_nowait()
                self.log_text.configure(state='normal')
                self.log_text.insert(tk.END, msg + "\n")
                self.log_text.see(tk.END)
                self.log_text.configure(state='disabled')
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self.update_logs)

    def update_tree_status(self, cam_id, status_text):
        if self.tree.exists(cam_id):
            item = self.tree.item(cam_id)
            values = list(item['values'])
            values[3] = status_text
            self.tree.item(cam_id, values=values)

    def refresh_desktop_tree(self):
        for item in self.tree.get_children(): self.tree.delete(item)
        sorted_cams = sorted(self.engine.cameras, key=lambda x: natural_keys(str(x.get('group', '默认分组')) + str(x['id'])))
        for cam in sorted_cams:
            status = "无录像"
            if cam['id'] in self.engine.go_running_cams:
                status = "正在录制 ⏺"
            elif self.engine.is_running:
                status = "等待引擎同步..."
            self.tree.insert("", tk.END, iid=cam["id"],
                             values=(cam["id"], str(cam.get("group", "默认分组")), str(cam.get("ip", "")), status))

    def delete_camera(self):
        selected_items = self.tree.selection()
        if not selected_items: return messagebox.showinfo("提示", "请先选中要删除的摄像头！")
        for item_id in selected_items:
            self.engine.web_api_delete_camera(item_id)
            logging.warning(f"🛡️ [安全审计] 本地超管从系统移专门除了摄像头: {item_id}")

    def preview_camera(self):
        selected_items = self.tree.selection()
        if not selected_items: return messagebox.showinfo("提示", "请先选中要预览！")
        item_id = selected_items[0]
        target_url = next((cam["url"] for cam in self.engine.cameras if str(cam["id"]) == str(item_id)), None)
        if not target_url: return

        cmd = ["ffplay", "-window_title", f"预览 - {item_id}", "-rtsp_transport", "tcp", "-x", "800", "-y", "450", "-i",
               target_url]
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except Exception as e:
            logging.error(f"预览异常: {str(e)}")

    def open_group_manager(self):
        win = tk.Toplevel(self.root)
        win.title("预设分层架构管理")
        win.geometry("550x350")
        win.transient(self.root)
        win.grab_set()

        f_top = tk.Frame(win)
        f_top.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(f_top, text="① 父级目录:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        cmb_parent = ttk.Combobox(f_top, width=28, state="readonly")
        cmb_parent.grid(row=0, column=1, padx=5, pady=5)

        ttk.Label(f_top, text="② 附加子目录:").grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        ent_grp = ttk.Entry(f_top, width=30)
        ent_grp.grid(row=1, column=1, padx=5, pady=5)
        ttk.Label(f_top, text="(例: 高中部/高一/1班)", foreground="gray").grid(row=1, column=2, padx=5, sticky=tk.W)

        listbox = tk.Listbox(win, height=10)
        listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        def refresh_list():
            listbox.delete(0, tk.END)
            parents = ["-- 作为根目录 (顶级) --"] + sorted(config_manager.GROUPS_DB, key=natural_keys)
            cmb_parent['values'] = parents
            cmb_parent.current(0)
            for g in sorted(config_manager.GROUPS_DB, key=natural_keys): listbox.insert(tk.END, g)
            self.refresh_group_combobox()

        def add_grp():
            p = cmb_parent.get()
            c = ent_grp.get().strip()
            if not c: return messagebox.showwarning("提示", "子级名称不可为空", parent=win)

            new_g = p + '/' + c if (p and p != "-- 作为根目录 (顶级) --") else c
            parts = new_g.split('/')
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
                refresh_list()
                ent_grp.delete(0, tk.END)
                messagebox.showinfo("成功", "架构层级解析并创建成功！", parent=win)
                logging.info(f"🛡️ [安全审计] 本地超管创建了组织层级架构: {new_g}")
            else:
                messagebox.showinfo("提示", "该层级已存在", parent=win)

        def del_grp():
            sel = listbox.curselection()
            if not sel: return
            g = listbox.get(sel[0])
            if g == "默认分组": return messagebox.showwarning("提示", "默认分组不可删", parent=win)
            if messagebox.askyesno("确认", f"确定删除预设层级 [{g}] 吗？\n(已在该组的摄像头不受影响)", parent=win):
                config_manager.GROUPS_DB.remove(g)
                config_manager.save_groups()
                refresh_list()
                logging.info(f"🛡️ [安全审计] 本地超管删除了组织层级架构: {g}")

        ttk.Button(f_top, text="➕ 智能创建层级", command=add_grp).grid(row=2, column=1, pady=10, sticky=tk.W)
        ttk.Button(win, text="❌ 删除选中的废弃分组", command=del_grp).pack(pady=5)
        refresh_list()

    def refresh_group_combobox(self):
        pass # UI已被精简导向网页，这里置空即可

    def open_user_manager(self):
        win = tk.Toplevel(self.root)
        win.title("本地系统账号分配中枢")
        win.geometry("600x300")
        win.transient(self.root)
        win.grab_set()

        f_add = tk.Frame(win)
        f_add.pack(fill=tk.X, padx=10, pady=10)

        tk.Label(f_add, text="账号:").grid(row=0, column=0, padx=2, pady=5)
        ent_usr = ttk.Entry(f_add, width=10)
        ent_usr.grid(row=0, column=1, padx=2)

        tk.Label(f_add, text="密码:").grid(row=0, column=2, padx=2)
        ent_pwd = ttk.Entry(f_add, width=10)
        ent_pwd.grid(row=0, column=3, padx=2)

        tk.Label(f_add, text="角色:").grid(row=0, column=4, padx=2)
        cmb_role = ttk.Combobox(f_add, values=["超管 (superadmin)", "普通管理员 (admin)", "使用者 (user)"], width=15, state="readonly")
        cmb_role.current(2)
        cmb_role.grid(row=0, column=5, padx=2)

        cols = ("账号", "密码", "角色权限", "备注")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=8)
        for c in cols: tree.heading(c, text=c)
        tree.column("账号", width=100)
        tree.column("密码", width=100)
        tree.column("角色权限", width=150)
        tree.column("备注", width=150)
        tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        def refresh():
            for item in tree.get_children(): tree.delete(item)
            for uname, info in config_manager.USERS_DB.items():
                tree.insert("", tk.END, iid=uname, values=(uname, info.get('password'), info.get('role'), info.get('desc')))

        def add_user():
            u = ent_usr.get().strip()
            p = ent_pwd.get().strip()
            r_str = cmb_role.get()
            if not u: return messagebox.showwarning("错误", "账号必填", parent=win)

            if p and p != '123456' and not is_complex_password(p):
                return messagebox.showwarning("错误", "密码太弱！必须包含字母和数字，且至少6位。\n(留空将默认设为 123456，下次登录强制改密)", parent=win)
            if not p: p = "123456"

            r_val = "user"
            if "superadmin" in r_str: r_val = "superadmin"
            elif "admin" in r_str: r_val = "admin"
            config_manager.USERS_DB[u] = {"password": p, "role": r_val, "desc": "本地创建"}
            config_manager.save_users()
            refresh()
            ent_usr.delete(0, tk.END); ent_pwd.delete(0, tk.END)
            logging.info(f"🛡️ [安全审计] 本地超管修改了用户账号: {u}")

        def reset_pwd():
            sel = tree.selection()
            if not sel: return messagebox.showwarning("提示", "请先在下方列表中选中要重置的账号", parent=win)
            u = sel[0]
            p = ent_pwd.get().strip() or "123456"
            if not is_complex_password(p):
                return messagebox.showwarning("错误", "密码太弱！必须包含字母和数字，且至少6位。\n(留空将默认设为 123456，下次登录强制改密)", parent=win)
            config_manager.USERS_DB[u]['password'] = p
            config_manager.save_users()
            refresh()
            ent_pwd.delete(0, tk.END)
            messagebox.showinfo("成功", f"账号 [{u}] 密码已强制重置为新密码！", parent=win)
            logging.warning(f"🛡️ [安全审计] 本地超管强制重置了账户 [{u}] 的密码")

        def del_user():
            sel = tree.selection()
            if not sel: return
            u = sel[0]
            if u == "admin": return messagebox.showerror("拒绝", "系统默认 admin 超管为底层账号，不可删除！", parent=win)
            del config_manager.USERS_DB[u]
            config_manager.save_users()
            refresh()
            logging.warning(f"🛡️ [安全审计] 本地超管删除了账户: {u}")

        btn_f = tk.Frame(win)
        btn_f.pack(fill=tk.X, padx=10, pady=5)
        ttk.Button(btn_f, text="➕ 保存/修改账号", command=add_user).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_f, text="🔑 强制重置密码", command=reset_pwd).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_f, text="❌ 删除选中", command=del_user).pack(side=tk.LEFT, padx=5)
        refresh()