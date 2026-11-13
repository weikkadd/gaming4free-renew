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
MAX_CLICKS     = 80            # 单次运行最大投票次数
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
            json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"},
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
    """使用 Livewire JavaScript 直接调用 extend 方法，返回结果"""
    from util import _LW_EXTEND_V3_JS, _LW_V2_JS, _LW_CLICK_JS
    
    results = []
    
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
    
    # 只认真正的 Livewire extend 调用（call_extend/direct_extend/emit_extend），
    # 不认按钮点击结果——_LW_CLICK_JS 可能点中非续期元素（如 <html>）造成假成功
    return {"results": results, "success": any(
        ("call_extend" in r.lower() or "direct_extend" in r.lower() or "emit_extend" in r.lower())
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
                            window.__lwResponses.push({type:'fetch', url:String(url).substring(0,120), status:res.status, body:t.substring(0,3000)});
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
                    try { window.__lwResponses.push({type:'xhr', url:String(xhr.__lwUrl).substring(0,120), status:xhr.status, body:String(xhr.responseText||'').substring(0,3000)}); } catch(e) {}
                });
            }
            return origSend.apply(this, arguments);
        };
        return 'installed';
    } catch(e) { return 'error:' + e.message; }
})();
"""


def capture_renew_response(sb):
    """续期点击未生效时：抓取浏览器对话框/新标签页/页面提示/Livewire 响应，定位真实原因"""
    # 1) 未处理的 alert/confirm 对话框
    try:
        alert = sb.driver.switch_to.alert
        log.warning(f"存在浏览器对话框: {alert.text}")
        alert.accept()
    except Exception:
        pass

    # 2) 新标签页（投票/广告流可能在新的标签页打开）
    try:
        handles = sb.driver.window_handles
        if len(handles) > 1:
            for h in handles[1:]:
                sb.driver.switch_to.window(h)
                log.warning(f"点击后打开了新标签页: url={sb.driver.current_url} title={sb.driver.title}")
                screenshot(sb, "new_tab")
            sb.driver.switch_to.window(handles[0])
    except Exception as e:
        log.warning(f"标签页检查失败: {e}")

    # 3) 页面 toast / 提示条
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

    # 4) Livewire 请求的真实响应（点击前已安装 hook）
    try:
        responses = sb.execute_script("return JSON.stringify((window.__lwResponses || []).slice(0, 3))")
        if responses and responses != "[]":
            log.warning(f"Livewire 响应捕获: {str(responses)[:1500]}")
    except Exception:
        pass


def click_renew_button(sb) -> bool:
    """gaming4free 续期：点击 console 页 '+ 90 min' 免费按钮（'+24h $x' 付费按钮不碰）"""
    # 安装 Livewire 请求捕获 hook（失败不影响点击）
    try:
        sb.execute_script(_LW_HOOK_JS)
    except Exception:
        pass
    try:
        result = sb.execute_script("""
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
            el.scrollIntoView({block: 'center'});
            el.click();
            var wireAttr = el.getAttribute('wire:click') || el.getAttribute('wire:click.prevent') || 'none';
            return 'clicked:' + t.substring(0, 40) + '|tag=' + el.tagName + '|wire=' + wireAttr;
        }
        return 'not_found';
        })();
        """)
        if not result or not str(result).startswith("clicked:"):
            log.warning(f"+90min 续期按钮未找到: {result}")
            return False
        log.info(f"✅ 点击续期按钮: {result}")
    except Exception as e:
        log.warning(f"点击续期按钮异常: {e}")
        return False

    time.sleep(4)

    # 点击后：若出现确认弹窗/Turnstile 则等待通过并提交；无弹窗则视为点击即续期
    no_modal_rounds = 0
    token_ok_rounds = 0
    ts_rendered = False
    for wait_round in range(24):                       # 最多约 60s
        try:
            state = str(sb.execute_script("""
        (function(){
        function visible(el){ var r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }
        var nodes = document.querySelectorAll('.vm-captcha, .cf-turnstile, [role=dialog], .modal.show, .modal[style*="block"], .popup');
        var modal = null;
        for (var k = 0; k < nodes.length; k++) { if (visible(nodes[k])) { modal = nodes[k]; break; } }
        var tsIframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
        if (!modal && !tsIframe) return 'no_modal';

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
            log.warning(f"弹窗检测异常: {e}")
            state = 'no_modal'

        if state.startswith("clicked:enable_ads"):
            log.info("已点击 enable ads，继续等待确认...")
        elif state.startswith("clicked:"):
            log.info(f"✅ 续期确认: {state}")
            return True
        elif state == "no_modal":
            no_modal_rounds += 1
            token_ok_rounds = 0
            if no_modal_rounds >= 3:
                log.info("无确认弹窗，点击即完成续期")
                return True
        elif state == "waiting:token_ok":
            token_ok_rounds += 1
            no_modal_rounds = 0
            if token_ok_rounds >= 2:
                log.info("Turnstile 已通过，等待自动提交")
                return True
        else:
            no_modal_rounds = 0
            token_ok_rounds = 0
            if wait_round % 4 == 0:
                log.info(f"等待验证码/弹窗... ({state}, {wait_round+1}/24)")
            # Turnstile 长时间未出 token：诊断 + 尝试手动渲染（每次点击只试一次）
            if not ts_rendered and state == "waiting:token_missing" and wait_round >= 8:
                ts_rendered = True
                try_render_turnstile(sb)
        time.sleep(2.5)

    screenshot(sb, "renew_no_confirm")
    log.warning("点击 +90min 后 60s 内未完成确认流程")
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
        log.info("未配置 MC_USERNAME，跳过登录")
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
    log.info(f"MC 用户:  {USERNAME or '(未配置)'}")
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

            # Step 4.1: 站点真实续期机制 = console 页 "+ 90 min" 免费按钮，优先走 click_renew_button；
            # 仅在其失败时才退回 Livewire extend（避免旧 Livewire 逻辑假成功跳过硬续期）
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

            # Step 4.4: 对比时间
            new_sec = get_remaining_seconds(sb)
            delta = new_sec - last_sec
            log.info(f"点击 #{click_count+1}: {last_sec}s -> {new_sec}s (Delta={delta}s)")

            if new_sec > last_sec:
                click_count += 1
                log.info(f"续期成功 (累计 {click_count} 次)")
                screenshot(sb, f"success_{click_count}")
                last_sec = new_sec
            else:
                log.warning("续期可能失败，时间未增加")
                screenshot(sb, f"fail_{click_count}")
                capture_renew_response(sb)
                sb.refresh()
                sb.sleep(3)
                last_sec = get_remaining_seconds(sb)
                click_count += 1

            # 冷却
            if last_sec >= (MAX_HOURS - 1) * 3600:
                break
            log.info(f"冷却 {COOLDOWN_SEC}s ...")
            for i in range(COOLDOWN_SEC, 0, -10):
                log.info(f"  剩 {i}s")
                time.sleep(10)

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

