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

from psutil import boot_time


@register("astrbot_plugin_examine", "语芮澈", "功能完善的入群自动考核插件！", "v1.4", "https://github.com/YuRuiChe/astrbot_plugin_examine")
class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.main_group_id = config.get("main_group_id", "")
        self.examine_group_id = config.get("examine_group_id", "")
        self.bot_name = config.get("bot_name", "")
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
        # 在读取文件之前，先规范化路径
        self.question = os.path.abspath(self.question)
        self.option = os.path.abspath(self.option)
        self.answer = os.path.abspath(self.answer)
        llm = config.get("llm") or {}
        self.disable_llm = llm.get("disable_llm", False)
        self.active_sessions = {}  # 用于记录活跃会话 {user_id: controller}

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
        if event.is_private_chat():
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
        """开始进行答题"""
        # 判断是否为私聊（私聊包括：普通私聊 + 临时会话）
        if event.is_private_chat():
            # 获取用户qq
            user_umo = event.unified_msg_origin
            user_umo = str(user_umo).replace(f'{self.bot_name}:FriendMessage:', '')
            user_id = event.get_sender_id()
            group_umo = f"{self.bot_name}:GroupMessage:{self.examine_group_id}"
            if user_id in self.active_sessions:
                yield event.plain_result("你已有正在进行的答题，请先完成或等待超时!")
                return
            self.active_sessions[user_id] = True
            try:
                # 获取群成员信息（如果用户不在群中，API 通常会返回错误或抛出异常）
                # 注意：不同适配器的 API 方法名可能略有不同
                member_info = await event.bot.get_group_member_info(
                    group_id=int(self.examine_group_id),
                    user_id=int(user_umo)
                )
                if member_info:
                    out = ""
                    check = ""
                    if self.randomly_selected_questions:  # 如果开启随机抽题
                        line_list = set()
                        for i in range(int(self.finally_questions)):
                            while True:
                                line = random.randint(1, self.total_number_of_questions)
                                # 检查这个题号是否已经被抽过
                                if line not in line_list:
                                    # 没抽过 → 把它加入已抽集合
                                    line_list.add(line)
                                    # 退出 while 循环，继续下一道题
                                    break
                                # 如果抽过了，不会执行 break，会继续 while 循环重新随机
                            try:
                                # 问题
                                with open(self.question, 'r', encoding='utf-8') as f:
                                    q = next(islice(f, line - 1, line), None)
                                    if q:
                                        q = q.rstrip('\n')
                            except FileNotFoundError:
                                logger.error(f"文件不存在: {self.question}")
                                return
                            except Exception as e:
                                logger.error(f"读取文件出错: {e}")
                                return
                            try:
                                # 选项
                                with open(self.option, 'r', encoding='utf-8') as f:
                                    o = next(islice(f, line - 1, line), None)
                                    if o:
                                        o = o.rstrip('\n')
                                        o = o.replace('[)', '\n')
                            except FileNotFoundError:
                                logger.error(f"文件不存在: {self.option}")
                                return
                            except Exception as e:
                                logger.error(f"读取文件出错: {e}")
                                return
                            try:
                                # 答案
                                with open(self.answer, 'r', encoding='utf-8') as f:
                                    a = next(islice(f, line - 1, line), None)
                                    if a:
                                        a = a.rstrip('\n')
                            except FileNotFoundError:
                                logger.error(f"文件不存在: {self.answer}")
                                return
                            except Exception as e:
                                logger.error(f"读取文件出错: {e}")
                                return
                            out = str(out) + f"\n{str(q)}\n{str(o)}\n"
                            check = str(check) + f"{str(a)}"

                        try:
                            try:
                                result = event.make_result()
                                result.chain = [Plain(f"新人{user_umo}开始答题！")]
                                await self.context.send_message(group_umo, result)
                                logger.info(f"用户{user_umo}开始答题！")
                            except Exception as e:
                                await event.send(event.plain_result("消息发送失败，请检查后台日志"))
                                logger.error(f"向群 {group_umo} 发送消息失败: {e}")
                            yield event.plain_result(f"考核开始，请使用“作答”指令以答题，“确定”指令以结束答题\n示例：\n作答abcabcabcabc")
                            yield event.plain_result(f"以下为题目，请于{self.limited_time}秒内完成\n\n{str(out)}")
                            logger.info("已发送题目！")
                            # ====================注册会话控制器====================
                            # @session_waiter 装饰器：创建一个等待用户回复的会话
                            # timeout=60: 会话超时时间60秒，超时后会抛出 TimeoutError
                            # record_history_chains=False: 不记录消息历史（节省内存）
                            @session_waiter(timeout=self.limited_time, record_history_chains=False)
                            async def quiz_waiter(controller: SessionController, event: AstrMessageEvent):
                                """
                                会话控制器的回调函数
                                在用户回复消息时会被调用
                                @session_waiter 回调中应使用 await event.send()，而不是 yield
                                """
                                logger.info("会话控制器正在运行")
                                if not hasattr(controller, 'initialized'):
                                    controller.if_answer = False
                                    controller.user_answer = ""
                                    controller.mark = 0
                                    controller.initialized = True
                                # 获取用户输入的文本，并去除首尾空格
                                answer = event.message_str.strip()
                                # ====================根据用户答案做出不同响应====================
                                if answer[:2] == "作答":
                                    if len(answer[2:]) == self.finally_questions:
                                        controller.if_answer = True
                                        controller.user_answer = str(answer[2:])
                                        await event.send(event.plain_result("是否确定答案？如确定请输入“确定”"))
                                        logger.info(f"用户答案为{controller.user_answer}")
                                        return
                                    else:
                                        await event.send(event.plain_result("你写多或者写少了！请重写"))
                                        logger.info(f"用户{user_umo}写多或者写少了！请重写")
                                        return

                                elif answer == "确定":
                                    if controller.if_answer:
                                        await event.send(event.plain_result("已退出答题模式，正在审核中"))
                                        logger.info("已退出答题模式，正在审核中")
                                        for i1 in range(self.finally_questions):
                                            if controller.user_answer[i1] == check[i1]:
                                                controller.mark += self.total_score / self.finally_questions
                                        if controller.mark >= self.passing_line:
                                            await event.send(event.plain_result(f"恭喜！你以{controller.mark}分的成绩通过了考核！请加入主群：{self.main_group_id}并退出审核群！"))
                                            logger.info(f"恭喜！用户{user_umo}以{controller.mark}分的成绩通过了考核！请加入主群：{self.main_group_id}并退出审核群！")
                                            try:
                                                result = event.make_result()
                                                result.chain = [Plain(f"通过:新人{user_umo}以{controller.mark}分的成绩通过了考核！")]
                                                logger.info(f"已向群{group_umo}发送{user_umo}的卡片")
                                                await self.context.send_message(group_umo, result)
                                            except Exception as e:
                                                await event.send(event.plain_result("消息发送失败，请检查后台日志"))
                                                logger.error(f"向群 {group_umo} 发送消息失败: {e}")
                                            controller.stop()
                                            del self.active_sessions[user_id]
                                            return
                                        else:
                                            await event.send(event.plain_result(f"你的成绩{controller.mark}分低于及格线{self.passing_line}分没有通过，请自觉退群"))
                                            logger.error(f"用户{user_umo}的成绩{controller.mark}分低于及格线{self.passing_line}分没有通过，请自觉退群")
                                            try:
                                                result = event.make_result()
                                                result.chain = [Plain(f"未通过:新人{user_umo}的成绩{controller.mark}分低于及格线{self.passing_line}分，未通过！")]
                                                await self.context.send_message(group_umo, result)
                                                logger.info(f"已向群{group_umo}发送{user_umo}的卡片")
                                            except Exception as e:
                                                await event.send(event.plain_result("消息发送失败，请检查后台日志"))
                                                logger.error(f"向群 {group_umo} 发送消息失败: {e}")
                                            controller.stop()
                                            del self.active_sessions[user_id]
                                            return
                                    else:
                                        await event.send(event.plain_result("未作答！不能结束！"))
                                        return
                            try:
                                # ====================启动会话控制器====================
                                # await 会阻塞在这里，等待用户回复或超时
                                # 在会话期间，用户的所有消息都会被 quiz_waiter 拦截处理
                                # 其他指令（如 /help）此时不会生效
                                await quiz_waiter(event)
                                return
                            # ====================异常处理====================
                            except TimeoutError:
                                # 用户规定时间内没有回复，触发超时
                                yield event.plain_result("答题超时！结束考核！请联系管理员处理")
                                logger.info(f"用户{user_umo}答题超时！结束考核！请联系管理员处理")
                                try:
                                    result = event.make_result()
                                    result.chain = [Plain(f"未通过:新人{user_umo}作答超时！")]
                                    await self.context.send_message(group_umo, result)
                                    logger.info(f"已向群{group_umo}发送{user_umo}的卡片")
                                except Exception as e:
                                    await event.send(event.plain_result("消息发送失败，请检查后台日志"))
                                    logger.error(f"向群 {group_umo} 发送消息失败: {e}")
                                if user_id in self.active_sessions:
                                    del self.active_sessions[user_id]
                                return
                            except Exception as e:
                                # 其他未预期的异常
                                logger.error(f"发生错误: {str(e)}")
                                yield event.plain_result(f"发生错误: {str(e)}")
                                if user_id in self.active_sessions:
                                    del self.active_sessions[user_id]
                                return
                            finally:
                                # ====================最终清理====================
                                # finally 块无论是否发生异常都会执行
                                # stop_event() 结束当前消息事件的传播
                                # 防止后续处理器（如其他插件或 LLM）再次处理这条消息
                                event.stop_event()
                        except Exception as e:
                            logger.error("会话控制器发生错误: " + str(e))
                            if user_id in self.active_sessions:
                                del self.active_sessions[user_id]
                            return

                    else:  # 没开启随机抽题
                        try:
                            # 问题
                            with open(self.question, 'r', encoding='utf-8') as f:
                                q = next(islice(f, self.finally_questions - 1, self.finally_questions), None)
                                if q:
                                    q = q.rstrip('\n')
                        except FileNotFoundError:
                            logger.error(f"文件不存在: {self.question}")
                            return
                        except Exception as e:
                            logger.error(f"读取文件出错: {e}")
                            return
                        try:
                            # 选项
                            with open(self.option, 'r', encoding='utf-8') as f:
                                o = next(islice(f, self.finally_questions - 1, self.finally_questions), None)
                                if o:
                                    o = o.rstrip('\n')
                                    o = o.replace('[)', '\n')
                        except FileNotFoundError:
                            logger.error(f"文件不存在: {self.option}")
                            return
                        except Exception as e:
                            logger.error(f"读取文件出错: {e}")
                            return
                        try:
                            # 答案
                            with open(self.answer, 'r', encoding='utf-8') as f:
                                a = next(islice(f, self.finally_questions - 1, self.finally_questions), None)
                                if a:
                                    a = a.rstrip('\n')
                        except FileNotFoundError:
                            logger.error(f"文件不存在: {self.answer}")
                            return
                        except Exception as e:
                            logger.error(f"读取文件出错: {e}")
                            return
                        out = str(out) + f"\n{str(q)}\n{str(o)}\n"
                        check = str(check) + f"{str(a)}"
                    try:
                        try:
                            result = event.make_result()
                            result.chain = [Plain(f"新人{user_umo}开始答题！")]
                            await self.context.send_message(group_umo, result)
                            logger.info(f"用户{user_umo}开始答题！")
                        except Exception as e:
                            await event.send(event.plain_result("消息发送失败，请检查后台日志"))
                            logger.error(f"向群 {group_umo} 发送消息失败: {e}")
                        yield event.plain_result(
                            f"考核开始，请使用“作答”指令以答题，“确定”指令以结束答题\n示例：\n作答abcabcabcabc")
                        yield event.plain_result(f"以下为题目，请于{self.limited_time}秒内完成\n\n{str(out)}")
                        logger.info("已发送题目！")

                        # ====================注册会话控制器====================
                        # @session_waiter 装饰器：创建一个等待用户回复的会话
                        # timeout=60: 会话超时时间60秒，超时后会抛出 TimeoutError
                        # record_history_chains=False: 不记录消息历史（节省内存）
                        @session_waiter(timeout=self.limited_time, record_history_chains=False)
                        async def quiz_waiter(controller: SessionController, event: AstrMessageEvent):
                            """
                            会话控制器的回调函数
                            在用户回复消息时会被调用
                            @session_waiter 回调中应使用 await event.send()，而不是 yield
                            """
                            logger.info("会话控制器正在运行")
                            if not hasattr(controller, 'initialized'):
                                controller.if_answer = False
                                controller.user_answer = ""
                                controller.mark = 0
                                controller.initialized = True
                            # 获取用户输入的文本，并去除首尾空格
                            answer = event.message_str.strip()
                            # ====================根据用户答案做出不同响应====================
                            if answer[:2] == "作答":
                                if len(answer[2:]) == self.finally_questions:
                                    controller.if_answer = True
                                    controller.user_answer = str(answer[2:])
                                    await event.send(event.plain_result("是否确定答案？如确定请输入“确定”"))
                                    logger.info(f"用户答案为{controller.user_answer}")
                                    return
                                else:
                                    await event.send(event.plain_result("你写多或者写少了！请重写"))
                                    logger.info(f"用户{user_umo}写多或者写少了！请重写")
                                    return

                            elif answer == "确定":
                                if controller.if_answer:
                                    await event.send(event.plain_result("已退出答题模式，正在审核中"))
                                    logger.info("已退出答题模式，正在审核中")
                                    for i1 in range(self.finally_questions):
                                        if controller.user_answer[i1] == check[i1]:
                                            controller.mark += self.total_score / self.finally_questions
                                    if controller.mark >= self.passing_line:
                                        await event.send(event.plain_result(
                                            f"恭喜！你以{controller.mark}分的成绩通过了考核！请加入主群：{self.main_group_id}并退出审核群！"))
                                        logger.info(
                                            f"恭喜！用户{user_umo}以{controller.mark}分的成绩通过了考核！请加入主群：{self.main_group_id}并退出审核群！")
                                        try:
                                            result = event.make_result()
                                            result.chain = [
                                                Plain(f"通过:新人{user_umo}以{controller.mark}分的成绩通过了考核！")]
                                            logger.info(f"已向群{group_umo}发送{user_umo}的卡片")
                                            await self.context.send_message(group_umo, result)
                                        except Exception as e:
                                            await event.send(event.plain_result("消息发送失败，请检查后台日志"))
                                            logger.error(f"向群 {group_umo} 发送消息失败: {e}")
                                        controller.stop()
                                        del self.active_sessions[user_id]
                                        return
                                    else:
                                        await event.send(event.plain_result(
                                            f"你的成绩{controller.mark}分低于及格线{self.passing_line}分没有通过，请自觉退群"))
                                        logger.error(
                                            f"用户{user_umo}的成绩{controller.mark}分低于及格线{self.passing_line}分没有通过，请自觉退群")
                                        try:
                                            result = event.make_result()
                                            result.chain = [Plain(
                                                f"未通过:新人{user_umo}的成绩{controller.mark}分低于及格线{self.passing_line}分，未通过！")]
                                            await self.context.send_message(group_umo, result)
                                            logger.info(f"已向群{group_umo}发送{user_umo}的卡片")
                                        except Exception as e:
                                            await event.send(event.plain_result("消息发送失败，请检查后台日志"))
                                            logger.error(f"向群 {group_umo} 发送消息失败: {e}")
                                        controller.stop()
                                        del self.active_sessions[user_id]
                                        return
                                else:
                                    await event.send(event.plain_result("未作答！不能结束！"))
                                    return

                        try:
                            # ====================启动会话控制器====================
                            # await 会阻塞在这里，等待用户回复或超时
                            # 在会话期间，用户的所有消息都会被 quiz_waiter 拦截处理
                            # 其他指令（如 /help）此时不会生效
                            await quiz_waiter(event)
                            return
                        # ====================异常处理====================
                        except TimeoutError:
                            # 用户规定时间内没有回复，触发超时
                            yield event.plain_result("答题超时！结束考核！请联系管理员处理")
                            logger.info(f"用户{user_umo}答题超时！结束考核！请联系管理员处理")
                            try:
                                result = event.make_result()
                                result.chain = [Plain(f"未通过:新人{user_umo}作答超时！")]
                                await self.context.send_message(group_umo, result)
                                logger.info(f"已向群{group_umo}发送{user_umo}的卡片")
                            except Exception as e:
                                await event.send(event.plain_result("消息发送失败，请检查后台日志"))
                                logger.error(f"向群 {group_umo} 发送消息失败: {e}")
                            if user_id in self.active_sessions:
                                del self.active_sessions[user_id]
                            return
                        except Exception as e:
                            # 其他未预期的异常
                            logger.error(f"发生错误: {str(e)}")
                            yield event.plain_result(f"发生错误: {str(e)}")
                            if user_id in self.active_sessions:
                                del self.active_sessions[user_id]
                            return
                        finally:
                            # ====================最终清理====================
                            # finally 块无论是否发生异常都会执行
                            # stop_event() 结束当前消息事件的传播
                            # 防止后续处理器（如其他插件或 LLM）再次处理这条消息
                            event.stop_event()
                    except Exception as e:
                        logger.error("会话控制器发生错误: " + str(e))
                        if user_id in self.active_sessions:
                            del self.active_sessions[user_id]
                        return
                else:
                    yield event.plain_result(f"你不在群 {self.examine_group_id} 中！请尝试先加群！")
                    return
            except Exception as e:
            # API 报错通常意味着用户不在群中或网络问题
                logger.error(f"查询成员失败: {e}")
                yield event.plain_result(f"你不在群 {self.examine_group_id} 中！（或查询失败）请尝试先加群！")
                return
        else:
            yield event.plain_result("请在私聊或临时会话中使用该指令")
            return

    async def terminate(self):
        '''当插件被卸载或停用时调用，用于释放资源（如关闭数据库连接、停止定时任务等）'''
        logger.info("插件正在终止...")
        # 在这里添加你的清理代码