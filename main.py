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


@register("astrbot_plugin_examine", "语芮澈", "功能完善的入群自动考核插件！", "v2.2.1", "https://github.com/YuRuiChe/astrbot_plugin_examine")
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
        self.randomly_selected_questions = answer.get("randomly_selected_questions", False)
        self.stream_output_question = answer.get("stream_output_question", False)
        self.finally_questions = answer.get("finally_questions", 15)
        self.total_score = answer.get("total_score", 100)
        self.passing_line = answer.get("passing_line", 60)
        self.limited_time = answer.get("limited_time", 100)
        self.read_time = answer.get("read_time", 60)
        question_bank = config.get("question_bank") or {}
        self.question_bank_file = question_bank.get("question_bank_file", "")
        # 在读取文件之前，先规范化路径
        self.question_bank_file = os.path.abspath(self.question_bank_file)
        card = config.get("card") or {}
        self.send_user_answer = card.get("send_user_answer", True)
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
        group_id = raw_message.get("group_id") 
        if str(group_id) != self.examine_group_id:
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
            user_umo = event.unified_msg_origin
            user_umo = str(user_umo).replace(f'{self.bot_name}:FriendMessage:', '')# 获取用户qq号
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
                    user_id=int(user_id)
                )
                if member_info:# 如果在群里
                    out = ""
                    check = ""
                    user_name = member_info.get("card") or member_info.get("nickname") or str(user_id)# 获取用户qq昵称
                    # 读取题库
                    try:
                        with open(self.question_bank_file, 'r', encoding='utf-8') as f:
                            question_bank_file = json.load(f)
                    except FileNotFoundError:
                        logger.error(f"文件不存在: {self.question}")
                        del self.active_sessions[user_id]
                        return
                    except Exception as e:
                        logger.error(f"读取文件出错: {e}")
                        del self.active_sessions[user_id]
                        return
                    if self.stream_output_question is False:# 如果关闭了逐题发送
                        if self.randomly_selected_questions:  # 如果开启随机抽题
                            line_list = set()
                            for i in range(int(self.finally_questions)):
                                while True:
                                    line = random.randint(1, len(question_bank_file))
                                    # 检查这个题号是否已经被抽过
                                    if line not in line_list:
                                        # 没抽过 → 把它加入已抽集合
                                        line_list.add(line)
                                        # 退出 while 循环，继续下一道题
                                        break
                                    # 如果抽过了，不会执行 break，会继续 while 循环重新随机
                                try:
                                    q = question_bank_file[str(line)]['question']
                                    o = question_bank_file[str(line)]['option']
                                    a = question_bank_file[str(line)]['answer']
                                except KeyError:
                                    logger.error(f"键不存在: {str(i+1)}")
                                    del self.active_sessions[user_id]
                                    return
                                out = str(out) + f"\n{str(q)}\n{str(o)}\n"
                                check = str(check) + f"{str(a)}"
                        elif self.randomly_selected_questions is False:# 没开启随机抽题
                            for i in range(int(self.finally_questions)):
                                try:
                                    q = question_bank_file[str(i+1)]['question']
                                    o = question_bank_file[str(i+1)]['option']
                                    a = question_bank_file[str(i+1)]['answer']
                                except KeyError:
                                    logger.error(f"键不存在: {str(i+1)}")
                                    del self.active_sessions[user_id]
                                    return
                                out = str(out) + f"\n{str(q)}\n{str(o)}\n"
                                check = str(check) + f"{str(a)}"
                        try:
                            try:
                                result = event.make_result()
                                result.chain = [Plain(f"账号{user_name}{user_umo}开始答题！")]
                                await self.context.send_message(group_umo, result)
                                logger.info(f"账号{user_name}{user_umo}开始答题！")
                            except Exception as e:
                                await event.send(event.plain_result("消息发送失败，请检查后台日志"))
                                logger.error(f"向群 {group_umo} 发送消息失败: {e}")
                                del self.active_sessions[user_id]
                                return
                            yield event.plain_result(f"考前须知：\n\n请使用“作答”指令以答题，“确定”指令以结束答题\n示例：\n作答abcabcabcabc（前面一定要有“作答”二字！）\n\n总共有{self.finally_questions}道题，写多写少会提示\n请于{self.limited_time}秒内完成答题\n\n题目将于{self.read_time}秒后发送")
                            logger.info("已发送考前须知！")
                            time.sleep(self.read_time)
                            yield event.plain_result(f"考核开始，以下为题目，请于{self.limited_time}秒内完成，现在开始计时\n\n{str(out)}")
                            logger.info("已发送题目！")
                            # ====================注册会话控制器====================
                            # @session_waiter 装饰器：创建一个等待用户回复的会话
                            # timeout，会话超时时间，超时后会抛出 TimeoutError
                            # record_history_chains=False: 不记录消息历史（节省内存）
                            @session_waiter(timeout=self.limited_time, record_history_chains=False)
                            async def quiz_waiter(controller: SessionController, event: AstrMessageEvent):
                                """
                                会话控制器的回调函数
                                在用户回复消息时会被调用
                                @session_waiter 回调中应使用 await event.send()，而不是 yield
                                """
                                # ===== 去重锁：防止同一事件被处理两次 =====
                                if not hasattr(controller, '_last_processed_msg_id'):
                                    controller._last_processed_msg_id = None
                                current_msg_id = event.message_obj.message_id if hasattr(event, 'message_obj') else None
                                if current_msg_id and current_msg_id == controller._last_processed_msg_id:
                                    logger.info(f"跳过重复消息: {current_msg_id}")
                                    return

                                if current_msg_id:
                                    controller._last_processed_msg_id = current_msg_id
                                # ===== 去重结束 =====
                                logger.info("会话控制器正在运行")
                                if not hasattr(controller, 'initialized'):
                                    controller.if_answer = False
                                    controller.user_answer = ""
                                    controller.mark = 0
                                    controller.initialized = True
                                    controller.user_answer_str = ""
                                # 获取用户输入的文本，并去除首尾空格
                                answer = event.message_str.strip()
                                # ====================根据用户答案做出不同响应====================
                                if answer[:2] == "作答":
                                    if len(answer[2:]) == self.finally_questions:
                                        controller.if_answer = True
                                        controller.user_answer = str(answer[2:])
                                        await event.send(event.plain_result("是否确定答案？如确定请输入“确定”"))
                                        logger.info(f"账号{user_name}答案为{controller.user_answer}")
                                        return
                                    else:
                                        await event.send(event.plain_result("你写多或者写少了！请重写"))
                                        logger.info(f"账号{user_name}{user_umo}写多或者写少了！请重写")
                                        return

                                elif answer == "确定":
                                    if controller.if_answer:
                                        await event.send(event.plain_result("已退出答题模式，正在审核中"))
                                        logger.info("已退出答题模式，正在审核中")
                                        for i1 in range(self.finally_questions):
                                            if controller.user_answer[i1] == check[i1]:
                                                controller.mark += self.total_score / self.finally_questions
                                                controller.user_answer_str = f"{controller.user_answer_str}|✅{i1+1}{controller.user_answer[i1]}"
                                            else:
                                                controller.user_answer_str = f"{controller.user_answer_str}|❌{i1+1}{controller.user_answer[i1]}"
                                        if controller.mark >= self.passing_line:
                                            await event.send(event.plain_result(
                                                f"恭喜！你以{controller.mark}分的成绩通过了考核！请加入主群：{self.main_group_id}并退出审核群！"))
                                            logger.info(
                                                f"恭喜！账号{user_name}{user_umo}以{controller.mark}分的成绩通过了考核！请加入主群：{self.main_group_id}并退出审核群！")
                                            if self.send_user_answer:
                                                try:
                                                    result = event.make_result()
                                                    result.chain = [Plain(f"✅通过:账号{user_name}{user_umo}以{controller.mark}分的成绩通过了考核！\n答案：\n{controller.user_answer_str}")]
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
                                                f"你的成绩{controller.mark}分低于及格线{self.passing_line}分没有通过！请联系管理员处理，或可尝试再次答题"))
                                            logger.error(
                                                f"账号{user_name}{user_umo}的成绩{controller.mark}分低于及格线{self.passing_line}分没有通过，请自觉退群")
                                            if self.send_user_answer:
                                                try:
                                                    result = event.make_result()
                                                    result.chain = [Plain(
                                                        f"❌未通过:账号{user_name}{user_umo}的成绩{controller.mark}分低于及格线{self.passing_line}分，未通过！\n答案：\n{controller.user_answer_str}")]
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
                                yield event.plain_result("答题超时！结束考核！请联系管理员处理，或可尝试再次答题")
                                logger.info(f"账号{user_name}{user_umo}答题超时！结束考核！")
                                try:
                                    result = event.make_result()
                                    result.chain = [Plain(f"❌未通过:账号{user_name}{user_umo}作答超时！")]
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
                    else:# 如果开启了逐题发送
                        # 发送开始答题消息（保留原样）
                        try:
                            result = event.make_result()
                            result.chain = [Plain(f"账号{user_name}{user_umo}开始答题！")]
                            await self.context.send_message(group_umo, result)
                            logger.info(f"账号{user_name}{user_umo}开始答题！")
                        except Exception as e:
                            await event.send(event.plain_result("消息发送失败，请检查后台日志"))
                            logger.error(f"向群 {group_umo} 发送消息失败: {e}")
                            del self.active_sessions[user_id]
                            return

                        yield event.plain_result(f"考前须知：\n\n请使用“作答”指令以答题，“确定”指令以结束答题，“跳转“指令以跳转题目\n示例：\n作答a（表示填写第一道题的答案）\n跳转1（表示跳转到第1题）\n\n总共有{self.finally_questions}道题\n请于{self.limited_time}秒内完成答题\n\n题目将于{self.read_time}秒后发送")
                        logger.info("已发送考前须知！")
                        time.sleep(self.read_time)

                        # ==================== 生成题目列表（按顺序或随机） ====================
                        question_keys = list(question_bank_file.keys())

                        if self.randomly_selected_questions:
                            selected_keys = random.sample(question_keys, min(self.finally_questions, len(question_keys)))
                        else:
                            # 顺序取前 self.finally_questions 个（若键不是从1开始，可调整为 sorted，这里按原顺序）
                            selected_keys = question_keys[:self.finally_questions]

                        # 构建题目列表，每个元素包含 question, option, answer
                        question_list = []
                        for key in selected_keys:
                            try:
                                q_data = question_bank_file[key]
                                question_list.append({
                                    'question': q_data['question'],
                                    'option': q_data['option'],
                                    'answer': q_data['answer']
                                })
                            except KeyError:
                                logger.error(f"题库键{key}缺少字段")
                                del self.active_sessions[user_id]
                                return

                        # 如果实际题目数量少于设定值，以实际为准
                        actual_total = len(question_list)
                        if actual_total < self.finally_questions:
                            logger.warning(f"实际题目数量{actual_total}少于设定{self.finally_questions}，将按实际数量进行")
                            self.finally_questions = actual_total

                        # ==================== 会话控制器（逐题交互） ====================
                        @session_waiter(timeout=self.limited_time, record_history_chains=False)
                        async def quiz_waiter(controller: SessionController, event: AstrMessageEvent):
                            """
                            会话控制器的回调函数
                            在用户回复消息时会被调用
                            @session_waiter 回调中应使用 await event.send()，而不是 yield
                            """
                            # ===== 去重锁：防止同一事件被处理两次 =====
                            if not hasattr(controller, '_last_processed_msg_id'):
                                controller._last_processed_msg_id = None
                            current_msg_id = event.message_obj.message_id if hasattr(event, 'message_obj') else None
                            if current_msg_id and current_msg_id == controller._last_processed_msg_id:
                                logger.info(f"跳过重复消息: {current_msg_id}")
                                return
                            if current_msg_id:
                                controller._last_processed_msg_id = current_msg_id
                            # ===== 去重结束 =====

                            logger.info("会话控制器正在运行")

                            # ===== 初始化会话状态（仅首次执行） =====
                            if not hasattr(controller, 'initialized'):
                                controller.initialized = True
                                controller.current_index = 0                 # 当前题号（从0开始）
                                controller.score = 0                         # 累计分数（保留原变量名）
                                controller.question_list = question_list     # 题目列表
                                controller.user_answer_str = ""              # 记录每题结果（保留原变量名）
                                controller.if_answer = False                 # 当前题是否已作答（原变量名）
                                controller.temp_answer = ""                  # 暂存当前题的答案
                                controller.state = 'waiting_answer'          # 状态：'waiting_answer' | 'waiting_confirm' | 'waiting_finish'

                                # 发送第一道题
                                await send_question(controller, event)
                                return

                            # ===== 处理用户输入 =====
                            user_input = event.message_str.strip()

                            # ---- 辅助函数：跳转到指定题目 ----
                            async def jump_to_question(target_num):
                                """跳转到第 target_num 题（1-based）"""
                                if target_num < 1 or target_num > self.finally_questions:
                                    await event.send(event.plain_result(f"题号范围应为 1~{self.finally_questions }"))
                                    return False
                                target_idx = target_num - 1
                                if target_idx == controller.current_index:
                                    await event.send(event.plain_result("您已在此题，无需跳转"))
                                    return False
                                # 跳转后，当前题不计分，重置状态
                                controller.current_index = target_idx
                                controller.state = 'waiting_answer'
                                controller.if_answer = False
                                controller.temp_answer = ""
                                # 发送新题目
                                await send_question(controller, event)
                                return True

                            # ---- 根据当前状态处理 ----
                            # 状态1: 等待用户作答
                            if controller.state == 'waiting_answer':
                                # 检查是否 "作答X"
                                if user_input.startswith("作答") and len(user_input[2:]) == 1:
                                    controller.temp_answer = user_input[2:]
                                    controller.if_answer = True
                                    controller.state = 'waiting_confirm'
                                    await event.send(event.plain_result(f"已收到答案：{controller.temp_answer}，请输入“确定”提交或“跳转N”跳转"))
                                    logger.info(f"账号{user_name}{user_umo}提交答案：{controller.temp_answer}")
                                    return
                                # 检查是否 "跳转N"
                                elif user_input.startswith("跳转"):
                                    parts = user_input[2:].strip()
                                    if parts.isdigit():
                                        target = int(parts)
                                        await jump_to_question(target)
                                        return
                                    else:
                                        await event.send(event.plain_result("格式错误！请使用“跳转N”，N为数字（如跳转3）"))
                                        return
                                else:
                                    await event.send(event.plain_result("请先输入“作答X”回答当前题目，或“跳转N”跳转"))
                                    return

                            # 状态2: 等待用户确认答案
                            elif controller.state == 'waiting_confirm':
                                # 用户输入 "确定"
                                if user_input == "确定":
                                    if not controller.if_answer:
                                        await event.send(event.plain_result("您还未作答，请先输入“作答X”"))
                                        return
                                    # 判题
                                    correct_answer = controller.question_list[controller.current_index]['answer'].strip()
                                    if controller.temp_answer == correct_answer:
                                        controller.score += self.total_score / self.finally_questions
                                        controller.user_answer_str += f"|✅{controller.current_index+1}{controller.temp_answer}"
                                        logger.info(f"账号{user_name}{user_umo}第{controller.current_index+1}题正确")
                                    else:
                                        controller.user_answer_str += f"|❌{controller.current_index+1}{controller.temp_answer}"
                                        logger.info(f"账号{user_name}{user_umo}第{controller.current_index+1}题错误，正确答案为{correct_answer}")
                                    # 移动到下一题
                                    controller.current_index += 1
                                    controller.if_answer = False
                                    controller.temp_answer = ""

                                    # 判断是否答完所有题
                                    if controller.current_index >= self.finally_questions:
                                        # 全部答完，进入等待最终确认状态
                                        controller.state = 'waiting_finish'
                                        await event.send(event.plain_result("所有题目已答完！请输入“确定”以结束考核并查看成绩"))
                                        logger.info(f"账号{user_name}{user_umo}已答完所有题，等待最终确认")
                                    else:
                                        # 发送下一题，重置状态为等待作答
                                        controller.state = 'waiting_answer'
                                        await send_question(controller, event)
                                    return

                                # 用户输入 "跳转N"
                                elif user_input.startswith("跳转"):
                                    parts = user_input[2:].strip()
                                    if parts.isdigit():
                                        target = int(parts)
                                        # 跳转前，当前题不计分，重置状态
                                        controller.if_answer = False
                                        controller.temp_answer = ""
                                        await jump_to_question(target)
                                        return
                                    else:
                                        await event.send(event.plain_result("格式错误！请使用“跳转N”，N为数字"))
                                        return
                                else:
                                    await event.send(event.plain_result("请输入“确定”提交当前答案，或“跳转N”跳转"))
                                    return

                            # 状态3: 等待最终确认（所有题已答完）
                            elif controller.state == 'waiting_finish':
                                if user_input == "确定":
                                    # 计算最终成绩
                                    await finalize_quiz(controller, event)
                                    controller.stop()
                                    del self.active_sessions[user_id]
                                    return
                                else:
                                    await event.send(event.plain_result("请输入“确定”以结束考核"))
                                    return

                        # ===== 辅助函数：发送当前题目 =====
                        async def send_question(controller, event):
                            """发送当前 controller.current_index 对应的题目"""
                            idx = controller.current_index + 1
                            try:
                                q_data = controller.question_list[controller.current_index]
                                question_text = f"第{idx}题（共{self.finally_questions}题）\n{q_data['question']}\n{q_data['option']}"
                                await event.send(event.plain_result(question_text))
                                logger.info(f"已发送第{idx}题给{user_name}{user_umo}")
                            except IndexError:
                                logger.error(f"题目列表越界，当前索引 {controller.current_index}")
                                await event.send(event.plain_result("题目加载失败，请联系管理员"))
                                controller.stop()
                                del self.active_sessions[user_id]

                        # ===== 辅助函数：最终成绩处理 =====
                        async def finalize_quiz(controller, event):
                            """计算并发送最终成绩，与前面重复代码保持一致"""
                            final_score = controller.score
                            if final_score >= self.passing_line:
                                await event.send(event.plain_result(
                                    f"恭喜！你以{final_score}分的成绩通过了考核！请加入主群：{self.main_group_id}并退出审核群！"
                                ))
                                logger.info(f"恭喜！账号{user_name}{user_umo}以{final_score}分的成绩通过了考核！")
                                if self.send_user_answer:
                                    try:
                                        result = event.make_result()
                                        result.chain = [Plain(
                                            f"✅通过:账号{user_name}{user_umo}以{final_score}分的成绩通过了考核！\n答案：\n{controller.user_answer_str}"
                                        )]
                                        await self.context.send_message(group_umo, result)
                                        logger.info(f"已向群{group_umo}发送{user_umo}的卡片")
                                    except Exception as e:
                                        await event.send(event.plain_result("消息发送失败，请检查后台日志"))
                                        logger.error(f"向群 {group_umo} 发送消息失败: {e}")
                            else:
                                await event.send(event.plain_result(
                                    f"你的成绩{final_score}分低于及格线{self.passing_line}分没有通过！请联系管理员处理，或可尝试再次答题"
                                ))
                                logger.error(f"账号{user_name}{user_umo}的成绩{final_score}分低于及格线{self.passing_line}分，未通过")
                                if self.send_user_answer:
                                    try:
                                        result = event.make_result()
                                        result.chain = [Plain(
                                            f"❌未通过:账号{user_name}{user_umo}的成绩{final_score}分低于及格线{self.passing_line}分，未通过！\n答案：\n{controller.user_answer_str}"
                                        )]
                                        await self.context.send_message(group_umo, result)
                                        logger.info(f"已向群{group_umo}发送{user_umo}的卡片")
                                    except Exception as e:
                                        await event.send(event.plain_result("消息发送失败，请检查后台日志"))
                                        logger.error(f"向群 {group_umo} 发送消息失败: {e}")

                        # ===== 启动会话 =====
                        try:
                            await quiz_waiter(event)
                            return
                        except TimeoutError:
                            # 超时处理（保留原样）
                            await event.send(event.plain_result("答题超时！结束考核！请联系管理员处理，或可尝试再次答题"))
                            logger.info(f"账号{user_name}{user_umo}答题超时！结束考核！")
                            try:
                                result = event.make_result()
                                result.chain = [Plain(f"❌未通过:账号{user_name}{user_umo}作答超时！")]
                                await self.context.send_message(group_umo, result)
                                logger.info(f"已向群{group_umo}发送{user_umo}的卡片")
                            except Exception as e:
                                await event.send(event.plain_result("消息发送失败，请检查后台日志"))
                                logger.error(f"向群 {group_umo} 发送消息失败: {e}")
                            if user_id in self.active_sessions:
                                del self.active_sessions[user_id]
                            return
                        except Exception as e:
                            logger.error(f"发生错误: {str(e)}")
                            await event.send(event.plain_result(f"发生错误: {str(e)}"))
                            if user_id in self.active_sessions:
                                del self.active_sessions[user_id]
                            return
                        finally:
                            event.stop_event()
                else:
                    yield event.plain_result(f"你不在群 {self.examine_group_id} 中！请尝试先加群！")
                    if user_id in self.active_sessions:
                        del self.active_sessions[user_id]
                    return
            except Exception as e:
            # API 报错通常意味着用户不在群中或网络问题
                logger.error(f"查询成员失败: {e}")
                yield event.plain_result(f"你不在群 {self.examine_group_id} 中！（或查询失败）请尝试先加群！")
                if user_id in self.active_sessions:
                    del self.active_sessions[user_id]
                return
        else:
            yield event.plain_result("请在私聊或临时会话中使用该指令")
            return

    async def terminate(self):
        '''当插件被卸载或停用时调用，用于释放资源（如关闭数据库连接、停止定时任务等）'''
        logger.info("插件正在终止...")
        # 在这里添加你的清理代码