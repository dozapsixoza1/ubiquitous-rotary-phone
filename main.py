"""
╔══════════════════════════════════════════════════════════════╗
║              MAFIA BOT — Full Starter Project                ║
║         python-telegram-bot v20+ | SQLite | asyncio          ║
╚══════════════════════════════════════════════════════════════╝

УСТАНОВКА:
    pip install python-telegram-bot==20.7 aiosqlite

ЗАПУСК:
    python mafia_bot.py

ПЕРЕМЕННЫЕ:
    BOT_TOKEN — вставь свой токен от @BotFather
    ADMIN_IDS  — список Telegram ID администраторов
"""

import asyncio
import random
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

import aiosqlite
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# ─────────────────────────── CONFIG ────────────────────────────

BOT_TOKEN = "8204466219:AAFmb3IS1523JYJp6KH55Zi4sGJxs5UtVnQ"
ADMIN_IDS = [7950038145]  # Telegram ID администраторов
DB_PATH = "mafia.db"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ──────────────────────────── ENUMS ────────────────────────────

class Phase(Enum):
    LOBBY   = "lobby"
    NIGHT0  = "night0"   # Знакомство мафии
    DAY     = "day"
    VOTE    = "vote"
    NIGHT   = "night"
    ENDED   = "ended"

class Faction(Enum):
    MAFIA   = "mafia"
    CITIZEN = "citizen"
    NEUTRAL = "neutral"

class RoleName(Enum):
    # Мафия
    MAFIA    = "Мафия"
    DON      = "Дон"
    # Мирные
    CITIZEN  = "Мирный"
    SHERIFF  = "Шериф"
    DOCTOR   = "Доктор"
    DETECTIVE= "Детектив"
    # Нейтралы
    MANIAC   = "Маньяк"
    WHORE    = "Путана"

# ─────────────────────────── ROLES ─────────────────────────────

@dataclass
class Role:
    name: RoleName
    faction: Faction
    emoji: str
    description: str
    has_night_action: bool = True

    def check_win(self, alive_players: list["Player"]) -> bool:
        """Проверяет, выиграла ли фракция этой роли."""
        alive_factions = {p.role.faction for p in alive_players}
        mafia_count  = sum(1 for p in alive_players if p.role.faction == Faction.MAFIA)
        civil_count  = sum(1 for p in alive_players if p.role.faction == Faction.CITIZEN)
        maniac_alive = any(p.role.name == RoleName.MANIAC for p in alive_players)

        if self.faction == Faction.MAFIA:
            return mafia_count >= civil_count and not maniac_alive
        if self.faction == Faction.CITIZEN:
            return mafia_count == 0 and not maniac_alive
        if self.name == RoleName.MANIAC:
            return len(alive_players) <= 2
        return False

ROLES: dict[RoleName, Role] = {
    RoleName.MAFIA:     Role(RoleName.MAFIA,     Faction.MAFIA,   "🔫", "Ночью выбирает жертву вместе с командой."),
    RoleName.DON:       Role(RoleName.DON,        Faction.MAFIA,   "👑", "Лидер мафии. Может проверить одного игрока — Шериф ли он."),
    RoleName.CITIZEN:   Role(RoleName.CITIZEN,    Faction.CITIZEN, "👤", "Мирный житель. Голосует днём, ищет мафию."),
    RoleName.SHERIFF:   Role(RoleName.SHERIFF,    Faction.CITIZEN, "🔍", "Ночью проверяет игрока — мафия или нет."),
    RoleName.DOCTOR:    Role(RoleName.DOCTOR,     Faction.CITIZEN, "💊", "Ночью лечит одного игрока (или себя, 1 раз за игру)."),
    RoleName.DETECTIVE: Role(RoleName.DETECTIVE,  Faction.CITIZEN, "🕵️", "Ночью узнаёт точную роль одного игрока."),
    RoleName.MANIAC:    Role(RoleName.MANIAC,     Faction.NEUTRAL, "🔪", "Убивает каждую ночь. Цель — выжить вдвоём с кем угодно."),
    RoleName.WHORE:     Role(RoleName.WHORE,      Faction.NEUTRAL, "💋", "Блокирует действие выбранного игрока на ночь."),
}

