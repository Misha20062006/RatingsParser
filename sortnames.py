
def create_tops():
    with open('users.txt', 'r', encoding='utf-8') as file:
        users = file.read().split('\n')
        if users[-1] == '':
            users.pop(-1)

    users_dict = {user.split()[0]: {'positive': user.split()[1], 'neutral': user.split()[2], 'negative': user.split()[3]} for user in users}
    users_positive_list = sorted([(user, users_dict[user]['positive']) for user in users_dict.keys()], key=lambda x: int(x[1]), reverse=True)
    users_neutral_list = sorted([(user, users_dict[user]['neutral']) for user in users_dict.keys()], key=lambda x: int(x[1]), reverse=True)
    users_negative_list = sorted([(user, users_dict[user]['negative']) for user in users_dict.keys()], key=lambda x: int(x[1]))

    with open('users_positive_sort.txt', 'w', encoding='utf-8') as file:
        number = 1
        for i in users_positive_list:
            file.write(f'{number}. {i[0]}: {i[1]}\n')
            number += 1
        file.close()

    with open('users_neutral_sort.txt', 'w', encoding='utf-8') as file:
        number = 1
        for i in users_neutral_list:
            file.write(f'{number}. {i[0]}: {i[1]}\n')
            number += 1
        file.close()

    with open('users_negative_sort.txt', 'w', encoding='utf-8') as file:
        number = 1
        for i in users_negative_list:
            file.write(f'{number}. {i[0]}: {i[1]}\n')
            number += 1
        file.close()
    return None

if __name__ == '__main__':
    main()