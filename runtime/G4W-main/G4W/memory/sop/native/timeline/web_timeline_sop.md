# Web Timeline SOP — 时间轴网页操作

## 站点位置
- 站点文件：`runtime/G4W-data/timeline/site/`
- 数据文件：`runtime/G4W-data/timeline/dashboard-data.json`

## 启动方式
必须通过 HTTP 服务器启动，否则 JS 无法加载本地数据文件（`file://` 协议下页面空白）：
```bash
cd runtime/G4W-data/timeline/site
python -m http.server <port>
```

## 注意事项
1. **不要直接双击 `index.html`** — `file://` 协议下 JS 无法读取本地数据文件，页面空白无数据
2. **数据写入后页面即时反映** — 数据存于 `dashboard-data.json`，前端读取该文件渲染，无需重启 HTTP 服务
3. **CSS 颜色已修复** — 时间块基础状态已补 `background-color: var(--event-color)`，非悬停时也能显示正确分类颜色

## 手机查看：自包含HTML生成

### 背景
HTTP 服务只能在电脑本地访问（`localhost`），手机无法打开。若要在手机上查看时间轴，需生成一个**数据内嵌的自包含HTML文件**，手机浏览器直接打开即可。

### 实现思路
站点前端（`site/`）是编译后的 React bundle（`main.js` ~4MB），数据存储在 `dashboard-data.json`。通过 Python 脚本将数据直接注入到 `index.html` 中，使 JS 加载时无需 fetch 外部数据文件。

### 操作步骤

1. **定位源文件**
   - 站点目录：`runtime/G4W-data/timeline/site/`
   - 数据文件：`runtime/G4W-data/timeline/dashboard-data.json`
   - 入口文件：`site/index.html`

2. **用 Python 合成单文件 HTML**
   - 读取 `index.html` 和 `dashboard-data.json`
   - 在 `index.html` 的 `<head>` 中注入内嵌数据脚本，拦截 fetch 请求返回本地数据
   - 输出为独立的 HTML 文件（如 `timeline-standalone.html`），保存在工作区

3. **发送给用户**
   - 通过 `file.send` 能力发送（见 `file_send_sop.md`）：
     ```python
     from G4W.features.native_sop import print_result
     print_result("file-send", "send", {"path": r"生成的HTML文件绝对路径"})
     ```

### 注意事项
- 生成的文件较大（~9MB，含全部 JS bundle + 数据），发送和手机加载均需耐心等待
- 数据是生成时刻的快照，不实时更新；需要最新数据时重新生成
- 若只需查看特定时间段，可考虑裁剪 `dashboard-data.json` 减少文件体积（按 `date` 字段过滤）
