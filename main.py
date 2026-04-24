from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig
from astrbot.api import logger # 使用 astrbot 提供的 logger 接口
import astrbot.api.message_components as Comp
from astrbot.api.message_components import Plain
import json
import os
import time
from itertools import islice
import aiohttp
import random
from pathlib import Path

@register("astrbot_plugin_examine", "语芮澈", "功能完善的入群自动发题插件！", "v1.0-beta", "https://github.com/YuRuiChe/astrbot_plugin_examine")
class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.main_group_id = config.get("main_group_id", "")
        self.examine_group_id = config.get("examine_group_id", "")
        group_entry_reminder = config.get("group_entry_reminder") or {}
        self.reminder_text = group_entry_reminder.get("reminder_text", "注意！进群就相当于开始答题！题目将于1min后发放至你的私聊，请于规定时间内完成答题")
        self.reminder_imgs = group_entry_reminder.get("reminder_img", "")
        self.whether_at = group_entry_reminder.get("whether_at", False)
        answer = config.get("answer") or {}
        self.total_number_of_questions = answer.get("total_number_of_questions", 0)
        self.total_score = answer.get("total_score", 100)
        self.passing_line = answer.get("passing_line", 60)
        self.limited_time = answer.get("limited_time", 100)
        self.randomly_selected_questions = answer.get("randomly_selected_questions", False)
        self.finally_questions = answer.get("finally_questions", 15)
        question_bank = config.get("question_bank") or {}
        self.question = question_bank.get("question", "")
        self.option = question_bank.get("option", "")
        self.answer = question_bank.get("answer", "")
        llm = config.get("llm") or {}
        self.disable_llm = llm.get("disable_llm", False)

    @filter.event_message_type(filter.EventMessageType.ALL)  # 监听所有类型的消息事件（包括群消息、私聊、通知等）
    async def handle_group_add(self, event: AstrMessageEvent):
        """入群提示"""
        # =====================事件类型校验====================
        # 检查事件对象是否包含原始消息数据
        if not hasattr(event, "message_obj") or not hasattr(event.message_obj, "raw_message"):
            return  # 没有原始消息数据，说明不是需要处理的通知类型，直接退出
        raw_message = event.message_obj.raw_message  # 获取原始消息字典
        if not raw_message or not isinstance(raw_message, dict):
            return  # 原始消息格式不正确，退出
        # 只处理通知类事件（notice），不处理消息类事件
        if raw_message.get("post_type") != "notice":
            return
        # ====================处理群成员入群事件====================
        if raw_message.get("notice_type") == "group_increase":  # 群人数增加 → 新人入群
            user_id = raw_message.get("user_id")  # 获取新成员的QQ号
            # 默认使用全局欢迎语
            welcome_message = self.reminder_text
            # 确定最终使用的图片
            image_to_use = self.reminder_imgs
            # ====================构建并发送欢迎消息====================
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
        # ====================开始发放题目====================
        if self.randomly_selected_questions:# 如果开启随机抽题
            out = ""
            check = ""
            for i in range(int(self.finally_questions)):
                line = random.randint(1, int(self.total_number_of_questions))
                line_list = []
                line_list.append(int(line))
                if line in line_list:
                    line_list.pop()
                    continue
                else:
                    # 问题
                    try:
                        with open(self.question, 'r', encoding='utf-8') as f:
                            q = next(islice(f, line - 1, line), None)
                            if q:
                                q = q.rstrip('\n')
                    except FileNotFoundError:
                        yield event.chain_result(f"文件不存在: {self.question}")
                    except Exception as e:
                        yield event.chain_result(f"读取文件出错: {e}")
                    # 选项
                    try:
                        with open(self.question, 'r', encoding='utf-8') as f:
                            o = next(islice(f, line - 1, line), None)
                            if o:
                                o = o.rstrip('\n')
                    except FileNotFoundError:
                        yield event.chain_result(f"文件不存在: {self.question}")
                    except Exception as e:
                        yield event.chain_result(f"读取文件出错: {e}")
                    # 答案
                    try:
                        with open(self.question, 'r', encoding='utf-8') as f:
                            a = next(islice(f, line - 1, line), None)
                            if a:
                                a = a.rstrip('\n')
                    except FileNotFoundError:
                        yield event.chain_result(f"文件不存在: {self.question}")
                    except Exception as e:
                        yield event.chain_result(f"读取文件出错: {e}")
                    a = a.replace('|', '\n')
                    out = str(out) + f"\n{str(q)}\n{str(o)}\n"
                    check = str(check) + f"{str(a)}"
            temporary_umo = f"aiocqhttp_default:PRIVATE:{user_id}_{self.examine_group_id}"
            time.sleep(60)
            await self.context.send_message(temporary_umo, [Plain(out)])
        else:# 没开启随机抽题
            pass

    @filter.command("作答")
    async def now_answer(self, event: AstrMessageEvent):
        """输入题目的答案"""
        pass

    async def terminate(self):
        '''当插件被卸载或停用时调用，用于释放资源（如关闭数据库连接、停止定时任务等）'''
        logger.info("插件正在终止...")
        # 在这里添加你的清理代码