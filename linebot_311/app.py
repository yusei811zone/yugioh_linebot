import os
import io
import re
import random
import math
from flask import Flask, request, abort
from dotenv import load_dotenv
from PIL import Image
from google import genai
from google.genai import types

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
    ShowLoadingAnimationRequest,
    QuickReply,
    QuickReplyItem,
    MessageAction
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, ImageMessageContent

load_dotenv()
channel_secret = os.getenv('LINE_CHANNEL_SECRET')
access_token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')

app = Flask(__name__)
configuration = Configuration(access_token=access_token)
handler = WebhookHandler(channel_secret)

client = genai.Client()
MODEL_ID = 'gemini-2.5-flash'

# ==========================================
# 暫存資料庫
# user_decks 結構: {"user_id": {"白龍": {"main": {}, "extra": {}, "side": {}}}}
# user_duels 結構: {"user_id": {"我方": 8000, "對方": 8000, "target": None}}
# user_states 結構: {"user_id": {"state": "NONE", "data": {}}} -> 用來記錄對話步驟！
# ==========================================
user_decks = {}  
user_duels = {}  
user_states = {} 

# --- 輔助函式：計算機快捷鍵 ---
def get_duel_menu():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="👈 調整我方", text="選擇調整我方")),
        QuickReplyItem(action=MessageAction(label="👉 調整對方", text="選擇調整對方")),
        QuickReplyItem(action=MessageAction(label="🎲 擲骰子", text="擲骰子 1")),
        QuickReplyItem(action=MessageAction(label="🪙 擲硬幣", text="擲硬幣 1")),
        QuickReplyItem(action=MessageAction(label="⚙️ 結算/重置", text="決鬥結算選單"))
    ])