# ──────────────────────────── РЕЖИМЫ ───────────────────────────

GAME_MODES: dict[str, dict] = {
    "classic": {
        "name": "🎩 Классика",
        "min_players": 4,
        "roles": {
            4:  [RoleName.MAFIA, RoleName.CITIZEN, RoleName.CITIZEN, RoleName.CITIZEN],
            5:  [RoleName.MAFIA, RoleName.SHERIFF, RoleName.CITIZEN, RoleName.CITIZEN, RoleName.CITIZEN],
            6:  [RoleName.MAFIA, RoleName.MAFIA, RoleName.SHERIFF, RoleName.DOCTOR, RoleName.CITIZEN, RoleName.CITIZEN],
            8:  [RoleName.DON, RoleName.MAFIA, RoleName.SHERIFF, RoleName.DOCTOR, RoleName.CITIZEN, RoleName.CITIZEN, RoleName.CITIZEN, RoleName.CITIZEN],
            10: [RoleName.DON, RoleName.MAFIA, RoleName.MAFIA, RoleName.SHERIFF, RoleName.DOCTOR, RoleName.DETECTIVE, RoleName.CITIZEN, RoleName.CITIZEN, RoleName.CITIZEN, RoleName.CITIZEN],
        }
    },
    "chaos": {
        "name": "💥 Хаос",
        "min_players": 6,
        "roles": {
            6:  [RoleName.MAFIA, RoleName.MAFIA, RoleName.SHERIFF, RoleName.DOCTOR, RoleName.MANIAC, RoleName.CITIZEN],
            8:  [RoleName.DON, RoleName.MAFIA, RoleName.SHERIFF, RoleName.DOCTOR, RoleName.MANIAC, RoleName.WHORE, RoleName.CITIZEN, RoleName.CITIZEN],
            10: [RoleName.DON, RoleName.MAFIA, RoleName.MAFIA, RoleName.SHERIFF, RoleName.DOCTOR, RoleName.DETECTIVE, RoleName.MANIAC, RoleName.WHORE, RoleName.CITIZEN, RoleName.CITIZEN],
        }
    },
}

def get_roles_for_count(mode: str, count: int) -> list[RoleName]:
    """Подбирает ближайший подходящий набор ролей."""
    roles_map = GAME_MODES[mode]["roles"]
    keys = sorted(roles_map.keys())
    chosen = keys[0]
    for k in keys:
        if k <= count:
            chosen = k
    base = list(roles_map[chosen])
    while len(base) < count:
        base.append(RoleName.CITIZEN)
    return base[:count]

# ─────────────────────────── PLAYER ────────────────────────────

@dataclass
class Player:
    user_id: int
    username: str
    role: Role = field(default=None)
    is_alive: bool = True
    is_blocked: bool = False      # Путана заблокировала
    self_heal_used: bool = False  # Доктор использовал самолечение

    @property
    def mention(self) -> str:
        return f"@{self.username}" if self.username else f"id{self.user_id}"

# ──────────────────────────── GAME ─────────────────────────────

@dataclass
class Game:
    chat_id: int
    mode: str = "classic"
    phase: Phase = Phase.LOBBY
    players: list[Player] = field(default_factory=list)
    day_number: int = 0
    votes: dict[int, int] = field(default_factory=dict)        # voter_id → target_id
    night_actions: dict[int, int] = field(default_factory=dict) # actor_id → target_id
    message_ids: list[int] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.now)

    # ── геттеры ──────────────────────────────────────────────

    def get_player(self, user_id: int) -> Optional[Player]:
        return next((p for p in self.players if p.user_id == user_id), None)

    @property
    def alive(self) -> list[Player]:
        return [p for p in self.players if p.is_alive]

    @property
    def mafia_players(self) -> list[Player]:
        return [p for p in self.alive if p.role.faction == Faction.MAFIA]

    # ── раздача ролей ─────────────────────────────────────────

    def assign_roles(self):
        role_names = get_roles_for_count(self.mode, len(self.players))
        random.shuffle(role_names)
        for player, rname in zip(self.players, role_names):
            player.role = ROLES[rname]

    # ── проверка победы ───────────────────────────────────────

    def check_winner(self) -> Optional[str]:
        alive = self.alive
        mafia_count  = sum(1 for p in alive if p.role.faction == Faction.MAFIA)
        civil_count  = sum(1 for p in alive if p.role.faction == Faction.CITIZEN)
        maniac_alive = any(p.role.name == RoleName.MANIAC for p in alive)

        if mafia_count == 0 and not maniac_alive:
            return "citizen"
        if mafia_count >= civil_count and not maniac_alive:
            return "mafia"
        if maniac_alive and len(alive) <= 2:
            return "maniac"
        return None

    # ── подсчёт голосов ───────────────────────────────────────

    def get_vote_result(self) -> Optional[int]:
        """Возвращает user_id игрока с большинством голосов (или None при ничье)."""
        if not self.votes:
            return None
        count: dict[int, int] = {}
        for target in self.votes.values():
            count[target] = count.get(target, 0) + 1
        max_votes = max(count.values())
        leaders = [uid for uid, v in count.items() if v == max_votes]
        return leaders[0] if len(leaders) == 1 else None

