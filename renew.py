#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gaming4free 自动续期脚本（GHA + sing-box proxy + seleniumbase UC mode）
================================================================
- 使用 seleniumbase UC mode 反检测
- 走 sing-box SOCKS5 代理出口（CF 自家 IP，几乎必过 Turnstile）
- 自动识别续期按钮，循环点击至 48h 上限
- 点击前后剩余时间对比，确保真成功
- 失败自动截图 + Telegram 通知
"""
import os
import re
import sys
import time
import json
import random
import socket
import logging
import requests
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# 配置区
# ---------------------------------------------------------------------------
SITE_URL       = os.getenv("GAME4FREE_RENEW_URL", "https://control.gaming4free.net/")
LOGIN_URL      = os.getenv("GAME4FREE_LOGIN_URL", "")
USERNAME       = os.getenv("GAME4FREE_USERNAME", "")
PASSWORD       = os.getenv("GAME4FREE_PASSWORD", "")
COOKIE_STR     = os.getenv("GAME4FREE_COOKIE", "")

_raw_proxy = os.getenv("PROXY_URL", "").strip()
_is_proxy = os.getenv("IS_PROXY", "").strip().lower() in ("true", "1", "yes")
if _is_proxy:
    PROXY_URL = "socks5://127.0.0.1:1080"
elif _raw_proxy and ("http" in _raw_proxy or "socks" in _raw_proxy):
    PROXY_URL = _raw_proxy
else:
    PROXY_URL = "socks5://127.0.0.1:1080"

MAX_HOURS      = 48            # 续期上限 48 小时
ADD_MINUTES    = 90            # 每次续期 +90 分钟（console 页 "+ 90 min" 按钮）
COOLDOWN_SEC   = 240           # 冷却 4 分钟
MAX_CLICKS     = 32            # 单次运行最大续期次数（48h 上限需 32 次 +90min；每轮约 4.5min，适配 GHA 180min 超时）
PAGE_TIMEOUT   = 60            # 单页操作超时
TURNSTILE_WAIT = 90            # Turnstile 等待上限

TG_TOKEN       = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID     = os.getenv("TG_CHAT_ID", "")

SHOT_DIR       = Path("debug_output")
SHOT_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("renew.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("renew")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def tg(msg: str):
    """Telegram 通知（失败不影响主流程）"""
    if not (TG_TOKEN and TG_CHAT_ID):
        log.warning("TG 未配置，跳过通知")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg},
            timeout=10,
        )
        r.raise_for_status()
        log.info("✅ TG 通知发送成功")
    except Exception as e:
        log.warning(f"TG 通知失败: {e}")


def parse_remaining_seconds(text: str) -> int:
    """从页面文本中解析剩余时间，返回秒数（-1 表示无法识别）"""
    if not text:
        return -1
    t = text.lower().strip()
    total = 0

    # 优先匹配 HH:MM:SS
    m = re.search(r"(\d{1,2}):(\d{2}):(\d{2})", t)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    # 匹配 MM:SS（排除 HH:MM:SS）
    m = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", t)
    if m:
        val = int(m.group(1)) * 60 + int(m.group(2))
        # 排除太小的值（如秒级倒计时）
        if val > 60:
            return val

    # 匹配 'Xh Xm' / 'Xd Xh' / 'X min' 等
    for unit, mult in [("d", 86400), ("day", 86400),
                        ("h", 3600),  ("hour", 3600),
                        ("m", 60),    ("min", 60), ("minute", 60),
                        ("s", 1),     ("sec", 1)]:
        m = re.search(rf"(\d+)\s*{unit}", t)
        if m:
            total += int(m.group(1)) * mult
    return total if total > 0 else -1


def human_sleep(a: float = 0.5, b: float = 1.5):
    """模拟人类反应时间"""
    time.sleep(random.uniform(a, b))


def screenshot(sb, name: str):
    """保存截图，返回路径"""
    p = SHOT_DIR / f"{datetime.now():%H%M%S}_{name}.png"
    try:
        sb.save_screenshot(str(p))
        log.info(f"截图: {p}")
    except Exception as e:
        log.warning(f"截图失败: {e}")
    return p


def fmt_hm(sec) -> str:
    """把秒数格式化为 'Xh Ym'，无法识别时返回 '未知'"""
    if sec is None or sec < 0:
        return "未知"
    return f"{sec // 3600}h {(sec % 3600) // 60}m"


def dump_html(sb, name: str):
    """保存当前页面 HTML，便于排查选择器失效/未登录等问题"""
    try:
        p = SHOT_DIR / f"{name}.html"
        with open(p, "w", encoding="utf-8") as f:
            f.write(sb.get_page_source())
        log.info(f"页面 HTML 已保存: {p}")
    except Exception as e:
        log.warning(f"保存页面 HTML 失败: {e}")


def page_state(sb) -> dict:
    """收集当前页面关键特征，用于判断是否已登录/按钮是否存在"""
    try:
        raw = sb.execute_script("""
        (function(){
            var out = {};
            out.url = location.href;
            out.title = document.title || '';
            var pwd = document.querySelector('input[type=password]');
            out.has_password = !!(pwd && pwd.getBoundingClientRect().width > 0);
            out.has_login_link = !!document.querySelector('a[href*="login" i]');
            out.btn_90min = false;
            var clickable = document.querySelectorAll('button, a, [role=button], [wire\\\\:click]');
            for (var i = 0; i < clickable.length; i++) {
                var t = (clickable[i].innerText || '').trim();
                if (/(?:\\+\\s*)?90\\s*min/i.test(t) && !/[$￥¥€]/.test(t) && !/24\\s*h/i.test(t)) {
                    out.btn_90min = true; break;
                }
            }
            out.vm_submit = !!document.querySelector('.vm-submit');
            out.wire_count = document.querySelectorAll('[wire\\\\:id]').length;
            out.turnstile = !!document.querySelector('.cf-turnstile');
            var body = (document.body && document.body.innerText) || '';
            out.body_head = body.replace(/\\s+/g, ' ').trim().substring(0, 300);
            return JSON.stringify(out);
        })();
        """)
        return json.loads(raw) if raw else {}
    except Exception as e:
        log.warning(f"page_state 获取失败: {e}")
        return {}


# ---------------------------------------------------------------------------
# 续期核心
# ---------------------------------------------------------------------------
def get_remaining_seconds(sb) -> int:
    """从页面提取剩余时间，返回秒数（-1 表示无法识别）"""
    try:
        selectors = [
            "#timeleft", ".timeleft", ".time-left",
            "#remaining", ".remaining", ".countdown",
            '[class*="time"]', '[id*="time"]',
            '[class*="remain"]', '[id*="remain"]',
        ]
        for sel in selectors:
            try:
                txt = sb.get_text(sel) if sb.is_element_visible(sel) else ""
                sec = parse_remaining_seconds(txt)
                if sec > 0:
                    log.info(f"剩余时间 [{sel}] = {txt} -> {sec}s ({sec//3600}h {(sec%3600)//60}m)")
                    return sec
            except Exception:
                continue

        # 兜底：整页文本提取（优先 HH:MM:SS 倒计时，如 "11:39:35 remaining"）
        try:
            body_text = sb.get_text("body") or ""
        except Exception:
            body_text = ""
        best = -1
        for mm in re.findall(r"\b(\d{1,2}:\d{2}:\d{2})\b", body_text):
            p = mm.split(":")
            val = int(p[0]) * 3600 + int(p[1]) * 60 + int(p[2])
            if best < val < MAX_HOURS * 3600 + 3600:
                best = val
        if best > 0:
            log.info(f"剩余时间 [body HH:MM:SS] -> {best}s ({fmt_hm(best)})")
            return best
        for line in body_text.split("\n"):
            sec = parse_remaining_seconds(line)
            if 60 < sec < MAX_HOURS * 3600 + 3600:
                log.info(f"剩余时间 [body line] = {line.strip()} -> {sec}s")
                return sec
        return -1
    except Exception as e:
        log.warning(f"提取剩余时间失败: {e}")
        return -1


def livewire_extend(sb) -> dict:
    """使用 Livewire JavaScript 直接调用续期方法，返回结果"""
    from util import _LW_CALL_BUTTON_JS, _LW_EXTEND_V3_JS, _LW_V2_JS, _LW_CLICK_JS

    results = []

    # 策略0: 按按钮真实 wire:click 方法名直调（原生 el.click() 可能未被 Livewire 事件管线接住，
    # GHA 日志表现为 __lwResponses 为空、剩余时间只降不升）
    try:
        result = sb.execute_script(_LW_CALL_BUTTON_JS)
        if result:
            log.info(f"按钮直调结果: {result}")
            results.append(str(result))
    except Exception as e:
        log.warning(f"按钮直调失败: {e}")

    # 策略1: Livewire v3 - $wire.call('extend')
    try:
        result = sb.execute_script(_LW_EXTEND_V3_JS)
        if result:
            log.info(f"Livewire v3 结果: {result}")
            results.append(result)
    except Exception as e:
        log.warning(f"Livewire v3 调用失败: {e}")
    
    # 策略2: Livewire v2 - livewire.emit('extend')
    try:
        result = sb.execute_script(_LW_V2_JS)
        if result:
            log.info(f"Livewire v2 结果: {result}")
            results.append(result)
    except Exception as e:
        log.warning(f"Livewire v2 调用失败: {e}")
    
    # 策略3: 直接点击 90 min 按钮
    try:
        result = sb.execute_script(_LW_CLICK_JS)
        if result:
            log.info(f"按钮点击结果: {result}")
            results.append(result)
    except Exception as e:
        log.warning(f"按钮点击失败: {e}")
    
    # 成功判定：按钮直调真实方法名（btn_call/btn_emit）或 Livewire extend 调用。
    # 不认原生点击（btn_native/旧 clicked:）——可能未进 Livewire 管线造成假成功，
    # 由 click_renew_button 内的 __lwResponses 校验兜底。
    return {"results": results, "success": any(
        (r.lower().startswith("btn_call:") or r.lower().startswith("btn_emit:")
         or "call_extend" in r.lower() or "direct_extend" in r.lower() or "emit_extend" in r.lower())
        for r in results
    )}


def try_render_turnstile(sb):
    """Turnstile 长时间未自动通过时：诊断并尝试手动渲染 widget"""
    try:
        ts_diag = sb.execute_script("""
        (function(){
        var out = [];
        out.push('window.turnstile: ' + (typeof window.turnstile !== 'undefined'));
        out.push('turnstile.render: ' + (typeof window.turnstile !== 'undefined' && typeof window.turnstile.render === 'function'));
        var widget = document.querySelector('.cf-turnstile');
        if (widget) {
            out.push('sitekey: ' + (widget.getAttribute('data-sitekey') || 'MISSING'));
            out.push('data-appear: ' + (widget.getAttribute('data-appearance') || ''));
        }
        var resp = document.querySelector('[name="cf-turnstile-response"]');
        out.push('response value: ' + (resp ? (resp.value || 'EMPTY').substring(0,30) : 'no_input'));
        var tsIframes = document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]');
        out.push('turnstile iframes: ' + tsIframes.length);
        for (var i = 0; i < tsIframes.length; i++) {
            out.push('  ts iframe[' + i + '] src=' + tsIframes[i].src.substring(0,100));
        }
        var allScripts = document.querySelectorAll('script[src*="turnstile"]');
        out.push('turnstile scripts: ' + allScripts.length);
        for (var i = 0; i < allScripts.length; i++) {
            out.push('  script src=' + allScripts[i].src);
        }
        return out.join('\\n');
        })();
        """)
        if ts_diag:
            log.info("=== Turnstile 诊断 ===\n" + str(ts_diag) + "\n=== 结束 ===")

        sitekey_result = sb.execute_script("""
        (function(){
        var scripts = document.querySelectorAll('script');
        for (var i = 0; i < scripts.length; i++) {
            var content = scripts[i].textContent || '';
            var match = content.match(/(0x[0-9a-zA-Z_]{15,})/);
            if (match) return 'script:' + match[1];
        }
        var wireEls = document.querySelectorAll('[wire\\\\:initial-data]');
        for (var i = 0; i < wireEls.length; i++) {
            var data = wireEls[i].getAttribute('wire:initial-data') || '';
            var match = data.match(/(0x[0-9a-zA-Z_]{15,})/);
            if (match) return 'wire:' + match[1];
        }
        var allEls = document.querySelectorAll('*');
        for (var i = 0; i < allEls.length && i < 500; i++) {
            var attrs = allEls[i].attributes;
            for (var j = 0; j < attrs.length; j++) {
                if (attrs[j].name.toLowerCase().indexOf('sitekey') !== -1) return 'attr:' + attrs[j].value;
                var m = attrs[j].value.match(/^(0x[0-9a-zA-Z_]{15,})$/);
                if (m) return 'attrval:' + m[1];
            }
        }
        for (var key in window) {
            try {
                var v = window[key];
                if (typeof v === 'string' && v.match(/^0x[0-9a-zA-Z_]{15,}$/)) return 'window.' + key + ':' + v;
            } catch(e) {}
        }
        return '';
        })();
        """)
        if sitekey_result:
            sitekey = str(sitekey_result).split(":")[-1]
            log.info(f"找到 Turnstile sitekey: {sitekey_result} -> {sitekey}")
            sb.execute_script(f"""
            (function(){{
            var widget = document.querySelector('.cf-turnstile');
            if (widget && window.turnstile) {{
                try {{
                    var widgetId = window.turnstile.render(widget, {{
                        sitekey: '{sitekey}',
                        theme: 'dark',
                        callback: function(token) {{
                            var input = document.querySelector('[name="cf-turnstile-response"]');
                            if (input) input.value = token;
                        }}
                    }});
                    window.__tsWidgetId = widgetId;
                }} catch(e) {{
                    window.__tsError = e.toString();
                }}
            }}
            }})();
            """)
            time.sleep(8)
            ts_resp = sb.execute_script("""
            (function(){
            var input = document.querySelector('[name="cf-turnstile-response"]');
            return (input && input.value) ? 'has_token:' + input.value.substring(0,20) : 'no_token';
            })();
            """)
            log.info(f"Turnstile render 后 response: {ts_resp}")
        else:
            log.warning("未找到 Turnstile sitekey，dump 页面 HTML")
            dump_html(sb, "no_sitekey")
    except Exception as e:
        log.warning(f"Turnstile 手动渲染失败: {e}")


# 安装在页面里的 Livewire 请求捕获 hook（幂等；页面刷新后重装即可）
_LW_HOOK_JS = """
(function(){
    if (window.__lwHookInstalled) return 'already';
    window.__lwHookInstalled = true;
    window.__lwResponses = [];
    window.__popups = [];
    window.__postMessages = [];
    // window.open 捕获：合成点击无 user activation 时，弹窗广告会被 Chrome 拦截
    try {
        var origOpen = window.open;
        window.open = function(u, n, f) {
            var ret = null;
            try { ret = origOpen.apply(this, arguments); } catch(e) {
                window.__popups.push({url: String(u || '').substring(0, 200), blocked: true, err: String(e).substring(0, 80), time: Date.now()});
                return null;
            }
            window.__popups.push({url: String(u || '').substring(0, 200), blocked: !ret, time: Date.now()});
            return ret;
        };
    } catch(e) {}
    // postMessage 捕获（广告完成回调通常 postMessage 通知父页；cloudflare 心跳最多留 2 条）
    try {
        window.addEventListener('message', function(ev) {
            try {
                var isCf = String(ev.origin || '').indexOf('challenges.cloudflare.com') !== -1;
                if (isCf) {
                    var cfCount = 0;
                    for (var q = 0; q < window.__postMessages.length; q++) { if (window.__postMessages[q].cf) cfCount++; }
                    if (cfCount >= 2) return;
                }
                window.__postMessages.push({
                    cf: isCf,
                    origin: String(ev.origin || '').substring(0, 80),
                    data: String(typeof ev.data === 'object' ? JSON.stringify(ev.data) : ev.data).substring(0, 150),
                    time: Date.now()
                });
                if (window.__postMessages.length > 10) window.__postMessages.shift();
            } catch(e) {}
        });
    } catch(e) {}
    // 全局 click 捕获：验证受信任点击是否真的落在续期按钮上
    try {
        window.__clicks = [];
        document.addEventListener('click', function(e) {
            try {
                var el = e.target;
                var onRenew = false;
                while (el) { if (el.getAttribute && el.getAttribute('data-g4f-renew')) { onRenew = true; break; } el = el.parentElement; }
                window.__clicks.push({
                    trusted: !!e.isTrusted,
                    tag: e.target && e.target.tagName ? e.target.tagName : '?',
                    onRenewBtn: onRenew,
                    x: e.clientX, y: e.clientY,
                    time: Date.now()
                });
                if (window.__clicks.length > 30) window.__clicks.shift();
            } catch(err) {}
        }, true);
    } catch(e) {}
    // JS 错误捕获（处理器静默异常会留痕）
    try {
        window.__jsErrors = [];
        window.addEventListener('error', function(ev) {
            try {
                window.__jsErrors.push({msg: String(ev.message || '').substring(0, 200), src: String(ev.filename || '').substring(0, 100), line: ev.lineno, time: Date.now()});
                if (window.__jsErrors.length > 10) window.__jsErrors.shift();
            } catch(e) {}
        }, true);
        window.addEventListener('unhandledrejection', function(ev) {
            try {
                var r = ev.reason;
                window.__jsErrors.push({rejection: String((r && r.message) || r || '').substring(0, 200), time: Date.now()});
                if (window.__jsErrors.length > 10) window.__jsErrors.shift();
            } catch(e) {}
        });
    } catch(e) {}
    try {
        var origFetch = window.fetch;
        window.fetch = function() {
            var args = arguments, url = (typeof args[0] === 'string') ? args[0] : ((args[0] && args[0].url) || '');
            var isLw = String(url).indexOf('livewire') !== -1;
            if (!isLw && args[1] && args[1].headers) {
                try {
                    var h = args[1].headers;
                    if (h.get && h.get('X-Livewire')) isLw = true;
                    else if (h['X-Livewire'] || h['x-livewire']) isLw = true;
                } catch(e) {}
            }
            var p = origFetch.apply(this, args);
            if (isLw) {
                p.then(function(res){
                    try {
                        res.clone().text().then(function(t){
                            window.__lwResponses.push({type:'fetch', url:String(url).substring(0,120), status:res.status, body:t.substring(0,60000)});
                        });
                    } catch(e) {}
                }).catch(function(){});
            }
            return p;
        };
        var origOpen = XMLHttpRequest.prototype.open;
        var origSend = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function(m, u) { this.__lwUrl = u; return origOpen.apply(this, arguments); };
        XMLHttpRequest.prototype.send = function() {
            var xhr = this;
            if (String(xhr.__lwUrl || '').indexOf('livewire') !== -1) {
                xhr.addEventListener('load', function(){
                    try { window.__lwResponses.push({type:'xhr', url:String(xhr.__lwUrl).substring(0,120), status:xhr.status, body:String(xhr.responseText||'').substring(0,60000)}); } catch(e) {}
                });
            }
            return origSend.apply(this, arguments);
        };
        return 'installed';
    } catch(e) { return 'error:' + e.message; }
})();
"""


def capture_renew_response(sb):
    """续期点击未生效时：抓取页面上下文证据（弹窗/回调/提示/Livewire 响应）定位真实原因。
    注意：UC 模式下 sb.* API（走 CDP 通道）可靠，原生 sb.driver.* 可能 connection refused，
    所以页面内诊断放最前，原生 driver 调用放最后且失败不影响其余步骤。"""
    # 1) 页面上下文诊断：按钮真实绑定/点击落点/弹窗/回调/SDK（不依赖 chromedriver）
    try:
        diag = sb.execute_script("""
        (function(){
            var out = {};
            var btn = null;
            var candidates = document.querySelectorAll('button, a, [role=button]');
            for (var i = 0; i < candidates.length; i++) {
                var t = (candidates[i].innerText || '').trim();
                if (/(?:\\+\\s*)?90\\s*min/i.test(t) && !/[$￥¥€]/.test(t) && !/24\\s*h/i.test(t)) { btn = candidates[i]; break; }
            }
            out.btnHtml = btn ? btn.outerHTML.substring(0, 500) : 'not_found';
            var p1 = btn ? btn.parentElement : null;
            var p2 = p1 ? p1.parentElement : null;
            out.parentHtml = p1 ? p1.outerHTML.substring(0, 900) : '';
            out.gpHtml = p2 ? p2.outerHTML.substring(0, 1500) : '';
            out.popups = (window.__popups || []).slice(-5);
            out.clicks = (window.__clicks || []).slice(-8);
            out.jsErrors = (window.__jsErrors || []).slice(-5);
            out.sdk = {
                ramp: typeof window.ramp !== 'undefined',
                vidazoo: typeof window.vidazoo !== 'undefined',
                alpine: typeof window.Alpine !== 'undefined',
                pw: typeof window.pw !== 'undefined'
            };
            var adScripts = [];
            var scripts = document.querySelectorAll('script[src]');
            for (var i = 0; i < scripts.length; i++) {
                var s = scripts[i].src || '';
                if (/playwire|ramp|vidazoo|swapscape|reward/i.test(s)) adScripts.push(s.substring(0, 120));
            }
            out.adScripts = adScripts.slice(0, 6);
            try {
                var adRes = [];
                var res = performance.getEntriesByType('resource') || [];
                for (var j = 0; j < res.length; j++) {
                    var nm = res[j].name || '';
                    if (/playwire|ramp|vidazoo|swapscape|rewarded/i.test(nm)) {
                        adRes.push({n: nm.substring(0, 100), size: res[j].transferSize || 0, ms: Math.round(res[j].duration || 0)});
                    }
                }
                out.adResources = adRes.slice(0, 8);
            } catch(e) {}
            var tsInput = document.querySelector('[name="cf-turnstile-response"]');
            out.tsInput = tsInput ? ('present,len=' + (tsInput.value || '').length) : 'absent';
            out.cfIframes = document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]').length;
            out.postMessages = (window.__postMessages || []).slice(-3);
            return JSON.stringify(out);
        })();
        """)
        if diag:
            log.warning(f"页面上下文诊断: {str(diag)[:3500]}")
            try:
                with open(SHOT_DIR / "page_diag.json", "w", encoding="utf-8") as f:
                    f.write(str(diag))
            except Exception:
                pass
    except Exception as e:
        log.warning(f"页面诊断失败: {e}")

    # 1b) 定位 +90min 按钮所属的 Livewire 组件，dump 其完整 HTML（含 Alpine x-data/@click 绑定）
    try:
        owner_raw = sb.execute_script("""
        (function(){
        var btns = document.querySelectorAll('button, a, [role=button]');
        var btn = null;
        for (var i = 0; i < btns.length; i++) {
            var t = (btns[i].innerText || '').trim();
            if (/(?:\\+\\s*)?90\\s*min/i.test(t) && !/[$￥¥€]/.test(t) && !/24\\s*h/i.test(t)) { btn = btns[i]; break; }
        }
        if (!btn) return JSON.stringify({found: false});
        var a = btn, owner = null;
        while (a) { if (a.getAttribute && a.getAttribute('wire\\\\:id')) { owner = a; break; } a = a.parentElement; }
        var comps = [];
        var wc = document.querySelectorAll('[wire\\\\:id]');
        for (var j = 0; j < wc.length; j++) {
            comps.push({id: String(wc[j].getAttribute('wire\\\\:id') || '').substring(0, 14),
                        name: String(wc[j].getAttribute('wire\\\\:name') || '')});
        }
        var tsInp = document.querySelector('[name="cf-turnstile-response"]');
        var tsParent = tsInp && tsInp.parentElement ? tsInp.parentElement.outerHTML.substring(0, 800) : '';
        return JSON.stringify({found: true,
            ownerName: owner ? (owner.getAttribute('wire\\\\:name') || '?') : 'none',
            ownerId: owner ? String(owner.getAttribute('wire\\\\:id') || '').substring(0, 14) : '',
            ownerHtml: owner ? owner.outerHTML.substring(0, 30000) : '',
            btnNoOwner: owner ? '' : btn.outerHTML.substring(0, 800),
            tsParent: tsParent,
            components: comps});
        })();
        """)
        od = json.loads(owner_raw) if owner_raw else {}
        if od.get("found"):
            log.warning(f"按钮所属组件: name={od.get('ownerName')} id={od.get('ownerId')} "
                        f"(页面共 {len(od.get('components', []))} 个 wire 组件: "
                        f"{[c.get('name') for c in od.get('components', [])]})")
            if od.get("tsParent"):
                log.warning(f"Turnstile 容器 HTML: {od['tsParent'][:400]}")
            with open(SHOT_DIR / "renew_component.html", "w", encoding="utf-8") as f:
                f.write(od.get("ownerHtml") or od.get("btnNoOwner") or "")
            log.info("组件完整 HTML 已存 debug_output/renew_component.html")
        else:
            log.warning("按钮未找到（可能正被 Livewire 重渲染）")
    except Exception as e:
        log.warning(f"组件定位失败: {e}")

    # 2) 页面 toast / 提示条
    try:
        toasts = sb.execute_script("""
        (function(){
            var sels = '[role=alert], .toast, .Toast, .alert, .notification, .snackbar, .swal2-container, .Toastify, .modal-content';
            var out = [];
            var nodes = document.querySelectorAll(sels);
            for (var i = 0; i < nodes.length && i < 10; i++) {
                var t = (nodes[i].innerText || '').trim();
                if (t && nodes[i].getBoundingClientRect().width > 0) out.push(t.substring(0, 200));
            }
            return out.join(' || ');
        })();
        """)
        if toasts:
            log.warning(f"页面提示: {toasts}")
    except Exception:
        pass

    # 3) Livewire 请求的真实响应（点击前已安装 hook）
    try:
        responses = sb.execute_script("return JSON.stringify((window.__lwResponses || []).slice(0, 3))")
        if responses and responses != "[]":
            log.warning(f"Livewire 响应捕获: {str(responses)[:1500]}")
    except Exception:
        pass

    # 4) 未处理的 alert/confirm 对话框（原生 driver 调用，可能失败）
    try:
        alert = sb.driver.switch_to.alert
        log.warning(f"存在浏览器对话框: {alert.text}")
        alert.accept()
    except Exception:
        pass

    # 5) 新标签页检查（原生 driver 调用，失败不影响上面所有诊断）
    try:
        handles = sb.driver.window_handles
        if len(handles) > 1:
            for h in handles[1:]:
                sb.driver.switch_to.window(h)
                log.warning(f"点击后打开了新标签页: url={sb.driver.current_url} title={sb.driver.title}")
                screenshot(sb, "new_tab")
            sb.driver.switch_to.window(handles[0])
    except Exception as e:
        log.warning(f"标签页检查失败(driver 不可用时忽略): {str(e)[:120]}")

    # 6) Livewire 方法探测（每次运行只做一次）
    probe_livewire_methods(sb)

    # 7) 站点 JS 源码扫描（每次运行只做一次，定位按钮处理器真实逻辑）
    scan_page_js(sb)

    # 8) Alpine 状态与 extendClicked 源码 dump（每次运行只做一次）
    dump_alpine_state(sb)


_PROBE_DONE = {"done": False}

def probe_livewire_methods(sb):
    """对页面所有 Livewire 组件逐个尝试候选续期方法（含带参变体），找出服务端真实存在的调用。
    调用不存在的方法 promise 会 reject；存在则 resolve。
    仅在首轮续期失败后执行一次。"""
    if _PROBE_DONE["done"]:
        return
    _PROBE_DONE["done"] = True
    try:
        launch = sb.execute_script("""
        (function(){
        try {
            if (!window.Livewire || !window.Livewire.all) return 'no_livewire';
            var comps = window.Livewire.all();
            var targets = [];
            for (var i = 0; i < comps.length; i++) {
                var name = '';
                try { name = (comps[i].snapshot && comps[i].snapshot.memo && comps[i].snapshot.memo.name) || ''; } catch(e) {}
                targets.push({c: comps[i], name: name || ('comp#' + i)});
            }
            var METHODS = ['watchAd', 'claimAd', 'claimAdReward', 'rewardAd', 'startAd', 'extend'];
            window.__probeResults = [];
            var chain = Promise.resolve();
            function probeComp(c, label) {
                METHODS.forEach(function(m) {
                    chain = chain.then(function() {
                        return c.$wire.call(m).then(function(resp) {
                            window.__probeResults.push({c: label, m: m, ok: true, r: JSON.stringify(resp).substring(0, 200)});
                        }).catch(function(err) {
                            window.__probeResults.push({c: label, m: m, ok: false, e: String((err && err.message) || err).substring(0, 120)});
                        });
                    });
                });
            }
            // 全组件扫描基础方法
            for (var k = 0; k < targets.length; k++) probeComp(targets[k].c, targets[k].name);
            // extend 带参变体：在每个组件上试一次 extend(90)
            targets.forEach(function(t) {
                chain = chain.then(function() {
                    return t.c.$wire.call('extend', 90).then(function(resp) {
                        window.__probeResults.push({c: t.name, m: 'extend(90)', ok: true, r: JSON.stringify(resp).substring(0, 200)});
                    }).catch(function(err) {
                        window.__probeResults.push({c: t.name, m: 'extend(90)', ok: false, e: String((err && err.message) || err).substring(0, 120)});
                    });
                });
            });
            return 'probing_' + targets.length + 'x' + (METHODS.length + 1);
        } catch(e) { return 'error:' + e.message; }
        })();
        """)
        log.info(f"Livewire 方法探测启动: {launch}")
        if str(launch).startswith("probing"):
            time.sleep(110)
            results = sb.execute_script("return JSON.stringify(window.__probeResults || [])")
            probed = json.loads(results) if results else []
            by_comp = {}
            for m in probed:
                key = m.get("c", "?")
                by_comp.setdefault(key, {"ok": [], "bad": []})
                if m.get("ok"):
                    by_comp[key]["ok"].append(f"{m['m']} -> {m.get('r', '')[:80]}")
                else:
                    by_comp[key]["bad"].append(m["m"])
            for comp, res in by_comp.items():
                if res["ok"]:
                    log.warning(f"[PROBE] 组件 {comp}: 可用调用 {res['ok']}")
                log.info(f"[PROBE] 组件 {comp}: 无效 {res['bad']}")
            ts_now = get_expires_ts(sb)
            log.info(f"[PROBE] 探测后 expiresTimestamp: {ts_now}")
    except Exception as e:
        log.warning(f"方法探测异常: {e}")


_SCAN_DONE = {"done": False}

_ALPINE_DONE = {"done": False}

def dump_alpine_state(sb):
    """直接从 Alpine 组件作用域拿 extendClicked() 源码 + 全部函数名 + g4f* 全局 + 内联脚本全文。
    这是定位续期流程前置条件与最终 Livewire 调用参数的最直接途径。每次运行只执行一次。"""
    if _ALPINE_DONE["done"]:
        return
    _ALPINE_DONE["done"] = True
    try:
        raw = sb.execute_script("""
        (function(){
        try {
            if (!window.Alpine) return JSON.stringify({err: 'no_alpine'});
            var el = document.querySelector('.rt-btn-free') || document.querySelector('.rt-actions') || document.querySelector('.rt-root');
            if (!el) return JSON.stringify({err: 'no_button_element'});
            var data = Alpine.$data(el);
            if (!data) return JSON.stringify({err: 'no_alpine_data'});
            var out = {};
            out.extendClicked = (typeof data.extendClicked === 'function')
                ? data.extendClicked.toString().substring(0, 12000) : ('type:' + typeof data.extendClicked);
            var fns = [], vals = {};
            for (var k in data) {
                try {
                    var v = data[k];
                    if (typeof v === 'function') {
                        if (k !== 'extendClicked') fns.push(k + ': ' + v.toString().substring(0, 500));
                    } else if (['isPremium','isNativeApp','extendDisabled','adLoading','webAdLoading',
                                'adWaiting','adCooldown','adCdLabel','webAdUrl','adSession','adFill',
                                'cooldownSecs','expires','balance','paidCost'].indexOf(k) !== -1) {
                        vals[k] = String(v).substring(0, 120);
                    }
                } catch(e) {}
            }
            out.state = vals;
            out.functions = fns;
            out.g4fGlobals = [];
            for (var g in window) {
                try {
                    if (/^g4f/i.test(g)) {
                        if (typeof window[g] === 'function') out.g4fGlobals.push(g + ': ' + window[g].toString().substring(0, 600));
                        else out.g4fGlobals.push(g + ' = ' + String(window[g]).substring(0, 120));
                    }
                } catch(e) {}
            }
            out.tsLoaded = typeof window.turnstile !== 'undefined';
            out._g4fTsWidgetId = String(window._g4fTsWidgetId);
            out.tsInputs = [];
            var tins = document.querySelectorAll('[name="cf-turnstile-response"]');
            for (var i = 0; i < tins.length; i++) {
                out.tsInputs.push({id: tins[i].id || '', len: (tins[i].value || '').length});
            }
            return JSON.stringify(out);
        } catch(e) { return JSON.stringify({err: String(e)}); }
        })();
        """)
        d = json.loads(raw) if raw else {}
        if d.get("err"):
            log.warning(f"Alpine dump 失败: {d['err']}")
            return
        src = d.get("extendClicked", "")
        log.warning(f"=== extendClicked 源码 ===\n{src[:3500]}\n=== extendClicked 源码结束 (全长 {len(src)}) ===")
        with open(SHOT_DIR / "extend_clicked.js", "w", encoding="utf-8") as f:
            f.write(src)
        log.warning(f"Alpine 状态: {json.dumps(d.get('state', {}), ensure_ascii=False)}")
        log.warning(f"Alpine 函数: {', '.join(x.split(':')[0] for x in d.get('functions', []))}")
        for g in d.get("g4fGlobals", [])[:12]:
            log.warning(f"[G4F] {g[:400]}")
        log.warning(f"turnstile: loaded={d.get('tsLoaded')} _g4fTsWidgetId={d.get('_g4fTsWidgetId')} "
                    f"inputs={json.dumps(d.get('tsInputs', []))}")
        # 内联脚本全文落盘（g4fTurnstile / 广告队列编排器都在里面）
        inline_raw = sb.execute_script("""
        (function(){
        var out = [];
        var scripts = document.querySelectorAll('script:not([src])');
        for (var i = 0; i < scripts.length; i++) {
            var t = scripts[i].textContent || '';
            if (t.length > 200) out.push('===== inline#' + i + ' (' + t.length + ' chars) =====\\n' + t.substring(0, 50000));
        }
        return out.join('\\n\\n');
        })();
        """)
        if inline_raw:
            with open(SHOT_DIR / "inline_scripts.txt", "w", encoding="utf-8") as f:
                f.write(str(inline_raw))
            log.info(f"内联脚本全文已存 debug_output/inline_scripts.txt ({len(str(inline_raw))} chars)")
    except Exception as e:
        log.warning(f"Alpine dump 异常: {e}")

def scan_page_js(sb):
    """下载页面所有脚本（内联+外链），搜索续期/广告/Turnstile 相关代码，
    定位 '+90 min' 按钮点击处理器的真实逻辑与前置条件。
    每次运行只执行一次，完整结果存 debug_output/js_scan.txt。"""
    if _SCAN_DONE["done"]:
        return
    _SCAN_DONE["done"] = True
    KEYWORDS = r"(watchAd|claimAd|adReward|rewardAd|startAd|renewal-timer|turnstile\.render|cf-turnstile|watch ad|watch-ad|adCooldown|expiresTimestamp|swapscape|ramp\.spa)"
    try:
        raw = sb.execute_script("""
        (function(){
        var inline = [], external = [];
        var scripts = document.querySelectorAll('script');
        for (var i = 0; i < scripts.length; i++) {
            var sc = scripts[i];
            if (sc.src) external.push(sc.src);
            else if ((sc.textContent || '').length > 40) inline.push(sc.textContent);
        }
        return JSON.stringify({inline: inline, external: external});
        })();
        """)
        data = json.loads(raw) if raw else {}
        inline_list = data.get("inline", []) or []
        external_list = data.get("external", []) or []
        log.info(f"JS 扫描: 内联脚本 {len(inline_list)} 个, 外链 {len(external_list)} 个")
        hits = []

        def scan_text(name, text):
            if not text:
                return
            try:
                count = 0
                for m in re.finditer(KEYWORDS, text, re.IGNORECASE):
                    s = max(0, m.start() - 300)
                    e = min(len(text), m.end() + 300)
                    hits.append({"src": str(name)[:140], "kw": m.group(0), "ctx": text[s:e].replace("\n", " ")[:650]})
                    count += 1
                    if count >= 4 or len(hits) >= 60:
                        return
            except Exception:
                pass

        for idx, t in enumerate(inline_list):
            scan_text(f"inline#{idx}", t)
        for src in external_list[:14]:
            text = None
            try:
                r = requests.get(src, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
                if r.ok and "just a moment" not in r.text[:600].lower():
                    text = r.text
            except Exception as e:
                log.info(f"  下载失败 {src[:100]}: {str(e)[:80]}")
            if not text:
                # 回退：页面上下文同步 XHR（同源带 cookie，可过 CF）
                try:
                    text = sb.execute_script("""
        (function(){
        try {
            var xhr = new XMLHttpRequest();
            xhr.open('GET', '%s', false);
            xhr.send(null);
            if (xhr.status >= 200 && xhr.status < 400) return xhr.responseText.substring(0, 1500000);
            return 'HTTP_' + xhr.status;
        } catch(e) { return 'FETCH_ERR:' + e.message; }
        })();
        """ % src)
                except Exception as e:
                    log.info(f"  页面 fetch 失败 {src[:100]}: {str(e)[:80]}")
            if text and not str(text).startswith("FETCH_ERR"):
                scan_text(src, text[:1500000])
            else:
                hits.append({"src": str(src)[:140], "kw": "FETCH_FAIL", "ctx": str(text)[:150]})
        out_path = SHOT_DIR / "js_scan.txt"
        with open(out_path, "w", encoding="utf-8") as f:
            for h in hits:
                f.write(f"=== {h['src']} [{h['kw']}]\n{h['ctx']}\n\n")
        log.warning(f"JS 扫描完成: {len(hits)} 处命中, 完整结果已存 {out_path}")
        for h in hits[:8]:
            log.warning(f"[JS] {h['src']} [{h['kw']}]: {h['ctx'][:280]}")
    except Exception as e:
        log.warning(f"JS 扫描异常: {e}")


def get_expires_ts(sb) -> int:
    """从已捕获的 Livewire 响应中解析服务端 expiresTimestamp（真值，比 UI 倒计时可靠）。
    续期成功时该值会增加 90 分钟；广告未走完则保持不变。解析不到返回 0。"""
    try:
        out = sb.execute_script("""
        (function(){
            var arr = window.__lwResponses || [];
            for (var i = arr.length - 1; i >= 0; i--) {
                var m = String(arr[i].body || '').match(/expiresTimestamp[^0-9]*(\\d{9,12})/);
                if (m) return parseInt(m[1], 10);
            }
            return 0;
        })();
        """)
        return int(out or 0)
    except Exception:
        return 0


def click_renew_button(sb) -> bool:
    """gaming4free 续期：点击 console 页 '+ 90 min' 免费按钮（'+24h $x' 付费按钮不碰）。
    免费续期 = 看广告得时长（v5 机制）：
      1) 点击前轮询等待按钮就绪（文案变为 watch ad 模式 / 从 disabled 恢复）
      2) 点击后统一等待：Playwire 广告播完 -> 容器内点「领取/继续」；或确认弹窗/Turnstile
      3) 以服务端 expiresTimestamp 为真值判定生效
      4) 全程不 refresh 页面（refresh 会打断正在进行的广告流程）"""
    from util import _LW_CALL_BUTTON_JS, _AD_CLICK_JS
    # 1) 安装 hook + 记录基线（服务端 expiresTimestamp 为续期真值）
    try:
        sb.execute_script(_LW_HOOK_JS)
    except Exception:
        pass
    ts_before = get_expires_ts(sb)
    if ts_before:
        log.info(f"基线 expiresTimestamp: {ts_before}")

    # 2) 点击前：轮询等待按钮就绪（最多 45s）——文案切到 watch ad 或按钮从 disabled 恢复
    ready = ""
    for _ in range(9):
        try:
            ready = str(sb.execute_script("""
        (function(){
        var btns = document.querySelectorAll('button, a, [role=button]');
        for (var i = 0; i < btns.length; i++) {
            var el = btns[i];
            var t = (el.innerText || el.textContent || '').trim();
            if (!t || t.length > 60) continue;
            if (!/(?:\\+\\s*)?90\\s*min/i.test(t)) continue;
            if (/[$￥¥€]/.test(t) || /24\\s*h/i.test(t)) continue;
            var r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return 'hidden';
            if (el.disabled) return 'disabled';
            return t.toLowerCase().indexOf('watch ad') !== -1 ? 'ad_ready' : 'ready';
        }
        return 'not_found';
        })();
        """))
        except Exception as e:
            log.warning(f"按钮就绪检测异常: {e}")
            ready = 'not_found'
        if ready in ("ready", "ad_ready"):
            break
        time.sleep(5)
    if ready not in ("ready", "ad_ready"):
        log.warning(f"续期按钮未就绪: {ready} —— 跳过本轮（冷却后重试）")
        return False
    log.info(f"按钮就绪: {ready}")

    # 3) 标记按钮 + 受信任点击。
    #    合成 el.click() 是不可信事件（无 user activation），站点 JS 在处理器里
    #    调 window.open 打开 Playwire 激励广告弹窗时会被 Chrome 直接拦截——
    #    这就是"点击后无广告无弹窗、expiresTimestamp 不变"的根因。
    #    uc_click 走 CDP 真实鼠标事件，带 user activation，弹窗可正常打开。
    try:
        result = str(sb.execute_script("""
        (function(){
        var candidates = document.querySelectorAll('button, a, [role=button], [wire\\\\:click], [wire\\\\:click\\\\.prevent]');
        for (var i = 0; i < candidates.length; i++) {
            var el = candidates[i];
            var t = (el.innerText || el.textContent || '').trim();
            if (!t || t.length > 60) continue;
            if (!/(?:\\+\\s*)?90\\s*min/i.test(t)) continue;
            if (/[$￥¥€]/.test(t) || /24\\s*h/i.test(t)) continue;
            if (el.disabled) return 'disabled';
            var r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return 'hidden';
            var olds = document.querySelectorAll('[data-g4f-renew]');
            for (var k = 0; k < olds.length; k++) olds[k].removeAttribute('data-g4f-renew');
            el.setAttribute('data-g4f-renew', '1');
            var wireAttr = el.getAttribute('wire:click') || el.getAttribute('wire:click.prevent') || 'none';
            // 落点检测：若按钮中心被其它元素覆盖，真实点击会点在覆盖层上
            var cx = r.left + r.width/2, cy = r.top + r.height/2;
            var hit = document.elementFromPoint(cx, cy);
            var hitInfo = (hit === el || el.contains(hit)) ? 'ok' : (hit ? 'blocked_by:' + hit.tagName + '.' + String(hit.className || '').substring(0, 40) : 'none');
            return 'marked:' + t.substring(0, 40) + '|tag=' + el.tagName + '|wire=' + wireAttr + '|hit=' + hitInfo;
        }
        return 'not_found';
        })();
        """))
        if not result.startswith("marked:"):
            log.warning(f"+90min 续期按钮未找到: {result}")
            return False
        log.info(f"已标记续期按钮: {result}")
    except Exception as e:
        log.warning(f"标记续期按钮异常: {e}")
        return False

    RENEW_SEL = '[data-g4f-renew="1"]'
    clicked = False
    try:
        sb.uc_click(RENEW_SEL)
        clicked = True
        log.info("✅ uc_click 受信任点击完成（带 user activation，弹窗广告可打开）")
    except Exception as e:
        log.warning(f"uc_click 异常: {e}")
        try:
            sb.reconnect()
        except Exception:
            pass
    if not clicked:
        try:
            pt_raw = sb.execute_script("""
            (function(){
            var el = document.querySelector('%s');
            if (!el) return null;
            el.scrollIntoView({block: 'center'});
            var r = el.getBoundingClientRect();
            return JSON.stringify({x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)});
            })();
            """ % RENEW_SEL)
            pt = json.loads(pt_raw) if pt_raw else None
            if pt:
                sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent",
                    {"type": "mousePressed", "x": pt["x"], "y": pt["y"], "button": "left", "clickCount": 1})
                sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent",
                    {"type": "mouseReleased", "x": pt["x"], "y": pt["y"], "button": "left", "clickCount": 1})
                clicked = True
                log.info("✅ CDP 鼠标事件受信任点击完成")
        except Exception as e:
            log.warning(f"CDP 点击异常: {e}")
    if not clicked:
        try:
            sb.execute_script("(function(){ var el = document.querySelector('%s'); if (el) el.click(); })();" % RENEW_SEL)
            log.info("⚠️ 已退回合成 click（无 user activation，弹窗广告可能被拦截）")
        except Exception as e:
            log.warning(f"合成点击异常: {e}")
            return False

    # 4) 点击后统一等待（最多约 120s）：
    #    点击 → g4fRewarded.request()（~5s no_fill）→ showExtendCaptcha → #g4f-ts-modal
    #    → Turnstile 挑战 → token → 站点回调(_g4fTsCallback) 自动完成续期。
    #    旧循环不认识 #g4f-ts-modal，模态开着却一直报 no_modal，白白错过整个流程。
    ad_seen = False
    no_modal_rounds = 0
    token_ok_rounds = 0
    g4f_rendered = False      # 是否已手动补渲染过 widget
    ts_clicks = 0             # Turnstile checkbox 点击次数
    diag_rendered = False     # 通用 try_render_turnstile 是否已跑过
    for wait_round in range(40):
        try:
            state = str(sb.execute_script("""
        (function(){
        function visible(el){ var r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }
        // ── g4f Turnstile 流程（最高优先级）──
        var tins = document.querySelectorAll('[name="cf-turnstile-response"]');
        for (var ti = 0; ti < tins.length; ti++) {
            if (tins[ti].value && tins[ti].value.length > 20) return 'ts_token_ok';
        }
        var g4fModal = document.getElementById('g4f-ts-modal');
        if (g4fModal && g4fModal.style.display === 'flex') {
            var wid = String(window._g4fTsWidgetId);
            var cfIfr = null;
            (function findIfr(root) {
                if (cfIfr) return;
                var f = root.querySelector('iframe[src*="challenges.cloudflare.com"]');
                if (f) { cfIfr = f; return; }
                var els = root.querySelectorAll('*');
                for (var i = 0; i < els.length; i++) {
                    if (els[i].shadowRoot) findIfr(els[i].shadowRoot);
                    if (cfIfr) return;
                }
            })(document);
            if (cfIfr) {
                var fr = cfIfr.getBoundingClientRect();
                return 'ts_iframe:x=' + Math.round(fr.left) + ',y=' + Math.round(fr.top) + ',w=' + Math.round(fr.width) + ',h=' + Math.round(fr.height);
            }
            return 'ts_modal:wid=' + wid;
        }
        // ── 广告容器/iframe（watchWebAd ready 路径）──
        var adVisible = 0;
        var adIframes = document.querySelectorAll('iframe[src*="playwire"], iframe[src*="swapscape"], iframe[src*="reward"], .vm-ad iframe, [class*="ad-iframe"] iframe');
        for (var a = 0; a < adIframes.length; a++) { if (visible(adIframes[a])) adVisible++; }
        if (adVisible === 0) {
            var adNodes = document.querySelectorAll('.vm-ad, .ad-modal, [class*="reward-ad"], [class*="watch-ad"], .ad-container');
            for (var b = 0; b < adNodes.length; b++) { if (visible(adNodes[b])) { adVisible = 1; break; } }
        }
        if (adVisible) return 'ad_playing';
        var nodes = document.querySelectorAll('.vm-captcha, .cf-turnstile, [role=dialog], .modal.show, .modal[style*="block"], .popup');
        var modal = null;
        for (var k = 0; k < nodes.length; k++) { if (visible(nodes[k])) { modal = nodes[k]; break; } }
        var tsIframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
        if (!modal && !tsIframe) {
            // 弹窗广告动向（window.open hook 捕获，页面上下文不依赖 chromedriver）
            var pops = window.__popups || [];
            if (pops.length) {
                var lastPop = pops[pops.length - 1];
                return 'popup:' + (lastPop.blocked ? 'blocked' : 'opened') + ':' + lastPop.url.substring(0, 80);
            }
            return 'no_modal';
        }

        if (!window.__enaClicked) {
            var all = document.querySelectorAll('button, a, [role=button]');
            for (var i = 0; i < all.length; i++) {
                var t = (all[i].innerText || '').trim().toLowerCase();
                if (t.indexOf('enable ads') !== -1 && !all[i].disabled && visible(all[i])) {
                    window.__enaClicked = true;
                    all[i].click();
                    return 'clicked:enable_ads';
                }
            }
        }

        var resp = document.querySelector('[name="cf-turnstile-response"]') || document.querySelector('input[name*="turnstile"]');
        var tokenOk = !!(resp && resp.value && resp.value.length > 20);

        var s = document.querySelector('.vm-submit');
        if (s && !s.disabled && visible(s)) {
            s.scrollIntoView({block: 'center'});
            s.click();
            return 'clicked:vm-submit';
        }
        if (modal) {
            var mbtns = modal.querySelectorAll('button, [role=button]');
            for (var j = 0; j < mbtns.length; j++) {
                var b = mbtns[j], bt = (b.innerText || '').trim();
                if (/confirm|submit|continue|vote|claim|extend|get\\s*\\d/i.test(bt) && !/[$￥¥]/.test(bt) && !b.disabled && visible(b)) {
                    b.scrollIntoView({block: 'center'});
                    b.click();
                    return 'clicked:modal:' + bt.substring(0, 30);
                }
            }
        }
        return 'waiting:' + (tokenOk ? 'token_ok' : 'token_missing');
        })();
        """))
        except Exception as e:
            log.warning(f"弹窗/广告检测异常: {e}")
            state = 'no_modal'

        if state == "ts_token_ok":
            token_ok_rounds += 1
            no_modal_rounds = 0
            if token_ok_rounds >= 2:
                log.info("✅ Turnstile token 已就绪，站点回调将自动完成续期")
                return True
            time.sleep(2)
            continue

        if state.startswith("ts_iframe:"):
            # 挑战 iframe 可见：CDP 受信任点击 checkbox 位置（左侧中部）
            m = re.search(r"x=(\d+),y=(\d+),w=(\d+),h=(\d+)", state)
            if m and ts_clicks < 3:
                ts_clicks += 1
                cx = int(m.group(1)) + min(30, int(m.group(3)) // 3)
                cy = int(m.group(2)) + int(m.group(4)) // 2
                try:
                    sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent",
                        {"type": "mousePressed", "x": cx, "y": cy, "button": "left", "clickCount": 1})
                    sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent",
                        {"type": "mouseReleased", "x": cx, "y": cy, "button": "left", "clickCount": 1})
                    log.info(f"已点击 Turnstile 挑战框 ({cx},{cy})，等待 token... (第{ts_clicks}次)")
                except Exception as e:
                    log.warning(f"Turnstile 点击异常: {e}")
            time.sleep(4)
            continue

        if state.startswith("ts_modal:wid=null"):
            # 站点 turnstile.render 没成功（teleport 时序/脚本问题）→ 手动补渲染，
            # 复用站点回调链：真 token 照常流入 _g4fTsCallback，服务端校验可过
            if not g4f_rendered:
                g4f_rendered = True
                try:
                    rr = sb.execute_script("""
                    (function(){
                    try {
                        if (typeof turnstile === 'undefined') return 'no_turnstile';
                        var w = document.getElementById('g4f-ts-widget');
                        if (!w) { w = document.createElement('div'); w.id = 'g4f-ts-widget'; document.body.appendChild(w); }
                        window._g4fTsWidgetId = turnstile.render('#g4f-ts-widget', {
                            sitekey: '0x4AAAAAADWeWAriMB7Vsqya',
                            theme: 'dark',
                            callback: function(tok) {
                                g4fCloseTurnstile();
                                if (window._g4fTsCallback) { window._g4fTsCallback(tok); window._g4fTsCallback = null; }
                            }
                        });
                        return 'rendered:' + window._g4fTsWidgetId;
                    } catch(e) { return 'error:' + e.message; }
                    })();
                    """)
                    log.info(f"站点未渲染 Turnstile widget，手动补渲染: {rr}")
                except Exception as e:
                    log.warning(f"手动渲染异常: {e}")
            elif wait_round % 5 == 0:
                log.info(f"Turnstile 模态打开（补渲染已尝试）: {state[:60]}, {wait_round+1}/40")
            time.sleep(3)
            continue

        if state.startswith("ts_modal:wid="):
            if wait_round % 5 == 0:
                log.info(f"Turnstile 模态打开，等待挑战/token... ({state[:70]}, {wait_round+1}/40)")
            time.sleep(3)
            continue

        if state == "ad_playing":
            ad_seen = True
            if wait_round % 5 == 0:
                log.info(f"广告播放中... (~{wait_round*3}s)")
            time.sleep(3)
            continue

        if state.startswith("popup:"):
            if ":blocked" in state:
                log.error(f"广告弹窗被 Chrome 拦截（点击缺少 user activation）: {state[:120]}")
                break
            ad_seen = True
            if wait_round % 5 == 0:
                log.info(f"广告弹窗已打开，等待播放/回调... (~{wait_round*3}s)")
            time.sleep(3)
            continue

        if state.startswith("clicked:enable_ads"):
            log.info("已点击 enable ads，继续等待确认...")
        elif state.startswith("clicked:"):
            # 弹窗/Turnstile 里的确认按钮已点。若刚播完广告，再点一次容器内「领取/继续」
            if ad_seen:
                try:
                    cr = str(sb.execute_script(_AD_CLICK_JS))
                    if cr.startswith("clicked:"):
                        log.info(f"✅ 广告完成后点击领取/继续: {cr}")
                    elif cr == "ad_running":
                        log.info(f"✅ 续期确认: {state}（广告容器仍活跃，稍后确认）")
                    else:
                        log.info(f"✅ 续期确认: {state}")
                except Exception as e:
                    log.warning(f"领取按钮点击异常: {e}")
                    log.info(f"✅ 续期确认: {state}")
            else:
                log.info(f"✅ 续期确认: {state}")
            return True
        elif state == "no_modal":
            no_modal_rounds += 1
            token_ok_rounds = 0
        elif state == "waiting:token_ok":
            token_ok_rounds += 1
            no_modal_rounds = 0
            if token_ok_rounds >= 2:
                log.info("Turnstile 已通过，等待自动提交")
                return True
        else:
            no_modal_rounds = 0
            token_ok_rounds = 0
            if wait_round % 5 == 0:
                log.info(f"等待验证码/弹窗... ({state}, {wait_round+1}/40)")
            if not diag_rendered and state == "waiting:token_missing" and wait_round >= 10:
                diag_rendered = True
                try_render_turnstile(sb)
        time.sleep(3)

    # 4.5) 弹窗若仍在打开，尽力关闭（激励奖励一般在弹窗关闭后结算到账）
    try:
        handles = sb.driver.window_handles
        if handles and len(handles) > 1:
            sb.driver.switch_to.window(handles[-1])
            sb.driver.close()
            sb.driver.switch_to.window(handles[0])
            log.info("已关闭广告弹窗，等待服务端结算")
            time.sleep(6)
    except Exception as e:
        log.warning(f"关闭弹窗失败(忽略): {str(e)[:100]}")

    # 5) 最终判定：服务端 expiresTimestamp 为真值；无广告无弹窗时兜底 wire 方法直调
    ts_after = get_expires_ts(sb)
    if ts_after and ts_before and ts_after > ts_before:
        log.info(f"✅ expiresTimestamp {ts_before} -> {ts_after}，续期生效")
        return True
    if ad_seen:
        log.info("广告已看完，等待服务端确认时长到账（主循环将校验时间）")
        return True
    # 无任何广告/弹窗/确认信号：退回 wire 方法直调
    log.info("未检测到广告/弹窗，尝试 wire 方法直调...")
    try:
        call_result = str(sb.execute_script(_LW_CALL_BUTTON_JS))
        log.info(f"按钮 wire 方法直调: {call_result}")
    except Exception as e:
        log.warning(f"按钮 wire 方法直调异常: {e}")
    time.sleep(4)
    ts_after2 = get_expires_ts(sb)
    if ts_after2 and ts_before and ts_after2 > ts_before:
        log.info(f"✅ wire 直调后 expiresTimestamp {ts_before} -> {ts_after2}")
        return True
    log.warning("无广告无弹窗且 expiresTimestamp 未变，本轮判定未生效（不刷新页面，冷却后重试）")
    return False


def handle_turnstile(sb) -> bool:
    """处理 Cloudflare Turnstile，返回是否通过"""
    try:
        iframe_sel = 'iframe[src*="challenges.cloudflare.com"]'
        # seleniumbase 新版 is_element_present() 不接受 timeout 参数；
        # 用 wait_for_element_present 做限时等待并捕获超时异常
        try:
            sb.wait_for_element_present(iframe_sel, timeout=3)
            iframe_present = True
        except Exception:
            iframe_present = False
        if not iframe_present:
            log.info("未检测到 Turnstile iframe，跳过")
            return True

        log.info("检测到 Cloudflare Turnstile，等待自动通过...")
        screenshot(sb, "turnstile_appear")

        for i in range(TURNSTILE_WAIT):
            try:
                val = sb.execute_script("""
                    let el = document.querySelector('[name="cf-turnstile-response"]');
                    if (!el) el = document.querySelector('input[name*="turnstile"]');
                    return el ? el.value : '';
                """)
                if val and len(val) > 20:
                    log.info(f"Turnstile 已通过 ({i}s)")
                    return True
            except Exception:
                pass

            if i == 3:
                try:
                    sb.switch_to_frame(iframe_sel)
                    try:
                        if sb.is_element_visible('input[type="checkbox"]'):
                            sb.click('input[type="checkbox"]', timeout=3)
                            log.info("点击 Turnstile checkbox")
                    except Exception:
                        pass
                    finally:
                        sb.switch_to_default_content()
                except Exception:
                    pass

            time.sleep(1)

        log.warning(f"Turnstile {TURNSTILE_WAIT}s 未通过")
        screenshot(sb, "turnstile_timeout")
        return False
    except Exception as e:
        log.warning(f"Turnstile 处理异常: {e}")
        return True


def inject_cookies(sb):
    """如果提供了 cookie 字符串，注入到当前域名"""
    if not COOKIE_STR:
        log.info("未配置 cookie，跳过注入")
        return
    pairs = [item.strip() for item in COOKIE_STR.split(";") if "=" in item]
    log.info(f"注入自定义 cookie ... (共 {len(pairs)} 条)")
    ok, fail = 0, 0
    for item in pairs:
        k, v = item.split("=", 1)
        k, v = k.strip(), v.strip()
        try:
            # SeleniumBase BaseCase 没有 set_cookie 方法，直接用底层 webdriver 注入
            sb.driver.add_cookie({"name": k, "value": v})
            ok += 1
        except Exception as e:
            fail += 1
            log.warning(f"  cookie {k[:40]} 注入失败: {str(e)[:80]}")
    log.info(f"cookie 注入完成: 成功 {ok}, 失败 {fail}")


def do_login(sb):
    """登录逻辑（如有）"""
    if not USERNAME:
        log.info("未配置 GAME4FREE_USERNAME，跳过登录")
        return
    log.info(f"尝试登录用户: {USERNAME}")

    user_selectors = ['input[name="username"]', 'input[name="user"]',
                      'input[name="mc_username"]', 'input[type="text"]',
                      'input[id*="user"]', 'input[name="email"]']
    pass_selectors = ['input[name="password"]', 'input[type="password"]']
    submit_selectors = ['button[type="submit"]', 'input[type="submit"]',
                         'button:contains("Login")', 'button:contains("登录")',
                         'button:contains("Sign in")']

    for sel in user_selectors:
        try:
            if sb.is_element_visible(sel):
                sb.type(sel, USERNAME, timeout=5)
                break
        except Exception:
            continue
    if PASSWORD:
        for sel in pass_selectors:
            try:
                if sb.is_element_visible(sel):
                    sb.type(sel, PASSWORD, timeout=5)
                    break
            except Exception:
                continue
    for sel in submit_selectors:
        try:
            if sb.is_element_visible(sel):
                human_sleep(0.5, 1.2)
                sb.click(sel, timeout=5)
                log.info("登录表单已提交")
                time.sleep(3)
                return
        except Exception:
            continue


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run():
    global PROXY_URL
    from seleniumbase import SB
    # 保存原始代理配置
    original_proxy_url = PROXY_URL
    # 解析代理端口用于预检
    port_match = re.search(r':(\d+)$', PROXY_URL.rstrip('/'))
    proxy_port = int(port_match.group(1)) if port_match else 1080

    log.info("=" * 60)
    log.info("gaming4free 续期启动")
    log.info(f"代理地址: {PROXY_URL}")
    log.info(f"目标站点: {SITE_URL}")
    log.info(f"用户名:  {USERNAME or '(未配置)'}")
    log.info("=" * 60)

    # 预检代理端口
    proxy_available = False
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(("127.0.0.1", proxy_port))
        s.close()
        log.info(f"代理 SOCKS5 端口 {proxy_port} 可用")
        proxy_available = True
    except Exception:
        log.warning(f"代理端口 {proxy_port} 不可达，尝试直连...")

    # 根据代理可用性设置代理参数
    if not proxy_available:
        log.info("代理不可用，使用直连模式")
        PROXY_URL = ""
    log.info("正在启动浏览器 (uc=True, headless=False)...")
    with SB(
        browser="chrome",
        uc=True,
        headless=False,
        incognito=False,
        proxy=PROXY_URL,
        
        disable_cookies=False,
        ad_block=False,
    ) as sb:
        log.info("✅ 浏览器启动成功")

        sb.set_window_size(1280, 800)
        sb.driver.set_page_load_timeout(PAGE_TIMEOUT)

        # Step 1: 打开站点
        try:
            sb.open(SITE_URL)
            sb.sleep(2)
        except Exception as e:
            log.error(f"打开站点失败: {e}")
            screenshot(sb, "open_fail")
            tg(f"❌ gaming4free 续期失败：站点打不开\n{e}")
            return

        # Step 2: 处理 CF 5 秒盾（如有）
        log.info("等待 CF 5 秒盾（如有）...")
        for _ in range(15):
            if "just a moment" in sb.get_text("body").lower():
                time.sleep(1)
            else:
                break

        # Step 3: 注入 cookie / 登录
        inject_cookies(sb)
        if LOGIN_URL:
            sb.open(LOGIN_URL)
            sb.sleep(2)
            do_login(sb)

        # 关键：注入 cookie 后必须重新导航一次，浏览器才会带上 cookie 建立会话；
        # 不刷新的话当前渲染的仍是未登录页面（按钮一直 not_found 的根因）
        sb.open(SITE_URL)
        sb.sleep(2)

        # 若重新导航后落在 CF 盾上，继续等待
        for _ in range(20):
            try:
                page_txt = (sb.get_page_title() or "") + " " + sb.get_text("body")
            except Exception:
                page_txt = ""
            low = page_txt.lower()
            if "just a moment" in low or "attention required" in low:
                time.sleep(1)
            else:
                break

        screenshot(sb, "dashboard")

        # Step 3.5: 登录态校验 —— cookie 失效时明确报错退出，不做无意义循环
        # （本站是无密码登录：Discord/Google 一键，登录页没有密码输入框，
        #   所以被重定向到 /login URL 才是最可靠的未登录信号）
        st = page_state(sb)
        log.info(f"页面状态: {json.dumps(st, ensure_ascii=False)}")
        on_login_page = "/login" in st.get("url", "")
        if on_login_page or (st.get("has_password") and st.get("wire_count", 0) == 0):
            dump_html(sb, "not_logged_in")
            screenshot(sb, "not_logged_in")
            log.error("未登录（被重定向到登录页）：cookie 注入失败或已失效，请更新 GAME4FREE_COOKIE")
            tg(f"❌ gaming4free 续期失败：未登录\n当前页面: {st.get('url', '?')}")
            return

        # Step 4: 主循环 - 反复点击续期直到接近 48h
        click_count = 0
        no_btn_streak = 0          # 连续找不到续期入口的次数
        fail_streak = 0           # 连续剩余时间未增加（点击未生效）的次数
        last_sec = get_remaining_seconds(sb)
        log.info(f"初始剩余: {last_sec}s ({fmt_hm(last_sec)})")

        if last_sec < 0 and not st.get("btn_90min"):
            log.warning("未检测到剩余时间倒计时，且页面上没有续期按钮 —— "
                        "请确认 GAME4FREE_RENEW_URL 指向服务器详情页 (/server/{id}) 而非首页")

        while click_count < MAX_CLICKS:
            # 接近上限就停
            if last_sec >= (MAX_HOURS - 1) * 3600:
                log.info(f"已接近 {MAX_HOURS}h 上限，停止续期")
                break

            # Step 4.1: 站点真实续期机制 = console 页 "+ 90 min" 免费按钮（看广告得时长，v5 机制）；
            # 点击前记录服务端 expiresTimestamp 基线，点击后以其增加为真值判定
            old_ts = get_expires_ts(sb)
            log.info("尝试点击续期按钮（+90 min）...")
            if click_renew_button(sb):
                log.info("✅ 按钮点击续期已触发")
                no_btn_streak = 0
            else:
                log.warning("按钮点击失败，尝试 Livewire extend...")
                lw_result = livewire_extend(sb)
                if not lw_result["success"]:
                    no_btn_streak += 1
                    screenshot(sb, f"no_btn_{click_count}")
                    if no_btn_streak == 1:
                        dump_html(sb, f"no_btn_page_{click_count}")
                        st2 = page_state(sb)
                        log.warning(f"页面状态: {json.dumps(st2, ensure_ascii=False)}")
                    log.warning(f"本次未找到续期入口（连续 {no_btn_streak}/5），刷新页面重试")
                    if no_btn_streak >= 5:
                        dump_html(sb, "abort_page")
                        st3 = page_state(sb)
                        if "/login" in st3.get("url", ""):
                            hint = "被重定向到登录页：cookie 注入失败或已失效"
                        elif st3.get("has_password") and st3.get("wire_count", 0) == 0:
                            hint = "页面显示登录表单，cookie 很可能已失效"
                        elif st3.get("wire_count", 0) == 0:
                            hint = "页面无 Livewire 组件：请确认 URL 指向 /server/{id} 详情页，或站点已改版"
                        else:
                            hint = "有 Livewire 组件但找不到续期按钮：站点选择器可能已改版，请查看 abort_page.html"
                        log.error(f"连续 5 次未找到续期入口，终止本轮。原因猜测: {hint}")
                        tg(f"❌ gaming4free 续期失败：连续 5 次未找到续期入口\n原因: {hint}")
                        return
                    sb.refresh()
                    sb.sleep(3)
                    last_sec = get_remaining_seconds(sb)
                    continue
                log.info("✅ Livewire extend 成功")
                no_btn_streak = 0

            # Step 4.2: 处理可能出现的 Turnstile
            human_sleep(1.0, 2.0)
            handle_turnstile(sb)

            # Step 4.3: 等待响应
            human_sleep(3.0, 5.0)
            sb.sleep(2)

            # Step 4.4: 对比时间（UI 倒计时 + 服务端 expiresTimestamp 双真值）
            new_sec = get_remaining_seconds(sb)
            new_ts = get_expires_ts(sb)
            delta = new_sec - last_sec
            log.info(f"点击 #{click_count+1}: {last_sec}s -> {new_sec}s (Delta={delta}s)")
            if old_ts and new_ts:
                log.info(f"expiresTimestamp: {old_ts} -> {new_ts}")

            ts_grew = (old_ts and new_ts and new_ts > old_ts)
            if new_sec > last_sec or ts_grew:
                click_count += 1
                fail_streak = 0
                log.info(f"续期成功 #{click_count} (+{new_sec - last_sec}s)" + (" [expiresTs确认]" if ts_grew else ""))
                screenshot(sb, f"success_{click_count}")
                last_sec = new_sec
            else:
                fail_streak += 1
                log.warning(f"续期可能失败，时间未增加（连续 {fail_streak}/3）")
                screenshot(sb, f"fail_{click_count}")
                capture_renew_response(sb)
                # 不 refresh —— 刷新会打断正在进行的广告续期流程（v5 机制）
                sb.sleep(3)
                last_sec = get_remaining_seconds(sb)
                # 连续未生效：主动走 Livewire 方法直调兜底，避免空转
                if fail_streak >= 2:
                    lw_result = livewire_extend(sb)
                    log.info(f"Livewire 兜底: {lw_result['results']} success={lw_result['success']}")
                    sb.sleep(5)
                    new_sec2 = get_remaining_seconds(sb)
                    new_ts2 = get_expires_ts(sb)
                    if new_sec2 > last_sec or (old_ts and new_ts2 and new_ts2 > old_ts):
                        log.info(f"Livewire 兜底成功: {last_sec}s -> {new_sec2}s")
                        click_count += 1
                        fail_streak = 0
                        last_sec = new_sec2
                if fail_streak >= 3:
                    log.error(f"连续 {fail_streak} 次续期未生效，终止本轮")
                    tg(f"❌ gaming4free 续期失败：连续 {fail_streak} 次点击未生效\n最终剩余: {fmt_hm(last_sec)}")
                    return
                click_count += 1

            # 冷却
            if last_sec >= (MAX_HOURS - 1) * 3600:
                break
            log.info(f"冷却 {COOLDOWN_SEC}s ...")
            for i in range(COOLDOWN_SEC, 0, -30):
                log.info(f"  剩 {i}s")
                time.sleep(30)

        # 收尾
        final_sec = get_remaining_seconds(sb)
        msg = (f"gaming4free 续期完成\n"
               f"成功点击: {click_count} 次\n"
               f"最终剩余: {fmt_hm(final_sec)}")
        log.info(msg)
        tg(msg)
        screenshot(sb, "final")


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("用户中断")
    except Exception as e:
        log.exception(f"未捕获异常: {e}")
        tg(f"❌ gaming4free 续期崩溃\n{e}")
        sys.exit(1)

