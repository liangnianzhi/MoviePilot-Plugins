"""
QdSignin 签到模板引擎

模板（YAML）结构示例：

    id: hdhaven
    name: 栖影 HDHaven
    url: https://hdhaven.com
    login:
      - goto: /login
      - fill: {selector: "#username", value: "{username}"}
      - fill: {selector: "#password", value: "{password}"}
      - click: "button[type=submit]"
      - wait: {selector: "a[href*='logout']", timeout: 30}
    checkin:
      - goto: /me
      - api: {method: GET, url: /api/auth/me, save: me}
      - assert: "1 === 1"
    success_keywords: ["签到成功"]
    already_keywords: ["今日已签到"]

动作一览（每个步骤是一个单键字典，键即动作名）：
    goto      打开页面        goto: /login  或  {url: /login, wait_until: networkidle}
    fill      填写输入框      {selector: "#username", value: "{username}"}
    click     点击元素        "button[type=submit]"  或  {selector: ..., timeout: 10}
    press     按键            {selector: "#password", key: Enter}
    wait      等待            {selector: ...} / {text: ...} / {url: ...} / {ms: 2000}
    sleep     固定等待秒数    sleep: 2
    text      读取文本        "body"
    js / eval 执行 JS         {script: "...", save: name}
    api       页面内 fetch    {method: POST, url: /api/xx, body: {...}, headers: {...}, ok_status: [200], save: name}
    expect    断言存在        {selector: ...} 或 {text: ...}
    assert    JS 断言         "document.title.includes('首页')"
    screenshot 截图           "name"
    reload    重新加载当前页
    back      后退
    turnstile 等人机验证        {sitekey: "0x...", timeout: 60, save: ts}

通用修饰键：
    when      仅当条件为真才执行（Python 表达式，可用账号字段，如 when: "sign_type == 'gamble'"）
    optional  该步骤失败不中断（true/false）

占位符：{url} {username} {password} 以及账号配置里的任意字段。
"""

import json
import os
import re
import time
from typing import Any, Dict, List, Optional

import yaml

from app.log import logger

PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

SHOT_DIR = "/config/plugins/QdSignin/shots"

NET_ERRORS = (
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_ABORTED",
    "ERR_TIMED_OUT",
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_EMPTY_RESPONSE",
    "ERR_SOCKET_NOT_CONNECTED",
    "ERR_NETWORK_CHANGED",
)

TS_POLL_JS = """() => {
  const nodes = Array.from(document.querySelectorAll('input[name*="turnstile"], textarea[name*="turnstile"]'));
  for (const node of nodes) { if (node.value && node.value.length > 20) return node.value; }
  if (window.__qdTsToken && window.__qdTsToken.length > 20) return window.__qdTsToken;
  try {
    if (window.turnstile && window.turnstile.getResponse) {
      const token = window.turnstile.getResponse();
      if (token && token.length > 20) return token;
    }
  } catch (e) {}
  return '';
}"""

TS_RENDER_JS = """(sitekey) => {
  if (!window.turnstile || !window.turnstile.render) return 'no-api';
  if (window.__qdTsRendered) return 'already';
  const box = document.createElement('div');
  box.id = '__qd_turnstile';
  box.style.cssText = 'position:fixed;left:-9999px;top:0;width:300px;height:80px;';
  document.body.appendChild(box);
  window.__qdTsToken = '';
  try {
    window.turnstile.render('#__qd_turnstile', {sitekey: sitekey, callback: (token) => { window.__qdTsToken = token; }});
    window.__qdTsRendered = true;
    return 'rendered';
  } catch (e) { return 'err:' + e.message; }
}"""

CF_MARKERS = (
    "Just a moment",
    "Enable JavaScript and cookies to continue",
    "challenges.cloudflare.com",
    "cf-browser-verification",
    "Attention Required",
)


class StepError(Exception):
    """步骤执行失败"""


class TemplateNotFound(Exception):
    """模板文件缺失"""


def render(value: Any, ctx: Dict[str, Any]) -> Any:
    """递归替换 {占位符}；未知占位符原样保留"""
    if isinstance(value, str):
        def _sub(match) -> str:
            key = match.group(1)
            return str(ctx[key]) if key in ctx else match.group(0)

        return PLACEHOLDER.sub(_sub, value)
    if isinstance(value, list):
        return [render(item, ctx) for item in value]
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    return value