# ────────────────────── GAME STORAGE ───────────────────────────

games: dict[int, Game] = {}  # chat_id → Game

def get_game(chat_id: int) -> Optional[Game]:
    return games.get(chat_id)

def create_game(chat_id: int) -> Game:
    g = Game(chat_id=chat_id)
    games[chat_id] = g
    return g

def delete_game(chat_id: int):
    games.pop(chat_id, None)

# ──────────────────────────── DATABASE ─────────────────────────

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                games_total INTEGER DEFAULT 0,
                games_won   INTEGER DEFAULT 0,
                kills       INTEGER DEFAULT 0,
                rating      INTEGER DEFAULT 1000,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS game_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER,
                mode        TEXT,
                winner      TEXT,
                players_count INTEGER,
                played_at   TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS game_players (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id     INTEGER,
                user_id     INTEGER,
                role        TEXT,
                survived    INTEGER,
                won         INTEGER
            );
        """)
        await db.commit()

async def ensure_user(user_id: int, username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
            (user_id, username)
        )
        await db.execute(
            "UPDATE users SET username=? WHERE user_id=?",
            (username, user_id)
        )
        await db.commit()

async def get_stats(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT username, games_total, games_won, kills, rating FROM users WHERE user_id=?",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "username": row[0], "games_total": row[1],
                "games_won": row[2], "kills": row[3], "rating": row[4]
            }

async def get_top(limit=10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT username, rating, games_total, games_won FROM users ORDER BY rating DESC LIMIT ?",
            (limit,)
        ) as cur:
            rows = await cur.fetchall()
            return [{"username": r[0], "rating": r[1], "games_total": r[2], "games_won": r[3]} for r in rows]

async def save_game_result(game: Game, winner: str):
    """Сохраняет результаты игры и обновляет рейтинг."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO game_history (chat_id, mode, winner, players_count) VALUES (?, ?, ?, ?)",
            (game.chat_id, game.mode, winner, len(game.players))
        )
        game_id = cur.lastrowid

        for p in game.players:
            won = _did_win(p, winner)
            delta = _elo_delta(won, len(game.players))

            await db.execute(
                "INSERT INTO game_players (game_id, user_id, role, survived, won) VALUES (?, ?, ?, ?, ?)",
                (game_id, p.user_id, p.role.name.value, int(p.is_alive), int(won))
            )
            await db.execute(
                """UPDATE users SET
                    games_total = games_total + 1,
                    games_won   = games_won + ?,
                    rating      = MAX(0, rating + ?)
                WHERE user_id = ?""",
                (int(won), delta, p.user_id)
            )
        await db.commit()

def _did_win(player: Player, winner: str) -> bool:
    if winner == "mafia"    and player.role.faction == Faction.MAFIA:   return True
    if winner == "citizen"  and player.role.faction == Faction.CITIZEN: return True
    if winner == "maniac"   and player.role.name == RoleName.MANIAC:    return True
    return False

def _elo_delta(won: bool, player_count: int) -> int:
    base = 25 if won else -15
    bonus = max(0, player_count - 6) * 2
    return base + bonus

# ──────────────────────── KEYBOARDS ────────────────────────────

