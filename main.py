#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
猜歌游戏插件 - 听歌识曲群聊互动
功能：
1. 从网易云热门歌单随机选歌
2. 发送语音片段，群友抢答
3. 🎶加入游戏（支持中途加入）
4. 答对自动下一轮
5. 排行榜功能
6. 真心话/大冒险环节
"""

import os
import json
import random
import time
import asyncio
import re
import aiohttp
from typing import Dict, Optional, List
from dataclasses import dataclass, field

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Star, Context, register

# 尝试导入 aiocqhttp 消息事件
try:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
except ImportError:
    AiocqhttpMessageEvent = None

# 插件目录
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, "data")

# 热门歌单ID
PLAYLISTS = {
    "热门": 3778678,       # 热歌榜
    "经典": 19723756,      # 经典老歌
}


# ==================== 数据模型 ====================
@dataclass
class GameSession:
    """游戏会话"""
    group_id: str
    status: str = "waiting"  # waiting / playing / ended
    participants: Dict[str, dict] = field(default_factory=dict)  # {user_id: {"name": str, "score": int}}
    current_song: dict = field(default_factory=dict)  # {id, name, artist}
    hint_level: int = 0
    round_num: int = 0
    start_time: float = 0
    timeout_task: Optional[asyncio.Task] = None
    umo: str = ""  # unified_msg_origin 用于主动发送消息
    creator_id: str = ""  # 创建者ID
    round_answered: bool = False  # 本轮是否已被答对（只有第一个答对的人得分）


# ==================== 插件主类 ====================
@register("astrbot_plugin_guess_song", "皓月", "猜歌游戏 - 听歌识曲群聊互动", "1.0.0")
class GuessSongPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}  # ✅ 正确接收配置
        
        # 游戏会话 {group_id: GameSession}
        self.sessions: Dict[str, GameSession] = {}
        
        # HTTP 会话
        self.http_session: Optional[aiohttp.ClientSession] = None
        
        # 配置项
        self.round_timeout = self.config.get("round_timeout", 60)
        self.max_rounds = self.config.get("max_rounds", 10)
        self.admin_ids = set(str(x) for x in self.config.get("admin_ids", []))
        self.min_players = self.config.get("min_players", 1)
        self.max_players = self.config.get("max_players", 20)
        self.voice_send_timeout = self.config.get("voice_send_timeout", 15)
        
        # 缓存清理配置
        self.cache_path = self.config.get("cache_path", "") or DATA_DIR
        self.cache_cleanup_hours = self.config.get("cache_cleanup_hours", 48)
        self.cleanup_task: Optional[asyncio.Task] = None
        
        # 歌单缓存
        self.playlist_cache: Dict[str, List[dict]] = {}
        
        # 已播放歌曲记录 {group_id: {song_id: timestamp}}
        self.played_songs: Dict[str, Dict[int, float]] = {}
        
        # 确保数据目录存在
        os.makedirs(DATA_DIR, exist_ok=True)

    async def initialize(self):
        """插件初始化"""
        logger.info("[猜歌游戏] 插件初始化")
        self.http_session = aiohttp.ClientSession()
        # 预加载歌单
        await self._preload_playlists()
        # 启动缓存清理任务
        if self.cache_cleanup_hours > 0:
            self.cleanup_task = asyncio.create_task(self._run_cleanup_task())
            logger.info(f"[猜歌游戏] 缓存清理任务已启动，周期: {self.cache_cleanup_hours}小时")

    async def terminate(self):
        """插件终止"""
        logger.info("[猜歌游戏] 插件正在关闭")
        # 取消缓存清理任务
        if self.cleanup_task:
            self.cleanup_task.cancel()
        # 取消所有超时任务
        for session in self.sessions.values():
            if session.timeout_task:
                session.timeout_task.cancel()
        # 关闭HTTP会话
        if self.http_session:
            await self.http_session.close()

    async def _run_cleanup_task(self):
        """定时清理缓存任务"""
        while True:
            try:
                await asyncio.sleep(self.cache_cleanup_hours * 3600)  # 等待指定小时
                await self._cleanup_cache()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[猜歌游戏] 缓存清理任务异常: {e}")

    async def _cleanup_cache(self):
        """清理缓存文件（删除超过指定时间的文件）"""
        if not os.path.exists(self.cache_path):
            logger.warning(f"[猜歌游戏] 缓存目录不存在: {self.cache_path}")
            return
        
        now = time.time()
        max_age = self.cache_cleanup_hours * 3600  # 最大保留时间（秒）
        deleted_count = 0
        deleted_size = 0
        
        try:
            for filename in os.listdir(self.cache_path):
                filepath = os.path.join(self.cache_path, filename)
                
                # 只处理文件，不处理目录
                if not os.path.isfile(filepath):
                    continue
                
                # 跳过非缓存文件（保留 .json 数据文件）
                if filename.endswith('.json'):
                    continue
                
                # 检查文件修改时间
                file_mtime = os.path.getmtime(filepath)
                if now - file_mtime > max_age:
                    file_size = os.path.getsize(filepath)
                    os.remove(filepath)
                    deleted_count += 1
                    deleted_size += file_size
            
            if deleted_count > 0:
                size_mb = deleted_size / (1024 * 1024)
                logger.info(f"[猜歌游戏] 缓存清理完成，删除 {deleted_count} 个文件，释放 {size_mb:.2f} MB")
            else:
                logger.info("[猜歌游戏] 缓存清理完成，无过期文件")
        except Exception as e:
            logger.error(f"[猜歌游戏] 缓存清理失败: {e}")

    # ==================== 网易云API ====================
    async def _netease_request(self, url: str, data: dict = None, method: str = "GET") -> dict:
        """网易云API请求"""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://music.163.com/",
            "Origin": "https://music.163.com",
        }
        cookies = {"appver": "2.9.11", "os": "pc"}
        timeout = aiohttp.ClientTimeout(total=10)
        
        try:
            if method.upper() == "POST":
                async with self.http_session.post(url, headers=headers, cookies=cookies, 
                                                   data=data or {}, timeout=timeout) as resp:
                    return await resp.json()
            else:
                async with self.http_session.get(url, headers=headers, cookies=cookies,
                                                  timeout=timeout) as resp:
                    return await resp.json()
        except Exception as e:
            logger.error(f"[猜歌游戏] API请求失败: {e}")
            return {}

    async def _preload_playlists(self):
        """预加载歌单"""
        for name, playlist_id in PLAYLISTS.items():
            try:
                songs = await self._fetch_playlist(playlist_id)
                if songs:
                    self.playlist_cache[name] = songs
                    logger.info(f"[猜歌游戏] 加载歌单 {name}: {len(songs)} 首歌")
            except Exception as e:
                logger.error(f"[猜歌游戏] 加载歌单 {name} 失败: {e}")

    def _is_valid_song_name(self, name: str) -> bool:
        """检查歌名是否为2-7个纯中文字符"""
        return bool(re.fullmatch(r"[\u4e00-\u9fff]{2,7}", (name or "").strip()))

    def _is_chinese_artist_name(self, name: str) -> bool:
        """检查歌手名是否为纯中文字符"""
        return bool(re.fullmatch(r"[\u4e00-\u9fff]+", (name or "").strip()))

    async def _fetch_playlist(self, playlist_id: int) -> List[dict]:
        """获取歌单歌曲列表（只获取免费中文歌曲）"""
        url = f"https://music.163.com/api/playlist/detail?id={playlist_id}"
        result = await self._netease_request(url)
        
        if not result or result.get("code") != 200:
            return []
        
        tracks = result.get("result", {}).get("tracks", [])
        songs = []
        vip_count = 0
        invalid_name_count = 0
        invalid_artist_count = 0
        for track in tracks[:200]:  # 扫描更多歌曲以补偿被过滤的
            # 过滤VIP歌曲：fee=0免费, fee=8低音质免费, 其他为VIP
            fee = track.get("fee", 1)
            if fee not in (0, 8):
                vip_count += 1
                continue
            
            song_name = track.get("name", "未知歌曲")
            
            # 过滤歌名：仅允许2-7个纯中文字符
            if not self._is_valid_song_name(song_name):
                invalid_name_count += 1
                continue

            artist_names = [a.get("name", "").strip() for a in track.get("artists", []) if a.get("name", "").strip()]
            if not artist_names or any(not self._is_chinese_artist_name(name) for name in artist_names):
                invalid_artist_count += 1
                continue

            artists = "、".join(artist_names)
            songs.append({
                "id": track.get("id"),
                "name": song_name,
                "artist": artists or "未知歌手"
            })
            
            if len(songs) >= 100:  # 最多保留100首
                break
        
        logger.info(
            f"[猜歌游戏] 歌单加载完成，符合规则歌曲: {len(songs)}，VIP已过滤: {vip_count}，"
            f"歌名不合规已过滤: {invalid_name_count}，歌手不合规已过滤: {invalid_artist_count}"
        )
        return songs


    async def _get_random_song(self, group_id: str = "") -> Optional[dict]:
        """随机获取一首歌，避免24小时内重复"""
        # 合并所有歌单
        all_songs = []
        for songs in self.playlist_cache.values():
            all_songs.extend(songs)
        
        if not all_songs:
            # 缓存为空，尝试重新加载
            await self._preload_playlists()
            for songs in self.playlist_cache.values():
                all_songs.extend(songs)
        
        if not all_songs:
            return None
        
        # 过滤24小时内已播放的歌曲
        now = time.time()
        one_day_ago = now - 86400  # 24小时
        
        if group_id and group_id in self.played_songs:
            # 清理超过24小时的记录
            self.played_songs[group_id] = {
                song_id: ts for song_id, ts in self.played_songs[group_id].items()
                if ts > one_day_ago
            }
            
            # 过滤已播放的歌曲
            played_ids = set(self.played_songs[group_id].keys())
            available_songs = [s for s in all_songs if s["id"] not in played_ids]
            
            if available_songs:
                song = random.choice(available_songs)
            else:
                # 所有歌都播过了，清空记录重新开始
                logger.info(f"[猜歌游戏] 群 {group_id} 所有歌曲已播放，重置记录")
                self.played_songs[group_id] = {}
                song = random.choice(all_songs)
        else:
            song = random.choice(all_songs)
        
        # 记录已播放
        if group_id:
            if group_id not in self.played_songs:
                self.played_songs[group_id] = {}
            self.played_songs[group_id][song["id"]] = now
        
        return song

    def _get_audio_url(self, song_id: int) -> str:
        """获取音频URL"""
        return f"https://music.163.com/song/media/outer/url?id={song_id}.mp3"

    # ==================== 工具方法 ====================
    def _get_group_id(self, event: AstrMessageEvent) -> str:
        """获取群组ID"""
        if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'group_id'):
            return str(event.message_obj.group_id) if event.message_obj.group_id else ""
        return ""

    def _get_user_info(self, event: AstrMessageEvent) -> tuple:
        """获取用户ID和昵称"""
        user_id = str(event.get_sender_id())
        nickname = event.get_sender_name() or f"用户{user_id[-4:]}"
        return user_id, nickname

    def _get_session(self, group_id: str) -> GameSession:
        """获取或创建游戏会话"""
        if group_id not in self.sessions:
            self.sessions[group_id] = GameSession(group_id=group_id)
        return self.sessions[group_id]

    def _get_hint(self, song_name: str, level: int) -> str:
        """生成提示（逐步揭示）"""
        if level <= 0:
            return "＊" * len(song_name)
        
        # 每级揭示一个字
        revealed = min(level, len(song_name))
        hint = list("＊" * len(song_name))
        for i in range(revealed):
            hint[i] = song_name[i]
        return "".join(hint)

    def _check_answer(self, user_input: str, correct_answer: str) -> bool:
        """检查答案是否正确"""
        # 标准化：去空格、转小写
        user_input = user_input.strip().lower().replace(" ", "")
        correct_answer = correct_answer.strip().lower().replace(" ", "")
        
        # 完全匹配
        if user_input == correct_answer:
            return True
        
        # 包含匹配（用户输入包含正确答案）
        if correct_answer in user_input:
            return True
        
        # 正确答案包含用户输入（答案较长时）
        if len(user_input) >= 2 and user_input in correct_answer:
            return True
        
        return False

    # ==================== 排行榜 ====================
    def _get_stats_path(self, group_id: str) -> str:
        """获取排行榜文件路径"""
        return os.path.join(DATA_DIR, f"stats_{group_id}.json")

    def _load_stats(self, group_id: str) -> dict:
        """加载排行榜"""
        path = self._get_stats_path(group_id)
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"[猜歌游戏] 加载排行榜失败: {e}")
        return {"users": {}}

    def _save_stats(self, group_id: str, data: dict):
        """保存排行榜"""
        path = self._get_stats_path(group_id)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[猜歌游戏] 保存排行榜失败: {e}")

    def _add_score(self, group_id: str, user_id: str, nickname: str, points: int = 1):
        """增加得分（历史总榜）"""
        stats = self._load_stats(group_id)
        users = stats.setdefault("users", {})
        
        if user_id not in users:
            users[user_id] = {"nickname": nickname, "score": 0, "wins": 0}
        
        users[user_id]["score"] += points
        users[user_id]["wins"] += 1
        users[user_id]["nickname"] = nickname  # 更新昵称
        
        self._save_stats(group_id, stats)

    # ==================== 游戏核心逻辑 ====================
    async def _start_game(self, event: AstrMessageEvent, group_id: str):
        """开始游戏，发送第一轮题目"""
        session = self._get_session(group_id)
        
        session.status = "playing"
        session.round_num = 0
        session.start_time = time.time()
        
        # 生成参与者列表
        player_list = ", ".join([data["name"] for data in session.participants.values()])
        
        await event.send(event.plain_result(
            f"🎮 猜歌游戏开始！共 {len(session.participants)} 人参与\n"
            f"👥 参与者: {player_list}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"规则：听歌猜歌名，第一个猜对得分！\n"
            f"（游戏进行中仍可发送 🎶 加入）"
        ))
        
        # 开始第一轮
        await self._next_round(event, group_id)

    async def _next_round(self, event: AstrMessageEvent, group_id: str):
        """进入下一轮"""
        session = self.sessions.get(group_id)
        if not session:
            return
        
        next_round = session.round_num + 1
        
        # 检查是否达到最大回合数
        if next_round > self.max_rounds:
            await self._end_game(event, group_id)
            return
        
        # 获取随机歌曲
        song = await self._get_random_song(group_id)
        if not song:
            await event.send(event.plain_result("❌ 无法获取歌曲，游戏结束。"))
            await self._end_game(event, group_id)
            return
        
        session.current_song = song
        session.hint_level = 0
        session.round_num = next_round
        session.round_answered = False  # 重置本轮答对标志
        
        # 取消之前的超时任务
        if session.timeout_task:
            session.timeout_task.cancel()
        
        # 发送音频
        audio_url = self._get_audio_url(song["id"])
        
        # 尝试发送语音消息
        if AiocqhttpMessageEvent and isinstance(event, AiocqhttpMessageEvent):
            try:
                payload = {
                    "group_id": int(group_id),
                    "message": [{"type": "record", "data": {"file": audio_url}}]
                }
                await asyncio.wait_for(
                    event.bot.call_action("send_group_msg", **payload),
                    timeout=self.voice_send_timeout,
                )
                
                await event.send(event.plain_result(
                    f"🎵 【第 {session.round_num}/{self.max_rounds} 轮】\n"
                    f"请听歌曲片段，猜歌名！\n"
                    f"⏰ {self.round_timeout}秒内作答\n"
                    f"💡 发送「#猜歌提示」获取提示"
                ))
            except asyncio.TimeoutError:
                logger.warning(f"[猜歌游戏] 发送语音超时（>{self.voice_send_timeout}s），已降级发送链接")
                await event.send(event.plain_result(
                    f"🎵 【第 {session.round_num}/{self.max_rounds} 轮】\n"
                    f"🔗 {audio_url}\n"
                    f"语音发送超时，已切换为链接播放\n"
                    f"⏰ {self.round_timeout}秒内作答"
                ))
            except Exception as e:
                logger.error(f"[猜歌游戏] 发送语音失败: {e}")
                await event.send(event.plain_result(
                    f"🎵 【第 {session.round_num}/{self.max_rounds} 轮】\n"
                    f"🔗 {audio_url}\n"
                    f"请听歌曲片段，猜歌名！\n"
                    f"⏰ {self.round_timeout}秒内作答"
                ))
        else:
            # 非QQ平台，发送链接
            await event.send(event.plain_result(
                f"🎵 【第 {session.round_num}/{self.max_rounds} 轮】\n"
                f"🔗 {audio_url}\n"
                f"请听歌曲片段，猜歌名！"
            ))
        
        # 启动超时任务
        session.timeout_task = asyncio.create_task(
            self._round_timeout(event, group_id)
        )

    async def _round_timeout(self, event: AstrMessageEvent, group_id: str):
        """回合超时处理"""
        try:
            await asyncio.sleep(self.round_timeout)
            
            session = self.sessions.get(group_id)
            if not session or session.status != "playing":
                return
            
            song = session.current_song
            
            # 发送超时消息
            await event.send(event.plain_result(
                f"⏰ 时间到！\n答案是：《{song['name']}》- {song['artist']}\n\n正在进入下一轮..."
            ))
            
            # 进入下一轮
            await asyncio.sleep(2)
            await self._next_round(event, group_id)
            
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[猜歌游戏] 超时处理异常: {e}")

    async def _end_game(self, event: AstrMessageEvent, group_id: str):
        """结束游戏并结算"""
        session = self.sessions.get(group_id)
        if not session:
            return
        
        # 取消超时任务
        if session.timeout_task:
            session.timeout_task.cancel()
        
        session.status = "ended"
        
        # 获取本局得分
        scores = session.participants
        
        if not scores:
            await event.send(event.plain_result("🎵 【猜歌游戏结束】\n本局无人参与"))
            del self.sessions[group_id]
            return
        
        # 生成本轮得分
        lines = ["🎵 【猜歌游戏结束】", f"共进行 {session.round_num} 轮", ""]
        
        # 排序得分
        sorted_scores = sorted(scores.items(), key=lambda x: x[1].get("score", 0), reverse=True)
        
        if sorted_scores:
            lines.append("📊 本局得分：")
            for i, (uid, data) in enumerate(sorted_scores, 1):
                score = data.get("score", 0)
                nickname = data.get("name", f"用户{uid[-4:]}")
                medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
                lines.append(f"{medal} {nickname}: {score}分")
                # 保存到历史排行榜（只保存得分者）
                if score > 0:
                    self._add_score(group_id, uid, nickname, score)
            
            # 真心话/大冒险逻辑（至少2人参与，包括0分的人）
            if len(sorted_scores) >= 2:
                winner_id, winner_data = sorted_scores[0]
                winner_name = winner_data.get("name", f"用户{winner_id[-4:]}")
                
                # 找出最低分玩家（包括0分）
                min_score = sorted_scores[-1][1].get("score", 0)
                losers = [(uid, data) for uid, data in sorted_scores if data.get("score", 0) == min_score]
                
                lines.append("")
                punishment_type = random.choice(["真心话", "大冒险"])
                
                if len(losers) == 1:
                    loser_id, loser_data = losers[0]
                    loser_name = loser_data.get("name", f"用户{loser_id[-4:]}")
                    lines.append(f"🎯 {winner_name} 获胜！")
                    lines.append(f"系统随机结果：{punishment_type}")
                    lines.append(f"请 {loser_name} 接受【{punishment_type}】挑战！")
                else:
                    loser_names = [data.get("name", f"用户{uid[-4:]}") for uid, data in losers]
                    lines.append(f"🎯 {winner_name} 获胜！")
                    lines.append(f"最低分有多人：{', '.join(loser_names)}")
                    lines.append(f"系统随机结果：{punishment_type}")
                    lines.append(f"请 {winner_name} 指定一人接受挑战！")
        else:
            lines.append("本轮无人得分")
        
        await event.send(event.plain_result("\n".join(lines)))
        
        # 清理会话
        del self.sessions[group_id]

    # ==================== 命令处理 ====================
    @filter.command("猜歌")
    async def cmd_create_game(self, event: AstrMessageEvent):
        """创建猜歌游戏"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 猜歌游戏仅支持群聊使用。")
            return
        
        # 检查是否已有游戏
        if group_id in self.sessions:
            session = self.sessions[group_id]
            if session.status == "waiting":
                count = len(session.participants)
                yield event.plain_result(
                    f"⏳ 已有游戏等待中\n"
                    f"👥 当前人数: {count}人\n\n"
                    f"💡 发送 🎶 加入游戏\n"
                    f"💡 发送「#开始猜歌」开始游戏"
                )
                return
            elif session.status == "playing":
                yield event.plain_result(
                    f"🎮 游戏进行中！\n"
                    f"第 {session.round_num}/{self.max_rounds} 轮\n"
                    f"发送 🎶 加入游戏\n"
                    f"发送「#猜歌退出」结束游戏"
                )
                return
        
        user_id, nickname = self._get_user_info(event)
        
        # 创建新游戏（等待加入阶段）
        session = GameSession(
            group_id=group_id,
            status="waiting",
            umo=event.unified_msg_origin,
            creator_id=user_id
        )
        session.participants[user_id] = {"name": nickname, "score": 0}
        
        self.sessions[group_id] = session
        
        yield event.plain_result(
            f"🎵 【猜歌游戏已创建】\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📖 游戏规则：\n"
            f"• 听歌曲片段猜歌名\n"
            f"• 抢答制，第一个猜对得分\n"
            f"• 每轮限时 {self.round_timeout} 秒\n"
            f"• 共 {self.max_rounds} 轮\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"✅ {nickname} 已加入 (1人)\n\n"
            f"💡 发送 🎶 加入游戏\n"
            f"💡 发送「#开始猜歌」开始游戏"
        )

    @filter.command("开始猜歌")
    async def cmd_start_game(self, event: AstrMessageEvent):
        """开始猜歌游戏"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 仅群聊可用")
            return
        
        # 检查游戏状态
        if group_id not in self.sessions:
            yield event.plain_result("❌ 请先发送「#猜歌」创建游戏")
            return
        
        session = self.sessions[group_id]
        
        if session.status == "playing":
            # 游戏进行中，显示当前题目
            song = session.current_song
            if song:
                hint = self._get_hint(song["name"], session.hint_level)
                yield event.plain_result(
                    f"🎵 游戏进行中！\n"
                    f"第 {session.round_num}/{self.max_rounds} 轮\n"
                    f"提示：{hint}\n"
                    f"💡 发送「#猜歌提示」获取更多提示"
                )
            return
        
        if session.status != "waiting":
            yield event.plain_result("❌ 游戏状态异常，请发送「#猜歌退出」后重试")
            return
        
        # 检查人数
        if len(session.participants) < self.min_players:
            yield event.plain_result(f"❌ 人数不足，至少需要 {self.min_players} 人才能开始")
            return
        
        user_id, nickname = self._get_user_info(event)
        
        # 验证权限（创建者或管理员）
        if session.creator_id != user_id and user_id not in self.admin_ids:
            yield event.plain_result("❌ 只有游戏创建者或管理员可以开始游戏")
            return
        
        yield event.plain_result(f"🚀 {nickname} 启动了游戏！正在获取歌曲...")
        await self._start_game(event, group_id)

    @filter.command("猜歌提示")
    async def cmd_hint(self, event: AstrMessageEvent):
        """获取提示"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 仅支持群聊使用。")
            return
        
        session = self.sessions.get(group_id)
        
        if not session or session.status != "playing":
            yield event.plain_result("❌ 当前没有进行中的猜歌游戏。发送「#猜歌」开始游戏。")
            return
        
        song = session.current_song
        session.hint_level += 1
        hint = self._get_hint(song["name"], session.hint_level)
        
        yield event.plain_result(
            f"💡 提示 #{session.hint_level}\n"
            f"歌名：{hint}\n"
            f"歌手：{song['artist']}"
        )

    @filter.command("猜歌答案")
    async def cmd_answer(self, event: AstrMessageEvent):
        """公布答案（管理员）"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 仅支持群聊使用。")
            return
        
        user_id, _ = self._get_user_info(event)
        session = self.sessions.get(group_id)
        
        # 检查权限
        if user_id not in self.admin_ids:
            yield event.plain_result("❌ 只有管理员可以提前公布答案。")
            return
        
        if not session or session.status != "playing":
            yield event.plain_result("❌ 当前没有进行中的猜歌游戏。")
            return
        
        song = session.current_song
        
        # 取消超时任务
        if session.timeout_task:
            session.timeout_task.cancel()
        
        yield event.plain_result(
            f"📢 管理员公布答案\n"
            f"答案是：《{song['name']}》- {song['artist']}\n\n"
            f"正在进入下一轮..."
        )
        
        await asyncio.sleep(2)
        await self._next_round(event, group_id)

    @filter.command("猜歌退出")
    async def cmd_end_game(self, event: AstrMessageEvent):
        """结束游戏"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 仅支持群聊使用。")
            return
        
        session = self.sessions.get(group_id)
        
        if not session or session.status not in ["waiting", "playing"]:
            yield event.plain_result("❌ 当前没有进行中的猜歌游戏。")
            return
        
        # 显示当前答案（如果有）
        if session.status == "playing" and session.current_song:
            song = session.current_song
            yield event.plain_result(f"📢 本轮答案：《{song['name']}》- {song['artist']}\n\n正在结算游戏...")
        
        await asyncio.sleep(1)
        await self._end_game(event, group_id)

    @filter.command("猜歌结束")
    async def cmd_admin_end_game(self, event: AstrMessageEvent):
        """管理员强制结束游戏"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 仅支持群聊使用。")
            return
        
        user_id, nickname = self._get_user_info(event)
        
        # 检查管理员权限
        if user_id not in self.admin_ids:
            yield event.plain_result("❌ 只有管理员可以强制结束游戏。")
            return
        
        session = self.sessions.get(group_id)
        
        if not session or session.status not in ["waiting", "playing"]:
            yield event.plain_result("❌ 当前没有进行中的猜歌游戏。")
            return
        
        # 显示当前答案（如果有）
        if session.status == "playing" and session.current_song:
            song = session.current_song
            yield event.plain_result(f"🛑 管理员 {nickname} 强制结束游戏\n📢 本轮答案：《{song['name']}》- {song['artist']}\n\n正在结算...")
        else:
            yield event.plain_result(f"🛑 管理员 {nickname} 强制结束游戏\n\n正在结算...")
        
        await asyncio.sleep(1)
        await self._end_game(event, group_id)

    @filter.command("猜歌排行")
    async def cmd_ranking(self, event: AstrMessageEvent):
        """查看排行榜"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 仅支持群聊使用。")
            return
        
        stats = self._load_stats(group_id)
        users = stats.get("users", {})
        
        if not users:
            yield event.plain_result("📊 暂无排行榜数据，快来玩猜歌游戏吧！")
            return
        
        # 按得分排序
        sorted_users = sorted(users.items(), key=lambda x: x[1].get("score", 0), reverse=True)
        
        lines = ["🏆 【猜歌排行榜】", ""]
        for i, (uid, data) in enumerate(sorted_users[:10], 1):
            nickname = data.get("nickname", f"用户{uid[-4:]}")
            score = data.get("score", 0)
            wins = data.get("wins", 0)
            medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
            lines.append(f"{medal} {nickname}: {score}分 ({wins}胜)")
        
        yield event.plain_result("\n".join(lines))


    @filter.command("猜歌帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示帮助"""
        yield event.plain_result(
            f"🎵 【猜歌游戏帮助】\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 命令列表：\n"
            f"  #猜歌 - 创建游戏\n"
            f"  🎶 - 加入游戏\n"
            f"  #开始猜歌 - 开始游戏\n"
            f"  #猜歌提示 - 获取提示\n"
            f"  #猜歌答案 - 公布答案（管理员）\n"
            f"  #猜歌结束 - 强制结束（管理员）\n"
            f"  #猜歌排行 - 查看历史排行榜\n"
            f"  #猜歌退出 - 结束游戏\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💡 玩法：\n"
            f"1. 发送 #猜歌 创建游戏\n"
            f"2. 群友发送 🎶 加入\n"
            f"3. 发送 #开始猜歌 开始\n"
            f"4. 直接发送歌名抢答\n"
            f"5. 答对自动下一轮！\n\n"
            f"🎭 游戏结束后：\n"
            f"最高分向最低分发起真心话/大冒险挑战！"
        )


    # ==================== 消息监听 ====================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息：处理🎶加入、直接猜答案"""
        text = event.message_str.strip() if event.message_str else ""
        if not text:
            return
        
        group_id = self._get_group_id(event)
        if not group_id:
            return
        
        # 检查是否有游戏
        session = self.sessions.get(group_id)
        if not session:
            return
        
        user_id, nickname = self._get_user_info(event)
        status = session.status
        
        # ========== 处理 🎶 加入游戏 ==========
        if "🎶" in text and status in {"waiting", "playing"}:
            # 检查是否已加入
            if user_id in session.participants:
                yield event.plain_result("❌ 你已经加入了")
                return
            
            # 检查人数上限
            if len(session.participants) >= self.max_players:
                yield event.plain_result(f"❌ 人数已满 ({self.max_players}人)")
                return
            
            # 加入游戏
            session.participants[user_id] = {"name": nickname, "score": 0}
            count = len(session.participants)
            
            if status == "waiting":
                yield event.plain_result(
                    f"✅ {nickname} 加入成功 ({count}人)\n"
                    f"💡 发送「#开始猜歌」开始游戏"
                )
            else:
                yield event.plain_result(f"✅ {nickname} 中途加入成功 ({count}人)")
            return
        
        # ========== 处理直接猜答案（游戏进行中）==========
        if status == "playing":
            correct_answer = session.current_song.get("name", "")
            if not correct_answer:
                return
            
            # 忽略命令
            if text.startswith("#") or text.startswith("/"):
                return
            
            # 检查答案
            if self._check_answer(text, correct_answer):
                # 检查本轮是否已被其他人答对
                if session.round_answered:
                    return  # 本轮已被答对，忽略后续答案
                
                # 必须加入游戏才可提交答案
                if user_id not in session.participants:
                    await event.send(event.plain_result("❌ 请先发送 🎶 加入游戏"))
                    return
                
                # 标记本轮已被答对（只有第一个答对的人得分）
                session.round_answered = True
                
                # 答对了！取消超时任务
                if session.timeout_task:
                    session.timeout_task.cancel()
                
                # 记录得分
                session.participants[user_id]["score"] = session.participants[user_id].get("score", 0) + 1
                total_score = session.participants[user_id]["score"]
                
                song = session.current_song
                
                await event.send(event.plain_result(
                    f"🎉 恭喜 {nickname} 答对了！\n"
                    f"答案：《{song['name']}》- {song['artist']}\n"
                    f"本轮得分：+1  总分：{total_score}\n\n"
                    f"正在进入下一轮..."
                ))
                
                # 进入下一轮
                await asyncio.sleep(2)
                await self._next_round(event, group_id)
                event.stop_event()
                return
