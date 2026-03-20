"""
games.py — All interactive games for the bot.
Each game returns a starting message + sets user_data state.
"""

import random
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
#  SPIN THE WHEEL
# ─────────────────────────────────────────────────────────────────
WHEEL_ITEMS = [
    "🎵 Apna favourite song share karo",
    "😂 Koi funny memory batao",
    "🤔 Ek deep question poochho",
    "🎨 Ek baar main kuch creative karo",
    "💭 Apna biggest dream batao",
    "🌟 Kisi ki tarif karo",
    "🎯 Ek challenge accept karo",
    "📖 Apni favourite book/movie batao",
    "🤝 Koi secret share karo",
    "🌈 Apna favourite color aur kyon batao",
    "🎮 Favourite game kya hai?",
    "🍕 Aaj kya khaya — rate karo /10",
    "🌍 Kahan travel karna chahte ho?",
    "💪 Koi hidden talent batao",
    "🌙 Aaj ke din ki sabse achi baat?",
]

def spin_wheel() -> str:
    item = random.choice(WHEEL_ITEMS)
    return (
        f"🎰 *Wheel Spin!*\n\n"
        f"┌─────────────────┐\n"
        f"│  {item:<17}  │\n"
        f"└─────────────────┘\n\n"
        f"_Yeh tum dono ke liye activity hai!_ 🎯"
    )


# ─────────────────────────────────────────────────────────────────
#  TRUTH OR DARE
# ─────────────────────────────────────────────────────────────────
TRUTH_QUESTIONS = [
    "Tumhari zindagi ki sabse embarrassing moment kya thi?",
    "Kya tumne kabhi kisi se jhooth bola? Kab?",
    "Pehla crush kaisa tha?",
    "Agar ek din ke liye koi bhi ho sako toh kaun banoge?",
    "Tumhara sabse bada darpok kya hai?",
    "Kisi ke baare mein sabse bura kya socha?",
    "Agar tumhara phone hack ho jaye toh sabse zyada kya daroge?",
    "Koi aisi cheez joh tum kisi ko nahi batate?",
    "Zindagi mein sabse badi galti kya ki thi?",
    "Agar unlimited paise hote toh pehle kya karte?",
]

DARE_CHALLENGES = [
    "Apna sabse silly selfie expression yahan describe karo!",
    "10 second ke liye apni favourite actress/actor ki mimicry karo",
    "Apna favourite dialogue — movie ka — bold text mein likho",
    "Koi tongue twister 3 baar fast bolo aur galat jagah batao",
    "Apna worst singing voice mein koi line likho",
    "Koi random fact batao jo tumhe genuinely cool lagta hai",
    "Aapke haath se ek emoji draw karo words se",
    "Apne aap ko ek word mein describe karo — justify karo",
    "Apni favourite movie ka worst scene describe karo",
    "Koi ringtone ya sound effect words se banaao",
]

def get_tod_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("💬 Truth",  callback_data="game:tod:truth"),
        InlineKeyboardButton("🎯 Dare",   callback_data="game:tod:dare"),
        InlineKeyboardButton("🔀 Random", callback_data="game:tod:random"),
    ], [
        InlineKeyboardButton("❌ End Game", callback_data="game:tod:end"),
    ]])

def tod_start() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "🎮 *Truth or Dare!*\n\n"
        "Apna choice chunein:\n"
        "💬 Truth — ek honest sawaal\n"
        "🎯 Dare — ek fun challenge\n"
        "🔀 Random — surprise!\n"
    )
    return text, get_tod_keyboard()

def tod_play(choice: str) -> tuple[str, InlineKeyboardMarkup]:
    if choice == "truth" or (choice == "random" and random.random() < 0.5):
        q    = random.choice(TRUTH_QUESTIONS)
        text = f"💬 *Truth:*\n\n_{q}_"
    else:
        d    = random.choice(DARE_CHALLENGES)
        text = f"🎯 *Dare:*\n\n_{d}_"
    return text, get_tod_keyboard()


# ─────────────────────────────────────────────────────────────────
#  20 QUESTIONS  (AI-powered guessing game)
# ─────────────────────────────────────────────────────────────────
def twenty_q_start() -> str:
    return (
        "🧩 *20 Questions!*\n\n"
        "Koi cheez, insaan ya jagah socho — main guess karunga!\n\n"
        "Bas reply karo *Yes* ya *No* (ya *Maybe* 😄)\n"
        "Shuru karte hain...\n\n"
        "_Pehla sawaal:_\n"
        "Kya yeh ek living being (jeevit cheez) hai?"
    )

