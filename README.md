# Gaming4Free 自动续期

GitHub Actions + SeleniumBase UC mode + Cloudflare Turnstile 验证。

面板地址：<https://control.gaming4free.net/>

## 文件结构

```
gaming4free-renew/
├── renew.py          # 主脚本 (单文件, 无外部模块依赖)
├── requirements.txt  # Python 依赖
└── README.md         # 说明文档
```

## 部署步骤

### 1. 配置 Secrets

| Secret 名 | 必填 | 说明 |
|---|---|---|
| `GAME4FREE_RENEW_URL` | 单账号 | 续期页面 URL |
| `GAME4FREE_COOKIE` | 单账号 | Cookie 字符串 |
| `GAME4FREE_ACCOUNTS` | 多账号 | 每行 `名称\|\|\|URL\|\|\|Cookie` |
| `TG_BOT_TOKEN` | 否 | Telegram Bot Token |
| `TG_CHAT_ID` | 否 | Telegram Chat ID |

### 2. 获取 Cookie

1. 浏览器登录 <https://control.gaming4free.net>
2. F12 -> Application -> Cookies -> 复制全部 Cookie

### 3. 运行

```bash
xvfb-run --auto-servernum --server-args="-screen 0 1920x1080x24" python renew.py
```

## 工作原理

1. 注入 Cookie 后重新加载页面建立会话，并校验登录态
2. SeleniumBase UC mode 启动 Chrome
3. 点击 console 页 "+ 90 min" 免费续期按钮（不碰 "+24h" 付费按钮）
4. 若出现 Turnstile 弹窗则等待自动通过后提交
5. 刷新页面比对剩余时间验证，失败自动截图 + dump HTML

## License

MIT

