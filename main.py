import os
import json
import discord
import sqlite3
import requests

from dotenv import load_dotenv
from discord.ext import commands

# .env 환경변수 로딩
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
PUBG_API_TOKEN = os.getenv("PUBG_API_TOKEN")
PLATFORM = "kakao"
PLAYER_NAME = "MNMNMNNNMN"

headers = {
    "Authorization": f"Bearer {PUBG_API_TOKEN}",
    "Accept": "application/vnd.api+json"
}

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

def init_db():
    conn = sqlite3.connect("matches.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS analyzed_matches (
            match_id TEXT PRIMARY KEY
        )
    """)
    conn.commit()
    conn.close()

# 매치 ID 저장
def save_analyzed_match(match_id):
    conn = sqlite3.connect("matches.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO analyzed_matches (match_id) VALUES (?)", (match_id,))
    conn.commit()
    conn.close()

# 이미 분석된 매치인지 확인
def is_match_already_analyzed(match_id):
    conn = sqlite3.connect("matches.db")
    c = conn.cursor()
    c.execute("SELECT 1 FROM analyzed_matches WHERE match_id = ?", (match_id,))
    result = c.fetchone()
    conn.close()
    return result is not None
    
# PUBG API 함수들
def get_player_id(player_name):
    url = f"https://api.pubg.com/shards/{PLATFORM}/players?filter[playerNames]={player_name}"
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        return None
    try:
        data = response.json()
        return data["data"][0]["id"]
    except Exception:
        return None

def get_recent_matches(player_id):
    url = f"https://api.pubg.com/shards/{PLATFORM}/players/{player_id}"
    response = requests.get(url, headers=headers)
    try:
        data = response.json()
        matches = data["data"]["relationships"]["matches"]["data"]
        return [match["id"] for match in matches]
    except KeyError:
        return []

def get_current_season_id():
    url = f"https://api.pubg.com/shards/{PLATFORM}/seasons"
    response = requests.get(url, headers=headers)
    data = response.json()
    for season in data.get("data", []):
        if season["attributes"].get("isCurrentSeason"):
            return season["id"]
    return "lifetime"

def get_player_stats(player_name, season_id):
    url = f"https://api.pubg.com/shards/{PLATFORM}/players?filter[playerNames]={player_name}"
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        return None
    try:
        account_id = response.json()["data"][0]["id"]
    except (KeyError, IndexError):
        return None
    ranked_url = f"https://api.pubg.com/shards/{PLATFORM}/players/{account_id}/seasons/{season_id}/ranked"
    response = requests.get(ranked_url, headers=headers)
    if response.status_code != 200:
        return None
    try:
        data = response.json()
        stats = data["data"]["attributes"]["rankedGameModeStats"]
        if "All" not in stats:
            return None
        all_stats = stats["All"]
        tier = all_stats["currentTier"].get("tier", "Unknown")
        sub_tier = all_stats["currentTier"].get("subTier", "")
        full_tier = f"{tier} {sub_tier}" if sub_tier else tier
        total_dmg = all_stats.get("damageDealt", 0)
        rounds_played = all_stats.get("roundsPlayed", 0)
        avg_dmg = total_dmg / rounds_played if rounds_played > 0 else 0.0
        return {"tier": full_tier, "avg_dmg": avg_dmg, "mode": "All"}
    except Exception:
        return None

def get_valid_matches_with_telemetry(player_id, max_matches=1, scan_limit=10):
    match_ids = get_recent_matches(player_id)
    valid_matches = []
    for match_id in match_ids[:scan_limit]:
        url = f"https://api.pubg.com/shards/{PLATFORM}/matches/{match_id}"
        response = requests.get(url, headers=headers)
        data = response.json()
        included = data.get("included", [])
        telemetry_items = [
            item for item in included
            if item["type"] == "asset" and item["attributes"].get("name") == "telemetry"
        ]
        if telemetry_items:
            telemetry_url = telemetry_items[0]["attributes"]["URL"]
            valid_matches.append((match_id, telemetry_url))
            if len(valid_matches) >= max_matches:
                break
    return valid_matches

async def analyze_kill_log_from_url(ctx, match_id, telemetry_url):
    telemetry_response = requests.get(telemetry_url, headers=headers)
    telemetry_data = telemetry_response.json()

    meta_url = f"https://api.pubg.com/shards/{PLATFORM}/matches/{match_id}"
    meta_response = requests.get(meta_url, headers=headers)
    match_data = meta_response.json()
    included = match_data.get("included", [])

    participant_id_by_name = {}
    team_by_participant_id = {}
    name_by_participant_id = {}
    my_team_id = None
    my_participant_id = None

    for obj in included:
        if obj["type"] == "participant":
            pid = obj["id"]
            name = obj["attributes"]["stats"].get("name")
            if name:
                participant_id_by_name[name] = pid
                name_by_participant_id[pid] = name
                if name == PLAYER_NAME:
                    my_participant_id = pid

        if obj["type"] == "roster":
            team_id = obj["attributes"]["stats"].get("teamId")
            for participant_ref in obj["relationships"]["participants"]["data"]:
                pid = participant_ref["id"]
                team_by_participant_id[pid] = team_id
                player_name = name_by_participant_id.get(pid)
                if player_name == PLAYER_NAME:
                    my_team_id = team_id

    if not my_team_id and my_participant_id:
        fallback_team = team_by_participant_id.get(my_participant_id)
        if fallback_team is not None:
            my_team_id = fallback_team

    if not my_team_id:
        await ctx.send("❌ 팀 정보를 찾을 수 없습니다.")
        return

    season_id = get_current_season_id()
    printed_players = set()

    lines = [f"📊 **매치 {match_id} - 우리 팀 사망 분석 결과**\n"]
    for event in telemetry_data:
        if event["_T"] not in ("LogPlayerKill", "LogPlayerKillV2"):
            continue

        killer = event.get("killer", {}) or {}
        victim = event.get("victim", {}) or {}
        killer_name = killer.get("name")
        victim_name = victim.get("name")

        if not killer_name or not victim_name:
            continue

        killer_id = participant_id_by_name.get(killer_name)
        victim_id = participant_id_by_name.get(victim_name)

        killer_team = team_by_participant_id.get(killer_id)
        victim_team = team_by_participant_id.get(victim_id)

        if victim_team == my_team_id and killer_team != my_team_id:
            if event["_T"] == "LogPlayerKill":
                damage_type = event.get("damageTypeCategory", "Unknown")
                damage_causer = event.get("damageCauserName", "Unknown")
                distance = event.get("distance", 0.0)
            else:
                dmg = event.get("killerDamageInfo", {})
                damage_type = dmg.get("damageTypeCategory", "Unknown")
                damage_causer = dmg.get("damageCauserName", "Unknown")
                distance = dmg.get("distance", 0.0)

            lines.append(f"- 💀 `{killer_name}` ▶ `{victim_name}` ({damage_type}, {damage_causer}, {distance:.1f}m)")

            if killer_name not in printed_players and killer_name != PLAYER_NAME:
                printed_players.add(killer_name)
                stats = get_player_stats(killer_name, season_id)
                if stats:
                    lines.append(f"   ↪ {killer_name} - 티어: **{stats['tier']}**, 평균 딜: `{stats['avg_dmg']:.1f}`")
                else:
                    lines.append(f"   ↪ {killer_name} - 통계 불러오기 실패")

    result_msg = "\n".join(lines)
    await ctx.send(result_msg if lines else "🔍 분석 결과 없음.")

# 명령어 등록
@bot.command(name="분석")
async def analyze_latest_match(ctx):
    await ctx.send("🔄 PUBG 최근 매치 분석 중...")
    player_id = get_player_id(PLAYER_NAME)
    if not player_id:
        await ctx.send("❌ 플레이어 ID를 찾을 수 없습니다.")
        return

    matches = get_valid_matches_with_telemetry(player_id, max_matches=1, scan_limit=10)
    if not matches:
        await ctx.send("❌ 텔레메트리 있는 매치를 찾지 못했습니다.")
        return

    match_id, telemetry_url = matches[0]

    if is_match_already_analyzed(match_id):
        await ctx.send("✅ 이미 분석한 최신 매치입니다.")
        return

    await analyze_kill_log_from_url(ctx, match_id, telemetry_url)
    save_analyzed_match(match_id)

# 봇 실행
if __name__ == "__main__":
    init_db()
    bot.run(DISCORD_TOKEN)
