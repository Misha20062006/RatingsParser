"""Lightweight application constants shared by the CLI and GUI."""

BASE_URL = "https://teslacraft.org"
ACCESS_CHECK_URL = f"{BASE_URL}/donate/"
DEFAULT_FORUM_URL = f"{BASE_URL}/forums/Флудильня.30/"
DEFAULT_APPEALS_URL = f"{BASE_URL}/forums/Апелляции-на-наказания-консоли.81/"
DEFAULT_FORUM_SCAN_END_ID = 150

KNOWN_FORUM_SECTIONS = (
    ("flud", "Флудильня", DEFAULT_FORUM_URL),
    ("news", "Новости сервера", f"{BASE_URL}/forums/Новости-сервера.4/"),
    ("questions", "Ответы на вопросы", f"{BASE_URL}/forums/Ответы-на-Ваши-вопросы.33/"),
    ("appeals", "Апелляции на наказания", DEFAULT_APPEALS_URL),
    ("cheating", "Читерство", f"{BASE_URL}/forums/Читерство.26/"),
    ("griefing", "Гриферство", f"{BASE_URL}/forums/Гриферство.48/"),
    ("chat", "Нарушения в чате", f"{BASE_URL}/forums/Нарушения-в-чате.90/"),
    ("bugs", "Прочие баги", f"{BASE_URL}/forums/Прочие-баги.44/"),
    ("suggestions", "Предложения по серверу", f"{BASE_URL}/forums/Предложения-по-улучшению-сервера.43/"),
)
