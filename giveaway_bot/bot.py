from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import logging
import random
import re
import shlex
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .storage import DB
from .utils import (
    esc,
    format_prize_items,
    jdump,
    jload,
    method_text,
    now,
    now_s,
    parse_dt,
    parse_duration,
    parse_int,
    parse_methods,
    parse_optional_int,
    parse_prize_items,
    parse_weight_map,
    sign_invite_payload,
    sign_join_payload,
    stable_result_hash,
    verify_invite_payload,
    verify_join_payload,
)

class GiveawayBot:

    def __init__(self, db: DB, api: TelegramClient) -> None:
        self.db = db
        self.api = api
        self.bot_username = ''
        self.invite_secret = hashlib.sha256(api.token.encode('utf-8')).hexdigest()
        self.finalize_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.scheduler_thread = threading.Thread(target=self.scheduler_loop, daemon=True)
        self.scheduler_thread.start()

    def run(self) -> None:
        offset = None
        try:
            self.bot_username = self.api.get_me().get('username', '')
        except Exception as exc:
            logging.warning('Bot username lookup failed at startup: %s', exc)
        logging.info('Bot started as @%s', self.bot_username or '(unknown)')
        while not self.stop_event.is_set():
            try:
                updates = self.api.get_updates(offset=offset, timeout=25)
                for update in updates:
                    offset = update['update_id'] + 1
                    self.handle_update(update)
                self.process_due_giveaways()
                self.process_due_claim_topics()
            except Exception as exc:
                logging.exception('Polling error: %s', exc)
                time.sleep(3)

    def scheduler_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.process_due_giveaways()
                self.process_due_claim_topics()
            except Exception as exc:
                logging.exception('Scheduler error: %s', exc)
            time.sleep(15)

    def handle_update(self, update: Dict[str, Any]) -> None:
        if 'message' in update:
            self.handle_message(update['message'])
        elif 'callback_query' in update:
            self.handle_callback(update['callback_query'])

    def is_private_chat(self, chat: Dict[str, Any]) -> bool:
        return chat.get('type') == 'private'

    def handle_message(self, message: Dict[str, Any]) -> None:
        user = message.get('from') or {}
        chat = message.get('chat') or {}
        text = (message.get('text') or '').strip()
        if not user:
            return
        self.db.upsert_user(user)
        if text.startswith('/start'):
            payload = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ''
            self.handle_start(message, payload)
            return
        if text.startswith('/'):
            self.handle_command(message, text)
            return
        if self.is_private_chat(chat):
            self.handle_keyword_join(message)
        else:
            self.handle_group_message(message)

    def handle_start(self, message: Dict[str, Any], payload: str) -> None:
        user = message['from']
        chat = message['chat']
        self.db.mark_private_chat_started(user['id'])
        if not self.is_private_chat(chat):
            self.api.send_message(chat['id'], '请继续在私聊中与我交互。')
            return
        if payload:
            signed_join = verify_join_payload(self.invite_secret, payload)
            if signed_join is not None:
                giveaway_id = signed_join
                giveaway = self.db.get_giveaway(giveaway_id)
                if giveaway:
                    self.show_private_panel(chat['id'], user['id'], giveaway_id)
                return
            signed_invite = verify_invite_payload(self.invite_secret, payload)
            if signed_invite:
                giveaway_id, referrer_id = signed_invite
                giveaway = self.db.get_giveaway(giveaway_id)
                if not giveaway:
                    self.api.send_message(chat['id'], '这个抽奖不存在，或已被删除。')
                    return
                if user['id'] == referrer_id:
                    self.api.send_message(chat['id'], '不能邀请自己。')
                    return
                if self.db.add_referral(giveaway_id, referrer_id, user['id'], payload):
                    self.db.log('referral_registered', user['id'], giveaway_id, {'referrer_user_id': referrer_id})
                    self.api.send_message(chat['id'], '邀请记录已保存。')
                else:
                    self.api.send_message(chat['id'], '这条邀请记录已经存在。')
                return
        active = self.db.all("SELECT * FROM giveaways WHERE status IN ('scheduled', 'live') ORDER BY id DESC LIMIT 3")
        if not active:
            self.api.send_message(chat['id'], self.help_text())
            return
        buttons = [[{'text': f'查看 #{row["id"]} {row["title"][:18]}', 'url': f'https://t.me/{self.bot_username}?start={sign_join_payload(self.invite_secret, row["id"])}'}] for row in active]
        self.api.send_message(chat['id'], '欢迎来到抽奖机器人。点击下方按钮查看可参与抽奖，参与仍在群里完成。', reply_markup={'inline_keyboard': buttons})

    def handle_command(self, message: Dict[str, Any], text: str) -> None:
        chat = message['chat']
        parts = text.split(maxsplit=1)
        command = parts[0].split('@', 1)[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ''
        if command == '/help':
            self.api.send_message(chat['id'], self.help_text())
            return
        if command in {'/new_giveaway', '/edit_giveaway', '/set_claim_topic', '/list_giveaways', '/end_giveaway', '/cancel_giveaway', '/export'} and not self.is_private_chat(chat):
            self.api.send_message(chat['id'], '请到和 bot 的私聊里执行管理员命令。')
            return
        if command == '/new_giveaway':
            self.create_giveaway_command(message, arg)
            return
        if command == '/edit_giveaway':
            self.edit_giveaway_command(message, arg)
            return
        if command == '/set_claim_topic':
            self.set_claim_topic_command(message, arg)
            return
        if command == '/list_giveaways':
            self.list_giveaways(message, arg)
            return
        if command == '/end_giveaway':
            self.close_giveaway(message, arg, cancel=False)
            return
        if command == '/cancel_giveaway':
            self.close_giveaway(message, arg, cancel=True)
            return
        if command == '/export':
            self.export_participants(message, arg)
            return
        self.api.send_message(chat['id'], '未识别的命令。发送 /help 查看可用命令。')

    def help_text(self) -> str:
        return (
            '<b>抽奖机器人帮助</b>\n\n'
            '管理员命令：\n'
            '/new_giveaway 直接创建抽奖\n'
            '/set_claim_topic &lt;id&gt; [flags] 修改领奖话题\n'
            '/list_giveaways 查看抽奖\n'
            '/end_giveaway &lt;id&gt; 提前开奖\n'
            '/cancel_giveaway &lt;id&gt; 取消抽奖\n'
            '/export &lt;id&gt; 导出参与名单\n'
            '/help 查看帮助\n\n'
            '参与都在私聊里完成；群里按钮只负责跳转到私聊。'
        )

    def new_giveaway_help_text(self) -> str:
        return (
            '<b>创建抽奖示例</b>\n\n'
            '<code>/new_giveaway -title "新年抽奖" -prize "AirPods" -num 3 -condition "关注频道并参与" '
            '-start now -t 8h -methods button,keyword,channel,invite -keyword "抽奖" '
            '-check_channel @mychannel -invite_need 2 -invite_bonus 1 '
            '-weight "{\"123456\":100,\"234567\":50}" -publish @mygroup -draw_n 100 '
            '-claim_group @claimgroup -claim_topic "领奖话题" -claim_hours 72</code>\n\n'
            '参数说明：\n'
            '• `-t 8h` 表示从开始时间起 8 小时后结束；也可以用 `1d2h30m`，不写则表示不限时长。\n'
            '• `-num 3` 表示抽 3 人中奖。\n'
            '• `-weight` 支持 JSON 或 `uid:weight`，用于指定抽奖人权重。\n'
            '• `-draw_n 100` 表示参与人数达到 100 人时自动开奖。\n'
            '• `-check_channel` 表示需要检查是否关注指定群组/频道。\n'
            '• `-claim_group`、`-claim_topic`、`-claim_hours` 用于创建领奖话题。\n'
            '• 如果不写 `-t` 和 `-end`，抽奖就是不限时间，直到手动开奖或人数触发。'
        )

    def parse_command_flags(self, arg: str) -> Dict[str, str]:
        if not arg.strip():
            return {}
        tokens = shlex.split(arg)
        result: Dict[str, str] = {}
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if not token.startswith('-'):
                raise ValueError(f'无法识别的参数：{token}')
            key = token.lstrip('-').lower()
            value = 'true'
            if i + 1 < len(tokens) and not tokens[i + 1].startswith('-'):
                value = tokens[i + 1]
                i += 2
            else:
                i += 1
            result[key] = value
        return result


    def flag_value(self, flags: Dict[str, str], *keys: str) -> Optional[str]:
        for key in keys:
            if key not in flags:
                continue
            value = flags.get(key)
            if value is None:
                continue
            value = str(value).strip()
            if value:
                return value
        return None



    def resolve_username_to_user_id(self, username: str) -> Optional[int]:
        return self.db.get_user_id_by_username(username)

    def create_giveaway_command(self, message: Dict[str, Any], arg: str) -> None:
        user = message['from']
        chat = message['chat']
        if not arg.strip():
            self.api.send_message(chat['id'], self.new_giveaway_help_text())
            return
        try:
            flags = self.parse_command_flags(arg)
            giveaway = self.build_giveaway_from_flags(user, flags)
            giveaway_id = self.db.create_giveaway(giveaway)
            for uid, weight in giveaway['special_weights'].items():
                self.db.set_giveaway_weight(giveaway_id, int(uid), int(weight), reason='special_weight')
            self.db.log('giveaway_created', user['id'], giveaway_id, {'title': giveaway['title'], 'auto_draw_mode': giveaway['auto_draw_mode']})
            self.api.send_message(chat['id'], f'✅ 抽奖已创建，编号 #{giveaway_id}。\n如果已到开始时间，机器人会自动发布公告。')
            created = self.db.get_giveaway(giveaway_id)
            if created and created['status'] == 'live' and not created.get('announcement_message_id'):
                self.announce_giveaway(giveaway_id)
        except Exception as exc:
            self.api.send_message(chat['id'], f'❌ 创建失败：{exc}')

    def build_giveaway_from_flags(self, user: Dict[str, Any], flags: Dict[str, str]) -> Dict[str, Any]:
        title = self.flag_value(flags, 'title')
        prize = self.flag_value(flags, 'prize')
        winner_count_text = self.flag_value(flags, 'num', 'winner_count', 'winners')
        publish_chat_ref = self.flag_value(flags, 'publish', 'publish_chat', 'chat')
        if not title:
            raise ValueError('抽奖标题不能为空。')
        if not prize:
            raise ValueError('奖品不能为空。')
        if not winner_count_text:
            raise ValueError('中奖人数不能为空。')
        if not publish_chat_ref:
            raise ValueError('公布结果的群组或频道不能为空。')
        if not self.is_admin_of_chat(publish_chat_ref, user['id']):
            raise ValueError('你不是结果发布群组/频道的管理员，无法创建。')

        start_time_text = self.flag_value(flags, 'start') or 'now'
        start_time = parse_dt(start_time_text)
        end_time = None
        if 'end' in flags and flags['end'].strip():
            end_time = parse_dt(flags['end'])
        else:
            duration_text = self.flag_value(flags, 't', 'time', 'duration')
            if duration_text:
                duration = parse_duration(duration_text)
                if duration is not None:
                    end_time = start_time + duration

        if end_time is not None and end_time <= start_time:
            raise ValueError('结束时间必须晚于开始时间。')

        methods_text = self.flag_value(flags, 'methods', 'method', 'entry')
        methods = parse_methods(methods_text) if methods_text else ['button']
        entry_keyword = self.flag_value(flags, 'keyword') or None
        if 'keyword' in methods and not entry_keyword:
            raise ValueError('选择了关键词参与方式时，必须提供 -keyword。')

        require_channel = False
        required_channel = self.flag_value(flags, 'check_channel', 'channel') or ''
        if required_channel:
            require_channel = True

        invite_required_count = parse_optional_int(self.flag_value(flags, 'invite_need', 'invite_count'), '邀请人数', default=0, minimum=0)
        invite_weight_bonus = parse_optional_int(self.flag_value(flags, 'invite_bonus', 'invite_weight_bonus'), '邀请加权', default=0, minimum=0)
        publish_thread_text = self.flag_value(flags, 'thread', 'publish_thread')
        publish_chat_thread_id = int(publish_thread_text) if publish_thread_text else None

        draw_when_participants_text = self.flag_value(flags, 'draw_n', 'draw_when', 'participants')
        draw_when_participants = parse_optional_int(draw_when_participants_text, '指定人数开奖人数', default=0, minimum=0) if draw_when_participants_text else None
        auto_draw_mode = 'time'
        if draw_when_participants:
            auto_draw_mode = 'participants'
        if draw_when_participants and end_time is not None:
            auto_draw_mode = 'both'
        if end_time is None and not draw_when_participants:
            auto_draw_mode = 'manual'

        special_weights = parse_weight_map(self.flag_value(flags, 'weight'), self.resolve_username_to_user_id)
        claim_topic_enabled = bool(self.flag_value(flags, 'claim_group') or self.flag_value(flags, 'claim_topic'))
        claim_group_ref = self.flag_value(flags, 'claim_group') or None
        claim_topic_name = self.flag_value(flags, 'claim_topic') or None
        claim_topic_hours = parse_optional_int(self.flag_value(flags, 'claim_hours', 'claim_topic_hours'), '领奖话题有效小时数', default=72, minimum=1)
        if claim_topic_enabled and not claim_group_ref:
            raise ValueError('启用领奖话题时必须提供 -claim_group。')
        if claim_topic_enabled and not self.is_admin_of_chat(claim_group_ref, user['id']):
            raise ValueError('你不是领奖群聊的管理员，无法创建领奖话题。')

        claim_deadline_base = end_time or start_time
        claim_deadline = (claim_deadline_base + dt.timedelta(hours=claim_topic_hours)).isoformat(sep=' ')
        seed_hash = hashlib.sha256(jdump({
            'title': title,
            'prize': prize,
            'winner_count': int(winner_count_text),
            'start_time': start_time.isoformat(sep=' '),
            'end_time': end_time.isoformat(sep=' ') if end_time else None,
            'publish_chat_ref': publish_chat_ref,
            'draw_when_participants': draw_when_participants,
        }).encode('utf-8')).hexdigest()

        return {
            'title': title,
            'prize': prize,
            'winner_count': int(winner_count_text),
            'participation_condition': self.flag_value(flags, 'condition', 'cond') or '无',
            'start_time': start_time.isoformat(sep=' '),
            'end_time': end_time.isoformat(sep=' ') if end_time else None,
            'auto_draw_mode': auto_draw_mode,
            'draw_when_participants': draw_when_participants,
            'entry_methods': methods,
            'entry_keyword': entry_keyword,
            'require_channel': require_channel,
            'required_channel': required_channel or None,
            'invite_required_count': invite_required_count,
            'invite_weight_bonus': invite_weight_bonus,
            'publish_chat_ref': publish_chat_ref,
            'publish_chat_thread_id': publish_chat_thread_id,
            'status': 'live' if start_time <= now() else 'scheduled',
            'created_by': user['id'],
            'created_by_username': user.get('username'),
            'claim_deadline': claim_deadline,
            'seed_hash': seed_hash,
            'claim_topic_enabled': claim_topic_enabled,
            'claim_group_ref': claim_group_ref,
            'claim_topic_name': claim_topic_name,
            'claim_topic_hours': claim_topic_hours,
            'claim_topic_thread_id': None,
            'claim_topic_invite_link': None,
            'claim_topic_expire_at': None,
            'claim_topic_deleted_at': None,
            'participant_notice_message_id': None,
            'participant_notice_deleted_at': None,
            'special_weights': special_weights,
        }

    def show_private_panel(self, chat_id: Any, user_id: int, giveaway_id: int, intro: str = '') -> None:
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            self.api.send_message(chat_id, '这个抽奖不存在，或已被删除。')
            return
        if giveaway['status'] == 'cancelled':
            self.api.send_message(chat_id, '这个抽奖已经取消。')
            return
        count = self.db.count_participants(giveaway_id)
        methods = jload(giveaway['entry_methods'], [])
        lines = []
        if intro:
            lines.append(intro)
        lines.extend([
            f'<b>抽奖 #{giveaway_id}</b>',
            f'活动：{esc(giveaway["title"])}',
            f'奖品：{esc(giveaway["prize"])}',
            f'已参与人数：{count}',
            f'中奖人数：{giveaway["winner_count"]}',
            f'开始时间：{esc(giveaway["start_time"])}',
            f'结束时间：{esc(giveaway["end_time"] or "不限")}',
            f'参与方式：{esc(method_text(methods, giveaway["entry_keyword"], giveaway["required_channel"], int(giveaway["invite_required_count"] or 0), int(giveaway["invite_weight_bonus"] or 0)))}',
            f'参与条件：{esc(giveaway["participation_condition"])}',
        ])
        buttons = []
        buttons.append([{'text': '参与抽奖', 'callback_data': f'join:{giveaway_id}'}])
        if int(giveaway['invite_required_count'] or 0) > 0 or int(giveaway['invite_weight_bonus'] or 0) > 0:
            buttons.append([{'text': '我的邀请链接', 'url': f'https://t.me/{self.bot_username}?start={sign_invite_payload(self.invite_secret, giveaway_id, user_id)}'}])
        self.api.send_message(chat_id, '\n'.join(lines), reply_markup={'inline_keyboard': buttons})

    def handle_callback(self, callback_query: Dict[str, Any]) -> None:
        user = callback_query.get('from') or {}
        message = callback_query.get('message') or {}
        chat = message.get('chat') or {}
        data = callback_query.get('data') or ''
        self.db.upsert_user(user)
        if data.startswith('list:'):
            self.handle_list_giveaways_callback(callback_query, data.split(':', 1)[1])
            return

        if data.startswith('verify:'):
            self.handle_result_verify(callback_query, int(data.split(':', 1)[1]))
            return
        if data.startswith('join:'):
            giveaway_id = int(data.split(':', 1)[1])
            giveaway = self.db.get_giveaway(giveaway_id)
            if not giveaway:
                self.api.answer_callback_query(callback_query['id'], '抽奖不存在。', show_alert=True)
                return
            if not self.db.has_private_chat_started(user['id']):
                self.api.answer_callback_query(callback_query['id'], '请先私聊 bot 一次，再回到群里参与。', show_alert=True)
                try:
                    self.api.send_message(user['id'], f'先和我私聊一次，再回到群里点参与。\n\n抽奖：#{giveaway_id} {giveaway["title"]}\n启动私聊：https://t.me/{self.bot_username}?start=join_{giveaway_id}')
                except Exception:
                    pass
                return
            ok, msg = self.try_join(giveaway_id, user, source='button', chat_id=chat.get('id'))
            self.api.answer_callback_query(callback_query['id'], msg, show_alert=not ok)
            if ok and chat.get('id'):
                self.send_temporary_message(chat['id'], f'✅ {self.display_user(user.get("username"), user.get("first_name"), user.get("last_name"), user["id"])} 已成功参与抽奖。')
            return
        self.api.answer_callback_query(callback_query['id'], '未识别的操作。', show_alert=True)

    def handle_group_message(self, message: Dict[str, Any]) -> None:
        user = message.get('from') or {}
        chat = message.get('chat') or {}
        text = (message.get('text') or '').strip()
        if not text or user.get('is_bot'):
            return
        if not self.is_bot_mentioned(message):
            return
        if not self.is_admin_of_chat(chat.get('id'), user.get('id')):
            return
        giveaway = self.parse_natural_giveaway(message)
        if not giveaway:
            return
        try:
            giveaway_id = self.db.create_giveaway(giveaway)
            for uid, weight in giveaway['special_weights'].items():
                self.db.set_giveaway_weight(giveaway_id, int(uid), int(weight), reason='special_weight')
            self.db.log('giveaway_created_natural', user['id'], giveaway_id, {'title': giveaway['title'], 'publish_chat_ref': giveaway['publish_chat_ref']})
            self.api.send_message(chat['id'], f'✅ 已根据群内消息创建抽奖 #{giveaway_id}。', message_thread_id=message.get('message_thread_id'))
            created = self.db.get_giveaway(giveaway_id)
            if created and created['status'] == 'live' and not created.get('announcement_message_id'):
                self.announce_giveaway(giveaway_id)
        except Exception as exc:
            self.api.send_message(chat['id'], f'❌ 创建失败：{exc}', message_thread_id=message.get('message_thread_id'))

    def is_bot_mentioned(self, message: Dict[str, Any]) -> bool:
        text = message.get('text') or ''
        if not self.bot_username:
            return False
        target = f'@{self.bot_username}'.lower()
        if target in text.lower():
            return True
        for entity in message.get('entities') or []:
            if entity.get('type') != 'mention':
                continue
            start = int(entity.get('offset', 0))
            end = start + int(entity.get('length', 0))
            if text[start:end].lower() == target:
                return True
        return False

    def _first_match(self, text: str, patterns: List[str]) -> Optional[str]:
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                return value or None
        return None

    def _int_match(self, text: str, patterns: List[str]) -> Optional[int]:
        value = self._first_match(text, patterns)
        return int(value) if value and value.isdigit() else None

    def _extract_freeform_title(self, text: str) -> Optional[str]:
        raw = (text or '').strip()
        if not raw:
            return None
        first_part = re.split(r'[\uFF0C,\u3002\uFF1B;]+', raw, maxsplit=1)[0].strip()
        if not first_part:
            return None
        if re.match(r'^(?:\u5956\u54C1|\u793C\u7269|\u5956\u9879|\u53D1\u5E03\u5230|\u516C\u5E03\u5230|\u5173\u6CE8|\u9700\u5173\u6CE8|\u52A0\u5165|\u5F00\u5956|\u622A\u6B62|\u7ED3\u675F|\u62BD\s*\d+\s*\u4EBA|\u6EE1\s*\d+\s*\u4EBA)', first_part, flags=re.IGNORECASE):
            return None
        if re.search(r'(?:\u5956\u54C1|\u53D1\u5E03\u5230|\u516C\u5E03\u5230|\u9700\u5173\u6CE8|\u9886\u5956|\u8BDD\u9898|\u6743\u91CD|\u622A\u6B62|\u7ED3\u675F)', first_part, flags=re.IGNORECASE):
            return None
        return first_part[:80]

    def _extract_special_weights(self, message: Dict[str, Any], text: str) -> Dict[int, int]:
        result: Dict[int, int] = {}
        for entity in message.get('entities') or []:
            if entity.get('type') != 'text_mention':
                continue
            user = entity.get('user') or {}
            start = int(entity.get('offset', 0))
            end = start + int(entity.get('length', 0))
            tail = text[end:end + 48]
            weight = self._int_match(tail, [r'(?:\u6743\u91CD|\u52A0\u6743|weight)\s*(\d+)', r'\+(\d+)'])
            if weight is not None:
                result[int(user['id'])] = max(1, weight)
        return result

    def _extract_special_weights_from_text(self, text: str) -> Dict[int, int]:
        result: Dict[int, int] = {}
        for match in re.finditer(r'@([\w_\-]+)\s*(?:\u6743\u91CD|\u52A0\u6743|weight)\s*(\d+)', text, flags=re.IGNORECASE):
            username = match.group(1)
            weight = max(1, int(match.group(2)))
            user_id = self.resolve_username_to_user_id(username)
            if user_id is not None:
                result[int(user_id)] = weight
        return result


    def parse_natural_giveaway(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        chat = message.get('chat') or {}
        user = message.get('from') or {}
        raw_text = (message.get('text') or '').strip()
        if not raw_text:
            return None

        text = raw_text
        if self.bot_username:
            text = re.sub(rf'@{re.escape(self.bot_username)}\b', '', text, flags=re.IGNORECASE).strip()

        clauses = [part.strip() for part in re.split(r'[，,。；;]+', text) if part.strip()]
        if not clauses:
            return None

        def normalize_duration_token(value: str) -> str:
            token = re.sub(r'\s+', '', value).lower()
            token = token.replace('小时', 'h').replace('时', 'h')
            token = token.replace('分钟', 'm').replace('分', 'm')
            token = token.replace('天', 'd')
            token = token.replace('秒', 's')
            return token

        title = self._extract_freeform_title(clauses[0]) or '群内抽奖'
        prize = None
        winner_count = None
        start_time = now()
        end_time: Optional[dt.datetime] = None
        publish_ref = str(chat.get('id'))
        required_channel = None
        claim_group_ref = None
        claim_topic_name = None
        invite_required_count = 0
        invite_weight_bonus = 0
        draw_when_participants = None
        participation_condition = None

        for clause in clauses:
            if title == '群内抽奖':
                candidate = self._extract_freeform_title(clause)
                if candidate:
                    title = candidate

            if prize is None:
                match = re.search(r'(?:奖品|礼物|奖项)\s*[:?]?\s*(.+)$', clause, flags=re.IGNORECASE)
                if match:
                    prize = match.group(1).strip()

            if winner_count is None:
                match = re.search(r'(?:抽|中奖|选出|中出)\s*(\d+)\s*人', clause, flags=re.IGNORECASE)
                if not match:
                    match = re.search(r'(?:中奖人数|抽取人数|中出人数|人数)\s*[:?]?\s*(\d+)', clause, flags=re.IGNORECASE)
                if match:
                    winner_count = max(1, int(match.group(1)))

            if end_time is None:
                duration_match = re.search(r'(\d+(?:\s*(?:天|d|小时|时|h|分钟|分|m|秒|s))+)', clause, flags=re.IGNORECASE)
                if duration_match and re.search(r'(?:后)?(?:结束|截止|开奖)', clause, flags=re.IGNORECASE):
                    duration = parse_duration(normalize_duration_token(duration_match.group(1)))
                    if duration is not None:
                        end_time = start_time + duration
                else:
                    absolute_match = re.search(r'(?:结束时间|截止时间|结束|截止)\s*[:?]?\s*(.+)$', clause, flags=re.IGNORECASE)
                    if absolute_match:
                        try:
                            end_time = parse_dt(absolute_match.group(1).strip())
                        except Exception:
                            pass

            publish_match = re.search(r'(?:发布到|公布到|发布于|发到|发送到)\s*(@[\w_\-]+|-?\d+)', clause, flags=re.IGNORECASE)
            if publish_match:
                publish_ref = publish_match.group(1).strip()

            channel_match = re.search(r'(?:需关注|关注|加入)\s*(@[\w_\-]+|-?\d+)', clause, flags=re.IGNORECASE)
            if channel_match:
                required_channel = channel_match.group(1).strip()

            claim_group_match = re.search(r'(?:领奖群|领奖群聊|领奖频道|领取群|领取群聊)\s*(@[\w_\-]+|-?\d+)', clause, flags=re.IGNORECASE)
            if claim_group_match:
                claim_group_ref = claim_group_match.group(1).strip()

            claim_topic_match = re.search(r'(?:领奖话题|领奖主题|话题)\s*[:?]?\s*(.+)$', clause, flags=re.IGNORECASE)
            if claim_topic_match and claim_topic_name is None:
                claim_topic_name = claim_topic_match.group(1).strip()

            invite_need_match = re.search(r'(?:邀请|拉人|拉新)\s*(\d+)\s*人', clause, flags=re.IGNORECASE)
            if invite_need_match:
                invite_required_count = max(0, int(invite_need_match.group(1)))

            invite_bonus_match = re.search(r'(?:每次邀请|邀请.*?增加|邀请.*?加权|邀请奖励)\s*(\d+)', clause, flags=re.IGNORECASE)
            if invite_bonus_match:
                invite_weight_bonus = max(0, int(invite_bonus_match.group(1)))

            draw_match = re.search(r'(?:满|达到|凑足)\s*(\d+)\s*人\s*(?:开奖|抽奖|结束)', clause, flags=re.IGNORECASE)
            if draw_match:
                draw_when_participants = max(1, int(draw_match.group(1)))

            if participation_condition is None:
                condition_match = re.search(r'(?:参与条件|条件)\s*[:?]?\s*(.+)$', clause, flags=re.IGNORECASE)
                if condition_match:
                    participation_condition = condition_match.group(1).strip()

        prize = prize or '奖品1'
        prize_items = parse_prize_items(prize)
        if winner_count is None:
            winner_count = max(1, sum(int(item.get('count') or 1) for item in prize_items))

        if end_time is None and draw_when_participants is None:
            end_time = start_time + dt.timedelta(hours=1)

        if participation_condition is None:
            parts = ['点击按钮参与']
            if required_channel:
                parts.append(f'关注 {required_channel}')
            if invite_required_count:
                parts.append(f'邀请 {invite_required_count} 人')
            if draw_when_participants:
                parts.append(f'满 {draw_when_participants} 人开奖')
            participation_condition = '?'.join(parts)

        special_weights = self._extract_special_weights(message, text)
        special_weights.update(self._extract_special_weights_from_text(text))

        claim_topic_hours = 72
        claim_topic_enabled = bool(claim_group_ref or claim_topic_name)
        if claim_topic_enabled and claim_group_ref and not self.is_admin_of_chat(claim_group_ref, user.get('id')):
            raise ValueError('你不是领奖群聊的管理员，无法创建领奖话题。')

        auto_draw_mode = 'time'
        if end_time is None and draw_when_participants is not None:
            auto_draw_mode = 'participants'
        elif end_time is None:
            auto_draw_mode = 'manual'
        elif draw_when_participants is not None:
            auto_draw_mode = 'both'

        seed_hash = hashlib.sha256(jdump({
            'title': title,
            'prize': prize,
            'prize_json': prize_items,
            'winner_count': winner_count,
            'start_time': start_time.isoformat(sep=' '),
            'end_time': end_time.isoformat(sep=' ') if end_time else None,
            'publish_chat_ref': publish_ref,
            'draw_when_participants': draw_when_participants,
        }).encode('utf-8')).hexdigest()

        return {
            'title': title,
            'prize': prize,
            'prize_json': prize_items,
            'winner_count': winner_count,
            'participation_condition': participation_condition,
            'start_time': start_time.isoformat(sep=' '),
            'end_time': end_time.isoformat(sep=' ') if end_time else None,
            'auto_draw_mode': auto_draw_mode,
            'draw_when_participants': draw_when_participants,
            'entry_methods': ['button'],
            'entry_keyword': None,
            'require_channel': bool(required_channel),
            'required_channel': required_channel,
            'invite_required_count': invite_required_count,
            'invite_weight_bonus': invite_weight_bonus,
            'publish_chat_ref': publish_ref,
            'publish_chat_thread_id': message.get('message_thread_id'),
            'status': 'live' if start_time <= now() else 'scheduled',
            'created_by': user['id'],
            'created_by_username': user.get('username'),
            'claim_deadline': (end_time or (start_time + dt.timedelta(hours=claim_topic_hours))).isoformat(sep=' '),
            'seed_hash': seed_hash,
            'claim_topic_enabled': claim_topic_enabled,
            'claim_group_ref': claim_group_ref,
            'claim_topic_name': claim_topic_name,
            'claim_topic_hours': claim_topic_hours,
            'claim_topic_thread_id': None,
            'claim_topic_invite_link': None,
            'claim_topic_expire_at': None,
            'claim_topic_deleted_at': None,
            'participant_notice_message_id': None,
            'participant_notice_deleted_at': None,
            'special_weights': special_weights,
        }

    def build_result_verification_text(self, giveaway: Dict[str, Any]) -> str:
        payload = jload(giveaway.get('result_payload_json'), None)
        if not payload:
            return (
                '<b>开奖结果哈希验证</b>' + chr(10) + chr(10) +
                '当前没有可验证的开奖数据，可能尚未开奖，或者开奖结果尚未成功写入数据库。'
            )

        current_hash = stable_result_hash(payload)
        stored_hash = giveaway.get('result_hash') or ''
        ok = bool(stored_hash) and stored_hash == current_hash
        expected_rule = "UTF-8 encoded JSON, sort_keys=True, separators=(',', ':')"
        return (
            '<b>开奖结果哈希验证</b>' + chr(10) + chr(10) +
            '算法：<code>SHA-256</code>' + chr(10) +
            f'标准化：<code>{esc(expected_rule)}</code>' + chr(10) +
            f'公告哈希：<code>{esc(stored_hash or "未设置")}</code>' + chr(10) +
            f'重算哈希：<code>{esc(current_hash)}</code>' + chr(10) +
            f'验证结果：<b>{"一致" if ok else "不一致"}</b>' + chr(10) + chr(10) +
            '验证方法：将开奖结果 JSON 按同样的标准化规则重新计算 SHA-256，再与公告哈希比对。'
        )

    def handle_result_verify(self, callback_query: Dict[str, Any], giveaway_id: int) -> None:
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            self.api.answer_callback_query(callback_query['id'], '抽奖不存在或已被删除。', show_alert=True)
            return
        payload = jload(giveaway.get('result_payload_json'), None)
        if not payload:
            self.api.answer_callback_query(callback_query['id'], '开奖结果尚未生成，或数据库里没有可验证的数据。', show_alert=True)
            return
        expected = stable_result_hash(payload)
        actual = giveaway.get('result_hash') or ''
        ok = expected == actual
        self.api.answer_callback_query(
            callback_query['id'],
            f'验证结果：{"一致" if ok else "不一致"}' + chr(10) +
            '算法：SHA-256' + chr(10) +
            f'重算值：{expected}' + chr(10) +
            f'公告值：{actual or "未设置"}',
            show_alert=True,
        )
        try:
            self.api.send_message(callback_query['from']['id'], self.build_result_verification_text(giveaway))
        except Exception:
            pass
            pass


    def display_user(self, username: Optional[str], first_name: Optional[str], last_name: Optional[str], user_id: int) -> str:
        name = ' '.join(part for part in [first_name, last_name] if part).strip()
        if username:
            return f'@{username}'
        if name:
            return f'{name} ({user_id})'
        return str(user_id)

    def display_user_html(self, username: Optional[str], first_name: Optional[str], last_name: Optional[str], user_id: int) -> str:
        name = ' '.join(part for part in [first_name, last_name] if part).strip()
        if username:
            return f'@{esc(username)}'
        if name:
            return f'{esc(name)} (<code>{user_id}</code>)'
        return f'<code>{user_id}</code>'

    def build_weighted_pool(self, giveaway: Dict[str, Any], participants: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [{'participant': participant, 'weight': max(1, int(self.get_entry_weight(giveaway, participant)))} for participant in participants]

    def weighted_sample_without_replacement(self, pool: List[Dict[str, Any]], count: int, rng: random.Random) -> List[Dict[str, Any]]:
        items = [dict(item) for item in pool]
        chosen: List[Dict[str, Any]] = []
        remaining = min(max(0, count), len(items))
        while remaining > 0 and items:
            total_weight = sum(max(1, int(item['weight'])) for item in items)
            pick = rng.uniform(0, total_weight)
            cumulative = 0.0
            index = 0
            for index, item in enumerate(items):
                cumulative += max(1, int(item['weight']))
                if cumulative >= pick:
                    break
            chosen.append(items.pop(index))
            remaining -= 1
        return chosen

    def create_claim_topic(self, giveaway_id: int) -> None:
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway or not giveaway.get('claim_group_ref'):
            return
        if giveaway.get('claim_topic_thread_id'):
            return
        topic_name = giveaway.get('claim_topic_name') or f'???? #{giveaway_id}'
        try:
            topic = self.api.create_forum_topic(giveaway['claim_group_ref'], topic_name)
            thread_id = int(topic.get('message_thread_id'))
            expire_at = giveaway.get('claim_deadline')
            invite_link = None
            if expire_at:
                try:
                    expire_dt = dt.datetime.fromisoformat(expire_at)
                    invite = self.api.create_chat_invite_link(
                        giveaway['claim_group_ref'],
                        name=f'claim-{giveaway_id}',
                        expire_date=int(expire_dt.timestamp()),
                    )
                    invite_link = invite.get('invite_link')
                except Exception as exc:
                    logging.warning('Failed to create claim invite link for giveaway %s: %s', giveaway_id, exc)
            self.db.update_giveaway(
                giveaway_id,
                claim_topic_thread_id=thread_id,
                claim_topic_invite_link=invite_link,
                claim_topic_expire_at=expire_at,
            )
        except Exception as exc:
            logging.warning('Failed to create claim topic for giveaway %s: %s', giveaway_id, exc)

    def process_due_claim_topics(self) -> None:
        current = now_s()
        for row in self.db.get_due_claim_topics(current):
            thread_id = row.get('claim_topic_thread_id')
            if not thread_id or row.get('claim_topic_deleted_at'):
                continue
            try:
                self.api.delete_forum_topic(row['claim_group_ref'], int(thread_id))
            except Exception as exc:
                logging.warning('Failed to delete claim topic for giveaway %s: %s', row['id'], exc)
            self.db.update_giveaway(row['id'], claim_topic_deleted_at=current)

    def send_temporary_message(self, chat_id: Any, text: str, delay: int = 10) -> None:
        try:
            result = self.api.send_message(chat_id, text)
            message_id = result.get('message_id')
            if message_id:
                timer = threading.Timer(delay, self.safe_delete_message, args=(chat_id, message_id))
                timer.daemon = True
                timer.start()
        except Exception as exc:
            logging.warning('Failed to send temporary message: %s', exc)

    def safe_delete_message(self, chat_id: Any, message_id: int) -> None:
        try:
            self.api.delete_message(chat_id, message_id)
        except Exception:
            pass

    def handle_keyword_join(self, message: Dict[str, Any]) -> None:
        text = (message.get('text') or '').strip()
        user = message['from']
        chat = message['chat']
        if not self.db.allow_action(f'keyword:{user["id"]}', min_interval_seconds=3, max_hits=10, window_seconds=60):
            return
        giveaways = self.db.all("SELECT * FROM giveaways WHERE status='live' ORDER BY id DESC")
        for giveaway in giveaways:
            keyword = (giveaway['entry_keyword'] or '').strip()
            if keyword and text.lower() == keyword.lower():
                ok, msg = self.try_join(giveaway['id'], user, source='keyword', chat_id=chat.get('id'))
                if ok:
                    self.send_temporary_message(chat['id'], msg)
                else:
                    self.api.send_message(chat['id'], msg)
                return

    def get_entry_weight(self, giveaway: Dict[str, Any], participant: Dict[str, Any]) -> int:
        weight_map = self.db.get_giveaway_weights(giveaway['id'])
        user_id = int(participant['telegram_user_id'])
        weight = max(1, int(weight_map.get(user_id, 1)))
        invited_count = self.db.count_referrals(giveaway['id'], user_id)
        bonus = int(giveaway['invite_weight_bonus'] or 0)
        if bonus > 0 and invited_count > 0:
            weight += invited_count * bonus
        return max(1, weight)

    def try_join(self, giveaway_id: int, user: Dict[str, Any], source: str, chat_id: Optional[int] = None) -> Tuple[bool, str]:
        if not self.db.allow_action(f'participate:{giveaway_id}:{user["id"]}', min_interval_seconds=5, max_hits=12, window_seconds=120):
            return False, '你操作太频繁了，请稍后再试。'
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            return False, '抽奖不存在。'
        if giveaway['status'] != 'live':
            return False, '这个抽奖当前未开放参与。'
        current = now()
        if current < dt.datetime.fromisoformat(giveaway['start_time']):
            return False, '抽奖还未开始。'
        end_time = giveaway['end_time']
        if end_time and current > dt.datetime.fromisoformat(end_time):
            return False, '抽奖已经结束。'
        if user.get('is_bot'):
            return False, '机器人账号不能参与抽奖。'
        if self.db.is_blacklisted(user['id']):
            return False, '你当前不符合参与资格。'
        if giveaway.get('require_channel'):
            ok, reason = self.check_required_channel(giveaway['required_channel'], user['id'])
            if not ok:
                return False, reason
        invite_required_count = int(giveaway['invite_required_count'] or 0)
        if invite_required_count > 0:
            count = self.db.count_referrals(giveaway_id, user['id'])
            if count < invite_required_count:
                return False, f'你还需要成功邀请 {invite_required_count - count} 人后才能参与。'
        invited_by = None
        ref = self.db.one('SELECT referrer_user_id FROM referrals WHERE giveaway_id=? AND referred_user_id=?', (giveaway_id, user['id']))
        if ref:
            invited_by = ref['referrer_user_id']
        ok, msg, inserted = self.db.add_participant(giveaway_id, user, source=source, invited_by=invited_by)
        if ok and ref:
            self.db.mark_referral_joined(giveaway_id, user['id'])
        if ok and inserted:
            self.db.log('participant_joined', user['id'], giveaway_id, {'source': source, 'chat_id': chat_id})
            threading.Thread(target=self.refresh_announcement_count, args=(giveaway_id,), daemon=True).start()
            threading.Thread(target=self.maybe_auto_draw, args=(giveaway_id,), daemon=True).start()
        return ok, msg

    def refresh_announcement_count(self, giveaway_id: int) -> None:
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway or not giveaway.get('announcement_sent') or not giveaway.get('announcement_message_id'):
            return
        try:
            text = self.build_announcement_text(giveaway)
            buttons = self.build_announcement_buttons(giveaway_id, giveaway)
            self.api.edit_message_text(
                giveaway['publish_chat_ref'],
                int(giveaway['announcement_message_id']),
                text,
                reply_markup={'inline_keyboard': buttons},
                message_thread_id=int(giveaway['publish_chat_thread_id']) if giveaway.get('publish_chat_thread_id') else None,
            )
        except Exception as exc:
            logging.warning('Failed to refresh announcement count for giveaway %s: %s', giveaway_id, exc)

    def check_required_channel(self, required_channel: Optional[str], user_id: int) -> Tuple[bool, str]:
        if not required_channel:
            return True, ''
        try:
            member = self.api.get_chat_member(required_channel, user_id)
            if member.get('status') in {'left', 'kicked'}:
                return False, '你还没有加入指定频道/群组，请先加入后再参与。'
            return True, ''
        except Exception as exc:
            logging.exception('Channel membership check failed: %s', exc)
            return False, '无法校验频道/群组资格，请联系管理员确认机器人权限。'

    def is_admin_of_chat(self, chat_ref: Any, user_id: int) -> bool:
        try:
            member = self.api.get_chat_member(chat_ref, user_id)
            return member.get('status') in {'administrator', 'creator'}
        except Exception as exc:
            logging.warning('Admin check failed for chat %s user %s: %s', chat_ref, user_id, exc)
            return False

    def process_due_giveaways(self) -> None:
        current = now_s()
        for row in self.db.due_to_start(current):
            if row['status'] == 'scheduled':
                self.db.update_giveaway(row['id'], status='live')
                self.announce_giveaway(row['id'])
        for row in self.db.all("SELECT * FROM giveaways WHERE status='live' AND announcement_sent=0 AND start_time<=?", (current,)):
            self.announce_giveaway(row['id'])
        for row in self.db.due_to_end(current):
            self.finalize_giveaway(row['id'], manual=False)
        for row in self.db.due_participant_targets():
            self.maybe_auto_draw(row['id'])

    def maybe_auto_draw(self, giveaway_id: int) -> None:
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway or giveaway['status'] != 'live':
            return
        target = int(giveaway['draw_when_participants'] or 0)
        if target <= 0:
            return
        current_count = self.db.count_participants(giveaway_id)
        if current_count >= target:
            self.finalize_giveaway(giveaway_id, manual=False)

    def build_announcement_buttons(self, giveaway_id: int, giveaway: Optional[Dict[str, Any]] = None) -> List[List[Dict[str, Any]]]:
        giveaway = giveaway or self.db.get_giveaway(giveaway_id)
        if not giveaway:
            return []
        buttons = [[{'text': '参与抽奖', 'callback_data': f'join:{giveaway_id}'}], [{'text': '验证开奖结果', 'callback_data': f'verify:{giveaway_id}'}]]
        if int(giveaway['invite_required_count'] or 0) > 0 or int(giveaway['invite_weight_bonus'] or 0) > 0:
            buttons.append([{'text': '我的邀请链接', 'url': f'https://t.me/{self.bot_username}?start={sign_invite_payload(self.invite_secret, giveaway_id, int(giveaway["created_by"]))}'}])
        return buttons

    def build_announcement_text(self, giveaway: Dict[str, Any]) -> str:
        methods = jload(giveaway['entry_methods'], [])
        count = self.db.count_participants(giveaway['id'])
        auto_draw = ''
        if giveaway.get('draw_when_participants'):
            auto_draw = f'\n📈 达到 {giveaway["draw_when_participants"]} 人自动开奖'
        end_text = giveaway['end_time'] or '不限'
        return (
            '🎉 <b>抽奖活动开始啦！</b>\n\n'
            f'🎁 奖品：{esc(giveaway["prize"])}\n'
            f'🏆 中奖人数：{esc(giveaway["winner_count"])}\n'
            f'📊 已参与人数：{count}\n'
            f'⏰ 截止时间：{esc(end_text)}\n'
            f'✅ 参与方式：{esc(method_text(methods, giveaway["entry_keyword"], giveaway["required_channel"], int(giveaway["invite_required_count"] or 0), int(giveaway["invite_weight_bonus"] or 0)))}'
            f'{auto_draw}\n\n'
            '点击下方按钮进入私聊参与。'
        )

    def announce_giveaway(self, giveaway_id: int) -> None:
        cur = self.db.exec("UPDATE giveaways SET announcement_sent=2, updated_at=? WHERE id=? AND announcement_sent=0", (now_s(), giveaway_id))
        if cur.rowcount != 1:
            return
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            return
        text = self.build_announcement_text(giveaway)
        buttons = self.build_announcement_buttons(giveaway_id, giveaway)
        try:
            result = self.api.send_message(
                giveaway['publish_chat_ref'],
                text,
                reply_markup={'inline_keyboard': buttons},
                message_thread_id=int(giveaway['publish_chat_thread_id']) if giveaway.get('publish_chat_thread_id') else None,
            )
            try:
                self.api.pin_chat_message(giveaway['publish_chat_ref'], int(result.get('message_id')))
            except Exception as exc:
                logging.warning('Failed to pin announcement for giveaway %s: %s', giveaway_id, exc)
        except Exception:
            self.db.update_giveaway(giveaway_id, announcement_sent=0)
            raise
        self.db.update_giveaway(giveaway_id, announcement_sent=1, announcement_message_id=result.get('message_id'))
        self.db.log('giveaway_announced', giveaway['created_by'], giveaway_id, {'message_id': result.get('message_id')})


"""
    def build_result_announcement_text(self, giveaway: Dict[str, Any], winner_lines: List[str], claim_deadline: str, finished_at: str, result_hash: str, valid_count: int) -> str:
        prize_text = format_prize_items(jload(giveaway.get('prize_json'), [])) or giveaway.get('prize') or '??'
        lines = [
            '?? ???????',
            '',
            f'???{esc(giveaway["title"])}',
            f'???{esc(prize_text)}',
            f'?????{esc(giveaway["winner_count"])}',
            f'???????{valid_count}',
            f'?????{esc(finished_at)}',
            f'?????<code>{esc(result_hash)}</code>',
            '',
            '?????',
        ]
        if winner_lines:
            lines.extend(f'? {esc(line)}' for line in winner_lines)
        else:
            lines.append('? ?????')
        lines.extend([
            '',
            f'?? {esc(claim_deadline)} ?????????',
            '??????????????????????????????',
        ])
        return '\n'.join(lines)
'.join(lines)

    def finalize_giveaway(self, giveaway_id: int, manual: bool = False) -> bool:
        with self.finalize_lock:
            if not self.db.start_finalization(giveaway_id):
                return False
            giveaway = self.db.get_giveaway(giveaway_id)
            if not giveaway or giveaway['status'] in {'ended', 'cancelled'}:
                return False
            participants = self.db.get_participants(giveaway_id)
            valid: List[Any] = []
            for participant in participants:
                if participant['valid'] == 0:
                    continue
                if self.db.is_blacklisted(participant['telegram_user_id']):
                    self.db.set_participant_validity(participant['id'], 0, '?????')
                    continue
                if giveaway.get('require_channel'):
                    ok, reason = self.check_required_channel(giveaway['required_channel'], participant['telegram_user_id'])
                    if not ok:
                        self.db.set_participant_validity(participant['id'], 0, reason)
                        continue
                valid.append(participant)
            weighted_pool = self.build_weighted_pool(giveaway, valid)
            total_weight = sum(item['weight'] for item in weighted_pool)
            winner_count = min(int(giveaway['winner_count']), len(valid))
            seed_material = jdump({
                'giveaway_id': giveaway_id,
                'seed_hash': giveaway.get('seed_hash'),
                'participants': sorted(p['telegram_user_id'] for p in valid),
                'weighted_pool': [(item['participant']['telegram_user_id'], item['weight']) for item in weighted_pool],
                'ended_at': now_s(),
            })
            seed_value = int(hashlib.sha256(seed_material.encode('utf-8')).hexdigest(), 16)
            rng = random.Random(seed_value)
            winners = self.weighted_sample_without_replacement(weighted_pool, winner_count, rng) if winner_count > 0 else []
            winner_lines: List[str] = []
            winner_payload: List[Dict[str, Any]] = []
            finished_at = now_s()
            for winner in winners:
                participant = winner['participant']
                telegram_user_id = int(participant['telegram_user_id'])
                display_name = self.display_user(participant.get('username'), participant.get('first_name'), participant.get('last_name'), telegram_user_id)
                winner_weight = int(winner['weight'])
                winner_payload.append({
                    'telegram_user_id': telegram_user_id,
                    'username': participant.get('username'),
                    'display_name': display_name,
                    'weight': winner_weight,
                    'winner_time': finished_at,
                })
                winner_lines.append(f'{display_name} | ?? {winner_weight} | ???? {finished_at}')
            if not winner_lines:
                winner_lines = ['?????']
            claim_hours = int(giveaway.get('claim_topic_hours') or 72)
            claim_deadline = (dt.datetime.fromisoformat(finished_at) + dt.timedelta(hours=claim_hours)).isoformat(sep=' ')
            result_payload = {
                'giveaway_id': giveaway_id,
                'title': giveaway['title'],
                'finished_at': finished_at,
                'participants': len(valid),
                'winners': winner_payload,
                'seed_hash': giveaway.get('seed_hash'),
                'winner_count': winner_count,
                'total_weight': total_weight,
            }
            result_hash = stable_result_hash(result_payload)
            text = self.build_result_announcement_text(giveaway, winner_lines, claim_deadline, finished_at, result_hash, len(valid))
            try:
                result = self.api.send_message(
                    giveaway['publish_chat_ref'],
                    text,
                    reply_markup={'inline_keyboard': [[{'text': '??????', 'callback_data': f'verify:{giveaway_id}'}]]},
                    message_thread_id=int(giveaway['publish_chat_thread_id']) if giveaway.get('publish_chat_thread_id') else None,
                )
                try:
                    self.api.pin_chat_message(giveaway['publish_chat_ref'], int(result.get('message_id')))
                except Exception as exc:
                    logging.warning('Failed to pin result for giveaway %s: %s', giveaway_id, exc)
            except Exception as exc:
                logging.exception('Failed to publish result for giveaway %s: %s', giveaway_id, exc)
                self.db.update_giveaway(giveaway_id, status='live', updated_at=now_s())
                return False
            self.db.finish_finalization(
                giveaway_id,
                status='ended',
                ended_at=finished_at,
                result_message_id=result.get('message_id'),
                winner_json=jdump(winner_payload),
                result_hash=result_hash,
                result_payload_json=jdump(result_payload),
                claim_deadline=claim_deadline,
            )
            self.db.log('giveaway_ended', giveaway['created_by'], giveaway_id, {'manual': manual, 'winner_count': len(winners), 'total_weight': total_weight, 'result_hash': result_hash})
            self.notify_winners(giveaway_id)
            if giveaway.get('claim_topic_enabled') and giveaway.get('claim_group_ref'):
                self.create_claim_topic(giveaway_id)
            return True

    def notify_winners(self, giveaway_id: int) -> None:
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            return
        winners = jload(giveaway.get('winner_json'), [])
        if not winners:
            return
        expire_at = giveaway.get('claim_topic_expire_at') or giveaway.get('claim_deadline')
        claim_link = giveaway.get('claim_topic_invite_link')
        participants = self.db.get_participants(giveaway_id)
        participant_map = {int(row['telegram_user_id']): row for row in participants}
        for item in winners:
            telegram_user_id = int(item['telegram_user_id'])
            participant = participant_map.get(telegram_user_id)
            username = item.get('username') or (participant.get('username') if participant else None)
            lines = [
                f'?? ???????{giveaway["title"]}?????',
                f'?????{display}',
            ]
            if claim_link:
                lines.append(f'?? {expire_at} ?????????????:\n{claim_link}')
            else:
                lines.append(f'?? {expire_at} ?????????')
            try:
                self.api.send_message(telegram_user_id, '\n'.join(lines))
                self.api.send_message(telegram_user_id, '
'.join(lines))
                self.db.set_participant_claim_notified(giveaway_id, telegram_user_id)
            except Exception as exc:
                logging.warning('Failed to notify winner %s: %s', telegram_user_id, exc)

    def create_claim_topic(self, giveaway_id: int) -> None:
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway or not giveaway.get('claim_group_ref'):
            return
        if giveaway.get('claim_topic_thread_id'):
            return
        topic_name = giveaway.get('claim_topic_name') or f'???? #{giveaway_id}'
        try:
            topic = self.api.create_forum_topic(giveaway['claim_group_ref'], topic_name)
            thread_id = int(topic.get('message_thread_id'))
            expire_at = giveaway.get('claim_deadline')
            invite_link = None
            if expire_at:
                try:
                    expire_dt = dt.datetime.fromisoformat(expire_at)
                    invite = self.api.create_chat_invite_link(
                        giveaway['claim_group_ref'],
                        name=f'claim-{giveaway_id}',
                        expire_date=int(expire_dt.timestamp()),
                    )
                    invite_link = invite.get('invite_link')
                except Exception as exc:
                    logging.warning('Failed to create claim invite link for giveaway %s: %s', giveaway_id, exc)
            self.db.update_giveaway(
                giveaway_id,
                claim_topic_thread_id=thread_id,
                claim_topic_invite_link=invite_link,
                claim_topic_expire_at=expire_at,
            )
        except Exception as exc:
            logging.warning('Failed to create claim topic for giveaway %s: %s', giveaway_id, exc)

    def process_due_claim_topics(self) -> None:
        current = now_s()
        for row in self.db.get_due_claim_topics(current):
            thread_id = row.get('claim_topic_thread_id')
            if not thread_id or row.get('claim_topic_deleted_at'):
                continue
            try:
                self.api.delete_forum_topic(row['claim_group_ref'], int(thread_id))
            except Exception as exc:
                logging.warning('Failed to delete claim topic for giveaway %s: %s', row['id'], exc)
            self.db.update_giveaway(row['id'], claim_topic_deleted_at=current)

    def parse_giveaway_id_arg(self, arg: str) -> int:
        match = re.match(r'^\s*#?(?P<gid>\d+)', arg.strip())
        if not match:
            raise ValueError('?????????? 1 ? #1?')
        return int(match.group('gid'))

    def build_giveaway_status_text(self, status: str) -> str:
        return {
            'scheduled': '???',
            'live': '???',
            'finalizing': '???',
    def build_giveaway_list_text(self, giveaways: List[Dict[str, Any]], status_filter: str) -> str:
        label = {'live': '???', 'ended': '???', 'all': '??'}.get(status_filter, '??')
        lines = [f'<b>???? - {esc(label)}</b>', '']
        if not giveaways:
            lines.append('????????????')
            return '\n'.join(lines)
            return self.db.all("SELECT * FROM giveaways WHERE status='live' ORDER BY id DESC")
        if status_filter == 'ended':
            return self.db.all("SELECT * FROM giveaways WHERE status IN ('ended', 'cancelled') ORDER BY id DESC")
        return self.db.list_giveaways(50)

    def normalize_list_filter(self, arg: str) -> str:
        token = (arg or '').strip().split(maxsplit=1)[0].lower() if (arg or '').strip() else ''
        if not token:
            return 'all'
        mapping = {
            'live': 'live',
            'ongoing': 'live',
            'active': 'live',
            'ended': 'ended',
            'finished': 'ended',
            'done': 'ended',
            'all': 'all',
            'all_giveaways': 'all',
        }
        if token in mapping:
            return mapping[token]
        raise ValueError('??? live?ended ? all')

    def build_list_query_buttons(self) -> List[List[Dict[str, Any]]]:
        return [
            [
                {'text': '???', 'callback_data': 'list:live'},
                {'text': '???', 'callback_data': 'list:ended'},
                {'text': '??', 'callback_data': 'list:all'},
            ],
        ]

    def build_giveaway_list_text(self, giveaways: List[Dict[str, Any]], status_filter: str) -> str:
        label = {'live': '???', 'ended': '???', 'all': '??'}.get(status_filter, '??')
        lines = [f'<b>???? - {esc(label)}</b>', '']
        if not giveaways:
            lines.append('????????????')
            return '
'.join(lines)
        for giveaway in giveaways[:20]:
            prize = format_prize_items(jload(giveaway.get('prize_json'), [])) or giveaway.get('prize') or '??'
            status_text = self.build_giveaway_status_text(giveaway.get('status') or '')
            participant_count = self.db.count_participants(giveaway['id'])
            creator = self.db.get_user_by_id(int(giveaway['created_by']))
            creator_username = creator.get('username') if creator else giveaway.get('created_by_username')
            creator_display = self.display_user_html(
                creator_username,
                creator.get('first_name') if creator else None,
                creator.get('last_name') if creator else None,
                int(giveaway['created_by'])
            )
            lines.extend([
                f'<b>#{giveaway["id"]} {esc(giveaway["title"])} </b>',
                f'???{esc(status_text)}',
                f'???{esc(prize)}',
                f'??????{participant_count}',
                f'???{esc(giveaway["start_time"])} -> {esc(giveaway["end_time"] or "??")}',
                f'????{creator_display}',
                '',
            ])
        return '
'.join(lines).rstrip()

    def send_giveaway_list(self, chat_id: Any, status_filter: str, message_id: Optional[int] = None) -> None:
        giveaways = self.get_giveaways_by_filter(status_filter)
        text = self.build_giveaway_list_text(giveaways, status_filter)
        buttons = {'inline_keyboard': self.build_list_query_buttons()}
        if message_id is not None:
            try:
                self.api.edit_message_text(chat_id, int(message_id), text, reply_markup=buttons)
                return
            except Exception as exc:
                logging.warning('Failed to edit giveaway list message: %s', exc)
        self.api.send_message(chat_id, text, reply_markup=buttons)

    def list_giveaways(self, message: Dict[str, Any], arg: str = '') -> None:
        chat = message['chat']
        if not arg.strip():
            self.api.send_message(chat['id'], '????????????', reply_markup={'inline_keyboard': self.build_list_query_buttons()})
            return
        try:
            status_filter = self.normalize_list_filter(arg)
            self.send_giveaway_list(chat['id'], status_filter)
        except Exception as exc:
            self.api.send_message(chat['id'], f'???????{exc}')

    def handle_list_giveaways_callback(self, callback_query: Dict[str, Any], status_filter: str) -> None:
        message = callback_query.get('message') or {}
        chat = message.get('chat') or {}
        self.api.answer_callback_query(callback_query['id'], '????????', show_alert=False)
        try:
            self.send_giveaway_list(chat['id'], status_filter, message_id=message.get('message_id'))
        except Exception as exc:
            logging.warning('Failed to show giveaway list: %s', exc)
            try:
                self.api.send_message(chat['id'], f'???????{exc}')
            except Exception:
                pass

    def close_giveaway(self, message: Dict[str, Any], arg: str, cancel: bool = False) -> None:
        user = message['from']
        chat = message['chat']
        if not arg.strip():
            usage = '/cancel_giveaway <id>' if cancel else '/end_giveaway <id>'
            self.api.send_message(chat['id'], f'??? {usage}??? #1 ? 1?')
            return
        try:
            giveaway_id = self.parse_giveaway_id_arg(arg)
        except Exception as exc:
            self.api.send_message(chat['id'], f'???????{exc}')
            return
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            self.api.send_message(chat['id'], '???????')
            return
        if not self.is_admin_of_chat(giveaway['publish_chat_ref'], user['id']):
            self.api.send_message(chat['id'], '??????????????????')
            return
        if giveaway['status'] in {'ended', 'cancelled'}:
            self.api.send_message(chat['id'], '???????????????????')
            return
        if cancel:
            self.db.update_giveaway(giveaway_id, status='cancelled', cancelled_at=now_s(), cancellation_reason='manual_cancel')
            self.db.log('giveaway_cancelled', user['id'], giveaway_id, {'manual': True})
            self.api.send_message(chat['id'], f'????? #{giveaway_id}?')
            return
        if giveaway['status'] != 'live':
            self.db.update_giveaway(giveaway_id, status='live')
        ok = self.finalize_giveaway(giveaway_id, manual=True)
        if ok:
            self.api.send_message(chat['id'], f'????? #{giveaway_id}?')
        else:
            self.api.send_message(chat['id'], '??????????????????')

    def set_claim_topic_command(self, message: Dict[str, Any], arg: str) -> None:
        user = message['from']
        chat = message['chat']
        if not arg.strip():
            self.api.send_message(chat['id'], '???/set_claim_topic <id> -claim_group @group -claim_topic "????" -claim_hours 72')
            return
        try:
            tokens = shlex.split(arg)
            giveaway_id = self.parse_giveaway_id_arg(tokens[0])
            flags = self.parse_command_flags(' '.join(tokens[1:])) if len(tokens) > 1 else {}
        except Exception as exc:
            self.api.send_message(chat['id'], f'???????{exc}')
            return
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            self.api.send_message(chat['id'], '???????')
            return
        if giveaway['status'] in {'ended', 'cancelled'}:
            self.api.send_message(chat['id'], '?????????????????????')
            return
        if not flags:
            self.api.send_message(chat['id'], self.giveaway_summary(giveaway_id))
            return
        updates: Dict[str, Any] = {}
        current_group_ref = giveaway.get('claim_group_ref')
        claim_group_ref = self.flag_value(flags, 'claim_group') or current_group_ref
        if 'claim_group' in flags:
            updates['claim_group_ref'] = flags['claim_group'] or None
        if 'claim_topic' in flags:
            updates['claim_topic_name'] = flags['claim_topic'] or None
        if 'claim_hours' in flags or 'claim_topic_hours' in flags:
            updates['claim_topic_hours'] = parse_optional_int(self.flag_value(flags, 'claim_hours', 'claim_topic_hours'), '????????', default=72, minimum=1)
        if ('claim_group' in flags or 'claim_topic' in flags or 'claim_hours' in flags or 'claim_topic_hours' in flags) and not claim_group_ref and not giveaway.get('claim_group_ref'):
            self.api.send_message(chat['id'], '????? -claim_group??????????')
            return
        if claim_group_ref and not self.is_admin_of_chat(claim_group_ref, user['id']):
            self.api.send_message(chat['id'], '??????????????????????')
            return
        updates['claim_topic_enabled'] = 1 if (updates.get('claim_group_ref') or claim_group_ref or updates.get('claim_topic_name') or giveaway.get('claim_topic_name')) else 0
        self.db.update_giveaway(giveaway_id, **updates)
        self.db.log('claim_topic_updated', user['id'], giveaway_id, updates)
        self.api.send_message(chat['id'], f'????? #{giveaway_id} ????????')

    def giveaway_summary(self, giveaway_id: int) -> str:
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            return '???????'
        return (
            f'<b>?? #{giveaway_id}</b>

'
            f'???{esc(giveaway["title"])}
'
            f'???{esc(giveaway["prize"])}
'
            f'?????{esc(giveaway["winner_count"])}
'
    def giveaway_summary(self, giveaway_id: int) -> str:
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            return '???????'
        return (
            f'<b>?? #{giveaway_id}</b>\n\n'
            f'???{esc(giveaway["title"])}\n'
            f'???{esc(giveaway["prize"])}\n'
            f'?????{esc(giveaway["winner_count"])}\n'
            f'?????{esc(giveaway["start_time"])}\n'
            f'?????{esc(giveaway["end_time"] or "??")}\n'
            f'?????{self.db.count_participants(giveaway_id)}\n'
            f'?????{esc(giveaway.get("claim_group_ref") or "???")} / {esc(giveaway.get("claim_topic_name") or "???")}\n'
            f'?????{esc(method_text(jload(giveaway["entry_methods"], []), giveaway["entry_keyword"], giveaway["required_channel"], int(giveaway["invite_required_count"] or 0), int(giveaway["invite_weight_bonus"] or 0)))}'
        )

"""

from .bot_tail import patch_giveaway_bot

patch_giveaway_bot(GiveawayBot)
