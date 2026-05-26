import tkinter as tk
import threading
import logging
import os

from core_engine import CoreEngine
from ui_components import StreamManagerUI
from web_server import ThreadingTCPServer, VideoPortalHandler

def main():
    # 1. 实例化 Tkinter 主窗口
    root = tk.Tk()
    
    # 2. 初始化核心业务引擎，将 root 传入便于子线程能够安全回调更新 UI (root.after)
    engine = CoreEngine(root)
    
    # 3. 依赖注入：将引擎绑定到 Web Server 的类属性中，使所有网络请求都能访问到底层状态
    VideoPortalHandler.engine = engine
    
    # 4. 初始化 UI，并将引擎与 UI 互相绑定
    ui = StreamManagerUI(root, engine)
    
    # 5. 启动看门狗、清理等后台核心线程池
    engine.start_threads()
    
    # 6. 后台启动 Web Server
    def start_web():
        port = 8080
        try:
            httpd = ThreadingTCPServer(("", port), VideoPortalHandler)
            engine.httpd = httpd  # 给引擎留一个引用以便退出时安全关闭
            logging.info(f"🚀 Web 引擎已全量接管并启动在端口 {port}。")
            httpd.serve_forever()
        except Exception as e:
            logging.error(f"Web Server 启动失败 (端口可能被占用): {e}")
            
    # 延迟2秒启动 Web Server，保持与原项目完全一致的启动体验
    root.after(2000, lambda: threading.Thread(target=start_web, daemon=True).start())

    # 7. 全局安全退出拦截逻辑
    def on_closing():
        engine.app_running = False
        
        # 安全销毁所有正在直播推流的进程
        for cid in list(engine.live_processes.keys()):
            engine.stop_live_stream(cid)
            
        # 安全关闭 Web 服务器
        if engine.httpd:
            threading.Thread(target=engine.httpd.shutdown).start()
            
        # 安全停止所有拉流作业
        if engine.is_running:
            try:
                engine.stop_all()
            except Exception:
                pass
                
        root.destroy()
        
    root.protocol("WM_DELETE_WINDOW", on_closing)
    
    # 进入系统事件主循环
    root.mainloop()

if __name__ == "__main__":
    main()