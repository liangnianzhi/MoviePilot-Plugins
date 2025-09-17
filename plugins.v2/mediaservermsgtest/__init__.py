# 导入必要的Python标准库和类型注释
import time  # 用于时间相关操作，如获取当前时间、格式化时间等
import subprocess  # 用于执行系统命令（curl）
import json  # 用于处理JSON数据
import os  # 用于处理文件路径
from typing import Any, List, Dict, Tuple, Optional  # 类型注释，提高代码可读性和IDE支持

# 导入应用内部模块
from app.core.event import eventmanager, Event  # 事件管理器，用于注册和处理事件
from app.helper.mediaserver import MediaServerHelper  # 媒体服务器助手类，管理媒体服务器连接
from app.log import logger  # 日志记录器，用于输出日志信息
from app.plugins import _PluginBase  # 插件基类，所有插件都需要继承这个类
from app.schemas import WebhookEventInfo, ServiceInfo  # 数据结构定义，Webhook事件信息和服务信息
from app.schemas.types import EventType, MediaType, MediaImageType, NotificationType  # 枚举类型定义
from app.utils.web import WebUtils  # Web工具类，提供网络相关功能


class MediaServerMsgTest(_PluginBase):
    """媒体服务器消息通知插件类，继承自_PluginBase基类"""
    
    # ===== 插件基本信息配置 =====
    plugin_name = "媒体库服务器通知测试版"  # 插件在界面上显示的名称
    plugin_desc = "发送Emby/Jellyfin/Plex服务器的播放、入库等通知消息。新入库时联动TMM命令更新刮削下载缺失图片"  # 插件功能描述
    plugin_icon = "mediaplay.png"  # 插件图标文件名
    plugin_version = "1.0"  # 插件版本号
    plugin_author = "liangnianzhi"  # 插件作者
    author_url = "https://github.com/liangnianzhi"  # 作者主页链接
    plugin_config_prefix = "mediaservermsgtest_"  # 插件配置项在数据库中的前缀
    plugin_order = 14  # 插件加载顺序，数字越小越先加载
    auth_level = 1  # 插件的权限级别，1表示普通用户可使用

    # ===== 私有属性定义 =====
    _enabled = False  # 插件是否启用的标志
    _add_play_link = False  # 是否在通知中添加播放链接的标志
    _mediaservers = None  # 配置的媒体服务器列表
    _types = []  # 用户选择的消息通知类型列表
    _webhook_msg_keys = {}  # 用于存储临时消息键值，防止重复通知

    # ===== 静态配置：Webhook动作映射 =====
    # 将服务器发送的事件名称映射为中文描述
    _webhook_actions = {
        "library.new": "新入库",  # 媒体库有新内容入库
        "system.webhooktest": "测试",  # Webhook测试事件
        "playback.start": "开始播放",  # Emby/Jellyfin播放开始事件
        "playback.stop": "停止播放",  # Emby/Jellyfin播放停止事件
        "user.authenticated": "登录成功",  # 用户登录成功事件
        "user.authenticationfailed": "登录失败",  # 用户登录失败事件
        "media.play": "开始播放",  # Plex播放开始事件
        "media.stop": "停止播放",  # Plex播放停止事件
        "PlaybackStart": "开始播放",  # 通用播放开始事件
        "PlaybackStop": "停止播放",  # 通用播放停止事件
        "item.rate": "标记了"  # 用户对媒体内容进行评分/标记
    }
    
    # ===== 静态配置：媒体路径到变量一的映射 =====
    # 根据媒体文件路径确定变量一的值，按优先级顺序排列
    _path_mapping = [
        # 动漫类路径 (0-2)
        (["/media/Movie/Episode/Donghua", "/media/Show/Episode/Donghua"], 0),
        (["/media/Movie/Episode/Anime", "/media/Show/Episode/Anime"], 1),
        (["/media/Movie/Episode/Animation", "/media/Show/Episode/Animation"], 2),
        
        # 纪录片路径 (3)
        (["/media/Movie/Documentary", "/media/Show/Documentary"], 3),
        
        # 真人系列路径 (4-8)
        (["/media/Movie/Series/SeriesRU", "/media/Show/Series/SeriesRU"], 4),
        (["/media/Movie/Series/SeriesCN", "/media/Show/Series/SeriesCN"], 5),
        (["/media/Movie/Series/SeriesKO", "/media/Show/Series/SeriesKO"], 6),
        (["/media/Movie/Series/SeriesJP", "/media/Show/Series/SeriesJP"], 7),
        (["/media/Movie/Series/SeriesIN", "/media/Show/Series/SeriesUS"], 8),
        
        # 美国系列和综艺国语 (9)
        (["/media/Movie/Series/SeriesUS", "/media/Show/Zongyi/ZongyiCN"], 9),
        
        # NSFW和综艺韩语 (10)
        (["/media/Movie/Series/NSFW", "/media/Show/Zongyi/ZongyiKO"], 10),
        
        # 综艺美国 (11)
        (["/media/Show/Zongyi/ZongyiUS"], 11),
        
        # NSFW剧集 (12)
        (["/media/Show/Episode/NSFW"], 12)
    ]

    # ===== 静态配置：服务器图标映射 =====
    # 为不同的媒体服务器提供默认图标URL
    _webhook_images = {
        "emby": "https://emby.media/notificationicon.png",  # Emby服务器图标
        "plex": "https://www.plex.tv/wp-content/uploads/2022/04/new-logo-process-lines-gray.png",  # Plex服务器图标
        "jellyfin": "https://play-lh.googleusercontent.com/SCsUK3hCCRqkJbmLDctNYCfehLxsS4ggD1ZPHIFrrAN1Tn9yhjmGMPep2D9lMaaa9eQi"  # Jellyfin服务器图标
    }

    def init_plugin(self, config: dict = None):
        """
        插件初始化方法
        当插件被加载时，系统会调用此方法来初始化插件配置
        
        Args:
            config: 从数据库或配置文件中读取的插件配置字典
        """
        
        if config:  # 如果有配置信息
            # 从配置中获取插件是否启用，默认为False
            self._enabled = config.get("enabled")
            # 从配置中获取用户选择的消息类型，默认为空列表
            self._types = config.get("types") or []
            # 从配置中获取用户选择的媒体服务器，默认为空列表
            self._mediaservers = config.get("mediaservers") or []
            # 从配置中获取是否添加播放链接的设置，默认为False
            self._add_play_link = config.get("add_play_link", False)

    def service_infos(self, type_filter: Optional[str] = None) -> Optional[Dict[str, ServiceInfo]]:
        """
        获取媒体服务器服务信息
        
        Args:
            type_filter: 可选的服务器类型过滤器（如"emby", "plex", "jellyfin"）
            
        Returns:
            字典类型，键为服务器名称，值为ServiceInfo对象；如果没有服务器返回None
        """
        
        # 检查是否配置了媒体服务器
        if not self._mediaservers:
            logger.warning("尚未配置媒体服务器，请检查配置")  # 记录警告日志
            return None

        # 使用MediaServerHelper获取服务器实例
        # type_filter: 按类型过滤服务器
        # name_filters: 按名称过滤服务器（只获取用户配置的服务器）
        services = MediaServerHelper().get_services(type_filter=type_filter, name_filters=self._mediaservers)
        if not services:  # 如果没有获取到任何服务器
            logger.warning("获取媒体服务器实例失败，请检查配置")
            return None

        active_services = {}  # 存储活动（已连接）的服务器
        # 遍历所有获取到的服务器
        for service_name, service_info in services.items():
            if service_info.instance.is_inactive():  # 检查服务器是否处于非活动状态
                logger.warning(f"媒体服务器 {service_name} 未连接，请检查配置")
            else:
                # 只保留已连接的服务器
                active_services[service_name] = service_info

        # 如果没有任何活动的服务器
        if not active_services:
            logger.warning("没有已连接的媒体服务器，请检查配置")
            return None

        return active_services  # 返回活动服务器字典

    def service_info(self, name: str) -> Optional[ServiceInfo]:
        """
        根据名称获取单个媒体服务器的服务信息
        
        Args:
            name: 媒体服务器名称
            
        Returns:
            ServiceInfo对象或None
        """
        # 获取所有服务器信息，如果为None则使用空字典
        service_infos = self.service_infos() or {}
        # 根据名称返回对应的服务器信息
        return service_infos.get(name)

    def get_state(self) -> bool:
        """
        获取插件启用状态
        
        Returns:
            布尔值，True表示插件已启用，False表示已禁用
        """
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        获取插件命令列表（此插件不提供命令功能）
        
        Returns:
            命令列表，此插件返回None
        """
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API列表（此插件不提供API功能）
        
        Returns:
            API列表，此插件返回None
        """
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        构建插件配置页面
        这个方法定义了用户在Web界面中看到的配置表单
        
        Returns:
            元组：(页面配置列表, 默认数据字典)
        """
        
        # 定义消息类型选项，用户可以选择接收哪些类型的通知
        types_options = [
            {"title": "新入库", "value": "library.new"},  # 新媒体入库通知
            {"title": "开始播放", "value": "playback.start|media.play|PlaybackStart"},  # 播放开始通知（支持多种格式）
            {"title": "停止播放", "value": "playback.stop|media.stop|PlaybackStop"},  # 播放停止通知（支持多种格式）
            {"title": "用户标记", "value": "item.rate"},  # 用户评分标记通知
            {"title": "测试", "value": "system.webhooktest"},  # 测试通知
            {"title": "登录成功", "value": "user.authenticated"},  # 用户登录成功通知
            {"title": "登录失败", "value": "user.authenticationfailed"},  # 用户登录失败通知
        ]
        
        # 返回页面配置和默认数据
        return [
            {
                'component': 'VForm',  # 使用Vuetify表单组件
                'content': [
                    {
                        'component': 'VRow',  # 第一行：插件启用开关和播放链接开关
                        'content': [
                            {
                                'component': 'VCol',  # 第一列：插件启用开关
                                'props': {
                                    'cols': 12,  # 在小屏幕上占满12列
                                    'md': 6     # 在中等屏幕上占6列（一半宽度）
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',  # 开关组件
                                        'props': {
                                            'model': 'enabled',  # 绑定到enabled配置项
                                            'label': '启用插件',  # 开关标签
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',  # 第二列：播放链接开关
                                'props': {
                                    'cols': 12,  # 在小屏幕上占满12列
                                    'md': 6      # 在中等屏幕上占6列
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',  # 开关组件
                                        'props': {
                                            'model': 'add_play_link',  # 绑定到add_play_link配置项
                                            'label': '添加播放链接',   # 开关标签
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',  # 第二行：媒体服务器选择
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12  # 占满整行
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',  # 下拉选择组件
                                        'props': {
                                            'multiple': True,   # 允许多选
                                            'chips': True,      # 以芯片形式显示选中项
                                            'clearable': True,  # 允许清空选择
                                            'model': 'mediaservers',  # 绑定到mediaservers配置项
                                            'label': '媒体服务器',     # 选择框标签
                                            # 动态获取可用的媒体服务器列表作为选项
                                            'items': [{"title": config.name, "value": config.name}
                                                      for config in MediaServerHelper().get_configs().values()]
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',  # 第三行：消息类型选择
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,  # 占满整行
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',  # 下拉选择组件
                                        'props': {
                                            'chips': True,      # 以芯片形式显示选中项
                                            'multiple': True,   # 允许多选
                                            'model': 'types',   # 绑定到types配置项
                                            'label': '消息类型', # 选择框标签
                                            'items': types_options  # 使用前面定义的消息类型选项
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',  # 第四行：配置说明信息
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,  # 占满整行
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',  # 警告/信息提示组件
                                        'props': {
                                            'type': 'info',     # 信息类型（蓝色）
                                            'variant': 'tonal', # 色调变体
                                            # 配置说明文本
                                            'text': '需要设置媒体服务器Webhook，回调相对路径为 /api/v1/webhook?token=API_TOKEN&source=媒体服务器名（3001端口），其中 API_TOKEN 为设置的 API_TOKEN。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            # 默认配置数据
            "enabled": False,  # 默认不启用插件
            "types": []       # 默认不选择任何消息类型
        }

    def get_page(self) -> List[dict]:
        """
        获取插件页面配置（此插件不提供独立页面）
        
        Returns:
            页面配置列表，此插件返回None
        """
        pass

    def _get_variable_one_from_path(self, media_path: str) -> Optional[int]:
        """
        根据媒体文件路径确定变量一的值
        
        Args:
            media_path: 媒体文件的完整路径
            
        Returns:
            对应的变量一数值，如果没有匹配则返回None
        """
        if not media_path:
            return None
            
        # 遍历路径映射配置，按顺序匹配
        for path_patterns, variable_value in self._path_mapping:
            # 检查路径是否包含任一匹配模式
            for pattern in path_patterns:
                if pattern in media_path:
                    logger.info(f"路径 '{media_path}' 匹配模式 '{pattern}' -> 变量一: {variable_value}")
                    return variable_value
        
        # 如果没有匹配到任何模式
        logger.warning(f"路径 '{media_path}' 未匹配到任何已知模式")
        return None

    def _execute_curl_command(self, event_info: WebhookEventInfo):
        """
        执行curl命令处理新入库事件
        
        Args:
            event_info: Webhook事件信息
        """
        try:
            # ===== 获取媒体文件路径 =====
            media_path = getattr(event_info, 'item_path', None)  # 获取媒体文件路径
            if not media_path:
                logger.warning("无法获取媒体文件路径，跳过curl命令执行")
                return
            
            # ===== 确定变量一：根据媒体文件路径 =====
            variable_one = self._get_variable_one_from_path(media_path)
            if variable_one is None:
                logger.warning(f"无法从路径 '{media_path}' 确定变量一，跳过curl命令执行")
                return
            
            # ===== 确定变量二：根据媒体路径（.strm文件处理） =====
            variable_two = ""  # 默认为空字符串
            
            if media_path and media_path.endswith('.strm'):  # 如果是.strm文件
                # 获取上一层级路径
                variable_two = os.path.dirname(media_path)
                logger.info(f"检测到.strm文件，变量二设置为: {variable_two}")
            else:
                logger.info("非.strm文件，变量二保持为空")
            
            # ===== 确定变量三：根据媒体类型 =====
            item_type = getattr(event_info, 'item_type', None)  # 获取媒体类型
            if item_type == "MOV":  # 电影类型
                variable_three = "movies"
            elif item_type in ["TV", "SHOW"]:  # 剧集类型
                variable_three = "tvshows"
            else:
                logger.warning(f"未识别的媒体类型: {item_type}，跳过curl命令执行")
                return
            
            # ===== 构建JSON数据 =====
            json_data = [
                {
                    "action": "update",
                    "scope": {
                        "name": "single",
                        "args": [str(variable_one)]  # 变量一转换为字符串
                    }
                },
                {
                    "action": "scrape",
                    "scope": {
                        "name": "new"
                    }
                },
                {
                    "action": "downloadMissingArtwork",
                    "scope": {
                        "name": "dataSource",
                        "args": [variable_two]  # 变量二（可能为空字符串）
                    }
                }
            ]
            
            # ===== 构建curl命令 =====
            api_url = f"http://localhost:7878/api/{variable_three}"  # 使用变量三构建URL
            api_key = "b23e06b9-8dbb-47cd-858e-72bde40fb3c6"  # API密钥
            
            curl_command = [
                "curl",
                "-d", json.dumps(json_data),  # JSON数据作为POST数据
                "-H", "Content-Type: application/json",  # 设置内容类型
                "-H", f"api-key: {api_key}",  # 设置API密钥
                "-X", "POST",  # 使用POST方法
                api_url  # 目标URL
            ]
            
            # ===== 执行curl命令 =====
            logger.info(f"执行curl命令处理新入库事件:")
            logger.info(f"  媒体路径: {media_path} -> 变量一: {variable_one}")
            logger.info(f"  .strm检查: {media_path} -> 变量二: {variable_two}")
            logger.info(f"  媒体类型: {item_type} -> 变量三: {variable_three}")
            logger.info(f"  API URL: {api_url}")
            
            # 使用subprocess执行curl命令
            result = subprocess.run(
                curl_command,
                capture_output=True,  # 捕获输出
                text=True,  # 以文本形式返回输出
                timeout=30  # 设置30秒超时
            )
            
            # ===== 处理执行结果 =====
            if result.returncode == 0:  # 命令执行成功
                logger.info("curl命令执行成功")
                if result.stdout:  # 如果有标准输出
                    logger.info(f"curl响应: {result.stdout}")
            else:  # 命令执行失败
                logger.error(f"curl命令执行失败，返回码: {result.returncode}")
                if result.stderr:  # 如果有错误输出
                    logger.error(f"curl错误信息: {result.stderr}")
                    
        except subprocess.TimeoutExpired:
            # 处理超时异常
            logger.error("curl命令执行超时（30秒）")
        except FileNotFoundError:
            # 处理curl命令不存在的情况
            logger.error("未找到curl命令，请确保系统已安装curl")
        except Exception as e:
            # 处理其他异常
            logger.error(f"执行curl命令时发生错误: {str(e)}")

    @eventmanager.register(EventType.WebhookMessage)  # 注册事件监听器，监听Webhook消息事件
    def send(self, event: Event):
        """
        发送通知消息的主要方法
        当系统接收到Webhook事件时，会调用此方法处理并发送通知
        
        Args:
            event: 包含事件数据的Event对象
        """
        
        # 检查插件是否启用
        if not self._enabled:
            return  # 如果插件未启用，直接返回不处理

        # 从事件中提取Webhook事件信息
        event_info: WebhookEventInfo = event.event_data
        if not event_info:  # 如果没有事件数据
            return  # 直接返回

        # 检查是否是支持的事件类型
        if not self._webhook_actions.get(event_info.event):
            return  # 如果事件类型不在支持列表中，直接返回

        # 检查用户是否选择了此类型的通知
        msgflag = False  # 消息标志位
        for _type in self._types:  # 遍历用户配置的消息类型
            # 每个类型可能包含多个事件名（用|分隔），检查当前事件是否在其中
            if event_info.event in _type.split("|"):
                msgflag = True  # 找到匹配的类型
                break
        if not msgflag:  # 如果没有匹配的类型
            logger.info(f"未开启 {event_info.event} 类型的消息通知")
            return  # 记录日志并返回

        # 检查是否有可用的媒体服务器
        if not self.service_infos():
            logger.info(f"未开启任一媒体服务器的消息通知")
            return

        # 如果事件指定了服务器名称，检查该服务器是否在配置中
        if event_info.server_name and not self.service_info(name=event_info.server_name):
            logger.info(f"未开启媒体服务器 {event_info.server_name} 的消息通知")
            return

        # 如果事件指定了服务器类型（channel），检查该类型是否在配置中
        if event_info.channel and not self.service_infos(type_filter=event_info.channel):
            logger.info(f"未开启媒体服务器类型 {event_info.channel} 的消息通知")
            return

        # 生成防重复消息的唯一键，用于过滤重复的停止播放消息
        expiring_key = f"{event_info.item_id}-{event_info.client}-{event_info.user_name}"
        
        # 特殊处理停止播放事件，防止重复通知
        if str(event_info.event) == "playback.stop" and expiring_key in self._webhook_msg_keys.keys():
            # 如果是停止播放事件且键已存在，刷新过期时间但不发送通知
            self.__add_element(expiring_key)
            return

        # ===== 特殊处理：新入库事件执行curl命令 =====
        if str(event_info.event) == "library.new":
            logger.info("检测到新入库事件，准备执行curl命令")
            self._execute_curl_command(event_info)

        # ===== 构建通知消息内容 =====
        
        # 根据媒体类型构建消息标题
        if event_info.item_type in ["TV", "SHOW"]:  # 电视剧
            message_title = f"{self._webhook_actions.get(event_info.event)}剧集 {event_info.item_name}"
        elif event_info.item_type == "MOV":  # 电影
            message_title = f"{self._webhook_actions.get(event_info.event)}电影 {event_info.item_name}"
        elif event_info.item_type == "AUD":  # 有声书
            message_title = f"{self._webhook_actions.get(event_info.event)}有声书 {event_info.item_name}"
        else:  # 其他类型或没有指定类型
            message_title = f"{self._webhook_actions.get(event_info.event)}"

        # 构建消息正文内容列表
        message_texts = []
        if event_info.user_name:  # 如果有用户名信息
            message_texts.append(f"用户：{event_info.user_name}")
        if event_info.device_name:  # 如果有设备信息
            message_texts.append(f"设备：{event_info.client} {event_info.device_name}")
        if event_info.ip:  # 如果有IP地址信息
            # 使用WebUtils获取IP地址的地理位置信息
            message_texts.append(f"IP地址：{event_info.ip} {WebUtils.get_location(event_info.ip)}")
        if event_info.percentage:  # 如果有播放进度信息
            percentage = round(float(event_info.percentage), 2)  # 保留两位小数
            message_texts.append(f"进度：{percentage}%")
        if event_info.overview:  # 如果有剧情概要
            message_texts.append(f"剧情：{event_info.overview}")
        # 添加当前时间
        message_texts.append(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))}")

        # 将所有消息内容用换行符连接
        message_content = "\n".join(message_texts)

        # ===== 处理消息图片 =====
        
        image_url = event_info.image_url  # 首先使用事件中提供的图片URL
        
        # 如果有TMDB ID，尝试获取更具体的图片（剧集背景图）
        if event_info.tmdb_id:
            # 获取季和集的ID（如果有的话）
            season_id = event_info.season_id if event_info.season_id else None
            episode_id = event_info.episode_id if event_info.episode_id else None

            # 使用链式调用获取特定的图片
            specific_image = self.chain.obtain_specific_image(
                mediaid=event_info.tmdb_id,     # TMDB媒体ID
                mtype=MediaType.TV,             # 媒体类型为电视剧
                image_type=MediaImageType.Backdrop,  # 图片类型为背景图
                season=season_id,               # 季ID
                episode=episode_id              # 集ID
            )
            if specific_image:  # 如果获取到了特定图片
                image_url = specific_image
                
        # 如果仍然没有图片，使用服务器的默认图标
        if not image_url:
            image_url = self._webhook_images.get(event_info.channel)

        # ===== 处理播放链接 =====
        
        play_link = None  # 初始化播放链接
        if self._add_play_link:  # 如果用户配置了添加播放链接
            if event_info.server_name:  # 如果指定了服务器名称
                # 根据服务器名称获取服务信息
                service = self.service_infos().get(event_info.server_name)
                if service:  # 如果找到了服务
                    # 获取媒体项的播放链接
                    play_link = service.instance.get_play_url(event_info.item_id)
            elif event_info.channel:  # 如果指定了服务器类型但没有具体名称
                # 获取该类型的所有服务器
                services = MediaServerHelper().get_services(type_filter=event_info.channel)
                # 遍历服务器尝试获取播放链接
                for service in services.values():
                    play_link = service.instance.get_play_url(event_info.item_id)
                    if play_link:  # 找到第一个有效链接就停止
                        break

        # ===== 处理防重复逻辑 =====
        
        if str(event_info.event) == "playback.stop":
            # 如果是停止播放事件，将键添加到过期字典中，防止重复通知
            self.__add_element(expiring_key)
        if str(event_info.event) == "playback.start":
            # 如果是开始播放事件，从过期字典中删除对应的键
            self.__remove_element(expiring_key)

        # ===== 发送通知消息 =====
        
        # 调用父类的post_message方法发送通知
        self.post_message(mtype=NotificationType.MediaServer,  # 通知类型为媒体服务器
                          title=message_title,      # 消息标题
                          text=message_content,     # 消息内容
                          image=image_url,          # 消息图片
                          link=play_link)           # 播放链接

    def __add_element(self, key, duration=600):
        """
        向过期字典中添加元素
        用于防止重复的停止播放通知
        
        Args:
            key: 元素的键（通常是媒体项-客户端-用户名的组合）
            duration: 过期时间（秒），默认600秒（10分钟）
        """
        expiration_time = time.time() + duration  # 计算过期时间戳
        # 如果元素已经存在，更新其过期时间；如果不存在，添加新元素
        self._webhook_msg_keys[key] = expiration_time

    def __remove_element(self, key):
        """
        从过期字典中移除指定元素
        
        Args:
            key: 要移除的元素键
        """
        # 使用字典推导式重建字典，排除指定的键
        self._webhook_msg_keys = {k: v for k, v in self._webhook_msg_keys.items() if k != key}

    def __get_elements(self):
        """
        获取当前有效的（未过期的）元素列表
        
        Returns:
            有效元素键的列表
        """
        current_time = time.time()  # 获取当前时间戳
        # 使用字典推导式过滤掉过期的元素
        self._webhook_msg_keys = {k: v for k, v in self._webhook_msg_keys.items() if v > current_time}
        # 返回剩余有效元素的键列表
        return list(self._webhook_msg_keys.keys())

    def stop_service(self):
        """
        插件停止服务时的清理方法
        当插件被禁用或系统关闭时调用
        
        此插件没有需要特殊清理的资源，所以方法体为空
        """
        pass