def twenty_q_system_prompt() -> str:
    return (
        "You are playing 20 Questions. The user is thinking of something. "
        "Ask yes/no questions one at a time to guess what they are thinking of. "
        "Keep track of answers and narrow down the possibilities. "
        "After each answer, ask one clear yes/no question. "
        "After 20 questions or when confident, make your final guess. "
        "Be playful and fun. Reply in Hinglish."
    )


# ─────────────────────────────────────────────────────────────────
#  STORY MODE  (collaborative storytelling)
# ─────────────────────────────────────────────────────────────────
STORY_STARTERS = [
    "Ek andheri raat mein, {name} ek purani haveli ke darwaze ke saamne khada/khadi tha/thi...",
    "2047 mein, jab robots ne cities sambhaal li thi, {name} ko ek secret mission mila...",
    "Samudra ke neeche ek invisible shehar mein, {name} pehli baar aankh kholta/kholti hai...",
    "Jab {name} ne apne purane daraaz mein ek magical letter paaya, sab kuch badal gaya...",
    "Ek parallel universe mein, {name} ke paas ek aisi shakti thi jo sirf raat mein kaam karti thi...",
]

def story_start(user_name: str = "Tum") -> str:
    starter = random.choice(STORY_STARTERS).format(name=user_name)
    return (
        f"📖 *Story Mode!*\n\n"
        f"Hum milke ek kahani likhenge! Main ek paragraph likhta hoon, phir tum, phir main...\n\n"
        f"*Shuruat:*\n\n"
        f"_{starter}_\n\n"
        f"_Ab tumhari baari! Agli line likho:_ ✍️"
    )

def story_system_prompt(char_name: str) -> str:
    return (
        f"You are {char_name}, co-writing a collaborative story with the user. "
        "The user writes one paragraph, then you continue the story with the next paragraph. "
        "Keep the story engaging, dramatic, and fun. Match the user's tone and genre. "
        "End each turn with an implicit cliffhanger or open thread for the user to continue. "
        "Reply in Hinglish. Keep each response to 2-3 sentences."
    )


# ─────────────────────────────────────────────────────────────────
#  DAILY HOROSCOPE
# ─────────────────────────────────────────────────────────────────
ZODIAC_SIGNS = {
    "♈ Aries":       ("Aries",       "Mar 21 – Apr 19"),
    "♉ Taurus":      ("Taurus",      "Apr 20 – May 20"),
    "♊ Gemini":      ("Gemini",      "May 21 – Jun 20"),
    "♋ Cancer":      ("Cancer",      "Jun 21 – Jul 22"),
    "♌ Leo":         ("Leo",         "Jul 23 – Aug 22"),
    "♍ Virgo":       ("Virgo",       "Aug 23 – Sep 22"),
    "♎ Libra":       ("Libra",       "Sep 23 – Oct 22"),
    "♏ Scorpio":     ("Scorpio",     "Oct 23 – Nov 21"),
    "♐ Sagittarius": ("Sagittarius", "Nov 22 – Dec 21"),
    "♑ Capricorn":   ("Capricorn",   "Dec 22 – Jan 19"),
    "♒ Aquarius":    ("Aquarius",    "Jan 20 – Feb 18"),
    "♓ Pisces":      ("Pisces",      "Feb 19 – Mar 20"),
}

def horoscope_keyboard() -> InlineKeyboardMarkup:
    signs = list(ZODIAC_SIGNS.keys())
    rows  = []
    for i in range(0, len(signs), 3):
        rows.append([
            InlineKeyboardButton(s, callback_data=f"game:horo:{ZODIAC_SIGNS[s][0]}")
            for s in signs[i:i+3]
        ])
    return InlineKeyboardMarkup(rows)

def horoscope_system_prompt(char_name: str, sign: str) -> str:
    from datetime import date
    today = date.today().strftime("%B %d, %Y")
    return (
        f"You are {char_name}, giving a fun and personal daily horoscope for {today}. "
        f"The user's zodiac sign is {sign}. "
        "Give a 3-4 line horoscope covering: love/relationships, career/study, energy/mood, and a lucky tip. "
        "Make it personal, warm, and slightly mysterious. Use emojis. Reply in Hinglish. "
        "End with a lucky number and lucky color."
    )


# ─────────────────────────────────────────────────────────────────
#  GAMES MAIN MENU KEYBOARD
# ─────────────────────────────────────────────────────────────────
def games_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Truth or Dare", callback_data="game:start:tod"),
         InlineKeyboardButton("🧩 20 Questions",  callback_data="game:start:20q")],
        [InlineKeyboardButton("📖 Story Mode",    callback_data="game:start:story"),
         InlineKeyboardButton("🔮 Horoscope",     callback_data="game:start:horo")],
        [InlineKeyboardButton("🎰 Spin the Wheel",callback_data="game:start:spin")],
        [InlineKeyboardButton("❌ Close",          callback_data="game:close")],
    ])
