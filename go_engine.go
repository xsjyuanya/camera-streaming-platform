package main

import (
    "encoding/json"
    "fmt"
    "log"
    "net/http"
    "os"
    "os/exec"
    "path/filepath"
    "sync"
    "time"
)

// 摄像头数据结构
type Camera struct {
    ID  string `json:"id"`
    URL string `json:"url"`
}

type StartAllRequest struct {
    Cameras []Camera `json:"cameras"`
}

var (
    processes  = make(map[string]*exec.Cmd)
    activeCams = make(map[string]bool) // 真正处于健康拉流状态的探针标记
    routines   = make(map[string]int)  // 记录每个摄像头的独立运行批次ID，防止僵尸并发踩踏
    mu         sync.Mutex
    isRunning  = false
)

// 工具函数：获取目录下最新修改的 mp4 文件大小
func getLatestFileSize(dir string) (string, int64) {
    files, err := os.ReadDir(dir)
    if err != nil {
        return "", 0
    }
    var latestFile string
    var latestTime time.Time
    var latestSize int64

    for _, file := range files {
        if !file.IsDir() && filepath.Ext(file.Name()) == ".mp4" {
            info, err := file.Info()
            if err == nil {
                if info.ModTime().After(latestTime) {
                    latestTime = info.ModTime()
                    latestFile = file.Name()
                    latestSize = info.Size()
                }
            }
        }
    }
    return latestFile, latestSize
}

// 核心拉流协程
func startStream(cam Camera, runID int) {
    mu.Lock()
    // 如果当前摄像头的批次号已经被更新（说明被重新启动了），则立刻废弃旧协程
    if routines[cam.ID] != runID {
        mu.Unlock()
        return
    }
    processes[cam.ID] = nil
    mu.Unlock()

    for {
        mu.Lock()
        valid := isRunning && routines[cam.ID] == runID
        mu.Unlock()
        if !valid {
            break
        }

        dir := filepath.Join(".", "video_storage", cam.ID)
        os.MkdirAll(dir, os.ModePerm)
        filePattern := filepath.Join(dir, cam.ID+"_%Y%m%d_%H%M%S.mp4")

        // 采用最纯净的拉流指令，舍弃容易引发并发拥堵的 -stimeout
        cmd := exec.Command("ffmpeg", "-y", "-rtsp_transport", "tcp", "-i", cam.URL,
            "-map", "0:v", "-map", "0:a?", "-c:v", "copy", "-c:a", "aac", "-f", "segment",
            "-segment_time", "600", "-segment_format", "mp4",
            "-reset_timestamps", "1", "-strftime", "1", filePattern)

        cmd.Stdout = os.Stdout
        cmd.Stderr = os.Stderr

        mu.Lock()
        processes[cam.ID] = cmd
        activeCams[cam.ID] = true // 标记为：真正开始健康拉流
        mu.Unlock()

        // 🐶【新增：文件体积看门狗探针】独立于网络层监控磁盘写入
        go func(rID int) {
            // 刚启动时给 FFmpeg 留出 30 秒建文件、出画面的缓冲期
            time.Sleep(30 * time.Second)
            var lastSize int64 = -1
            var lastFile string = ""

            for {
                time.Sleep(20 * time.Second) // 之后每隔 20 秒巡视一次

                mu.Lock()
                validGuard := isRunning && routines[cam.ID] == rID
                cmdRef := processes[cam.ID]
                mu.Unlock()

                // 如果任务被换代了，或者进程还没起来，退出看门狗
                if !validGuard || cmdRef == nil || cmdRef.Process == nil {
                    break
                }

                targetDir := filepath.Join(".", "video_storage", cam.ID)
                latestFile, currentSize := getLatestFileSize(targetDir)

                if latestFile != "" {
                    // 核心判定：如果文件名字没变，而且大小一点都没增加，判定为物理断网假死！
                    if latestFile == lastFile && currentSize == lastSize {
                        log.Printf("🐶 [看门狗触发] 摄像头 [%s] 录像画面假死 (20秒无数据写入)，执行强制处决！", cam.ID)
                        cmdRef.Process.Kill()
                        break // 杀掉后，原本阻塞的 cmd.Run() 会直接报错跳出，走入下面的 8秒报警流程
                    }
                    lastFile = latestFile
                    lastSize = currentSize
                }
            }
        }(runID)

        // 阻塞运行 FFmpeg (如果网络断了，看门狗会把它 kill 掉从而结束阻塞)
        err := cmd.Run()

        // 进程已退出，立刻剥离健康标记，触发 Python 端的断线告警
        mu.Lock()
        activeCams[cam.ID] = false
        mu.Unlock()

        mu.Lock()
        valid = isRunning && routines[cam.ID] == runID
        mu.Unlock()
        if !valid {
            break
        }

        if err != nil {
            log.Printf("⚠️ 摄像头 [%s] 拉流意外中断 (原因: %v)，进入 8 秒冷却...", cam.ID, err)
        } else {
            log.Printf("⚠️ 摄像头 [%s] 拉流结束，进入 8 秒冷却...", cam.ID)
        }

        // 强制冷却 8 秒，保障网页探针必定能记录异常
        for i := 0; i < 8; i++ {
            mu.Lock()
            valid = isRunning && routines[cam.ID] == runID
            mu.Unlock()
            if !valid {
                break
            }
            time.Sleep(1 * time.Second)
        }

        mu.Lock()
        valid = isRunning && routines[cam.ID] == runID
        mu.Unlock()
        if valid {
            log.Printf("🔄 摄像头 [%s] 结束冷却，准备尝试重连...", cam.ID)
        }
    }
}