def kb_join_lobby(game_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✋ Войти в игру", callback_data=f"join:{game_id}"),
        InlineKeyboardButton("🚀 Начать",       callback_data=f"start_game:{game_id}"),
    ]])

def kb_vote(alive: list[Player], voter_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(f"{p.mention}", callback_data=f"vote:{p.user_id}")]
        for p in alive if p.user_id != voter_id
    ]
    return InlineKeyboardMarkup(buttons)

def kb_night_targets(alive: list[Player], actor_id: int, include_self=False) -> InlineKeyboardMarkup:
    buttons = []
    for p in alive:
        if p.user_id == actor_id and not include_self:
            continue
        buttons.append([InlineKeyboardButton(f"{p.mention}", callback_data=f"night:{p.user_id}")])
    return InlineKeyboardMarkup(buttons)

def kb_mode_select() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(v["name"], callback_data=f"mode:{k}")
        for k, v in GAME_MODES.items()
    ]])

# ──────────────────────── HELPERS ──────────────────────────────

async def send_roles_to_players(context: ContextTypes.DEFAULT_TYPE, game: Game):
    """Рассылает каждому игроку его роль в личку."""
    mafia_team = [p.mention for p in game.mafia_players]
    for player in game.players:
        role = player.role
        text = (
            f"🎭 Твоя роль: *{role.emoji} {role.name.value}*\n\n"
            f"_{role.description}_\n\n"
        )
        if role.faction == Faction.MAFIA:
            text += f"👥 Твоя команда: {', '.join(mafia_team)}"
        try:
            await context.bot.send_message(player.user_id, text, parse_mode="Markdown")
        except Exception:
            pass  # Пользователь не начал диалог с ботом

async def announce(context, chat_id: int, text: str, reply_markup=None):
    await context.bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=reply_markup)

def players_list_text(game: Game) -> str:
    lines = []
    for i, p in enumerate(game.players, 1):
        status = "✅" if p.is_alive else "💀"
        lines.append(f"{i}. {status} {p.mention}")
    return "\n".join(lines)

def alive_list_text(game: Game) -> str:
    return "\n".join(f"• {p.mention}" for p in game.alive)

# ────────────────────── HANDLERS: LOBBY ────────────────────────

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id > 0:
        await update.message.reply_text("⚠️ Игру можно создать только в групповом чате!")
        return
    if get_game(chat_id):
        await update.message.reply_text("⚠️ Игра уже существует. /endgame — завершить текущую.")
        return

    game = create_game(chat_id)
    user = update.effective_user
    await ensure_user(user.id, user.username or user.first_name)
    game.players.append(Player(user.id, user.username or user.first_name))

    text = (
        f"🎲 *Новая игра Мафия!*\n\n"
        f"Создал: {user.mention_markdown()}\n"
        f"Игроки (1): {players_list_text(game)}\n\n"
        f"Нажми *Войти в игру* чтобы присоединиться.\n"
        f"Организатор нажимает *Начать* когда все готовы."
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb_join_lobby(chat_id))

async def cb_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split(":")[1])
    game = get_game(chat_id)
    if not game or game.phase != Phase.LOBBY:
        return

    user = query.from_user
    if any(p.user_id == user.id for p in game.players):
        await query.answer("Ты уже в игре!", show_alert=True)
        return
    if len(game.players) >= 15:
        await query.answer("Максимум 15 игроков.", show_alert=True)
        return

    await ensure_user(user.id, user.username or user.first_name)
    game.players.append(Player(user.id, user.username or user.first_name))

    text = (
        f"🎲 *Мафия — Лобби*\n\n"
        f"Режим: {GAME_MODES[game.mode]['name']}\n"
        f"Игроков: {len(game.players)}\n\n"
        f"{players_list_text(game)}"
    )
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb_join_lobby(chat_id))

async def cb_start_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = int(query.data.split(":")[1])
    game = get_game(chat_id)

    if not game or game.phase != Phase.LOBBY:
        return
    if query.from_user.id != game.players[0].user_id:
        await query.answer("Только создатель может начать игру!", show_alert=True)
        return

    min_p = GAME_MODES[game.mode]["min_players"]
    if len(game.players) < min_p:
        await query.answer(f"Минимум {min_p} игроков для этого режима!", show_alert=True)
        return

    await query.edit_message_text("⏳ Игра начинается...")
    await start_game(context, game)