def load_template(path: str) -> Dict[str, Any]:
    """读取模板 YAML"""
    if not os.path.exists(path):
        raise TemplateNotFound(f"模板不存在：{path}")
    with open(path, "r", encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}
    if not isinstance(data, dict):
        raise StepError(f"模板格式错误（顶层应为字典）：{path}")
    if not data.get("url"):
        raise StepError(f"模板缺少 url 字段：{path}")
    return data


class SiteRunner:
    """执行单个站点的一次签到"""

    def __init__(self, template: Dict[str, Any], account: Dict[str, Any],
                 options: Dict[str, Any]):
        self.tpl = template
        self.account = account
        self.opt = options or {}
        self.context: Any = None
        self.page: Any = None
        self.logs: List[str] = []
        self.texts: List[str] = []
        self.vars: Dict[str, Any] = {}
        self.api_results: List[Dict[str, Any]] = []

    # ---------- 基础 ----------

    def ctx(self) -> Dict[str, Any]:
        ctx = {
            "url": str(self.tpl.get("url") or "").rstrip("/"),
            "username": str(self.account.get("username") or ""),
            "password": str(self.account.get("password") or ""),
        }
        for key, value in self.account.items():
            ctx.setdefault(key, value)
        return ctx

    def absolute(self, path: str) -> str:
        if not path:
            return self.ctx()["url"]
        if path.startswith("http://") or path.startswith("https://"):
            return path
        base = self.ctx()["url"]
        return base + ("" if path.startswith("/") else "/") + path

    def log(self, message: str) -> None:
        self.logs.append(message)
        logger.info(f"[QdSignin] {message}")

    def _when(self, spec: Any) -> bool:
        if not isinstance(spec, dict):
            return True
        expr = spec.get("when")
        if not expr:
            return True
        try:
            allowed = {**self.ctx(), **self.vars}
            return bool(eval(str(expr), {"__builtins__": {}}, allowed))
        except Exception as error:
            logger.warning(f"[QdSignin] when 条件无法求值（{expr}）：{error}")
            return True

    def _settle_cf(self, timeout_s: float = 30) -> None:
        """检测到 Cloudflare 挑战页时等待其自动通过"""
        deadline = time.time() + max(timeout_s, 0)
        warned = False
        while time.time() < deadline:
            try:
                html = self.page.content()
            except Exception:
                return
            if not any(marker in html for marker in CF_MARKERS):
                return
            if not warned:
                self.log(f"检测到 Cloudflare 挑战，等待自动通过（最多 {int(timeout_s)} 秒）")
                warned = True
            self.page.wait_for_timeout(1500)
        if warned:
            self.log("Cloudflare 挑战等待超时，继续执行")

    # ---------- 动作 ----------

    def act_goto(self, spec: Any) -> None:
        url = spec if isinstance(spec, str) else spec.get("url", "")
        wait_until = "domcontentloaded" if isinstance(spec, str) else spec.get("wait_until", "domcontentloaded")
        default_retry = self.opt.get("goto_retry", 3)
        retry = int(float(default_retry if isinstance(spec, str) else spec.get("retry", default_retry)))
        retry = max(retry, 1)
        target = self.absolute(render(url, self.ctx()))
        self.log(f"打开 {target}")
        last_error: Optional[Exception] = None
        for attempt in range(1, retry + 1):
            try:
                self.page.goto(target, wait_until=wait_until)
                last_error = None
                break
            except Exception as error:
                last_error = error
                text = str(error).strip()
                first_line = text.splitlines()[0] if text else type(error).__name__
                transient = any(marker in text for marker in NET_ERRORS)
                if attempt >= retry or not transient:
                    break
                self.log(f"第 {attempt}/{retry} 次打开失败：{first_line}，3 秒后重试")
                self.page.wait_for_timeout(3000)
        if last_error is not None:
            text = str(last_error).strip()
            first_line = text.splitlines()[0] if text else type(last_error).__name__
            raise StepError(
                f"打开 {target} 失败：{first_line}"
                "（连接被断开/超时类错误通常是代理节点不可用，可更换节点或稍后重试）"
            )
        if not (isinstance(spec, dict) and spec.get("no_cf")):
            self._settle_cf(float(self.opt.get("cf_timeout", 30)))

    def act_reload(self, spec: Any) -> None:
        self.page.reload()
        self._settle_cf(float(self.opt.get("cf_timeout", 30)))

    def act_back(self, spec: Any) -> None:
        self.page.go_back()

    def act_fill(self, spec: Any) -> None:
        items = spec if isinstance(spec, list) else [spec]
        for item in items:
            selector = render(item.get("selector", ""), self.ctx())
            value = render(item.get("value", ""), self.ctx())
            self.log(f"填写 {selector}")
            self.page.fill(selector, str(value))

    def act_click(self, spec: Any) -> None:
        if isinstance(spec, str):
            selector, timeout = spec, None
        else:
            selector, timeout = spec.get("selector", ""), spec.get("timeout")
        selector = render(selector, self.ctx())
        self.log(f"点击 {selector}")
        if timeout:
            self.page.click(selector, timeout=int(float(timeout) * 1000))
        else:
            self.page.click(selector)

    def act_press(self, spec: Any) -> None:
        selector = render(spec.get("selector", "body"), self.ctx())
        self.page.press(selector, spec.get("key", "Enter"))

    def act_wait(self, spec: Any) -> None:
        spec = spec if isinstance(spec, dict) else {"selector": str(spec)}
        timeout_ms = int(float(spec.get("timeout", self.opt.get("timeout", 30))) * 1000)
        if spec.get("ms"):
            self.log(f"等待 {spec['ms']} 毫秒")
            self.page.wait_for_timeout(int(spec["ms"]))
        elif spec.get("selector"):
            selector = render(spec["selector"], self.ctx())
            self.log(f"等待元素 {selector}")
            self.page.wait_for_selector(selector, timeout=timeout_ms)
        elif spec.get("text"):
            text = render(spec["text"], self.ctx())
            self.log(f"等待文本 {text}")
            self.page.get_by_text(text, exact=False).first.wait_for(timeout=timeout_ms)
        elif spec.get("url"):
            pattern = render(spec["url"], self.ctx())
            self.log(f"等待跳转 {pattern}")
            self.page.wait_for_url(pattern, timeout=timeout_ms)
        else:
            self.page.wait_for_timeout(timeout_ms)

    def act_sleep(self, spec: Any) -> None:
        seconds = float(spec)
        self.log(f"休眠 {seconds} 秒")
        self.page.wait_for_timeout(int(seconds * 1000))

    def act_text(self, spec: Any) -> None:
        selector = spec if isinstance(spec, str) else spec.get("selector", "body")
        selector = render(selector, self.ctx())
        try:
            content = self.page.inner_text(selector, timeout=8000)
        except Exception:
            content = self.page.inner_text("body", timeout=8000)
        self.texts.append(content or "")

    def act_eval(self, spec: Any) -> None:
        self._run_js(spec)

    def act_js(self, spec: Any) -> None:
        self._run_js(spec)

    def _run_js(self, spec: Any) -> None:
        script = spec if isinstance(spec, str) else spec.get("script", "")
        save_as = None if isinstance(spec, str) else spec.get("save")
        script = render(script, self.ctx())
        result = self.page.evaluate(script)
        if result is not None:
            self.texts.append(json.dumps(result, ensure_ascii=False))
            self.log(f"js 结果 {str(result)[:200]}")
        if save_as:
            self.vars[save_as] = result

    def act_assert(self, spec: Any) -> None:
        if isinstance(spec, dict):
            script = spec.get("script") or spec.get("expr") or ""
            msg = str(spec.get("msg") or "")
        else:
            script = spec or ""
            msg = ""
        script = render(script, self.ctx())
        result = self.page.evaluate("() => Boolean(" + str(script) + ")")
        if not result:
            try:
                body = " ".join((self.page.inner_text("body") or "").split())[:160]
                detail = f"（当前页面：{self.page.url}；页面提示：{body}）"
            except Exception:
                detail = ""
            raise StepError((msg or f"断言不通过：{script}") + detail)
        self.log("断言通过")

    def act_turnstile(self, spec: Any) -> None:
        """等待 Cloudflare Turnstile 人机验证通过并取回 token"""
        spec = spec if isinstance(spec, dict) else {"sitekey": str(spec or "")}
        timeout_s = float(spec.get("timeout", self.opt.get("turnstile_timeout", 60)))
        sitekey = render(spec.get("sitekey") or "", self.ctx())
        save_as = str(spec.get("save") or "turnstile_token")
        deadline = time.time() + max(timeout_s, 1.0)
        token = ""
        rendered = False
        while time.time() < deadline:
            try:
                token = self.page.evaluate(TS_POLL_JS) or ""
            except Exception:
                token = ""
            if token:
                break
            if sitekey and not rendered:
                rendered = True
                try:
                    state = self.page.evaluate(TS_RENDER_JS, sitekey)
                    self.log(f"人机验证控件渲染：{state}")
                except Exception as error:
                    self.log(f"人机验证控件渲染异常：{error}")
            self.page.wait_for_timeout(1000)
        if not token:
            raise StepError(
                f"人机验证未通过（等待 {int(timeout_s)} 秒未取到 Turnstile token）。"
                "容器内交互式验证无法自动完成，请在该账号配置里改用 cookie 方式"
            )
        self.vars[save_as] = token
        self.log(f"人机验证通过，token 长度 {len(token)}")

    def act_api(self, spec: Any) -> None:
        """在页面内发 fetch，复用当前登录态与浏览器指纹"""
        method = str(spec.get("method") or "GET").upper()
        url = self.absolute(render(spec.get("url", ""), self.ctx()))
        body = render(spec.get("body"), self.ctx())
        headers = render(spec.get("headers") or {}, self.ctx())
        ok_status = spec.get("ok_status") or []
        save_as = spec.get("save")
        payload = {
            "method": method,
            "url": url,
            "body": None if body is None else (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)),
            "headers": headers,
        }
        self.log(f"调用接口 {method} {url}")
        result = self._fetch(payload)
        if result.get("ok") and self._looks_like_cf(str(result.get("text") or "")):
            self.log("接口返回 Cloudflare 挑战页，尝试恢复后重试")
            self._settle_cf(float(self.opt.get("cf_timeout", 30)))
            result = self._fetch(payload)
        self.api_results.append({"url": url, "method": method, **result})
        text = str(result.get("text") or "")
        self.texts.append(text)
        parsed = None
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if save_as:
            self.vars[save_as] = parsed if parsed is not None else text
        self.log(f"接口返回 HTTP {result.get('status')}")
        if ok_status and result.get("status") not in [int(s) for s in ok_status]:
            raise StepError(f"接口状态异常 HTTP {result.get('status')}：{text[:200]}")
        if spec.get("require_json") and parsed is None:
            raise StepError(f"接口未返回 JSON：{text[:200]}")

    def _fetch(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.page.evaluate(
            """async (p) => {
                const init = {method: p.method, headers: p.headers || {}, credentials: 'include'};
                if (p.body !== null && p.method !== 'GET') { init.body = p.body; }
                try {
                    const r = await fetch(p.url, init);
                    const t = await r.text();
                    return {ok: true, status: r.status, text: t};
                } catch (e) {
                    return {ok: false, status: 0, text: String(e)};
                }
            }""",
            payload,
        )

    @staticmethod
    def _looks_like_cf(text: str) -> bool:
        if not text:
            return False
        head = text.lstrip()[:200].lower()
        if not (head.startswith("<") or head.startswith("<!doctype")):
            return False
        return any(marker.lower() in text.lower() for marker in CF_MARKERS)

    def act_screenshot(self, spec: Any) -> None:
        name = render(str(spec or "shot"), self.ctx())
        os.makedirs(SHOT_DIR, exist_ok=True)
        path = os.path.join(SHOT_DIR, f"{name}-{int(time.time())}.png")
        self.page.screenshot(path=path, full_page=True)
        self.log(f"截图 {path}")

    def act_expect(self, spec: Any) -> None:
        spec = spec if isinstance(spec, dict) else {"selector": str(spec)}
        timeout_ms = int(float(spec.get("timeout", self.opt.get("timeout", 30))) * 1000)
        if spec.get("selector"):
            selector = render(spec["selector"], self.ctx())
            if not self.page.query_selector(selector):
                self.page.wait_for_selector(selector, timeout=timeout_ms)
            self.log(f"断言元素存在 {selector}")
            return
        if spec.get("text"):
            text = render(spec["text"], self.ctx())
            if text not in (self.page.inner_text("body") or ""):
                raise StepError(f"页面未出现预期文本：{text}")
            self.log(f"断言文本存在 {text}")
            return
        raise StepError("expect 需要 selector 或 text")

    # ---------- 执行 ----------

    def _inject_cookies(self, text: str) -> None:
        """把 k=v; k2=v2 形式的 cookie 串注入当前上下文（跳过登录步骤时用）"""
        from urllib.parse import urlparse

        host = urlparse(str(self.tpl.get("url") or "")).hostname or ""
        domain = "." + host if host else ""
        items = []
        for part in text.replace(chr(10), ";").split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip()
            if not key:
                continue
            items.append({"name": key, "value": value.strip(), "domain": domain, "path": "/"})
        if items:
            self.context.add_cookies(items)
            self.log(f"注入 cookie {len(items)} 条")

    def run_steps(self, steps: Optional[List[Any]], phase: str) -> None:
        if not steps:
            return
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                raise StepError(f"{phase} 第 {index} 步格式错误（应为字典）：{step}")
            if not self._when(step):
                self.log(f"{phase} 第 {index} 步：条件不满足，跳过")
                continue
            optional = bool(step.get("optional"))
            for action, spec in step.items():
                if action in ("when", "optional"):
                    continue
                handler = getattr(self, "act_" + action, None)
                if not handler:
                    raise StepError(f"未知动作：{action}（{phase} 第 {index} 步）")
                self.log(f"{phase} 第 {index} 步：{action}")
                try:
                    handler(spec)
                except StepError:
                    raise
                except Exception as error:
                    if optional:
                        self.log(f"{phase} 第 {index} 步失败但已忽略：{error}")
                        continue
                    raise StepError(f"{phase} 第 {index} 步 {action} 失败：{type(error).__name__}: {error}") from error

    def _eval_flag(self, expr: Any) -> bool:
        """求值模板里的 *_if 判定表达式（Python，可引用账号字段与 api 保存的变量）"""
        if not expr:
            return False
        try:
            return bool(eval(str(expr), {"__builtins__": {}}, {**self.ctx(), **self.vars}))
        except Exception as error:
            logger.warning(f"[QdSignin] 判定表达式无法求值（{expr}）：{error}")
            return False

    def judge(self) -> Dict[str, Any]:
        corpus = "\n".join(self.texts)
        try:
            corpus += "\n" + (self.page.inner_text("body") or "")
        except Exception:
            pass
        if self._eval_flag(self.tpl.get("fail_if")):
            return {"ok": False, "status": "fail", "message": "命中失败判定（fail_if）"}
        if self._eval_flag(self.tpl.get("already_if")):
            return {"ok": True, "status": "already", "message": "判定为今日已签到（already_if）"}
        if self._eval_flag(self.tpl.get("success_if")):
            return {"ok": True, "status": "success", "message": "判定为签到成功（success_if）"}
        already = [kw for kw in (self.tpl.get("already_keywords") or []) if kw and kw in corpus]
        success = [kw for kw in (self.tpl.get("success_keywords") or []) if kw and kw in corpus]
        failed = [kw for kw in (self.tpl.get("fail_keywords") or []) if kw and kw in corpus]
        if failed and not success:
            return {"ok": False, "status": "fail", "message": f"命中失败关键词：{'/'.join(failed)}"}
        if already:
            return {"ok": True, "status": "already", "message": f"已签到：{'/'.join(already)}"}
        if success:
            return {"ok": True, "status": "success", "message": f"签到成功：{'/'.join(success)}"}
        return {"ok": False, "status": "unknown", "message": "未匹配到成功/已签到关键词，请检查模板关键词"}

    def run(self) -> Dict[str, Any]:
        from app.adapters.network.browser import launch_browser_context

        started = time.time()
        headless = bool(self.opt.get("headless", True))
        proxy = self.opt.get("proxy") or None
        if proxy and "://" not in str(proxy):
            proxy = "http://" + str(proxy)
        timeout_ms = int(float(self.opt.get("timeout", 30)) * 1000)
        try:
            self.context = launch_browser_context(
                headless=headless,
                proxy=proxy,
                locale="zh-CN",
                timezone="Asia/Shanghai",
                humanize=bool(self.opt.get("humanize", False)),
            )
            self.context.set_default_timeout(timeout_ms)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.set_default_timeout(timeout_ms)
            self.log(f"浏览器就绪 headless={headless} proxy={bool(proxy)}")
            cookie_text = str(self.account.get("cookie") or self.account.get("cookies") or "").strip()
            if cookie_text:
                self._inject_cookies(cookie_text)
            self.run_steps(self.tpl.get("login"), "登录")
            self.run_steps(self.tpl.get("checkin"), "签到")
            verdict = self.judge()
        except Exception as error:
            logger.error(f"[QdSignin] 执行异常：{error}")
            verdict = {"ok": False, "status": "error", "message": f"{type(error).__name__}: {error}"}
        finally:
            try:
                if self.context:
                    self.context.close()
            except Exception:
                pass
        verdict["cost"] = round(time.time() - started, 1)
        verdict["logs"] = self.logs
        verdict["api"] = self.api_results
        return verdict
