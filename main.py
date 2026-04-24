from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig
from astrbot.api import logger # 使用 astrbot 提供的 logger 接口
import astrbot.api.message_components as Comp
from astrbot.api.message_components import Plain
from astrbot.core.utils.session_waiter import session_waiter, SessionController  # 会话控制器
import json
import os
import time
from itertools import islice
import aiohttp
import random
from pathlib import Path

@register("astrbot_plugin_examine", "语芮澈", "功能完善的入群自动考核插件！", "v1.0-beta", "https://github.com/YuRuiChe/astrbot_plugin_examine")
class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.main_group_id = config.get("main_group_id", "")
        self.examine_group_id = config.get("examine_group_id", "")
        group_entry_reminder = config.get("group_entry_reminder") or {}
        self.reminder_text = group_entry_reminder.get("reminder_text", "欢迎！请私聊发送“开始答题”以开始测试")
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

    @filter.command("开始答题")
    async def start_answer(self, event: AstrMessageEvent):
        """开始答题"""
        # 判断是否为私聊（私聊包括：普通私聊 + 临时会话）
        if event.is_private_chat():
            if self.randomly_selected_questions:  # 如果开启随机抽题
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
                # ====================注册会话控制器====================
                # @session_waiter 装饰器：创建一个等待用户回复的会话
                # timeout=60: 会话超时时间60秒，超时后会抛出 TimeoutError
                # record_history_chains=False: 不记录消息历史（节省内存）
                @session_waiter(timeout=self.limited_time, record_history_chains=False)
                async def quiz_waiter(controller: SessionController, event: AstrMessageEvent):
                    """
                    会话控制器的回调函数
                    在用户回复消息时会被调用

                    参数:
                        controller: SessionController 对象，用于控制会话行为
                        event: 用户回复的消息事件
                    """
                    try:
                        # 获取用户输入的文本，并去除首尾空格
                        answer = event.message_str.strip()
                        # ====================根据用户答案做出不同响应====================
                        if answer == "2":
                            # send() 方法直接发送消息（与 yield 等效）
                            await event.send(event.plain_result("✅ 回答正确！答题结束，现在可以执行其他指令了。"))
                            controller.stop()  # 结束会话控制，释放会话
                        elif answer == "确定":
                            await event.send(event.plain_result("已退出答题模式。"))
                            controller.stop()  # 结束会话控制
                        # 情况3：答案错误
                        else:
                            # 发送错误提示，要求重新回答
                            await event.send(event.plain_result("❌ 答案错误，请重新回答（输入 exit 退出）"))
                            # keep() 保持会话继续等待
                            # reset_timeout=True: 重置超时计时器，让用户重新获得60秒答题时间
                            controller.keep(timeout=60, reset_timeout=True)

                        # ====================启动会话控制器====================
                        # await 会阻塞在这里，等待用户回复或超时
                        # 在会话期间，用户的所有消息都会被 quiz_waiter 拦截处理
                        # 其他指令（如 /help）此时不会生效
                        await quiz_waiter(event)
                    # ====================异常处理====================
                    except TimeoutError:
                        # 用户规定时间内没有回复，触发超时
                        yield event.plain_result("答题超时！结束考核")
                    except Exception as e:
                        # 其他未预期的异常
                        yield event.plain_result(f"发生错误: {str(e)}")
                    finally:
                        # ====================最终清理====================
                        # finally 块无论是否发生异常都会执行
                        # stop_event() 结束当前消息事件的传播
                        # 防止后续处理器（如其他插件或 LLM）再次处理这条消息
                        event.stop_event()
            else:  # 没开启随机抽题
                pass
        else:
            yield event.plain_result("请在私聊或临时会话中使用该指令")
            return

    async def terminate(self):
        '''当插件被卸载或停用时调用，用于释放资源（如关闭数据库连接、停止定时任务等）'''
        logger.info("插件正在终止...")
        # 在这里添加你的清理代码