async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    game = get_game(chat_id)
    if not game or game.phase != Phase.LOBBY:
        await update.message.reply_text("Сначала создай игру командой /newgame")
        return
    if update.effective_user.id != game.players[0].user_id:
        await update.message.reply_text("Только создатель может менять режим.")
        return
    await update.message.reply_text("Выбери режим игры:", reply_markup=kb_mode_select())

async def cb_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    mode = query.data.split(":")[1]
    game = get_game(query.message.chat_id)
    if game and game.phase == Phase.LOBBY:
        game.mode = mode
        await query.edit_message_text(f"✅ Режим изменён на {GAME_MODES[mode]['name']}")

# ──────────────────── GAME FLOW ─────────────────────────────────

async def start_game(context: ContextTypes.DEFAULT_TYPE, game: Game):
    game.assign_roles()
    game.phase = Phase.NIGHT0
    game.day_number = 0

    await announce(context, game.chat_id,
        f"🎭 *Роли розданы!*\n"
        f"Проверьте личные сообщения — там ваша роль.\n\n"
        f"Игроков: {len(game.players)}\n"
        f"Режим: {GAME_MODES[game.mode]['name']}"
    )
    await send_roles_to_players(context, game)

    # Небольшая пауза на знакомство мафии
    await asyncio.sleep(10)
    await begin_day(context, game)

async def begin_day(context: ContextTypes.DEFAULT_TYPE, game: Game):
    game.phase = Phase.DAY
    game.day_number += 1
    game.votes = {}

    winner = game.check_winner()
    if winner:
        await end_game(context, game, winner)
        return

    text = (
        f"☀️ *День {game.day_number}*\n\n"
        f"Живые игроки:\n{alive_list_text(game)}\n\n"
        f"Обсуждайте! Через 90 секунд начнётся голосование.\n"
        f"Или напишите /vote чтобы начать голосование раньше."
    )
    await announce(context, game.chat_id, text)
    context.job_queue.run_once(
        lambda ctx: asyncio.create_task(begin_vote(ctx, game)),
        when=90,
        name=f"vote_{game.chat_id}"
    )

async def begin_vote(context: ContextTypes.DEFAULT_TYPE, game: Game):
    if game.phase != Phase.DAY:
        return
    game.phase = Phase.VOTE
    game.votes = {}

    text = (
        f"🗳️ *Голосование!*\n\n"
        f"Каждый живой игрок должен проголосовать.\n"
        f"Нажми на имя того, кого хочешь исключить:"
    )
    for p in game.alive:
        try:
            await context.bot.send_message(
                p.user_id,
                text,
                reply_markup=kb_vote(game.alive, p.user_id),
                parse_mode="Markdown"
            )
        except Exception:
            pass

    await announce(context, game.chat_id,
        f"🗳️ *Голосование началось!*\n"
        f"Игроки получили кнопки в личных сообщениях.\n"
        f"Жду голосов..."
    )

async def cb_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    voter_id  = query.from_user.id
    target_id = int(query.data.split(":")[1])

    # Ищем игру где этот игрок участвует
    game = next(
        (g for g in games.values()
         if g.phase == Phase.VOTE and g.get_player(voter_id)),
        None
    )
    if not game:
        return

    voter = game.get_player(voter_id)
    if not voter or not voter.is_alive:
        return

    game.votes[voter_id] = target_id
    await query.edit_message_text(
        f"✅ Голос принят! Проголосовало: {len(game.votes)}/{len(game.alive)}"
    )

    # Все проголосовали?
    if len(game.votes) >= len(game.alive):
        await process_vote_result(context, game)

async def process_vote_result(context: ContextTypes.DEFAULT_TYPE, game: Game):
    result_id = game.get_vote_result()
    if result_id:
        target = game.get_player(result_id)
        target.is_alive = False
        count = sum(1 for v in game.votes.values() if v == result_id)
        text = (
            f"☠️ *{target.mention}* исключён из игры голосованием ({count} голосов)!\n"
            f"Роль: {target.role.emoji} {target.role.name.value}"
        )
    else:
        text = "🤝 *Голоса разделились!* Никто не исключён."

    await announce(context, game.chat_id, text)

    winner = game.check_winner()
    if winner:
        await end_game(context, game, winner)
        return

    await asyncio.sleep(3)
    await begin_night(context, game)

