# MD转长图 SOP

将Markdown文件（含图片）渲染为全页长图（PNG），保留完整格式和所有图片，通过微信发送。

## 适用场景
- 用户要求将归档的md文档（小红书/知乎等）转成长图
- md中包含`![图片](相对路径)`引用，图片在`image/`子目录中

## 关键前置

### 依赖检查
```python
# 检查Edge是否可用（必须在有GUI或headless模式下启动）
which msedge  # 或检查 Program Files (x86)/Microsoft/Edge/Application/
# 检查Python库：markdown（标准库可用）
```

### 🔴 避坑红线（致命）
1. **必须用浏览器CDP全页截图**：fpdf2纯文本无图；weasyprint Windows缺libgobject；pandoc缺pdflatex
2. **启动Edge时必须加 `--remote-allow-origins=*`**：否则CDP WebSocket连接被拒绝
3. **截图加 `fullPage: true`**：否则只截视口区域，长图被截断
4. **长图文件直接落盘到md所在目录**：禁止写到temp目录（会被清理）
5. **file_send用`print_result("file-send", "send", {"path":绝对路径})`**：用固定入口入队，禁止直接改Outbox

### 前置检查清单
- [ ] md文件所在目录是否存在
- [ ] md引用的图片文件是否在 `image/<笔记名>/` 下存在
- [ ] Edge浏览器是否安装
- [ ] 目标输出目录是否可写

## 流程步骤

### Step 1: 读取md文件内容
```python
import os
md_path = r"收藏/小红书/太爱武康路啦.md"
with open(md_path, "r", encoding="utf-8") as f:
    content = f.read()
```

### Step 2: 将md转为HTML（内嵌图片base64）
图片引用格式：`![描述](image/<笔记名>/img_01.webp)`
需要将每个图片文件读取后转为base64 data URI，替换md中的相对路径引用。

```python
import base64
import re

md_dir = os.path.dirname(md_path)
image_dir = os.path.join(md_dir, "image", note_name)

def embed_images(md_content, image_dir):
    def _replace(match):
        alt = match.group(1)
        rel_path = match.group(2)
        img_path = os.path.join(image_dir, rel_path)
        if os.path.exists(img_path):
            with open(img_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            ext = os.path.splitext(img_path)[1].lower()
            mime = {"webp": "image/webp", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}
            return f'<img src="data:{mime.get(ext.lstrip("."), "image/png")};base64,{b64}" alt="{alt}">'
        return match.group(0)
    return re.sub(r'!\[(.*?)\]\((.*?)\)', _replace, md_content)
```

### Step 3: 启动Edge headless带CDP
```python
import subprocess, time
edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
cdp_port = 9222
proc = subprocess.Popen([
    edge_path,
    f"--remote-debugging-port={cdp_port}",
    "--headless=new",
    "--no-first-run",
    "--no-default-browser-check",
    "--user-data-dir=C:/temp/edge-headless-profile"
])
time.sleep(3)  # 等待启动
```

### Step 4: 通过CDP截全页长图
```python
import requests, json, base64 as b64mod

ws_resp = requests.get(f"http://localhost:{cdp_port}/json")
ws_url = ws_resp.json()[0]["webSocketDebuggerUrl"]

# 使用Python websocket-client库
import websocket
ws = websocket.create_connection(ws_url)

# 导航到本地HTML文件
html_abs_path = os.path.abspath(html_path)
file_url = "file://" + html_abs_path.replace("\\", "/")

def send_cmd(ws, method, params=None):
    msg = json.dumps({"id": 1, "method": method, "params": params or {}})
    ws.send(msg)
    return json.loads(ws.recv())

# 1. 导航
send_cmd(ws, "Page.navigate", {"url": file_url})
time.sleep(2)

# 2. 截全页长图（fullPage: true 是关键！）
result = send_cmd(ws, "Page.captureScreenshot", {
    "format": "png",
    "fullPage": True
})
png_data = b64mod.b64decode(result["result"]["data"])

# 保存到md同目录
output_path = os.path.join(md_dir, f"{note_name}_长图.png")
with open(output_path, "wb") as f:
    f.write(png_data)

ws.close()
proc.terminate()
```

### Step 5: 通过file_send发送
```python
from G4W.features.native_sop import print_result
print_result("file-send", "send", {"path": output_path})
```

## 典型踩坑记录
| 问题 | 原因 | 解决 |
|------|------|------|
| WebSocket连接被拒绝 | 启动Edge没加`--remote-allow-origins=*` | 加参数 |
| 长图只截了一部分 | 没设`fullPage: true` | 加参数 |
| file_send入队了但没收到 | context_path传错，或文件写到了temp目录 | 文件落盘到收藏目录，context_path用正确值 |
| 图片显示不出来 | 图片用相对路径引用而没有base64嵌入 | 转HTML时嵌入base64 |