func handleStartAll(w http.ResponseWriter, r *http.Request) {
    var req StartAllRequest
    if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
        http.Error(w, err.Error(), http.StatusBadRequest)
        return
    }

    mu.Lock()
    isRunning = true
    mu.Unlock()

    for _, cam := range req.Cameras {
        mu.Lock()
        routines[cam.ID]++
        currentRunID := routines[cam.ID]
        mu.Unlock()

        go startStream(cam, currentRunID) // Go 的轻量级协程
    }
    fmt.Fprintf(w, "{\"status\":\"ok\"}")
    log.Printf("✅ 收到集中启动指令，共下发 %d 路并发拉流", len(req.Cameras))
}

func handleStartSingle(w http.ResponseWriter, r *http.Request) {
    var cam Camera
    if err := json.NewDecoder(r.Body).Decode(&cam); err != nil {
        http.Error(w, err.Error(), http.StatusBadRequest)
        return
    }

    mu.Lock()
    isRunning = true
    routines[cam.ID]++
    currentRunID := routines[cam.ID]
    mu.Unlock()

    go startStream(cam, currentRunID)
    fmt.Fprintf(w, "{\"status\":\"ok\"}")
}

func handleStopAll(w http.ResponseWriter, r *http.Request) {
    mu.Lock()
    isRunning = false
    for id, cmd := range processes {
        routines[id]++ // 废弃所有仍在沉睡或运行的旧协程
        if cmd != nil && cmd.Process != nil {
            cmd.Process.Kill()
        }
        delete(processes, id)
        delete(activeCams, id)
    }
    mu.Unlock()
    fmt.Fprintf(w, "{\"status\":\"ok\"}")
    log.Printf("🛑 收到紧急停止指令，所有拉流已终止")
}

// 供 Python 轮询状态的接口
func handleStatus(w http.ResponseWriter, r *http.Request) {
    mu.Lock()
    defer mu.Unlock()

    running := make([]string, 0)

    // 只返回真正在健康拉流的摄像头，一旦掉线就不在列表内
    for id, active := range activeCams {
        if active {
            running = append(running, id)
        }
    }

    response := map[string]interface{}{
        "running": running,
    }
    json.NewEncoder(w).Encode(response)
}

func main() {
    http.HandleFunc("/start_all", handleStartAll)
    http.HandleFunc("/start_single", handleStartSingle)
    http.HandleFunc("/stop_all", handleStopAll)
    http.HandleFunc("/status", handleStatus)

    log.Println("🚀 Golang 底层引擎启动 (引入独立磁盘级[体积看门狗]，通杀一切物理断网)")
    log.Fatal(http.ListenAndServe(":8081", nil))
}