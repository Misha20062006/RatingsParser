import os
import asyncio
import time
from patchright.async_api import async_playwright
from sortnames import create_tops
from datetime import datetime
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.ERROR,
    filename='last_log.log',
    filemode="w",
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding='utf-8'
)


with open('users.txt', 'w', encoding='utf-8'):
    pass

program_start = time.time()
print(
    'Программа пройдётся по айди пользователей от старых к новым (от 1 до Вашего числа) и соберёт статистику по рейтингам в файлы .txt')
print('Введите количество пользователей форума для сбора статистики. Например: 100 (число должно быть больше 0)')
while True:
    USERS_COUNT = (input('Количество пользователей: '))
    if USERS_COUNT.isdigit():
        USERS_COUNT = int(USERS_COUNT)
        if USERS_COUNT <= 0:
            print('Число должно быть больше 0')
        else:
            break
    else:
        print('Надо ввести положительное число')

print('Укажите количество страниц, которое будет задействовано одновременно при сборе статистики')
print('Не рекомендуется указывать больше 20')
while True:
    WINDOWS_IN_BROWSER = (input('Количество страниц для одновременной проверки: '))
    if WINDOWS_IN_BROWSER.isdigit():
        WINDOWS_IN_BROWSER = int(WINDOWS_IN_BROWSER)
        if WINDOWS_IN_BROWSER <= 0:
            print('Число должно быть больше 0')
        else:
            break
    else:
        print('Надо ввести положительное число')

TRIES_FOR_CHECK = 100

FIRST_ID = 1


async def start_browser(p):
    while True:
        datadir = 'user-data'
        context = await p.chromium.launch_persistent_context(
            user_data_dir=datadir,
            headless=False,
            channel="chrome"
        )
        test_page = await context.new_page()
        await test_page.goto('https://teslacraft.org/donate/')
        try:
            await test_page.wait_for_selector('.rekt_titleContainer', timeout=15000)
            await test_page.close()
            break
        except Exception as e:
            await test_page.close()
            await context.close()
            logging.exception(f'Не удалось открыть стартовую страницу. Скорее всего, не удалось пройти капчу. Перезапуск браузера. {e}')
            await asyncio.sleep(2)
    return context


async def create_pages(context):
    page = await context.new_page()
    await page.route("**/*", lambda route, request: asyncio.create_task(
        route.continue_() if request.resource_type == "document" else route.abort()
    ))
    return page


async def lookup_pages(page, profile_link):
    try:
        await page.goto(profile_link, wait_until='domcontentloaded', timeout=1000000)
    except Exception as e:
        logging.exception(f'Ошибка при переходе на страницу: {e}')

    username_element = await page.query_selector('.username')
    positive_rating_element = await page.query_selector(
        'div.userInfo > dl.userStats.pairsInline > dd:nth-child(8) > span:nth-child(1)')
    rating_dd = await page.query_selector('div.userInfo > dl.userStats.pairsInline > dd:nth-child(8)')
    negative_rating_element = await page.query_selector(
        'div.userInfo > dl.userStats.pairsInline > dd:nth-child(8) > span:nth-child(2)')
    try:
        username = await username_element.inner_text()
        positive_rating = await positive_rating_element.inner_text()
        rating_text = await rating_dd.inner_text()
        neutral_rating = rating_text.split('/')[1].strip()
        negative_rating = await negative_rating_element.inner_text()
    except Exception as e:
        logging.exception(f'Ошибка, ник не был найден: {e}')
        return None
    return username, positive_rating, neutral_rating, negative_rating


def write_to_file_and_print(users_info):
    with open('users.txt', 'a', encoding='utf-8') as file:
        for user in users_info:
            if user is None:
                pass
            else:
                name = user[0]
                positive_rating, neutral_rating, negative_rating = int(user[1].replace(' ', '').replace('+', '')), int(
                    user[2].replace(' ', '')), int(user[3].replace(' ', ''))

                print(name, positive_rating, neutral_rating, negative_rating)
                file.write(f'{name} {positive_rating} {neutral_rating} {negative_rating}\n')


async def main():
    number_of_attempts = TRIES_FOR_CHECK
    pages_list = []
    async with async_playwright() as p:
        context = await start_browser(p)
        for batch in range(FIRST_ID, USERS_COUNT + 1, WINDOWS_IN_BROWSER):
            time_start = time.time()
            running_batch = min(WINDOWS_IN_BROWSER, USERS_COUNT - batch + 1)
            if number_of_attempts == TRIES_FOR_CHECK:
                if pages_list:
                    for page in pages_list:
                        await page.close()
                pages_list = await asyncio.gather(*[create_pages(context=context) for _ in range(running_batch)])
                number_of_attempts = 1
            else:
                number_of_attempts += 1
            profile_links = [f'https://teslacraft.org/members/.{batch + step}/card' for step in range(running_batch)]
            tasks = [lookup_pages(page=pages_list[profile_link_number], profile_link=profile_links[profile_link_number]) for profile_link_number in range(len(profile_links))]
            users_info = await asyncio.gather(*tasks)
            write_to_file_and_print(users_info)
            print(f'Время проверки {WINDOWS_IN_BROWSER} страниц: {time.time() - time_start}')
    create_tops()
    print(f'Программа закончила работу')
    print(f'Все возникшие ошибки, если они были, указаны в файле "last_log.log" ')
    print(f'Время выполнения всей программы: {time.time() - program_start}')


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as ex:
        logging.exception(f'Ошибка во время выполнения: {ex}')
    input("\nНажмите Enter, чтобы закрыть...")
