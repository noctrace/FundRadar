# Fund-Radar 基金筛选看板

基于 **GitHub Actions + GitHub Pages** 的纯静态基金筛选看板。  
每天自动抓取开放式基金数据，写入 JSON；浏览器直接打开 `index.html` 完成排序与 ECharts 可视化。

在线示例（若已开启 Pages）：`https://noctrace.github.io/FundRader/`

---

## 功能

- 每日自动更新（GitHub Actions，北京时间约 23:30）
- 纯静态托管（GitHub Pages，无需服务器）
- 高收益 / 亏损基金列表 + 每日涨跌（飙升 / 暴跌）
- 点击表格行查看前十大重仓股、细分类行业分布（半导体 / 电池 / 军工等）
- 本地 ECharts（`vendor/echarts.min.js`），不依赖外网图表 CDN
- 表头排序、无限滚动

---

## 架构

```text
GitHub Actions 每天执行
  → python generate_data.py
  → 更新 data/*.json 并 commit
  → GitHub Pages 托管静态文件

用户浏览器
  → 打开 index.html
  → fetch data/*.json
  → 本地 echarts 画图
```

数据不是盘中实时；持仓来自最新季报，通常滞后 1–2 个月。

---

## 仓库结构

```text
FundRadar/
├── index.html                 # 静态主页（Pages 入口）
├── generate_data.py           # Actions / 本地数据生成
├── fund_data.py               # AKShare 抓取 + 筛选 + 细行业聚合
├── requirements.txt
├── .nojekyll                  # Pages 允许点开头目录
├── .github/workflows/
│   └── update-data.yml        # 每日更新 data/*.json
├── data/                      # 静态 JSON（需提交进仓库）
│   ├── funds.json
│   ├── loss_funds.json
│   ├── surge_funds.json
│   ├── plunge_funds.json
│   ├── meta.json
│   └── stock_industry_cache.json
├── vendor/
│   └── echarts.min.js
└── icon/
    └── icon.jpg
```

---

## 本地预览

```bash
git clone https://github.com/noctrace/FundRader.git
cd FundRader

# 可选：重新生成最新数据（需能访问东方财富接口）
pip install -r requirements.txt
python generate_data.py

# 任意静态服务器
python -m http.server 8080
# 浏览器打开 http://localhost:8080
```

---

## 固定筛选阈值

| 参数 | 阈值 |
|------|------|
| 近1年 | ≥ 100% |
| 近6月 | ≥ 60% |
| 近3月 | ≥ 40% |
| 近1月 | ≥ 25% |

修改阈值请编辑 `generate_data.py` 顶部常量。

亏损基金默认：近1月 ≤ -15%。  
当日飙升 / 暴跌默认：日涨跌幅 > 6% / < -6%。

---

## 部署到 GitHub Pages

1. 将本仓库推送到 GitHub（建议仓库名与 Pages URL 一致，如 `FundRader`）
2. 仓库 **Settings → Pages**
   - Source: **Deploy from a branch**
   - Branch: `main`（或 `master`）/ `/ (root)`
3. **Settings → Actions → General → Workflow permissions**
   - 选择 **Read and write permissions**
   - 保存
4. **Actions → Daily Fund Data Update → Run workflow** 手动跑一次，确认 `data/*.json` 能更新
5. 打开：`https://<用户名>.github.io/<仓库名>/`

> 若仓库是项目站（非 `username.github.io`），URL 形如  
> `https://noctrace.github.io/FundRader/`  
> 当前前端资源使用相对路径，适配该子路径。

---

## 数据来源

| 接口 / 逻辑 | 说明 |
|-------------|------|
| `fund_open_fund_rank_em` | 开放式基金排行 |
| `fund_portfolio_hold_em` | 前十大重仓股 |
| 重仓股细行业聚合 | 东财个股细分类 + 缓存 `stock_industry_cache.json` |

由 GitHub Actions 每天自动抓取；失败时可在 Actions 页手动重跑。

---

## 许可证

GNU AGPL v3.0，见 `LICENSE`。
