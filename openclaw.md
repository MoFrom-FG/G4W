# OpenClaw G4W 開發與環境建置指南

這份文件記錄了在 WSL/Linux 中使用 **Conda** 環境建置 G4W 專案的流程，以及本專案中所有關鍵組件的路徑位置。方便未來的開發者（或失憶後的你）快速上手並除錯。

---

## 1. 核心路徑對照表 (The "Where is everything?" Guide)

G4W 的架構分為「底座 (GA)」與「微信主程式 (G4W)」兩塊。

### 📁 執行與設定相關
- **專案根目錄**：`~/Aggie/Aggie`
- **主要啟動入口 (Linux/WSL)**：`tools/linux/G4W` (自動判定啟動嚮導或看板)
- **Linux 運維腳本目錄**：`tools/linux/` (包含 `start.sh`, `stop.sh`, `dashboard.sh`, `status.sh`)
- **環境變數載入腳本**：`tools/linux/env.sh` (決定了用哪個 Python 環境啟動)
- **更新來源 (GitHub Repo) 覆寫**：`runtime/G4W-update-repo.txt` (寫入 `louis71803/Aggie` 即可切換更新源)

### 📁 後端架構 (Python)
- **GA 底座 (GenericAgent)**：`runtime/app/`
- **G4W 主程式**：`runtime/G4W-main/`
- **看板後端 (Dashboard Server)**：`runtime/G4W-main/G4W/dashboard/server.py`
  - 處理所有 `/api/*` 的請求，並決定啟動哪些 `builtin_services` (如主服務、Embedding 服務)。
- **微信與控制器 (Controller/Handlers)**：
  - Controller (總管邏輯)：`runtime/G4W-main/G4W/agents/controller.py`
  - Handlers (處理各種工具/功能)：`runtime/G4W-main/G4W/agents/handlers.py`
  - 微信通訊 (長輪詢)：`runtime/G4W-main/G4W/wechat/weixin.py`

### 📁 前端架構 (Web)
- **看板前端**：`runtime/G4W-main/G4W/dashboard/static/`
  - 主要邏輯：`app.js`
  - 首次使用嚮導：`wizard.html`
  - 儀表板主畫面：`index.html`

### 📁 向量檢索 (Embedding)
- **獨立虛擬環境與安裝腳本**：`runtime/G4W-main/G4W/memory/vector/install_embedding.py`
- **實際運作目錄**：`runtime/G4W-embedding/` (包含 `.venv`, `server.py`, `models/`)

### 📁 資料儲存與隱私檔案 (不會進 Git)
- **API Key 設定**：`runtime/app/mykey.py` (支援多模型)
- **環境設定 (.env)**：`runtime/G4W-main/.env`
- **所有對話、記憶與狀態**：`runtime/G4W-data/` (包含 `accounts/`, `memory/`, `dashboard-services.json` 等)

---

## 2. 系統環境準備 (在乾淨的 Ubuntu / WSL2 上)

G4W 需要一些底層 C/C++ 函式庫，以及 Python 的基礎環境。

```bash
# 1. 更新套件清單
sudo apt update && sudo apt upgrade -y

# 2. 安裝核心編譯工具
sudo apt install -y build-essential curl wget git file pkg-config libssl-dev

# 3. 安裝 Linux 下 Tauri/GUI 元件所需的底層依賴 (若需開發桌面版)
sudo apt install -y libwebkit2gtk-4.1-dev libgtk-3-dev libayatana-appindicator3-dev librsvg2-dev

# 4. （可選）安裝 Miniconda (若您還沒有安裝)
mkdir -p ~/miniconda3
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O ~/miniconda3/miniconda.sh
bash ~/miniconda3/miniconda.sh -b -u -p ~/miniconda3
rm -rf ~/miniconda3/miniconda.sh
~/miniconda3/bin/conda init bash
source ~/.bashrc
```

---

## 3. 建立並設定 Conda 虛擬環境 (aggie_env)

這個專案我們約定**絕對不污染 `base` 環境**，所有的依賴都安裝在名為 `aggie_env` 的 conda 環境中。

```bash
# 1. 建立名為 aggie_env 的 Python 3.12 環境 (G4W 要求 >=3.10)
conda create -n aggie_env python=3.12 -y

# 2. 啟動環境
conda activate aggie_env

# 3. 安裝系統級別的依賴 (以防萬一，通常 conda 會處理)
# sudo apt install -y python3.12-venv
```

---

## 4. 專案依賴安裝與初始化

進入您的專案目錄，開始安裝所有必需的 Python 套件。

```bash
# 假設專案在 ~/Aggie/Aggie
cd ~/Aggie/Aggie

# 確保已啟用環境
conda activate aggie_env

# 1. 安裝額外的必備基礎套件 (Pillow 負責處理圖片/視覺)
pip install psutil pillow requests beautifulsoup4 bottle simple-websocket-server aiohttp

# 2. 以 editable 模式安裝 GA 底座
pip install -e runtime/app

# (若需開發前端，在此目錄執行 npm install)
# cd runtime/app/frontends/desktop && npm install
```

---

## 5. 啟動與除錯指南

### 啟動順序
每次要開發或啟動專案時，**請務必先進入 `aggie_env` 環境**，否則腳本會降級使用系統 Python，導致不斷跳回「首次使用嚮導」。

```bash
# 1. 切換目錄並啟動環境
cd ~/Aggie/Aggie
conda activate aggie_env

# 2. 關閉所有舊服務 (保證端口不衝突)
bash tools/linux/stop.sh

# 3. 背景啟動看板 (Dashboard, Port 18180)
bash tools/linux/dashboard.sh --bg

# 4. 背景啟動主服務 (Conductor & Worker)
bash tools/linux/start.sh --bg

# 5. 查看運行狀態
bash tools/linux/status.sh
```
啟動後，使用 Windows 的瀏覽器訪問 `http://127.0.0.1:18180` 即可進入控制中心。

### 常見除錯 (Troubleshooting)

1. **一直跳回「首次使用嚮導」**：
   - 原因：腳本找不到完整的 Python 環境。
   - 解法：確保執行啟動腳本前，有跑 `conda activate aggie_env`。
2. **「模型輸出監視器」顯示已停止 / 進程特徵未匹配**：
   - 原因：環境中缺少 `psutil`，導致無法掃描進程 PID。
   - 解法：在 `aggie_env` 執行 `pip install psutil`。
3. **主服務日誌一直刷 `HTTP Error 524 / 554`**：
   - 解法：已經修復！在 `runtime/G4W-main/G4W/wechat/weixin.py` 裡，將這些狀態碼加入了長輪詢超時白名單。
4. **AI 視覺/讀圖功能卡死或四處尋找檔案**：
   - 解法：已經實作了 `runtime/app/memory/vision_api.py`，支援多模型 Fallback (429/503 自動重試切換)。必須確保 `aggie_env` 有安裝 `pillow` (PIL)，否則 Base64 轉換會報錯。
5. **找不到 Embedding 任務 / 顯示 `No module named torch`**：
   - 解法：Embedding 是**完全獨立的虛擬環境** (`runtime/G4W-embedding/.venv`)。若損壞，請在控制中心的「環境配置」頁面重新點擊「安裝 / 重裝」，系統會自動重新建置並下載 PyTorch 與模型，**切勿手動安裝 torch 到 `aggie_env` 中**以防空間暴增。
