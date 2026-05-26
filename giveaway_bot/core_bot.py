from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import random
import re
import shlex
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .storage import DB
from .telegram_api import TelegramClient
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
        offset_raw = self.db.get_bot_state('update_offset')
        offset = int(offset_raw) if offset_raw and offset_raw.isdigit() else None
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
                    self.db.set_bot_state('update_offset', str(offset))
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

    def safe_answer_callback_query(self, callback_query_id: str, text: str = '', show_alert: bool = False) -> None:
        try:
            self.api.answer_callback_query(callback_query_id, text, show_alert=show_alert)
        except Exception as exc:
            logging.warning('Callback answer failed: %s', exc)

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

    def handle_callback(self, callback_query: Dict[str, Any]) -> None:
        data = callback_query.get('data') or ''
        message = callback_query.get('message') or {}
        chat = message.get('chat') or {}
        user = callback_query.get('from') or {}
        self.db.upsert_user(user)
        self.safe_answer_callback_query(callback_query['id'])
        if data.startswith('join:'):
            try:
                giveaway_id = int(data.split(':', 1)[1])
            except Exception:
                return
            ok, msg = self.try_join(giveaway_id, user, source='button', chat_id=chat.get('id'))
            reply = f'✅ {self.display_user(user.get("username"), user.get("first_name"), user.get("last_name"), int(user["id"]))} 已成功参与抽奖。' if ok else f'❌ {msg}'
            self.send_temporary_message(chat.get('id'), reply, message_thread_id=message.get('message_thread_id'))
            return
        if data.startswith('verify:'):
            try:
                giveaway_id = int(data.split(':', 1)[1])
            except Exception:
                return
            self.send_temporary_message(chat.get('id'), self.build_result_verification_text(giveaway_id), message_thread_id=message.get('message_thread_id'))
            return
        if data.startswith('list:'):
            self.handle_list_giveaways_callback(callback_query, data.split(':', 1)[1])

    def handle_start(self, message: Dict[str, Any], payload: str) -> None:
        user = message['from']
        chat = message['chat']
        self.db.mark_private_chat_started(user['id'])
        if not self.is_private_chat(chat):
            self.api.send_message(chat['id'], '请先私聊 bot 再继续。')
            return
        if payload:
            joined_id = verify_join_payload(self.invite_secret, payload)
            if joined_id is not None:
                if self.db.get_giveaway(joined_id):
                    self.show_private_panel(chat['id'], user['id'], joined_id)
                return
            invited = verify_invite_payload(self.invite_secret, payload)
            if invited:
                giveaway_id, referrer_id = invited
                giveaway = self.db.get_giveaway(giveaway_id)
                if not giveaway:
                    self.api.send_message(chat['id'], '抽奖不存在。')
                    return
                if user['id'] == referrer_id:
                    self.api.send_message(chat['id'], '不能给自己邀请加成。')
                    return
                if self.db.add_referral(giveaway_id, referrer_id, user['id'], payload):
                    self.db.log('referral_registered', user['id'], giveaway_id, {'referrer_user_id': referrer_id})
                    self.api.send_message(chat['id'], '邀请关系已记录。')
                else:
                    self.api.send_message(chat['id'], '邀请关系已存在。')
                return
        active = self.db.all("SELECT * FROM giveaways WHERE status IN ('scheduled', 'live') ORDER BY id DESC LIMIT 3")
        if not active:
            self.api.send_message(chat['id'], self.help_text())
            return
        buttons = [[{'text': f'参与 #{row["id"]} {row["title"][:18]}', 'url': f'https://t.me/{self.bot_username}?start={sign_join_payload(self.invite_secret, row["id"])}'}] for row in active]
        self.api.send_message(chat['id'], '你可以直接选择一个抽奖参与。', reply_markup={'inline_keyboard': buttons})

    def handle_command(self, message: Dict[str, Any], text: str) -> None:
        chat = message['chat']
        parts = text.split(maxsplit=1)
        command = parts[0].split('@', 1)[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ''
        admin_commands = {'/new_giveaway', '/edit_giveaway', '/set_claim_topic', '/list_giveaways', '/end_giveaway', '/cancel_giveaway', '/export'}
        if command == '/help':
            self.api.send_message(chat['id'], self.help_text())
            return
        if command in admin_commands and not self.is_private_chat(chat):
            self.api.send_message(chat['id'], '请到私聊里使用这个管理员命令。')
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
        self.api.send_message(chat['id'], '未知命令，发送 /help 查看帮助。')

    def help_text(self) -> str:
        return (
            '<b>抽奖机器人帮助</b>\n\n'
            '/new_giveaway 创建抽奖\n'
            '/edit_giveaway 编辑抽奖\n'
            '/set_claim_topic 设置领奖话题\n'
            '/list_giveaways 查看抽奖\n'
            '/end_giveaway 立即开奖\n'
            '/cancel_giveaway 取消抽奖\n'
            '/export 导出参与名单\n'
            '/help 查看帮助\n\n'
            '群里 @bot 自然语言建奖也支持。'
        )

    def new_giveaway_help_text(self) -> str:
        return (
            '<b>创建抽奖示例</b>\n\n'
            '<code>/new_giveaway -title "新年抽奖" -prize "AirPods" -num 3 -condition "关注频道并参与" '
            '-start now -t 8h -methods button,keyword,channel,invite -keyword "抽奖" '
            '-check_channel @mychannel -invite_need 2 -invite_bonus 1 '
            '-weight "@toiqi:100,123456:50" -publish @mygroup -draw_n 100 '
            '-claim_group @claimgroup -claim_topic "领奖话题" -claim_hours 72</code>\n\n'
            '说明：\n'
            '-t 8h 表示 8 小时后结束。\n'
            '-num 3 表示 3 人中奖。\n'
            '-weight 支持 uid:weight 或 @username:weight。\n'
            '-draw_n 100 表示满 100 人自动开奖。\n'
            '-check_channel 表示需要先关注指定频道/群。'
        )

    def flag_value(self, flags: Dict[str, str], *keys: str) -> Optional[str]:
        for key in keys:
            if key in flags:
                value = flags.get(key)
                if value is None:
                    return None
                raw = str(value).strip()
                return raw if raw else None
        return None

    def parse_command_flags(self, arg: str) -> Dict[str, str]:
        if not arg.strip():
            return {}
        tokens = shlex.split(arg)
        result: Dict[str, str] = {}
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if not token.startswith('-'):
                raise ValueError(f'参数格式错误：{token}')
            key = token.lstrip('-').lower()
            value = 'true'
            if i + 1 < len(tokens) and not tokens[i + 1].startswith('-'):
                value = tokens[i + 1]
                i += 2
            else:
                i += 1
            result[key] = value
        return result

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
            self.api.send_message(chat['id'], f'✅ 抽奖已创建 #{giveaway_id}')
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
            raise ValueError('缺少抽奖标题')
        if not prize:
            raise ValueError('缺少奖品')
        if not winner_count_text:
            raise ValueError('缺少中奖人数')
        if not publish_chat_ref:
            raise ValueError('缺少公布位置')
        if not self.is_admin_of_chat(publish_chat_ref, user['id']):
            raise ValueError('你不是公布群/频道的管理员')

        start_time_text = self.flag_value(flags, 'start') or 'now'
        start_time = parse_dt(start_time_text)
        end_time = None
        end_text = self.flag_value(flags, 'end', 'end_time')
        if end_text:
            end_time = parse_dt(end_text)
        else:
            duration_text = self.flag_value(flags, 't', 'time', 'duration')
            if duration_text:
                duration = parse_duration(duration_text)
                if duration is not None:
                    end_time = start_time + duration

        methods_text = self.flag_value(flags, 'methods', 'method', 'entry')
        methods = parse_methods(methods_text) if methods_text else ['button']
        entry_keyword = self.flag_value(flags, 'keyword')
        if 'keyword' in methods and not entry_keyword:
            raise ValueError('选择 keyword 参与时必须提供 -keyword')

        required_channel = self.flag_value(flags, 'check_channel', 'channel')
        require_channel = bool(required_channel)
        invite_required_count = parse_optional_int(self.flag_value(flags, 'invite_need', 'invite_count'), '邀请人数', default=0, minimum=0)
        invite_weight_bonus = parse_optional_int(self.flag_value(flags, 'invite_bonus', 'invite_weight_bonus'), '邀请加权', default=0, minimum=0)
        draw_when_participants = None
        draw_text = self.flag_value(flags, 'draw_n', 'draw_when', 'participants')
        if draw_text:
            draw_when_participants = parse_optional_int(draw_text, '自动开奖人数', default=0, minimum=1)
            if draw_when_participants <= 0:
                draw_when_participants = None

        if draw_when_participants and end_time is None:
            auto_draw_mode = 'participants'
        elif draw_when_participants and end_time is not None:
            auto_draw_mode = 'both'
        elif end_time is None:
            auto_draw_mode = 'manual'
        else:
            auto_draw_mode = 'time'

        special_weights = parse_weight_map(self.flag_value(flags, 'weight'), self.resolve_username_to_user_id)
        claim_group_ref = self.flag_value(flags, 'claim_group')
        claim_topic_name = self.flag_value(flags, 'claim_topic')
        claim_topic_hours = parse_optional_int(self.flag_value(flags, 'claim_hours', 'claim_topic_hours'), '领奖有效时长', default=72, minimum=1)
        claim_topic_enabled = bool(claim_group_ref or claim_topic_name)
        if claim_topic_enabled and not claim_group_ref:
            raise ValueError('开启领奖话题时必须提供 -claim_group')
        if claim_group_ref and not self.is_admin_of_chat(claim_group_ref, user['id']):
            raise ValueError('你不是领奖群/频道的管理员')

        claim_deadline_base = end_time or (start_time + dt.timedelta(hours=1))
        claim_deadline = (claim_deadline_base + dt.timedelta(hours=claim_topic_hours)).isoformat(sep=' ')
        seed_hash = hashlib.sha256(json.dumps({
            'title': title,
            'prize': prize,
            'winner_count': int(winner_count_text),
            'start_time': start_time.isoformat(sep=' '),
            'end_time': end_time.isoformat(sep=' ') if end_time else None,
            'publish_chat_ref': publish_chat_ref,
            'draw_when_participants': draw_when_participants,
        }, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()

        return {
            'title': title,
            'prize': prize,
            'prize_json': parse_prize_items(prize),
            'winner_count': int(winner_count_text),
            'participation_condition': self.flag_value(flags, 'condition', 'cond') or '按公告参与',
            'start_time': start_time.isoformat(sep=' '),
            'end_time': end_time.isoformat(sep=' ') if end_time else None,
            'auto_draw_mode': auto_draw_mode,
            'draw_when_participants': draw_when_participants,
            'entry_methods': methods,
            'entry_keyword': entry_keyword,
            'require_channel': require_channel,
            'required_channel': required_channel,
            'invite_required_count': invite_required_count,
            'invite_weight_bonus': invite_weight_bonus,
            'publish_chat_ref': publish_chat_ref,
            'publish_chat_thread_id': None,
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
            self.api.send_message(chat_id, '抽奖不存在。')
            return
        if giveaway['status'] == 'cancelled':
            self.api.send_message(chat_id, '这个抽奖已取消。')
            return
        count = self.db.count_participants(giveaway_id)
        methods = jload(giveaway['entry_methods'], [])
        lines = []
        if intro:
            lines.append(intro)
        lines.extend([
            f'<b>抽奖 #{giveaway_id}</b>',
            f'标题：{esc(giveaway["title"])}',
            f'奖品：{esc(format_prize_items(jload(giveaway.get("prize_json"), [])) or giveaway["prize"])}',
            f'已参与人数：{count}',
            f'中奖人数：{giveaway["winner_count"]}',
            f'开始时间：{esc(giveaway["start_time"])}',
            f'结束时间：{esc(giveaway["end_time"] or "不限")}',
            f'参与方式：{esc(method_text(methods, giveaway.get("entry_keyword"), giveaway.get("required_channel"), int(giveaway.get("invite_required_count") or 0), int(giveaway.get("invite_weight_bonus") or 0)))}',
            f'参与条件：{esc(giveaway["participation_condition"])}',
        ])
        buttons = [[{'text': '参与抽奖', 'callback_data': f'join:{giveaway_id}'}]]
        if int(giveaway.get('invite_required_count') or 0) > 0 or int(giveaway.get('invite_weight_bonus') or 0) > 0:
            buttons.append([{'text': '邀请加成', 'url': f'https://t.me/{self.bot_username}?start={sign_invite_payload(self.invite_secret, giveaway_id, user_id)}'}])
        self.api.send_message(chat_id, '\n'.join(lines), reply_markup={'inline_keyboard': buttons})

    def is_admin_of_chat(self, chat_ref: Any, user_id: int) -> bool:
        if not chat_ref:
            return False
        try:
            member = self.api.get_chat_member(chat_ref, int(user_id))
            return member.get('status') in {'administrator', 'creator'}
        except Exception as exc:
            logging.warning('Admin check failed for chat %s user %s: %s', chat_ref, user_id, exc)
            return False

    def display_user(self, username: Optional[str], first_name: Optional[str], last_name: Optional[str], user_id: int) -> str:
        name = ' '.join(part for part in [first_name, last_name] if part).strip()
        if username:
            return f'@{username}'
        if name:
            return f'{name} ({user_id})'
        return str(user_id)

    def display_user_html(self, username: Optional[str], first_name: Optional[str], last_name: Optional[str], user_id: int) -> str:
        name = ' '.join(part for part in [first_name, last_name] if part).strip() or f'用户{user_id}'
        if username:
            return f'<a href="https://t.me/{esc(username)}">@{esc(username)}</a>'
        return f'<a href="tg://user?id={user_id}">{esc(name)}</a>'

    def send_temporary_message(self, chat_id: Any, text: str, delay_seconds: int = 10, reply_markup: Optional[Dict[str, Any]] = None, message_thread_id: Optional[int] = None) -> None:
        try:
            result = self.api.send_message(chat_id, text, reply_markup=reply_markup, message_thread_id=message_thread_id)
        except Exception as exc:
            logging.warning('Failed to send temporary message to %s: %s', chat_id, exc)
            return
        message_id = int(result.get('message_id') or 0)
        if message_id <= 0:
            return
        def _delete_later() -> None:
            time.sleep(max(1, int(delay_seconds)))
            self.safe_delete_message(chat_id, message_id)
        threading.Thread(target=_delete_later, daemon=True).start()

    def safe_delete_message(self, chat_id: Any, message_id: int) -> None:
        try:
            self.api.delete_message(chat_id, int(message_id))
        except Exception as exc:
            logging.debug('Failed to delete message %s in %s: %s', message_id, chat_id, exc)
    def check_required_channel(self, channel_ref: Any, user_id: int) -> Tuple[bool, str]:
        if not channel_ref:
            return True, ''
        try:
            member = self.api.get_chat_member(channel_ref, int(user_id))
        except Exception as exc:
            return False, f'无法校验关注状态，请先确认 bot 已加入目标群/频道：{exc}'
        status = str(member.get('status') or '').lower()
        if status in {'creator', 'administrator', 'member'}:
            return True, ''
        if status == 'restricted' and member.get('is_member'):
            return True, ''
        return False, f'请先关注或加入 {channel_ref} 后再参与。'
    def get_entry_weight(self, giveaway: Dict[str, Any], participant: Dict[str, Any]) -> int:
        telegram_user_id = int(participant.get('telegram_user_id') or participant.get('id') or 0)
        base_weight = int(self.db.get_giveaway_weights(int(giveaway['id'])).get(telegram_user_id, 1))
        invite_bonus = int(giveaway.get('invite_weight_bonus') or 0)
        if invite_bonus > 0:
            base_weight += self.db.count_referrals(int(giveaway['id']), telegram_user_id) * invite_bonus
        return max(1, base_weight)
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
        target = int(giveaway.get('draw_when_participants') or 0)
        if target <= 0:
            return
        if self.db.count_participants(giveaway_id) >= target:
            self.finalize_giveaway(giveaway_id, manual=False)

    def announce_giveaway(self, giveaway_id: int) -> None:
        cur = self.db.exec("UPDATE giveaways SET announcement_sent=2, updated_at=? WHERE id=? AND announcement_sent=0", (now_s(), giveaway_id))
        if cur.rowcount != 1:
            return
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            return
        try:
            text = self.build_announcement_text(giveaway)
            buttons = self.build_announcement_buttons(giveaway_id, giveaway)
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
            self.db.update_giveaway(giveaway_id, announcement_sent=1, announcement_message_id=result.get('message_id'))
            self.db.log('giveaway_announced', giveaway['created_by'], giveaway_id, {'message_id': result.get('message_id')})
        except Exception:
            self.db.update_giveaway(giveaway_id, announcement_sent=0)
            raise

    def build_announcement_buttons(self, giveaway_id: int, giveaway: Optional[Dict[str, Any]] = None) -> List[List[Dict[str, Any]]]:
        giveaway = giveaway or self.db.get_giveaway(giveaway_id)
        if not giveaway:
            return []
        buttons = [
            [{'text': '参与抽奖', 'callback_data': f'join:{giveaway_id}'}],
            [{'text': '验证开奖结果', 'callback_data': f'verify:{giveaway_id}'}],
        ]
        if int(giveaway.get('invite_required_count') or 0) > 0 or int(giveaway.get('invite_weight_bonus') or 0) > 0:
            buttons.append([{'text': '邀请入口', 'url': f'https://t.me/{self.bot_username}?start={sign_invite_payload(self.invite_secret, giveaway_id, int(giveaway["created_by"]))}'}])
        return buttons

    def build_announcement_text(self, giveaway: Dict[str, Any]) -> str:
        methods = jload(giveaway['entry_methods'], [])
        count = self.db.count_participants(giveaway['id'])
        prize = format_prize_items(jload(giveaway.get('prize_json'), [])) or giveaway.get('prize') or '奖品'
        auto_draw = ''
        if giveaway.get('draw_when_participants'):
            auto_draw = f'\n满 {giveaway["draw_when_participants"]} 人自动开奖'
        return (
            '🎉 抽奖活动开始啦！\n\n'
            f'🎁 奖品：{esc(prize)}\n'
            f'🏆 中奖人数：{esc(giveaway["winner_count"])}\n'
            f'👥 已参与人数：{count}\n'
            f'⏰ 截止时间：{esc(giveaway["end_time"] or "不限")}\n'
            f'✅ 参与方式：{esc(method_text(methods, giveaway.get("entry_keyword"), giveaway.get("required_channel"), int(giveaway.get("invite_required_count") or 0), int(giveaway.get("invite_weight_bonus") or 0)))}'
            f'{auto_draw}\n\n'
            '点击下方按钮参与抽奖。'
        )

    def refresh_announcement_count(self, giveaway_id: int) -> None:
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway or int(giveaway.get('announcement_sent') or 0) != 1 or not giveaway.get('announcement_message_id'):
            return
        try:
            self.api.edit_message_text(
                giveaway['publish_chat_ref'],
                int(giveaway['announcement_message_id']),
                self.build_announcement_text(giveaway),
                reply_markup={'inline_keyboard': self.build_announcement_buttons(giveaway_id, giveaway)},
                message_thread_id=int(giveaway['publish_chat_thread_id']) if giveaway.get('publish_chat_thread_id') else None,
            )
        except Exception as exc:
            logging.warning('Failed to refresh announcement count for giveaway %s: %s', giveaway_id, exc)

    def _first_match(self, text: str, patterns: List[str]) -> Optional[str]:
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                return value or None
        return None

    def _extract_freeform_title(self, text: str) -> Optional[str]:
        raw = (text or '').strip()
        if not raw:
            return None
        first_part = re.split(r'[，,。；;]+', raw, maxsplit=1)[0].strip()
        if not first_part:
            return None
        if re.match(r'^(?:奖品|礼物|奖项|发布到|公布到|关注|需关注|加入|开奖|截止|结束|抽\s*\d+\s*人|满\s*\d+\s*人)', first_part, flags=re.IGNORECASE):
            return None
        if re.search(r'(?:奖品|发布到|公布到|需关注|领奖|话题|权重|截止|结束)', first_part, flags=re.IGNORECASE):
            return None
        return first_part[:80]

    def _extract_special_weights_from_text(self, text: str) -> Dict[int, int]:
        result: Dict[int, int] = {}
        patterns = [r'@([\w_\-]+)\s*(?:权重|加权|weight)\s*(\d+)', r'@([\w_\-]+)\s*[:=]\s*(\d+)']
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
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

        title = self._extract_freeform_title(text) or '抽奖活动'
        prize = '奖品1'
        winner_count = 1
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

        prize_match = re.search(r'奖品\s*[:：]?\s*([^，,。；;]+)', text, flags=re.IGNORECASE)
        if prize_match:
            prize = prize_match.group(1).strip()
        winner_match = re.search(r'抽\s*(\d+)\s*人', text, flags=re.IGNORECASE)
        if winner_match:
            winner_count = max(1, int(winner_match.group(1)))
        time_match = re.search(r'(\d+)\s*(小时|时|h|H|天|d|分钟|分|m|秒|s)\s*后\s*结束', text, flags=re.IGNORECASE)
        if time_match:
            unit = time_match.group(2).lower()
            unit_map = {'小时': 'h', '时': 'h', 'h': 'h', '天': 'd', 'd': 'd', '分钟': 'm', '分': 'm', 'm': 'm', '秒': 's', 's': 's'}
            duration = parse_duration(f"{time_match.group(1)}{unit_map.get(unit, 'h')}")
            if duration is not None:
                end_time = start_time + duration
        publish_match = re.search(r'(?:发布到|公布到|发到|发布于)\s*(@[\w_\-]+|-?\d+)', text, flags=re.IGNORECASE)
        if publish_match:
            publish_ref = publish_match.group(1).strip()
        channel_match = re.search(r'(?:需关注|关注|加入)\s*(@[\w_\-]+|-?\d+)', text, flags=re.IGNORECASE)
        if channel_match:
            required_channel = channel_match.group(1).strip()
        claim_group_match = re.search(r'(?:领奖群|领奖到|领奖发布到|领取群)\s*(@[\w_\-]+|-?\d+)', text, flags=re.IGNORECASE)
        if claim_group_match:
            claim_group_ref = claim_group_match.group(1).strip()
        claim_topic_match = re.search(r'(?:领奖话题|领奖主题|话题)\s*[:：]?\s*([^，,。；;]+)', text, flags=re.IGNORECASE)
        if claim_topic_match:
            claim_topic_name = claim_topic_match.group(1).strip()
        invite_need_match = re.search(r'邀请\s*(\d+)\s*人', text, flags=re.IGNORECASE)
        if invite_need_match:
            invite_required_count = max(0, int(invite_need_match.group(1)))
        invite_bonus_match = re.search(r'邀请.*?加权\s*(\d+)', text, flags=re.IGNORECASE)
        if invite_bonus_match:
            invite_weight_bonus = max(0, int(invite_bonus_match.group(1)))
        draw_match = re.search(r'满\s*(\d+)\s*人\s*开奖', text, flags=re.IGNORECASE)
        if draw_match:
            draw_when_participants = max(1, int(draw_match.group(1)))
        cond_match = re.search(r'参与条件\s*[:：]?\s*([^，,。；;]+)', text, flags=re.IGNORECASE)
        if cond_match:
            participation_condition = cond_match.group(1).strip()
        if end_time is None and draw_when_participants is None:
            end_time = start_time + dt.timedelta(hours=1)

        methods = ['button']
        if re.search(r'关键词|关键字|keyword', text, flags=re.IGNORECASE):
            methods.append('keyword')
        if required_channel:
            methods.append('channel')
        if invite_required_count > 0 or invite_weight_bonus > 0:
            methods.append('invite')

        special_weights = self._extract_special_weights_from_text(text)
        claim_topic_hours = 72
        claim_topic_enabled = bool(claim_group_ref or claim_topic_name)
        if claim_topic_enabled and not claim_group_ref:
            claim_group_ref = publish_ref
        claim_deadline_base = end_time or (start_time + dt.timedelta(hours=1))
        claim_deadline = (claim_deadline_base + dt.timedelta(hours=claim_topic_hours)).isoformat(sep=' ')
        seed_hash = hashlib.sha256(json.dumps({
            'title': title,
            'prize': prize,
            'winner_count': winner_count,
            'start_time': start_time.isoformat(sep=' '),
            'end_time': end_time.isoformat(sep=' ') if end_time else None,
            'publish_chat_ref': publish_ref,
            'draw_when_participants': draw_when_participants,
        }, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()

        return {
            'title': title,
            'prize': prize,
            'prize_json': parse_prize_items(prize),
            'winner_count': winner_count,
            'participation_condition': participation_condition or '按公告参与',
            'start_time': start_time.isoformat(sep=' '),
            'end_time': end_time.isoformat(sep=' ') if end_time else None,
            'auto_draw_mode': 'both' if (end_time and draw_when_participants) else ('participants' if draw_when_participants else ('manual' if end_time is None else 'time')),
            'draw_when_participants': draw_when_participants,
            'entry_methods': methods,
            'entry_keyword': self._first_match(text, [r'(?:关键词|关键字)\s*[:：]?\s*([^，,。；;]+)']),
            'require_channel': bool(required_channel),
            'required_channel': required_channel,
            'invite_required_count': invite_required_count,
            'invite_weight_bonus': invite_weight_bonus,
            'publish_chat_ref': publish_ref,
            'publish_chat_thread_id': None,
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

    def handle_group_message(self, message: Dict[str, Any]) -> None:
        user = message.get('from') or {}
        chat = message.get('chat') or {}
        text = (message.get('text') or '').strip()
        if not text or user.get('is_bot'):
            return
        if self.is_bot_mentioned(message):
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
                self.api.send_message(chat['id'], f'✅ 已根据群内消息创建抽奖 #{giveaway_id}', message_thread_id=message.get('message_thread_id'))
                created = self.db.get_giveaway(giveaway_id)
                if created and created['status'] == 'live' and not created.get('announcement_message_id'):
                    self.announce_giveaway(giveaway_id)
            except Exception as exc:
                self.api.send_message(chat['id'], f'❌ 创建失败：{exc}', message_thread_id=message.get('message_thread_id'))
            return
        giveaways = self.db.all("SELECT * FROM giveaways WHERE status IN ('scheduled', 'live') AND entry_keyword IS NOT NULL ORDER BY id DESC")
        normalized_text = text.lower().strip()
        for giveaway in giveaways:
            keyword = (giveaway.get('entry_keyword') or '').strip().lower()
            if keyword and keyword == normalized_text:
                ok, msg = self.try_join(int(giveaway['id']), user, source='keyword', chat_id=chat.get('id'))
                if ok:
                    self.send_temporary_message(chat['id'], f'✅ {self.display_user(user.get("username"), user.get("first_name"), user.get("last_name"), int(user["id"]))} 已成功参与抽奖。', message_thread_id=message.get('message_thread_id'))
                else:
                    self.send_temporary_message(chat['id'], f'❌ {msg}', message_thread_id=message.get('message_thread_id'))
                return

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
    def build_result_verification_text(self, giveaway_id: int) -> str:
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            return '找不到这个抽奖。'
        result_payload = jload(giveaway.get('result_payload_json'), {})
        result_hash = giveaway.get('result_hash') or stable_result_hash(result_payload)
        seed_hash = giveaway.get('seed_hash') or ''
        return (
            f'<b>开奖结果验证</b>\n\n'
            f'抽奖编号：{giveaway_id}\n'
            f'结果哈希：<code>{esc(result_hash)}</code>\n'
            f'种子哈希：<code>{esc(seed_hash)}</code>\n'
            f'验证算法：SHA-256\n\n'
            '验证方法：把结果载荷按规范化 JSON 序列化后计算 SHA-256，\n'
            '并与消息中的结果哈希一致即可。'
        )

    def notify_winners(self, giveaway_id: int) -> None:
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            return
        winners = jload(giveaway.get('winner_json'), [])
        if not winners:
            return
        claim_deadline = giveaway.get('claim_topic_expire_at') or giveaway.get('claim_deadline') or ''
        claim_link = giveaway.get('claim_topic_invite_link')
        participants = self.db.get_participants(giveaway_id)
        participant_map = {int(row['telegram_user_id']): row for row in participants}
        for item in winners:
            telegram_user_id = int(item['telegram_user_id'])
            participant = participant_map.get(telegram_user_id)
            display_name = item.get('display_name') or self.display_user_html(
                item.get('username') or (participant.get('username') if participant else None),
                item.get('first_name') or (participant.get('first_name') if participant else None),
                item.get('last_name') or (participant.get('last_name') if participant else None),
                telegram_user_id,
            )
            lines = [
                '🎉 恭喜中奖！',
                '',
                f'活动：{esc(giveaway["title"])}',
                f'中奖身份：{display_name}',
                f'中奖权重：{esc(item.get("weight", 1))}',
            ]
            if claim_link:
                lines.append(f'领奖话题：{claim_link}')
                if claim_deadline:
                    lines.append(f'请在 {esc(claim_deadline)} 前进入领奖话题。')
            elif claim_deadline:
                lines.append(f'请在 {esc(claim_deadline)} 前联系管理员领奖。')
            try:
                self.api.send_message(telegram_user_id, '\n'.join(lines))
                self.db.set_participant_claim_notified(giveaway_id, telegram_user_id)
            except Exception as exc:
                logging.warning('Failed to notify winner %s: %s', telegram_user_id, exc)
    def build_result_announcement_text(self, giveaway: Dict[str, Any], winner_lines: List[str], claim_deadline: str, finished_at: str, result_hash: str, valid_count: int) -> str:
        prize_text = format_prize_items(jload(giveaway.get('prize_json'), [])) or giveaway.get('prize') or '奖品'
        lines = [
            '🎊 抽奖结果公布！',
            '',
            f'活动：{esc(giveaway["title"])}',
            f'奖品：{esc(prize_text)}',
            f'中奖人数：{esc(giveaway["winner_count"])}',
            f'有效参与人数：{valid_count}',
            f'开奖时间：{esc(finished_at)}',
            f'开奖结果哈希：<code>{esc(result_hash)}</code>',
            '',
            '中奖名单：',
        ]
        if winner_lines:
            lines.extend(f'• {line}' for line in winner_lines)
        else:
            lines.append('• 暂无符合条件的中奖者')
        lines.extend([
            '',
            f'领奖截止：{esc(claim_deadline)}',
            '验证算法：SHA-256。',
            '验证方式：对开奖结果载荷做规范化 JSON 序列化后计算哈希，并与消息中的结果哈希一致即可。',
        ])
        return '\n'.join(lines)

    def finalize_giveaway(self, giveaway_id: int, manual: bool = False) -> bool:
        import datetime as dt
        import hashlib
        import random
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
                    self.db.set_participant_validity(participant['id'], 0, '黑名单用户')
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
                'participants': sorted(int(p['telegram_user_id']) for p in valid),
                'weighted_pool': [(int(item['participant']['telegram_user_id']), int(item['weight'])) for item in weighted_pool],
                'winner_count': winner_count,
                'total_weight': total_weight,
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
                display_name = self.display_user_html(participant.get('username'), participant.get('first_name'), participant.get('last_name'), telegram_user_id)
                winner_weight = int(winner['weight'])
                winner_payload.append({
                    'telegram_user_id': telegram_user_id,
                    'username': participant.get('username'),
                    'first_name': participant.get('first_name'),
                    'last_name': participant.get('last_name'),
                    'display_name': display_name,
                    'weight': winner_weight,
                    'winner_time': finished_at,
                })
                winner_lines.append(f'{display_name} | 权重 {winner_weight} | {finished_at}')
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
            result = self.api.send_message(giveaway['publish_chat_ref'], text, reply_markup={'inline_keyboard': [[{'text': '验证开奖结果', 'callback_data': f'verify:{giveaway_id}'}]]}, message_thread_id=int(giveaway['publish_chat_thread_id']) if giveaway.get('publish_chat_thread_id') else None)
            try:
                self.api.pin_chat_message(giveaway['publish_chat_ref'], int(result.get('message_id')))
            except Exception as exc:
                logging.warning('Failed to pin result for giveaway %s: %s', giveaway_id, exc)
            self.db.finish_finalization(giveaway_id, status='ended', ended_at=finished_at, result_message_id=result.get('message_id'), winner_json=jdump(winner_payload), result_hash=result_hash, result_payload_json=jdump(result_payload), claim_deadline=claim_deadline)
            self.db.log('giveaway_ended', giveaway['created_by'], giveaway_id, {'manual': manual, 'winner_count': len(winners), 'total_weight': total_weight, 'result_hash': result_hash})
            if giveaway.get('claim_topic_enabled') and giveaway.get('claim_group_ref'):
                self.create_claim_topic(giveaway_id)
            self.notify_winners(giveaway_id)
            return True

    def try_join(self, giveaway_id: int, user: Dict[str, Any], source: str = 'button', chat_id: Optional[int] = None) -> Tuple[bool, str]:
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            return False, '抽奖不存在。'
        if giveaway.get('status') == 'cancelled':
            return False, '这个抽奖已取消。'
        if giveaway.get('status') == 'ended':
            return False, '这个抽奖已经结束。'
        try:
            now_dt = dt.datetime.now().replace(microsecond=0)
            if giveaway.get('start_time') and dt.datetime.fromisoformat(giveaway['start_time']) > now_dt:
                return False, '这个抽奖还没开始。'
            if giveaway.get('end_time') and dt.datetime.fromisoformat(giveaway['end_time']) < now_dt:
                return False, '这个抽奖已经截止。'
        except Exception:
            pass
        if user.get('is_bot'):
            return False, '机器人不能参与。'
        if self.db.is_blacklisted(int(user['id'])):
            return False, '你当前无法参与。'
        if source == 'button' and not self.db.has_private_chat_started(int(user['id'])):
            return False, '请先私聊 bot 一次，再回到群里点击参与。'
        methods = jload(giveaway.get('entry_methods'), [])
        if source == 'button' and 'button' not in methods:
            return False, '这个抽奖不支持按钮参与。'
        if source == 'keyword' and 'keyword' not in methods:
            return False, '这个抽奖不支持关键词参与。'
        if giveaway.get('require_channel'):
            ok, reason = self.check_required_channel(giveaway.get('required_channel'), int(user['id']))
            if not ok:
                return False, reason
        invite_required_count = int(giveaway.get('invite_required_count') or 0)
        if invite_required_count > 0:
            invite_count = self.db.count_referrals(giveaway_id, int(user['id']))
            if invite_count < invite_required_count:
                return False, f'还差 {invite_required_count - invite_count} 个有效邀请。'
        invited_by = None
        referral = self.db.one('SELECT referrer_user_id FROM referrals WHERE giveaway_id=? AND referred_user_id=? LIMIT 1', (giveaway_id, int(user['id'])))
        if referral:
            invited_by = int(referral['referrer_user_id'])
        ok, msg, _created = self.db.add_participant(giveaway_id, user, source=source, invited_by=invited_by)
        if not ok:
            return False, msg
        if referral:
            self.db.mark_referral_joined(giveaway_id, int(user['id']))
        if giveaway.get('announcement_sent') == 1 and giveaway.get('announcement_message_id'):
            threading.Thread(target=self.refresh_announcement_count, args=(giveaway_id,), daemon=True).start()
        if giveaway.get('draw_when_participants') and giveaway.get('auto_draw_mode') in {'participants', 'both'}:
            threading.Thread(target=self.maybe_auto_draw, args=(giveaway_id,), daemon=True).start()
        return True, '已成功参与。'








