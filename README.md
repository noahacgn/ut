# Uniqlo UT L 码降价监控

每天自动抓取优衣库中国（uniqlo.cn）的 UT 男装印花 T 恤，当 L 码出现新的降价商品时，通过 QQ 邮箱发送 HTML 图文提醒。

## 功能特性

- 纯 Python 3.13 标准库实现，零运行时依赖。
- 命中条件：现价 ≤ ¥59、原价 > 现价（必须真实打折）、L 码有货。
- HTML 邮件包含商品大图（内联 `cid:`）、现价 / 原价、详情链接。
- 已提醒商品持久化到 `state/uniqlo_ut_l_alerted.json`，同一商品不会重复推送。

## 工作原理

脚本调用 `https://d.uniqlo.cn/p/hmall-sc-service/search/...` 查询类目 `utyinhua_m`，按价格升序分页抓取；核心常量集中在 `scripts/uniqlo_price_watch.py:20-32`，筛选规则见 `is_target_product`（`scripts/uniqlo_price_watch.py:239-249`）。主流程在发送邮件成功后才写状态文件，SMTP 失败时下次会重试。

## 快速开始

三个必填环境变量（详见 `scripts/uniqlo_price_watch.py:30-32`）：

| 变量 | 含义 |
|---|---|
| `UNIQLO_MAIL_FROM` | 发件 QQ 邮箱，同时作为 SMTP 登录名 |
| `UNIQLO_MAIL_TO` | 收件邮箱 |
| `UNIQLO_QQ_SMTP_AUTH_CODE` | QQ 邮箱 SMTP **授权码**，不是登录密码 |

本地运行：

```bash
export UNIQLO_MAIL_FROM="your_qq@qq.com"
export UNIQLO_MAIL_TO="receiver@example.com"
export UNIQLO_QQ_SMTP_AUTH_CODE="<QQ 邮箱 SMTP 授权码>"

python scripts/uniqlo_price_watch.py
```

授权码在 QQ 邮箱「设置 → 账号 → POP3/IMAP/SMTP 服务」开启后生成。

## GitHub Actions 自动化

工作流文件：`.github/workflows/uniqlo-price-watch.yml`。

- **调度**：`cron: '5 9 * * *'` with `timezone: 'Asia/Shanghai'`，即每天北京时间 **09:05** 执行；Actions 页面也可手动触发。
- **仓库 Settings 中需配置**：
  - **Variables**（非敏感）：`UNIQLO_MAIL_FROM`、`UNIQLO_MAIL_TO`
  - **Secrets**（敏感）：`UNIQLO_QQ_SMTP_AUTH_CODE`
- **权限**：工作流声明了 `contents: write`，运行后若 `state/uniqlo_ut_l_alerted.json` 有变更，会以 `github-actions[bot]` 身份自动 commit 回主分支，保证下次执行能正确去重。

## 开发

```bash
python -m unittest discover tests    # 运行测试
ruff check .                         # 代码静态检查
mypy scripts                         # 类型检查（strict）
```

如需修改筛选条件（价格阈值、尺码、商品类目等），直接编辑 `scripts/uniqlo_price_watch.py` 开头的常量即可。
