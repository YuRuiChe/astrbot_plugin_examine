from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig
from astrbot.api import logger # 使用 astrbot 提供的 logger 接口
import astrbot.api.message_components as Comp
import json
import os
import aiohttp
import random
from pathlib import Path

@register("入群做题", "语芮澈", "功能完善的入群自动发题插件！", "v1.0-beta", "https://github.com/YuRuiChe/astrbot_plugin_examine")
class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.main_group_id = config.get("main_group_id", "")
        self.examine_group_id = config.get("examine_group_id", "")
        entry = config.get("group_entry_reminder") or {}
        self.reminder_text = entry.get("reminder_text", "欢迎！请私聊给bot发送“开始答题”以作答！")
        self.reminder_imgs = entry.get("reminder_img", "")
        self.whether_at = entry.get("whether_at", False)
        answer = config.get("answer") or {}
        self.total_questions = answer.get("total_number_of_questions", 0)
        self.total_score = answer.get("total_score", 100)
        self.passing_line = answer.get("passing_line", 60)
        self.limited_time = answer.get("limited_time", 100)
        self.random_select = answer.get("randomly_selected_questions", False)
        bank = config.get("question_bank") or {}
        self.question_path = bank.get("question", "")
        self.option_path = bank.get("option", "")
        self.answer_path = bank.get("answer", "")

    @filter.event_message_type(filter.EventMessageType.ALL)  # 监听所有类型的消息事件（包括群消息、私聊、通知等）
    async def handle_group_add(self, event: AstrMessageEvent):
        """入群提示"""
        # ==================== 第一部分：事件类型校验 ====================
        # 检查事件对象是否包含原始消息数据
        if not hasattr(event, "message_obj") or not hasattr(event.message_obj, "raw_message"):
            return  # 没有原始消息数据，说明不是需要处理的通知类型，直接退出
        raw_message = event.message_obj.raw_message  # 获取原始消息字典
        if not raw_message or not isinstance(raw_message, dict):
            return  # 原始消息格式不正确，退出
        # 只处理通知类事件（notice），不处理消息类事件
        if raw_message.get("post_type") != "notice":
            return
        # ==================== 第二部分：处理群成员增加（入群）事件 ====================
        if raw_message.get("notice_type") == "group_increase":  # 群人数增加 → 新人入群
            user_id = raw_message.get("user_id")  # 获取新成员的QQ号
            # 默认使用全局欢迎语
            welcome_message = self.group_entry_reminder
            # 确定最终使用的图片
            image_to_use = self.reminder_img
            # ========== 构建并发送欢迎消息 ==========
            if image_to_use:  # 如果有欢迎图片
                # 判断图片是URL还是本地路径（只能使用本地路径）
                if image_to_use.startswith("http://") or image_to_use.startswith("https://"):
                    logger.warning(f"Invalid image URL: {image_to_use}")  # 图片URL无效，记录警告
                    # 降级处理：只发送文字，不发图片
                    chain = [
                        Comp.At(qq=user_id) if self.whether_at else Comp.Plain(""),
                        Comp.Plain(welcome_message),
                    ]
                else:
                    # 本地图片：从路径读取
                    chain = [
                        Comp.At(qq=user_id) if self.whether_at else Comp.Plain(""),
                        Comp.Plain(welcome_message),
                        Comp.Image.fromFileSystem(image_to_use),
                    ]
                yield event.chain_result(chain)  # 发送消息链（带图片）
            else:
                # 无图片：只发送文字欢迎消息
                chain = [
                    Comp.At(qq=user_id) if self.whether_at else Comp.Plain(""),
                    Comp.Plain(welcome_message),
                ]
                yield event.chain_result(chain)

    async def terminate(self):
        '''当插件被卸载或停用时调用，用于释放资源（如关闭数据库连接、停止定时任务等）'''
        logger.info("插件正在终止...")
        # 在这里添加你的清理代码