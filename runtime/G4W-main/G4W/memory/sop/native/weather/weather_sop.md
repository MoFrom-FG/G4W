# 天气查询 SOP

> 固定脚本：查中国天气网的城市天气预报
> 适用范围：7天天气预报、逐小时预报、城市代码查询
> 不适用：国际天气、长期气候统计

## 城市代码获取

- 访问 `http://www.weather.com.cn/weather1d/` 搜索城市名
- 从URL中提取数字ID
- 西安代码：`101110101`

## 7天天气预报

### 固定流程

1. 构造URL：`http://www.weather.com.cn/weather/{城市代码}.shtml`
2. 用 Python `requests` 直接抓取 HTML（**不要用浏览器**）
3. 用 `BeautifulSoup` 解析
4. 天气数据在 `class="t"` 的 `<table>` 中
5. 每行含：日期、天气现象、温度（最高/最低）、风力

### 代码模板

```python
import requests
from bs4 import BeautifulSoup

city_code = "101110101"
url = f"http://www.weather.com.cn/weather/{city_code}.shtml"
resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
soup = BeautifulSoup(resp.text, "html.parser")
rows = soup.find("table", class_="t").find_all("tr")[1:]
for row in rows:
    cols = row.find_all("td")
    print(f"{cols[0].text.strip()} | {cols[1].text.strip()} | {cols[2].text.strip()} | {cols[3].text.strip()}")
```

## 已知城市代码

| 城市 | 代码 |
|:---|:---:|
| 西安 | 101110101 |
| 北京 | 101010100 |
| 上海 | 101020100 |
| 广州 | 101280101 |
