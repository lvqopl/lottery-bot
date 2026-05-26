import sqlite3
import threading
from typing import Any, Dict, List, Optional, Tuple

from .utils import jdump, jload, now_s


class DB:
    def __init__(self, path: str) -> None:
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()
        self.migrate_schema()

    def exec(self, sql: str, params: Tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def one(self, sql: str, params: Tuple[Any, ...] = ()) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def init_schema(self) -> None:
        with self.lock:
            self.conn.executescript(
                '''
                CREATE TABLE IF NOT EXISTS users (
                    telegram_user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    is_bot INTEGER DEFAULT 0,
                    blacklisted INTEGER DEFAULT 0,
                    private_chat_started INTEGER DEFAULT 0,
                    private_chat_started_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS giveaways (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    prize TEXT NOT NULL,
                    prize_json TEXT,
                    winner_count INTEGER NOT NULL,
                    participation_condition TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT,
                    auto_draw_mode TEXT NOT NULL DEFAULT 'time',
                    draw_when_participants INTEGER,
                    entry_methods TEXT NOT NULL,
                    entry_keyword TEXT,
                    require_channel INTEGER DEFAULT 0,
                    required_channel TEXT,
                    invite_required_count INTEGER DEFAULT 0,
                    invite_weight_bonus INTEGER DEFAULT 0,
                    publish_chat_ref TEXT NOT NULL,
                    publish_chat_thread_id INTEGER,
                    status TEXT NOT NULL,
                    created_by INTEGER NOT NULL,
                    created_by_username TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    announcement_sent INTEGER DEFAULT 0,
                    announcement_message_id INTEGER,
                    result_message_id INTEGER,
                    claim_deadline TEXT NOT NULL,
                    seed_hash TEXT,
                    result_hash TEXT,
                    result_payload_json TEXT,
                    winner_json TEXT,
                    ended_at TEXT,
                    cancelled_at TEXT,
                    cancellation_reason TEXT,
                    claim_topic_enabled INTEGER DEFAULT 0,
                    claim_group_ref TEXT,
                    claim_topic_name TEXT,
                    claim_topic_hours INTEGER DEFAULT 72,
                    claim_topic_thread_id INTEGER,
                    claim_topic_invite_link TEXT,
                    claim_topic_expire_at TEXT,
                    claim_topic_deleted_at TEXT,
                    participant_notice_message_id INTEGER,
                    participant_notice_deleted_at TEXT
                );

                CREATE TABLE IF NOT EXISTS giveaway_weights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    giveaway_id INTEGER NOT NULL,
                    telegram_user_id INTEGER NOT NULL,
                    weight INTEGER NOT NULL DEFAULT 1,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(giveaway_id, telegram_user_id)
                );

                CREATE TABLE IF NOT EXISTS participants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    giveaway_id INTEGER NOT NULL,
                    telegram_user_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    source TEXT,
                    invited_by INTEGER,
                    joined_at TEXT NOT NULL,
                    valid INTEGER DEFAULT 1,
                    invalid_reason TEXT,
                    claim_notified INTEGER DEFAULT 0,
                    UNIQUE(giveaway_id, telegram_user_id)
                );

                CREATE TABLE IF NOT EXISTS referrals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    giveaway_id INTEGER NOT NULL,
                    referrer_user_id INTEGER NOT NULL,
                    referred_user_id INTEGER NOT NULL,
                    payload TEXT,
                    created_at TEXT NOT NULL,
                    joined_at TEXT,
                    UNIQUE(giveaway_id, referred_user_id)
                );

                CREATE TABLE IF NOT EXISTS states (
                    user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    flow TEXT NOT NULL,
                    step TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    actor_user_id INTEGER,
                    target_id INTEGER,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS rate_limits (
                    key TEXT PRIMARY KEY,
                    last_at TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    hits INTEGER NOT NULL
                );
                '''
            )
            self.conn.commit()

    def migrate_schema(self) -> None:
        with self.lock:
            giveaways_cols = {row['name'] for row in self.conn.execute('PRAGMA table_info(giveaways)').fetchall()}
            users_cols = {row['name'] for row in self.conn.execute('PRAGMA table_info(users)').fetchall()}
            participants_cols = {row['name'] for row in self.conn.execute('PRAGMA table_info(participants)').fetchall()}

            extra_giveaway_cols = {
                'prize_json': 'ALTER TABLE giveaways ADD COLUMN prize_json TEXT',
                'end_time': 'ALTER TABLE giveaways ADD COLUMN end_time TEXT',
                'auto_draw_mode': "ALTER TABLE giveaways ADD COLUMN auto_draw_mode TEXT NOT NULL DEFAULT 'time'",
                'draw_when_participants': 'ALTER TABLE giveaways ADD COLUMN draw_when_participants INTEGER',
                'publish_chat_thread_id': 'ALTER TABLE giveaways ADD COLUMN publish_chat_thread_id INTEGER',
                'participant_notice_message_id': 'ALTER TABLE giveaways ADD COLUMN participant_notice_message_id INTEGER',
                'participant_notice_deleted_at': 'ALTER TABLE giveaways ADD COLUMN participant_notice_deleted_at TEXT',
                'claim_topic_hours': 'ALTER TABLE giveaways ADD COLUMN claim_topic_hours INTEGER DEFAULT 72',
                'claim_topic_enabled': 'ALTER TABLE giveaways ADD COLUMN claim_topic_enabled INTEGER DEFAULT 0',
                'result_hash': 'ALTER TABLE giveaways ADD COLUMN result_hash TEXT',
                'result_payload_json': 'ALTER TABLE giveaways ADD COLUMN result_payload_json TEXT',
                'claim_group_ref': 'ALTER TABLE giveaways ADD COLUMN claim_group_ref TEXT',
                'claim_topic_name': 'ALTER TABLE giveaways ADD COLUMN claim_topic_name TEXT',
                'claim_topic_thread_id': 'ALTER TABLE giveaways ADD COLUMN claim_topic_thread_id INTEGER',
                'claim_topic_invite_link': 'ALTER TABLE giveaways ADD COLUMN claim_topic_invite_link TEXT',
                'claim_topic_expire_at': 'ALTER TABLE giveaways ADD COLUMN claim_topic_expire_at TEXT',
                'claim_topic_deleted_at': 'ALTER TABLE giveaways ADD COLUMN claim_topic_deleted_at TEXT',
            }
            for column, ddl in extra_giveaway_cols.items():
                if column not in giveaways_cols:
                    self.conn.execute(ddl)

            if 'claim_notified' not in participants_cols:
                self.conn.execute('ALTER TABLE participants ADD COLUMN claim_notified INTEGER DEFAULT 0')
            if 'private_chat_started' not in users_cols:
                self.conn.execute('ALTER TABLE users ADD COLUMN private_chat_started INTEGER DEFAULT 0')
            if 'private_chat_started_at' not in users_cols:
                self.conn.execute('ALTER TABLE users ADD COLUMN private_chat_started_at TEXT')
            self.conn.commit()

    def upsert_user(self, user: Dict[str, Any]) -> None:
        self.exec(
            '''
            INSERT INTO users (telegram_user_id, username, first_name, last_name, is_bot, blacklisted, private_chat_started, private_chat_started_at, updated_at)
            VALUES (?, ?, ?, ?, ?, COALESCE((SELECT blacklisted FROM users WHERE telegram_user_id=?), 0), COALESCE((SELECT private_chat_started FROM users WHERE telegram_user_id=?), 0), (SELECT private_chat_started_at FROM users WHERE telegram_user_id=?), ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                is_bot=excluded.is_bot,
                updated_at=excluded.updated_at
            ''',
            (user['id'], user.get('username'), user.get('first_name'), user.get('last_name'), 1 if user.get('is_bot') else 0, user['id'], user['id'], user['id'], now_s()),
        )

    def get_user_id_by_username(self, username: str) -> Optional[int]:
        raw = username.strip().lstrip('@')
        if not raw:
            return None
        row = self.one('SELECT telegram_user_id FROM users WHERE lower(username)=lower(?) LIMIT 1', (raw,))
        return int(row['telegram_user_id']) if row else None

    def get_user_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        return self.one('SELECT * FROM users WHERE telegram_user_id=? LIMIT 1', (user_id,))

    def mark_private_chat_started(self, user_id: int) -> None:
        self.exec(
            '''
            INSERT INTO users (telegram_user_id, private_chat_started, private_chat_started_at, updated_at)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                private_chat_started=1,
                private_chat_started_at=COALESCE(users.private_chat_started_at, excluded.private_chat_started_at),
                updated_at=excluded.updated_at
            ''',
            (user_id, now_s(), now_s()),
        )

    def has_private_chat_started(self, user_id: int) -> bool:
        row = self.one('SELECT private_chat_started FROM users WHERE telegram_user_id=?', (user_id,))
        return bool(row and row['private_chat_started'])

    def blacklist(self, user_id: int, value: bool = True) -> None:
        self.exec(
            '''
            INSERT INTO users (telegram_user_id, blacklisted, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET blacklisted=excluded.blacklisted, updated_at=excluded.updated_at
            ''',
            (user_id, 1 if value else 0, now_s()),
        )

    def is_blacklisted(self, user_id: int) -> bool:
        row = self.one('SELECT blacklisted FROM users WHERE telegram_user_id=?', (user_id,))
        return bool(row and row['blacklisted'])

    def set_state(self, user_id: int, chat_id: int, flow: str, step: str, data: Dict[str, Any]) -> None:
        self.exec(
            '''
            INSERT INTO states (user_id, chat_id, flow, step, data_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id=excluded.chat_id,
                flow=excluded.flow,
                step=excluded.step,
                data_json=excluded.data_json,
                updated_at=excluded.updated_at
            ''',
            (user_id, chat_id, flow, step, jdump(data), now_s()),
        )

    def get_state(self, user_id: int) -> Optional[Dict[str, Any]]:
        row = self.one('SELECT * FROM states WHERE user_id=?', (user_id,))
        if not row:
            return None
        return {'user_id': row['user_id'], 'chat_id': row['chat_id'], 'flow': row['flow'], 'step': row['step'], 'data': jload(row['data_json'], {}), 'updated_at': row['updated_at']}

    def clear_state(self, user_id: int) -> None:
        self.exec('DELETE FROM states WHERE user_id=?', (user_id,))

    def log(self, event_type: str, actor_user_id: Optional[int], target_id: Optional[int], details: Dict[str, Any]) -> None:
        self.exec('INSERT INTO logs (event_type, actor_user_id, target_id, details_json, created_at) VALUES (?, ?, ?, ?, ?)', (event_type, actor_user_id, target_id, jdump(details), now_s()))

    def allow_action(self, key: str, min_interval_seconds: int, max_hits: int, window_seconds: int) -> bool:
        row = self.one('SELECT * FROM rate_limits WHERE key=?', (key,))
        current = now_s()
        if not row:
            self.exec('INSERT INTO rate_limits (key, last_at, window_start, hits) VALUES (?, ?, ?, ?)', (key, current, current, 1))
            return True
        from datetime import datetime

        current_dt = datetime.fromisoformat(current)
        last_dt = datetime.fromisoformat(row['last_at'])
        window_dt = datetime.fromisoformat(row['window_start'])
        if (current_dt - last_dt).total_seconds() < min_interval_seconds:
            return False
        if (current_dt - window_dt).total_seconds() >= window_seconds:
            self.exec('UPDATE rate_limits SET last_at=?, window_start=?, hits=? WHERE key=?', (current, current, 1, key))
            return True
        hits = int(row['hits']) + 1
        if hits > max_hits:
            self.exec('UPDATE rate_limits SET last_at=? WHERE key=?', (current, key))
            return False
        self.exec('UPDATE rate_limits SET last_at=?, hits=? WHERE key=?', (current, hits, key))
        return True

    def set_giveaway_weight(self, giveaway_id: int, telegram_user_id: int, weight: int, reason: str = '') -> None:
        self.exec(
            '''
            INSERT INTO giveaway_weights (giveaway_id, telegram_user_id, weight, reason, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(giveaway_id, telegram_user_id) DO UPDATE SET
                weight=excluded.weight,
                reason=excluded.reason,
                updated_at=excluded.updated_at
            ''',
            (giveaway_id, telegram_user_id, weight, reason, now_s(), now_s()),
        )

    def get_giveaway_weights(self, giveaway_id: int) -> Dict[int, int]:
        rows = self.all('SELECT telegram_user_id, weight FROM giveaway_weights WHERE giveaway_id=?', (giveaway_id,))
        return {int(row['telegram_user_id']): int(row['weight']) for row in rows}

    def start_finalization(self, giveaway_id: int) -> bool:
        cur = self.exec("UPDATE giveaways SET status='finalizing', updated_at=? WHERE id=? AND status='live'", (now_s(), giveaway_id))
        return cur.rowcount == 1

    def finish_finalization(self, giveaway_id: int, **fields: Any) -> None:
        fields['updated_at'] = now_s()
        sql = ', '.join(f'{k}=?' for k in fields)
        params = tuple(fields.values()) + (giveaway_id,)
        self.exec(f'UPDATE giveaways SET {sql} WHERE id=?', params)

    def create_giveaway(self, data: Dict[str, Any]) -> int:
        fields = [
            ('title', data['title']),
            ('prize', data['prize']),
            ('prize_json', jdump(data.get('prize_json') or [])),
            ('winner_count', data['winner_count']),
            ('participation_condition', data['participation_condition']),
            ('start_time', data['start_time']),
            ('end_time', data.get('end_time')),
            ('auto_draw_mode', data.get('auto_draw_mode', 'time')),
            ('draw_when_participants', data.get('draw_when_participants')),
            ('entry_methods', jdump(data['entry_methods'])),
            ('entry_keyword', data.get('entry_keyword')),
            ('require_channel', 1 if data.get('require_channel') else 0),
            ('required_channel', data.get('required_channel')),
            ('invite_required_count', data.get('invite_required_count', 0)),
            ('invite_weight_bonus', data.get('invite_weight_bonus', 0)),
            ('publish_chat_ref', data['publish_chat_ref']),
            ('publish_chat_thread_id', data.get('publish_chat_thread_id')),
            ('status', data['status']),
            ('created_by', data['created_by']),
            ('created_by_username', data.get('created_by_username')),
            ('created_at', now_s()),
            ('updated_at', now_s()),
            ('announcement_sent', 0),
            ('claim_deadline', data['claim_deadline']),
            ('seed_hash', data.get('seed_hash')),
            ('result_hash', data.get('result_hash')),
            ('result_payload_json', data.get('result_payload_json')),
            ('winner_json', jdump(data.get('winner_json') or [])),
            ('claim_topic_enabled', 1 if data.get('claim_topic_enabled') else 0),
            ('claim_group_ref', data.get('claim_group_ref')),
            ('claim_topic_name', data.get('claim_topic_name')),
            ('claim_topic_hours', data.get('claim_topic_hours', 72)),
            ('claim_topic_thread_id', data.get('claim_topic_thread_id')),
            ('claim_topic_invite_link', data.get('claim_topic_invite_link')),
            ('claim_topic_expire_at', data.get('claim_topic_expire_at')),
            ('claim_topic_deleted_at', data.get('claim_topic_deleted_at')),
            ('participant_notice_message_id', data.get('participant_notice_message_id')),
            ('participant_notice_deleted_at', data.get('participant_notice_deleted_at')),
        ]
        columns = ', '.join(name for name, _ in fields)
        placeholders = ', '.join('?' for _ in fields)
        values = tuple(value for _, value in fields)
        cur = self.exec(
            f'''
            INSERT INTO giveaways (
                {columns}
            ) VALUES ({placeholders})
            ''',
            values,
        )
        return int(cur.lastrowid)

    def update_giveaway(self, giveaway_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields['updated_at'] = now_s()
        sql = ', '.join(f'{k}=?' for k in fields)
        params = tuple(fields.values()) + (giveaway_id,)
        self.exec(f'UPDATE giveaways SET {sql} WHERE id=?', params)

    def get_giveaway(self, giveaway_id: int) -> Optional[Dict[str, Any]]:
        return self.one('SELECT * FROM giveaways WHERE id=?', (giveaway_id,))

    def list_giveaways(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self.all('SELECT * FROM giveaways ORDER BY id DESC LIMIT ?', (limit,))

    def due_to_start(self, current: str) -> List[Dict[str, Any]]:
        return self.all("SELECT * FROM giveaways WHERE status='scheduled' AND start_time<=? ORDER BY start_time ASC", (current,))

    def due_to_end(self, current: str) -> List[Dict[str, Any]]:
        return self.all("SELECT * FROM giveaways WHERE status='live' AND end_time IS NOT NULL AND end_time<=? ORDER BY end_time ASC", (current,))

    def get_due_claim_topics(self, current: str) -> List[Dict[str, Any]]:
        return self.all("SELECT * FROM giveaways WHERE claim_topic_enabled=1 AND claim_topic_deleted_at IS NULL AND claim_topic_expire_at IS NOT NULL AND claim_topic_expire_at<=?", (current,))

    def due_participant_targets(self) -> List[Dict[str, Any]]:
        return self.all("SELECT * FROM giveaways WHERE status='live' AND auto_draw_mode='participants' AND draw_when_participants IS NOT NULL AND draw_when_participants>0")

    def add_participant(self, giveaway_id: int, user: Dict[str, Any], source: str, invited_by: Optional[int] = None) -> Tuple[bool, str, bool]:
        try:
            self.exec(
                '''
                INSERT INTO participants (
                    giveaway_id, telegram_user_id, username, first_name, last_name, source, invited_by, joined_at, valid, invalid_reason, claim_notified
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, 0)
                ''',
                (giveaway_id, user['id'], user.get('username'), user.get('first_name'), user.get('last_name'), source, invited_by, now_s()),
            )
            return True, '已成功参与。', True
        except sqlite3.IntegrityError:
            return False, '你已经参与过这个抽奖了。', False

    def get_participants(self, giveaway_id: int) -> List[Dict[str, Any]]:
        return self.all('SELECT * FROM participants WHERE giveaway_id=? ORDER BY joined_at ASC, id ASC', (giveaway_id,))

    def count_participants(self, giveaway_id: int, valid_only: bool = False) -> int:
        if valid_only:
            row = self.one('SELECT COUNT(*) AS c FROM participants WHERE giveaway_id=? AND valid=1', (giveaway_id,))
        else:
            row = self.one('SELECT COUNT(*) AS c FROM participants WHERE giveaway_id=?', (giveaway_id,))
        return int(row['c'] if row else 0)

    def count_referrals(self, giveaway_id: int, referrer_user_id: int) -> int:
        row = self.one('SELECT COUNT(*) AS c FROM referrals WHERE giveaway_id=? AND referrer_user_id=? AND joined_at IS NOT NULL', (giveaway_id, referrer_user_id))
        return int(row['c'] if row else 0)

    def add_referral(self, giveaway_id: int, referrer_user_id: int, referred_user_id: int, payload: str) -> bool:
        try:
            self.exec('INSERT INTO referrals (giveaway_id, referrer_user_id, referred_user_id, payload, created_at) VALUES (?, ?, ?, ?, ?)', (giveaway_id, referrer_user_id, referred_user_id, payload, now_s()))
            return True
        except sqlite3.IntegrityError:
            return False

    def mark_referral_joined(self, giveaway_id: int, referred_user_id: int) -> None:
        self.exec('UPDATE referrals SET joined_at=? WHERE giveaway_id=? AND referred_user_id=?', (now_s(), giveaway_id, referred_user_id))

    def set_participant_validity(self, participant_id: int, valid: int, reason: str = '') -> None:
        self.exec('UPDATE participants SET valid=?, invalid_reason=? WHERE id=?', (valid, reason, participant_id))

    def set_participant_claim_notified(self, giveaway_id: int, telegram_user_id: int) -> None:
        self.exec('UPDATE participants SET claim_notified=1 WHERE giveaway_id=? AND telegram_user_id=?', (giveaway_id, telegram_user_id))



