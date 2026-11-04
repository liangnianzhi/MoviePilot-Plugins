import json
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import yaml
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType

from .engine import SiteRunner, TemplateNotFound, load_template

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
DATA_DIR = "/config/plugins/QdSignin"
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
MAX_HISTORY = 200


class QdSignin(_PluginBase):
    # 插件名称
    plugin_name = "QD 签到"
    # 插件描述
    plugin_desc = "用账号密码登录站点并自动签到，签到逻辑由 YAML 模板驱动（goto/fill/click/wait/api 等步骤）。"
    # 插件图标
    plugin_icon = "check.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "liangnianzhi"
    # 作者主页
    author_url = "https://github.com/liangnianzhi"
    # 插件配置项ID前缀
    plugin_config_prefix = "qdsignin_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 1

    _enable: bool = False
    _notify: bool = True
    _onlyonce: bool = False
    _cron: str = "0 8 * * *"
    _headless: bool = True
    _humanize: bool = False
    _proxy: str = ""
    _timeout: int = 30
    _sites: str = ""

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enable = bool(config.get("enable"))
        self._notify = bool(config.get("notify", True))
        self._onlyonce = bool(config.get("onlyonce"))
        self._cron = (config.get("cron") or "0 8 * * *").strip()
        self._headless = bool(config.get("headless", True))
        self._humanize = bool(config.get("humanize"))
        self._proxy = (config.get("proxy") or "").strip()
        self._timeout = int(config.get("timeout") or 30)
        self._sites = config.get("sites") or ""
        os.makedirs(DATA_DIR, exist_ok=True)
        if self._onlyonce:
            logger.info("[QdSignin] 检测到「立即运行一次」，3 秒后开始执行")
            self._onlyonce = False
            self.__update_config()

    # ---------- 配置 ----------

    def __update_config(self):
        self.update_config({
            "enable": self._enable,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "headless": self._headless,
            "humanize": self._humanize,
            "proxy": self._proxy,
            "timeout": self._timeout,
            "sites": self._sites,
        })

    def _proxy_value(self) -> Optional[str]:
        if self._proxy:
            return self._proxy
        env_proxy = getattr(settings, "PROXY_HOST", None)
        return env_proxy or None

    def _site_list(self) -> List[Dict[str, Any]]:
        raw = (self._sites or "").strip()
        if not raw:
            return []
        try:
            data = yaml.safe_load(raw)
        except Exception as error:
            logger.error(f"[QdSignin] 站点配置解析失败：{error}")
            return []
        if isinstance(data, dict):
            data = data.get("sites") or []
        if not isinstance(data, list):
            logger.error("[QdSignin] 站点配置应为列表")
            return []
        sites = []
        for item in data:
            if not isinstance(item, dict):
                continue
            name = item.get("template") or item.get("site")
            if not name:
                logger.error(f"[QdSignin] 站点条目缺少 template：{item}")
                continue
            entry = dict(item)
            entry["template"] = str(name)
            entry.setdefault("site", entry["template"])
            entry.setdefault("enabled", True)
            sites.append(entry)
        return sites

    # ---------- 模板 ----------

    @staticmethod
    def list_templates() -> List[str]:
        if not os.path.isdir(TEMPLATE_DIR):
            return []
        return sorted(f[:-5] for f in os.listdir(TEMPLATE_DIR) if f.endswith((".yaml", ".yml")))

    def _template_path(self, name: str) -> str:
        for suffix in (".yaml", ".yml"):
            path = os.path.join(TEMPLATE_DIR, name + suffix)
            if os.path.exists(path):
                return path
        raise TemplateNotFound(f"模板 {name} 不存在，可用模板：{', '.join(self.list_templates())}")

    # ---------- 执行 ----------

    def _run_one(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        name = entry.get("site") or entry.get("template")
        started = time.time()
        try:
            template = load_template(self._template_path(entry["template"]))
            runner = SiteRunner(
                template=template,
                account=entry,
                options={
                    "headless": self._headless,
                    "humanize": self._humanize,
                    "proxy": self._proxy_value(),
                    "timeout": self._timeout,
                },
            )
            result = runner.run()
        except Exception as error:
            logger.error(f"[QdSignin] {name} 执行失败：{error}")
            result = {"ok": False, "status": "error", "message": f"{type(error).__name__}: {error}", "logs": []}
        result["site"] = name
        result["account"] = entry.get("username") or ""
        result["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        result["cost"] = result.get("cost") or round(time.time() - started, 1)
        logger.info(f"[QdSignin] {name}({result['account']}) 结果：{result['status']} {result['message']}")
        return result

    def checkin_all(self, only_site: Optional[str] = None) -> List[Dict[str, Any]]:
        sites = [s for s in self._site_list() if s.get("enabled", True)]
        if only_site:
            sites = [s for s in sites if (s.get("site") or s.get("template")) == only_site]
        if not sites:
            logger.warning("[QdSignin] 没有启用的站点，跳过")
            return []
        results = []
        for entry in sites:
            results.append(self._run_one(entry))
        self.__save_history(results)
        if self._notify:
            self.__notify(results)
        return results

    def __notify(self, results: List[Dict[str, Any]]):
        if not results:
            return
        ok = len([r for r in results if r.get("ok")])
        lines = [f"成功 {ok}/{len(results)}"]
        for r in results:
            icon = "✅" if r.get("ok") else "❌"
            lines.append(f"{icon} {r['site']}：{r['message']}")
        try:
            self.post_message(title="【QD 签到】", text="\n".join(lines))
        except Exception as error:
            logger.error(f"[QdSignin] 发送通知失败：{error}")

    def __save_history(self, results: List[Dict[str, Any]]):
        history = self.__load_history()
        for item in results:
            history.append({
                "time": item.get("time"),
                "site": item.get("site"),
                "account": item.get("account"),
                "status": item.get("status"),
                "message": item.get("message"),
                "cost": item.get("cost"),
            })
        history = history[-MAX_HISTORY:]
        try:
            with open(HISTORY_FILE, "w", encoding="utf-8") as fp:
                json.dump(history, fp, ensure_ascii=False, indent=2)
        except Exception as error:
            logger.error(f"[QdSignin] 写历史失败：{error}")

    def __load_history(self) -> List[Dict[str, Any]]:
        try:
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                return data if isinstance(data, list) else []
        except Exception:
            pass
        return []

    # ---------- 插件契约 ----------

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/qd_signin",
            "event": EventType.PluginAction,
            "desc": "QD 签到",
            "category": "签到",
            "data": {"action": "qd_signin"}
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/run",
            "endpoint": self.api_run,
            "methods": ["POST"],
            "auth": "bear",
            "summary": "立即执行签到",
            "description": "可传 site 指定单个站点，不传则执行全部启用站点",
        }, {
            "path": "/templates",
            "endpoint": self.api_templates,
            "methods": ["GET"],
            "auth": "bear",
            "summary": "列出可用模板",
        }, {
            "path": "/history",
            "endpoint": self.api_history,
            "methods": ["GET"],
            "auth": "bear",
            "summary": "最近签到记录",
        }]

    def api_run(self, site: Optional[str] = None) -> dict:
        results = self.checkin_all(only_site=site)
        if not results:
            return {"success": False, "message": "没有启用的站点"}
        lines = [f"{'✅' if r['ok'] else '❌'} {r['site']}：{r['message']}" for r in results]
        return {"success": all(r.get("ok") for r in results), "message": "\n".join(lines)}

    def api_templates(self) -> dict:
        return {"success": True, "message": ", ".join(self.list_templates()) or "无模板"}

    def api_history(self) -> dict:
        return {"success": True, "data": self.__load_history()[-30:]}

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enable:
            return []
        if self._onlyonce:
            return [{
                "id": "QdSigninOnce",
                "name": "QD 签到（立即运行一次）",
                "trigger": "date",
                "func": self.checkin_all,
                "kwargs": {"run_date": datetime.now() + timedelta(seconds=3)},
            }]
        if not self._cron or str(self._cron).strip().count(" ") != 4:
            logger.warning(f"[QdSignin] cron 表达式无效：{self._cron}")
            return []
        return [{
            "id": "QdSignin",
            "name": "QD 签到服务",
            "trigger": CronTrigger.from_crontab(self._cron.strip()),
            "func": self.checkin_all,
            "kwargs": {},
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        templates = self.list_templates()
        sample = "sites:\n"
        if templates:
            sample += f"  - site: {templates[0]}\n    template: {templates[0]}\n    username: 你的账号\n    password: 你的密码\n    enabled: true\n"
        else:
            sample += "  - site: example\n    template: example\n    username: 账号\n    password: 密码\n    enabled: true\n"
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VSwitch", "props": {"model": "enable", "label": "启用插件"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VSwitch", "props": {"model": "notify", "label": "发送通知"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VSwitch", "props": {"model": "headless", "label": "无头模式"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VSwitch", "props": {"model": "humanize", "label": "拟人化操作"}}]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VTextField", "props": {"model": "cron", "label": "执行周期（cron）", "placeholder": "0 8 * * *"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VTextField", "props": {"model": "proxy", "label": "代理（留空用系统代理）", "placeholder": "http://mihomo:7890"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 2},
                                "content": [{"component": "VTextField", "props": {"model": "timeout", "label": "超时（秒）", "type": "number"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 2},
                                "content": [{"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即运行一次"}}]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VTextarea",
                                    "props": {
                                        "model": "sites",
                                        "label": "站点账号配置（YAML）",
                                        "rows": 10,
                                        "placeholder": sample,
                                    }
                                }]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VBtn",
                                    "props": {
                                        "color": "primary",
                                        "variant": "flat",
                                        "block": True,
                                        "prepend-icon": "mdi-play",
                                        "onclick": "function(e) { if (!confirm('立即执行全部启用站点的签到？')) return; "
                                                   "window.MoviePilotAPI.post('plugin/QdSignin/run', {}).then(function(r) { "
                                                   "alert(r && r.message ? r.message : '已触发') })"
                                                   ".catch(function(err) { console.error(err); alert('执行失败，请查看日志') }) }",
                                    }
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [{
                                    "component": "VAlert",
                                    "props": {
                                        "type": "info",
                                        "variant": "tonal",
                                        "text": f"可用模板：{', '.join(templates) or '（templates 目录为空）'}"
                                    }
                                }]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enable": self._enable,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "headless": self._headless,
            "humanize": self._humanize,
            "proxy": self._proxy,
            "timeout": self._timeout,
            "sites": self._sites or sample,
        }

    def get_page(self) -> List[dict]:
        history = list(reversed(self.__load_history()))[:15]
        rows = []
        for item in history:
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "text": str(item.get("time") or "")},
                    {"component": "td", "text": str(item.get("site") or "")},
                    {"component": "td", "text": str(item.get("account") or "")},
                    {"component": "td", "text": str(item.get("status") or "")},
                    {"component": "td", "text": str(item.get("message") or "")},
                ]
            })
        if not rows:
            rows = [{"component": "tr", "content": [{"component": "td", "props": {"colspan": 5}, "text": "暂无记录"}]}]
        return [{
            "component": "VCard",
            "props": {"variant": "tonal"},
            "content": [
                {"component": "VCardTitle", "text": "最近签到记录"},
                {"component": "VCardText", "content": [{
                    "component": "VTable",
                    "props": {"dense": True},
                    "content": [
                        {"component": "thead", "content": [{"component": "tr", "content": [
                            {"component": "th", "text": "时间"},
                            {"component": "th", "text": "站点"},
                            {"component": "th", "text": "账号"},
                            {"component": "th", "text": "状态"},
                            {"component": "th", "text": "说明"},
                        ]}]},
                        {"component": "tbody", "content": rows},
                    ]
                }]}
            ]
        }]

    def get_state(self) -> bool:
        return self._enable

    def stop_service(self):
        pass