def reset_state(user_id):
    user_states[user_id] = {"state": "NONE", "data": {}}

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    user_id = event.source.user_id
    user_message = event.message.text.strip()
    
    # 初始化使用者資料庫
    if user_id not in user_decks: user_decks[user_id] = {}
    if user_id not in user_states: reset_state(user_id)

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        try: line_bot_api.show_loading_animation(ShowLoadingAnimationRequest(chat_id=user_id, loading_seconds=5))
        except: pass

        reply_messages = []
        current_state = user_states[user_id]["state"]

        # ==========================================
        # 全域中斷指令 (如果使用者中途點擊其他選單，重置狀態)
        # ==========================================
        if user_message in ["決鬥計算機", "開啟計算機", "我的牌組", "功能選單", "隨機工具", "取消"]:
            reset_state(user_id)
            current_state = "NONE"

        # ==========================================
        # 狀態機 (State Machine) - 處理多步驟對話
        # ==========================================
        if current_state != "NONE":
            state_data = user_states[user_id]["data"]

            # 1. 建立牌組 - 等待輸入名稱
            if current_state == "WAIT_CREATE_DECK":
                deck_name = user_message
                if deck_name in user_decks[user_id]:
                    reply_messages.append(TextMessage(text=f"❌ 牌組【{deck_name}】已經存在囉！請換個名字，或點擊「取消」。", quick_reply=QuickReply(items=[QuickReplyItem(action=MessageAction(label="❌ 取消", text="取消"))])))
                else:
                    user_decks[user_id][deck_name] = {"main": {}, "extra": {}, "side": {}}
                    reset_state(user_id)
                    reply_messages.append(TextMessage(text=f"✅ 成功建立牌組：【{deck_name}】！\n請點擊「我的牌組」進入編輯。"))

            # 2. 編輯牌組 - 等待輸入要編輯的目標牌組
            elif current_state == "WAIT_EDIT_TARGET":
                deck_name = user_message
                if deck_name not in user_decks[user_id]:
                    reply_messages.append(TextMessage(text=f"❌ 找不到牌組【{deck_name}】！請確認名稱是否正確，或點擊「取消」。", quick_reply=QuickReply(items=[QuickReplyItem(action=MessageAction(label="❌ 取消", text="取消"))])))
                else:
                    reset_state(user_id)
                    reply_messages.append(TextMessage(
                        text=f"🎯 已鎖定牌組【{deck_name}】！\n請選擇你要進行的操作：",
                        quick_reply=QuickReply(items=[
                            QuickReplyItem(action=MessageAction(label="➕ 新增主牌", text=f"準備新增主牌 {deck_name}")),
                            QuickReplyItem(action=MessageAction(label="➕ 新增額外", text=f"準備新增額外 {deck_name}")),
                            QuickReplyItem(action=MessageAction(label="➕ 新增備牌", text=f"準備新增備牌 {deck_name}")),
                            QuickReplyItem(action=MessageAction(label="🗑️ 刪除卡片", text=f"準備刪除卡片 {deck_name}")),
                            QuickReplyItem(action=MessageAction(label="🔍 查看此牌組", text=f"查看特定牌組 {deck_name}"))
                        ])
                    ))

            # 3. 刪除牌組 - 等待輸入要刪除的目標
            elif current_state == "WAIT_DELETE_TARGET":
                deck_name = user_message
                if deck_name not in user_decks[user_id]:
                    reply_messages.append(TextMessage(text=f"❌ 找不到牌組【{deck_name}】！請確認名稱，或點擊「取消」。", quick_reply=QuickReply(items=[QuickReplyItem(action=MessageAction(label="❌ 取消", text="取消"))])))
                else:
                    user_states[user_id] = {"state": "WAIT_DELETE_CONFIRM", "data": {"deck_name": deck_name}}
                    reply_messages.append(TextMessage(
                        text=f"⚠️ 警告：確定要永久刪除牌組【{deck_name}】嗎？\n此動作無法復原！",
                        quick_reply=QuickReply(items=[
                            QuickReplyItem(action=MessageAction(label="✅ 確定刪除", text="確認刪除牌組")),
                            QuickReplyItem(action=MessageAction(label="❌ 取消", text="取消"))
                        ])
                    ))

            # 4. 刪除牌組 - 二次確認
            elif current_state == "WAIT_DELETE_CONFIRM":
                if user_message == "確認刪除牌組":
                    deck_name = state_data["deck_name"]
                    del user_decks[user_id][deck_name]
                    reset_state(user_id)
                    reply_messages.append(TextMessage(text=f"🗑️ 已成功刪除牌組【{deck_name}】！"))
                else:
                    reset_state(user_id)
                    reply_messages.append(TextMessage(text="已取消刪除操作。"))

            # 5. 新增/刪除單卡 - 處理輸入的卡名與數量
            elif current_state in ["WAIT_ADD_CARD", "WAIT_REMOVE_CARD"]:
                deck_name = state_data["deck_name"]
                action_type = state_data["type"] # main, extra, side, or remove
                
                deck_data = user_decks[user_id][deck_name]
                items = {}
                
                # 解析輸入字串 (例: 灰流麗*3 融合)
                for item in user_message.split():
                    if '*' in item:
                        parts = item.rsplit('*', 1)
                        try: items[parts[0]] = int(parts[1])
                        except: items[item] = 1
                    else: items[item] = 1

                log, error_log = [], []
                
                if current_state == "WAIT_ADD_CARD":
                    dt_tw = "主牌組" if action_type == "main" else ("額外牌組" if action_type == "extra" else "備牌")
                    limits = {"main": 60, "extra": 15, "side": 15}
                    
                    for c_name, c_cnt in items.items():
                        current_type_total = sum(deck_data[action_type].values())
                        current_card_total = sum(deck_data[d].get(c_name, 0) for d in ["main", "extra", "side"])

                        if current_type_total + c_cnt > limits[action_type]:
                            error_log.append(f"❌ {c_name}：{dt_tw}已達上限 ({limits[action_type]}張)")
                            continue
                        if current_card_total + c_cnt > 3:
                            error_log.append(f"❌ {c_name}：同名卡最多3張 (現有{current_card_total}張)")
                            continue

                        deck_data[action_type][c_name] = deck_data[action_type].get(c_name, 0) + c_cnt
                        log.append(f"✅ {c_name} * {c_cnt}")
                        
                else: # WAIT_REMOVE_CARD
                    for c_name, c_cnt in items.items():
                        remaining = c_cnt
                        for dt in ["main", "extra", "side"]:
                            if remaining <= 0: break
                            if c_name in deck_data[dt]:
                                del_amt = min(deck_data[dt][c_name], remaining)
                                deck_data[dt][c_name] -= del_amt
                                remaining -= del_amt
                                if deck_data[dt][c_name] == 0: del deck_data[dt][c_name]
                        actual_del = c_cnt - remaining
                        if actual_del > 0: log.append(f"🗑️ 移除 {c_name} * {actual_del}")
                        else: log.append(f"⚠️ 牌組中找不到 {c_name}")

                reset_state(user_id)
                res_text = f"🗂️ 【{deck_name}】更新結果：\n"
                if log: res_text += "\n".join(log) + "\n"
                if error_log: res_text += "\n".join(error_log)
                
                # 附上返回該牌組選單的按鈕
                reply_messages.append(TextMessage(
                    text=res_text.strip(),
                    quick_reply=QuickReply(items=[
                        QuickReplyItem(action=MessageAction(label="🔙 繼續編輯此牌組", text=f"繼續編輯 {deck_name}")),
                        QuickReplyItem(action=MessageAction(label="🔍 查看此牌組", text=f"查看特定牌組 {deck_name}"))
                    ])
                ))

        # ==========================================
        # 一般指令路由 (沒有在對話狀態中時)
        # ==========================================
        if current_state == "NONE" and not reply_messages:
            
            # --- 取消指令 ---
            if user_message == "取消":
                reply_messages.append(TextMessage(text="✅ 已取消目前操作。"))

            # --- 計算機系統 (修復關鍵點！) ---
            elif user_message in ["開啟計算機", "決鬥計算機"]:
                if user_id not in user_duels:
                    user_duels[user_id] = {"我方": 8000, "對方": 8000, "target": None}
                    text = "⚔️ 決鬥開始！ ⚔️\n➖➖➖➖➖➖\n我方 LP: 8000\n對方 LP: 8000\n\n👇 請選擇要調整哪一方的血量："
                else:
                    p1, p2 = user_duels[user_id]["我方"], user_duels[user_id]["對方"]
                    text = f"⚔️ 計算機運作中\n➖➖➖➖➖➖\n我方 LP: {p1}\n對方 LP: {p2}\n\n👇 請選擇要調整哪一方的血量："
                reply_messages.append(TextMessage(text=text, quick_reply=get_duel_menu()))

            elif user_message in ["選擇調整我方", "選擇調整對方"]:
                if user_id not in user_duels: user_duels[user_id] = {"我方": 8000, "對方": 8000, "target": None}
                target = "我方" if "我方" in user_message else "對方"
                user_duels[user_id]["target"] = target
                
                reply_messages.append(TextMessage(
                    text=f"🎯 已鎖定【{target}】\n請輸入數字 (例: -1000)\n或點擊常用數值：",
                    quick_reply=QuickReply(items=[
                        QuickReplyItem(action=MessageAction(label="-1000", text="-1000")),
                        QuickReplyItem(action=MessageAction(label="-500", text="-500")),
                        QuickReplyItem(action=MessageAction(label="+1000", text="+1000")),
                        QuickReplyItem(action=MessageAction(label="÷2 (減半)", text="生命值減半")),
                        QuickReplyItem(action=MessageAction(label="↩️ 取消", text="開啟計算機"))
                    ])
                ))

            elif user_message == "生命值減半":
                if user_id in user_duels and user_duels[user_id].get("target"):
                    target = user_duels[user_id]["target"]
                    user_duels[user_id][target] = math.ceil(user_duels[user_id][target] / 2)
                    p1, p2 = user_duels[user_id]["我方"], user_duels[user_id]["對方"]
                    reply_messages.append(TextMessage(text=f"🩸 【血量更新】\n我方 LP: {p1}\n對方 LP: {p2}", quick_reply=get_duel_menu()))
                else:
                    reply_messages.append(TextMessage(text="❌ 請先點擊「👈 調整我方」或「👉 調整對方」！", quick_reply=get_duel_menu()))

            elif match := re.match(r'^([+-])\s*(\d+)$', user_message):
                if user_id in user_duels and user_duels[user_id].get("target"):
                    target = user_duels[user_id]["target"]
                    operator, amount = match.group(1), int(match.group(2))
                    if operator == '+': user_duels[user_id][target] += amount
                    else: user_duels[user_id][target] -= amount
                    
                    p1, p2 = user_duels[user_id]["我方"], user_duels[user_id]["對方"]
                    text = f"🩸 【血量更新】 ({target} {operator}{amount})\n我方 LP: {p1}\n對方 LP: {p2}"
                    
                    if p1 <= 0 or p2 <= 0:
                        text += "\n➖➖➖➖➖➖\n🏆 決鬥結束 🏆\n"
                        if p1 <= 0 and p2 <= 0: text += "雙方血量歸零，平局 (DRAW)！"
                        elif p1 <= 0: text += "我方血量歸零，對方獲勝！"
                        else: text += "對方血量歸零，我方獲勝！"
                        del user_duels[user_id]
                        reply_messages.append(TextMessage(text=text))
                    else:
                        reply_messages.append(TextMessage(text=text, quick_reply=get_duel_menu()))
                else:
                    reply_messages.append(TextMessage(text="❌ 請先選擇目標！", quick_reply=get_duel_menu()))

            elif user_message == "決鬥結算選單":
                reply_messages.append(TextMessage(
                    text="⚙️ 請選擇結算方式或重新開始：",
                    quick_reply=QuickReply(items=[
                        QuickReplyItem(action=MessageAction(label="🏳️ 我方投降", text="我方投降")),
                        QuickReplyItem(action=MessageAction(label="🏳️ 對方投降", text="對方投降")),
                        QuickReplyItem(action=MessageAction(label="✨ 我方特殊勝利", text="我方特殊勝利")),
                        QuickReplyItem(action=MessageAction(label="🔄 重新決鬥 (重置)", text="決鬥開始"))
                    ])
                ))
                
            elif user_message in ["決鬥開始", "重新決鬥"]:
                user_duels[user_id] = {"我方": 8000, "對方": 8000, "target": None}
                reply_messages.append(TextMessage(text="⚔️ 決鬥開始！ ⚔️\n➖➖➖➖➖➖\n我方 LP: 8000\n對方 LP: 8000", quick_reply=get_duel_menu()))

            elif user_message in ["我方投降", "對方投降", "我方特殊勝利", "對方特殊勝利"]:
                if user_id in user_duels:
                    if "投降" in user_message:
                        loser = "我方" if "我方" in user_message else "對方"
                        winner = "對方" if loser == "我方" else "我方"
                        reply_messages.append(TextMessage(text=f"🏳️ {loser} 選擇了投降，本局由 {winner} 獲勝！"))
                    else:
                        winner = "我方" if "我方" in user_message else "對方"
                        reply_messages.append(TextMessage(text=f"✨ 達成特殊勝利條件！\n🏆 恭喜 {winner} 贏得本局決鬥！"))
                    del user_duels[user_id]
                else: reply_messages.append(TextMessage(text="❌ 決鬥尚未開始！"))

            # --- 隨機工具 ---
            elif match := re.match(r'^擲骰子\s*(\d+)?', user_message):
                times = min(int(match.group(1)) if match.group(1) else 1, 20)
                results = [random.randint(1, 6) for _ in range(times)]
                text = f"🎲 擲骰子 {times} 次的結果：\n" + "\n".join([f"第 {i+1} 次：【 {res} 】" for i, res in enumerate(results)]) + f"\n\n✨ 總和：{sum(results)}"
                reply_messages.append(TextMessage(text=text, quick_reply=get_duel_menu() if user_id in user_duels else None))

            elif match := re.match(r'^擲硬幣\s*(\d+)?', user_message):
                times = min(int(match.group(1)) if match.group(1) else 1, 20)
                results = [random.choice(["正面 🌕", "反面 🌑"]) for _ in range(times)]
                text = f"🪙 擲硬幣 {times} 次的結果：\n" + "\n".join([f"第 {i+1} 次：{res}" for i, res in enumerate(results)])
                reply_messages.append(TextMessage(text=text, quick_reply=get_duel_menu() if user_id in user_duels else None))

            elif user_message == "隨機工具":
                reply_messages.append(TextMessage(
                    text="🎲 請選擇隨機工具，或自行輸入(例: 擲骰子 5)：",
                    quick_reply=QuickReply(items=[
                        QuickReplyItem(action=MessageAction(label="🎲 擲骰子 1次", text="擲骰子 1")),
                        QuickReplyItem(action=MessageAction(label="🪙 擲硬幣 1次", text="擲硬幣 1")),
                        QuickReplyItem(action=MessageAction(label="🎲 擲骰子 3次", text="擲骰子 3"))
                    ])
                ))

            # --- 全新牌組管理主選單 ---
            elif user_message == "我的牌組":
                reply_messages.append(TextMessage(
                    text="🗂️ 【牌組管理系統】\n請點擊下方快捷鍵操作：",
                    quick_reply=QuickReply(items=[
                        QuickReplyItem(action=MessageAction(label="➕ 建立牌組", text="流程_建立牌組")),
                        QuickReplyItem(action=MessageAction(label="📝 編輯牌組", text="流程_編輯牌組")),
                        QuickReplyItem(action=MessageAction(label="🔍 查看牌組清單", text="流程_查看牌組")),
                        QuickReplyItem(action=MessageAction(label="🗑️ 刪除牌組", text="流程_刪除牌組"))
                    ])
                ))

            # --- 牌組流程觸發 ---
            elif user_message == "流程_建立牌組":
                user_states[user_id] = {"state": "WAIT_CREATE_DECK", "data": {}}
                reply_messages.append(TextMessage(text="📝 請直接輸入你要建立的「牌組名稱」\n(例如：白龍、閃刀姬)：", quick_reply=QuickReply(items=[QuickReplyItem(action=MessageAction(label="❌ 取消", text="取消"))])))

            elif user_message == "流程_編輯牌組":
                if not user_decks[user_id]:
                    reply_messages.append(TextMessage(text="🗂️ 你目前還沒有建立任何牌組喔！\n請先點擊「我的牌組」>「建立牌組」。"))
                else:
                    decks_str = "\n".join([f"▪️ {d}" for d in user_decks[user_id].keys()])
                    user_states[user_id] = {"state": "WAIT_EDIT_TARGET", "data": {}}
                    reply_messages.append(TextMessage(text=f"🗂️ 你的牌組列表：\n{decks_str}\n\n📝 請直接輸入你要編輯的「牌組名稱」：", quick_reply=QuickReply(items=[QuickReplyItem(action=MessageAction(label="❌ 取消", text="取消"))])))

            elif user_message == "流程_查看牌組":
                if not user_decks[user_id]:
                    reply_messages.append(TextMessage(text="🗂️ 目前沒有牌組！"))
                else:
                    decks_str = "\n".join([f"▪️ {d}" for d in user_decks[user_id].keys()])
                    reply_messages.append(TextMessage(text=f"🗂️ 你的牌組總覽：\n{decks_str}\n\n💡 若要查看詳細卡表，請點擊「我的牌組」>「編輯牌組」進入操作！"))

            elif user_message == "流程_刪除牌組":
                if not user_decks[user_id]:
                    reply_messages.append(TextMessage(text="🗂️ 目前沒有任何牌組可以刪除！"))
                else:
                    decks_str = "\n".join([f"▪️ {d}" for d in user_decks[user_id].keys()])
                    user_states[user_id] = {"state": "WAIT_DELETE_TARGET", "data": {}}
                    reply_messages.append(TextMessage(text=f"🗂️ 你的牌組列表：\n{decks_str}\n\n⚠️ 請直接輸入你要【刪除】的牌組名稱：", quick_reply=QuickReply(items=[QuickReplyItem(action=MessageAction(label="❌ 取消", text="取消"))])))

            # --- 編輯牌組的快捷指令處理 (由 QuickReply 觸發) ---
            elif match := re.match(r'^準備(新增主牌|新增額外|新增備牌|刪除卡片) (.+)$', user_message):
                action_map = {"新增主牌": "main", "新增額外": "extra", "新增備牌": "side", "刪除卡片": "remove"}
                action_str, deck_name = match.group(1), match.group(2)
                
                user_states[user_id] = {"state": "WAIT_ADD_CARD" if "新增" in action_str else "WAIT_REMOVE_CARD", 
                                        "data": {"type": action_map[action_str], "deck_name": deck_name}}
                
                reply_messages.append(TextMessage(
                    text=f"📝 準備【{action_str}】至牌組：{deck_name}\n\n請直接輸入卡名與數量 (不同卡片請用空格隔開)。\n範例：『青眼白龍*3 融合*1』",
                    quick_reply=QuickReply(items=[QuickReplyItem(action=MessageAction(label="❌ 取消", text="取消"))])
                ))

            elif match := re.match(r'^(繼續編輯|查看特定牌組) (.+)$', user_message):
                cmd_type, deck_name = match.group(1), match.group(2)
                if deck_name not in user_decks[user_id]:
                    reply_messages.append(TextMessage(text=f"❌ 找不到牌組【{deck_name}】"))
                else:
                    if cmd_type == "繼續編輯":
                        reply_messages.append(TextMessage(
                            text=f"🎯 操作牌組：【{deck_name}】",
                            quick_reply=QuickReply(items=[
                                QuickReplyItem(action=MessageAction(label="➕ 新增主牌", text=f"準備新增主牌 {deck_name}")),
                                QuickReplyItem(action=MessageAction(label="➕ 新增額外", text=f"準備新增額外 {deck_name}")),
                                QuickReplyItem(action=MessageAction(label="➕ 新增備牌", text=f"準備新增備牌 {deck_name}")),
                                QuickReplyItem(action=MessageAction(label="🗑️ 刪除卡片", text=f"準備刪除卡片 {deck_name}")),
                                QuickReplyItem(action=MessageAction(label="🔍 查看此牌組", text=f"查看特定牌組 {deck_name}"))
                            ])
                        ))
                    else: # 查看特定牌組
                        deck_data = user_decks[user_id][deck_name]
                        text = f"🗂️ 【{deck_name}】完整卡表\n➖➖➖➖➖➖\n"
                        for dt, dt_tw in [("main", "主牌組"), ("extra", "額外牌組"), ("side", "備牌")]:
                            total = sum(deck_data[dt].values())
                            text += f"🔹 {dt_tw} ({total}張)：\n"
                            if total == 0: text += "(空)\n"
                            for c_name, c_cnt in deck_data[dt].items(): text += f" - {c_name} * {c_cnt}\n"
                            text += "\n"
                        reply_messages.append(TextMessage(text=text.strip(), quick_reply=QuickReply(items=[QuickReplyItem(action=MessageAction(label="🔙 回到編輯", text=f"繼續編輯 {deck_name}"))])))

            # --- Gemini Fallback ---
            else:
                try:
                    line_bot_api.show_loading_animation(ShowLoadingAnimationRequest(chat_id=user_id, loading_seconds=15))
                    response = client.models.generate_content(
                        model=MODEL_ID, contents=user_message,
                        config=types.GenerateContentConfig(tools=[{"google_search": {}}], system_instruction="你是一位專精「遊戲王 OCG 賽制」的裁判。2026年，嚴格根據最新環境回答。")
                    )
                    reply_messages.append(TextMessage(text=response.text))
                except Exception as e:
                    reply_messages.append(TextMessage(text=f"抱歉，系統思考時發生錯誤：{str(e)}"))

        # 統一送出
        if reply_messages:
            line_bot_api.reply_message_with_http_info(ReplyMessageRequest(reply_token=event.reply_token, messages=reply_messages))

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    message_id, user_id = event.message.id, event.source.user_id
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        try: line_bot_api.show_loading_animation(ShowLoadingAnimationRequest(chat_id=user_id, loading_seconds=30))
        except: pass

        blob_api = MessagingApiBlob(api_client)
        img = Image.open(io.BytesIO(blob_api.get_message_content(message_id)))
        
        prompt = "請提供：1.【名稱】2.【效果】3.【系列】4.【推薦組法】5.【禁卡表】。結尾加上：『💡 點擊「我的牌組」即可將卡片加入你的牌組中喔！』"
        try:
            response = client.models.generate_content(model=MODEL_ID, contents=[prompt, img], config=types.GenerateContentConfig(tools=[{"google_search": {}}], system_instruction="你是一位專精「遊戲王 OCG 賽制」的裁判。現在是2026年。"))
            reply_text = response.text
        except Exception as e:
            reply_text = f"辨識失敗，錯誤：{str(e)}"
        line_bot_api.reply_message_with_http_info(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_text)]))

if __name__ == "__main__":
    print(" 遊戲王啟動中...")
    app.run(port=5000, debug=True)