async def begin_night(context: ContextTypes.DEFAULT_TYPE, game: Game):
    game.phase = Phase.NIGHT
    game.night_actions = {}

    # Сбрасываем блокировки
    for p in game.alive:
        p.is_blocked = False

    await announce(context, game.chat_id,
        f"🌙 *Ночь {game.day_number}*\n\n"
        f"Город засыпает...\n"
        f"Проверьте личные сообщения — там ваши действия."
    )

    # Рассылаем ночные действия
    for player in game.alive:
        role = player.role
        if not role.has_night_action:
            continue

        if role.name == RoleName.MAFIA:
            # Мафия убивает
            targets = [p for p in game.alive if p.role.faction != Faction.MAFIA]
            try:
                await context.bot.send_message(
                    player.user_id,
                    f"🔫 *Мафия* — выбери жертву:",
                    reply_markup=kb_night_targets(targets, player.user_id),
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        elif role.name == RoleName.DON:
            targets = [p for p in game.alive if p.role.faction != Faction.MAFIA]
            try:
                await context.bot.send_message(
                    player.user_id,
                    f"👑 *Дон* — выбери жертву (или кого проверить на Шерифа):",
                    reply_markup=kb_night_targets(targets, player.user_id),
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        elif role.name == RoleName.SHERIFF:
            try:
                await context.bot.send_message(
                    player.user_id,
                    f"🔍 *Шериф* — кого проверить?",
                    reply_markup=kb_night_targets(game.alive, player.user_id),
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        elif role.name == RoleName.DOCTOR:
            include_self = not player.self_heal_used
            try:
                await context.bot.send_message(
                    player.user_id,
                    f"💊 *Доктор* — кого вылечить?",
                    reply_markup=kb_night_targets(game.alive, player.user_id, include_self),
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        elif role.name == RoleName.DETECTIVE:
            try:
                await context.bot.send_message(
                    player.user_id,
                    f"🕵️ *Детектив* — чью роль узнать?",
                    reply_markup=kb_night_targets(game.alive, player.user_id),
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        elif role.name == RoleName.MANIAC:
            try:
                await context.bot.send_message(
                    player.user_id,
                    f"🔪 *Маньяк* — выбери жертву:",
                    reply_markup=kb_night_targets(game.alive, player.user_id),
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        elif role.name == RoleName.WHORE:
            try:
                await context.bot.send_message(
                    player.user_id,
                    f"💋 *Путана* — кого заблокировать?",
                    reply_markup=kb_night_targets(game.alive, player.user_id),
                    parse_mode="Markdown"
                )
            except Exception:
                pass

    # Таймер ночи — 60 секунд
    context.job_queue.run_once(
        lambda ctx: asyncio.create_task(resolve_night(ctx, game)),
        when=60,
        name=f"night_{game.chat_id}"
    )

async def cb_night_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    actor_id  = query.from_user.id
    target_id = int(query.data.split(":")[1])

    game = next(
        (g for g in games.values()
         if g.phase == Phase.NIGHT and g.get_player(actor_id)),
        None
    )
    if not game:
        return

    actor = game.get_player(actor_id)
    if not actor or not actor.is_alive:
        return

    # Для мафии: все мафиози должны проголосовать за одну жертву
    # Здесь упрощённо — последний голос мафии побеждает
    game.night_actions[actor_id] = target_id
    await query.edit_message_text(f"✅ Действие выбрано.")

async def resolve_night(context: ContextTypes.DEFAULT_TYPE, game: Game):
    if game.phase != Phase.NIGHT:
        return

    results = []
    healed_id = None
    blocked_ids = set()

    # 1. Путана блокирует
    for p in game.alive:
        if p.role.name == RoleName.WHORE and p.user_id in game.night_actions:
            target_id = game.night_actions[p.user_id]
            blocked_ids.add(target_id)
            target = game.get_player(target_id)
            if target:
                target.is_blocked = True

    # 2. Доктор лечит
    for p in game.alive:
        if p.role.name == RoleName.DOCTOR and p.user_id in game.night_actions:
            if p.user_id not in blocked_ids:
                healed_id = game.night_actions[p.user_id]
                if healed_id == p.user_id:
                    p.self_heal_used = True

    # 3. Мафия убивает (голосует большинством)
    mafia_votes: dict[int, int] = {}
    for p in game.mafia_players:
        if p.user_id in game.night_actions and p.user_id not in blocked_ids:
            t = game.night_actions[p.user_id]
            mafia_votes[t] = mafia_votes.get(t, 0) + 1
    if mafia_votes:
        kill_id = max(mafia_votes, key=lambda k: mafia_votes[k])
        target = game.get_player(kill_id)
        if target and target.is_alive:
            if kill_id == healed_id:
                results.append(f"💊 Ночью кто-то был ранен, но *доктор спас его!*")
            else:
                target.is_alive = False
                results.append(f"🔫 Ночью мафия убила *{target.mention}*. Роль: {target.role.emoji} {target.role.name.value}")
        else:
            results.append("🌙 Ночью ничего не произошло.")
    else:
        results.append("🌙 Ночью было тихо...")

    # 4. Маньяк убивает
    for p in game.alive:
        if p.role.name == RoleName.MANIAC and p.user_id in game.night_actions:
            if p.user_id not in blocked_ids:
                target = game.get_player(game.night_actions[p.user_id])
                if target and target.is_alive:
                    if target.user_id == healed_id:
                        results.append(f"💊 Маньяк пытался убить, но *доктор спас жертву!*")
                    else:
                        target.is_alive = False
                        results.append(f"🔪 Маньяк убил *{target.mention}*!")

    # 5. Шериф и Детектив получают результаты в ЛС
    for p in game.alive:
        if p.role.name == RoleName.SHERIFF and p.user_id in game.night_actions:
            if p.user_id not in blocked_ids:
                target = game.get_player(game.night_actions[p.user_id])
                if target:
                    is_mafia = target.role.faction == Faction.MAFIA
                    msg = f"🔍 Проверка: {target.mention} — {'🔴 МАФИЯ' if is_mafia else '🟢 Мирный'}"
                    try:
                        await context.bot.send_message(p.user_id, msg, parse_mode="Markdown")
                    except Exception:
                        pass

        elif p.role.name == RoleName.DETECTIVE and p.user_id in game.night_actions:
            if p.user_id not in blocked_ids:
                target = game.get_player(game.night_actions[p.user_id])
                if target:
                    msg = f"🕵️ Роль {target.mention}: {target.role.emoji} *{target.role.name.value}*"
                    try:
                        await context.bot.send_message(p.user_id, msg, parse_mode="Markdown")
                    except Exception:
                        pass

    night_report = "\n".join(results)
    await announce(context, game.chat_id,
        f"🌅 *Рассвет — День {game.day_number + 1}*\n\n{night_report}\n\n"
        f"Живые игроки:\n{alive_list_text(game)}"
    )

    winner = game.check_winner()
    if winner:
        await end_game(context, game, winner)
        return

    await asyncio.sleep(3)
    await begin_day(context, game)

async def end_game(context: ContextTypes.DEFAULT_TYPE, game: Game, winner: str):
    game.phase = Phase.ENDED
    winners_map = {
        "mafia":   "🔫 *Победила МАФИЯ!*",
        "citizen": "✌️ *Победили МИРНЫЕ!*",
        "maniac":  "🔪 *Победил МАНЬЯК!*",
    }
    title = winners_map.get(winner, "🏁 Игра завершена!")

    roles_reveal = "\n".join(
        f"{'✅' if p.is_alive else '💀'} {p.mention} — {p.role.emoji} {p.role.name.value}"
        for p in game.players
    )

    await announce(context, game.chat_id,
        f"{title}\n\n"
        f"*Роли всех игроков:*\n{roles_reveal}\n\n"
        f"Игра длилась {game.day_number} дней."
    )

    await save_game_result(game, winner)
    delete_game(game.chat_id)

# ──────────────────── HANDLERS: STATS ──────────────────────────

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user.id, user.username or user.first_name)
    s = await get_stats(user.id)
    if not s:
        await update.message.reply_text("Статистика не найдена.")
        return

    winrate = (s["games_won"] / s["games_total"] * 100) if s["games_total"] > 0 else 0
    text = (
        f"📊 *Статистика {s['username']}*\n\n"
        f"⭐ Рейтинг: `{s['rating']}`\n"
        f"🎮 Игр сыграно: `{s['games_total']}`\n"
        f"🏆 Побед: `{s['games_won']}`\n"
        f"📈 Винрейт: `{winrate:.1f}%`\n"
        f"💀 Убийств: `{s['kills']}`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top = await get_top(10)
    if not top:
        await update.message.reply_text("Рейтинг пуст.")
        return

    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, p in enumerate(top):
        medal = medals[i] if i < 3 else f"{i+1}."
        winrate = (p["games_won"] / p["games_total"] * 100) if p["games_total"] > 0 else 0
        lines.append(f"{medal} {p['username']} — ⭐{p['rating']} ({winrate:.0f}%)")

    await update.message.reply_text(
        "🏆 *Топ игроков*\n\n" + "\n".join(lines),
        parse_mode="Markdown"
    )

# ──────────────────── HANDLERS: ADMIN ──────────────────────────

async def cmd_endgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    game = get_game(chat_id)

    if not game:
        await update.message.reply_text("Нет активной игры.")
        return

    is_admin = user_id in ADMIN_IDS or user_id == game.players[0].user_id
    if not is_admin:
        await update.message.reply_text("Только создатель или администратор может завершить игру.")
        return

    delete_game(chat_id)
    await update.message.reply_text("🛑 Игра принудительно завершена.")

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    text = (
        "🔧 *Админ-панель*\n\n"
        f"Активных игр: `{len(games)}`\n\n"
        "*Команды:*\n"
        "/endgame — завершить игру в чате\n"
        "/top — топ игроков\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_vote_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ускорить голосование."""
    chat_id = update.effective_chat.id
    game = get_game(chat_id)
    if game and game.phase == Phase.DAY:
        # Отменяем автотаймер
        jobs = context.job_queue.get_jobs_by_name(f"vote_{chat_id}")
        for job in jobs:
            job.schedule_removal()
        await begin_vote(context, game)

# ─────────────────────── MAIN ──────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user.id, user.username or user.first_name)

    if update.effective_chat.id > 0:
        # Личка — показываем помощь
        await update.message.reply_text(
            f"👋 Привет, {user.first_name}!\n\n"
            f"Я бот для игры в *Мафию*!\n\n"
            f"Добавь меня в групповой чат и используй /newgame\n\n"
            f"*Команды:*\n"
            f"/stats — моя статистика\n"
            f"/top — топ игроков",
            parse_mode="Markdown"
        )

async def post_init(app: Application):
    await init_db()
    await app.bot.set_my_commands([
        BotCommand("newgame",  "Создать новую игру"),
        BotCommand("mode",     "Сменить режим игры"),
        BotCommand("vote",     "Начать голосование сейчас"),
        BotCommand("endgame",  "Завершить игру"),
        BotCommand("stats",    "Моя статистика"),
        BotCommand("top",      "Топ игроков"),
        BotCommand("admin",    "Админ-панель"),
    ])

def main():

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Handlers
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("mode",    cmd_mode))
    app.add_handler(CommandHandler("vote",    cmd_vote_now))
    app.add_handler(CommandHandler("endgame", cmd_endgame))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("top",     cmd_top))
    app.add_handler(CommandHandler("admin",   cmd_admin))

    app.add_handler(CallbackQueryHandler(cb_join,         pattern=r"^join:"))
    app.add_handler(CallbackQueryHandler(cb_start_game,   pattern=r"^start_game:"))
    app.add_handler(CallbackQueryHandler(cb_mode,         pattern=r"^mode:"))
    app.add_handler(CallbackQueryHandler(cb_vote,         pattern=r"^vote:"))
    app.add_handler(CallbackQueryHandler(cb_night_action, pattern=r"^night:"))

    log.info("🎲 Mafia Bot запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
