"""Every string the person reads. Nothing user-facing lives anywhere else."""

from seawork.domain.profile import CareerStage

WELCOME = (
    "Здравствуйте! Я подбираю вакансии в морской индустрии под ваш опыт.\n\n"
    "Я храню ваши ответы, реакции на вакансии и то, что вам уже показывал. "
    "/delete удаляет всё это, /export присылает файлом."
)

STAGE_QUESTION = "Вы уже работаете в морской отрасли или только присматриваетесь?"

STAGE_BUTTONS: dict[CareerStage, str] = {
    CareerStage.WORKING: "Уже работаю",
    CareerStage.ENTRY: "Хочу попасть в отрасль",
    CareerStage.UNDECIDED: "Пока не определился",
}

STAGE_NAMES: dict[CareerStage, str] = {
    **STAGE_BUTTONS,
    CareerStage.EXPERIENCED: "Опытный специалист",
}

DIRECTIONS_QUESTION = "Что вам интереснее? Отметьте одно или несколько направлений."
COUNTRY_QUESTION = (
    "В какой стране вас могли бы оформить? Спрашиваю, чтобы не показывать вакансии, "
    "на которые вы не сможете откликнуться."
)

DONE = "Готово"
SKIP = "Пропустить"
SKIP_COUNTRY = "Другая — пропустить"
PICK_SOMETHING = "Отметьте хотя бы один вариант или пропустите."
SAVED_PROFILE = "Записал."
PICKED_STAGE = "Ваш этап: {stage}."

OPEN_VACANCY = "Открыть вакансию"
INTERESTING = "👍 Интересно"
NOT_FOR_ME = "👎 Не то"
SIMILAR = "Похожие ({count})"
SAVED = "✓ Сохранено"
DISMISSED = "Больше не покажем"
TAKE_BACK = "Вернуть"
REACTION_SAVED = "Сохранено"
REACTION_DISMISSED = "Больше не покажем"
REACTION_REOPENED = "Вернул"

EMPTY_FEED = (
    "Сейчас новых подходящих вакансий нет. Подборка приходит каждое утро, не больше пяти в день."
)
SIMILAR_EMPTY = "Похожих вакансий сейчас нет."
SIMILAR_HEADER = "Похожие вакансии:"
SIMILAR_MORE = "…и ещё {count}"

GONE = "Данных больше нет. Начните заново: /start"
PRESS_START = "Нажмите /start, чтобы начать."
STALE_BUTTON = "Кнопка устарела."

HELP = (
    "/feed — подборка сейчас\n"
    "/profile — что я о вас знаю\n"
    "/pause — не присылать подборку по утрам\n"
    "/resume — присылать снова\n"
    "/export — выгрузить ваши данные файлом\n"
    "/delete — удалить все ваши данные"
)

PAUSED = "Хорошо, утренняя подборка приостановлена. /feed работает, /resume вернёт рассылку."
RESUMED = "Утренняя подборка снова включена."

DELETE_CONFIRM = (
    "Удалить профиль, реакции, историю подборок и привязку к Telegram? Это нельзя отменить."
)
DELETE_YES = "Да, удалить"
DELETE_NO = "Нет"
DELETED = "Всё удалено. Если захотите вернуться: /start"
DELETE_CANCELLED = "Ничего не удалял."

EXPORT_FILENAME = "seawork-export.json"

PROFILE_HEADER = "Что я о вас знаю:"
PROFILE_STAGE = "Этап: {value}"
PROFILE_DIRECTIONS = "Направления: {value}"
PROFILE_COUNTRIES = "Страна оформления: {value}"
PROFILE_UNKNOWN = "не знаю"
PROFILE_CHANGE_STAGE = "Изменить этап"
PROFILE_CHANGE_DIRECTIONS = "Направления"
PROFILE_CHANGE_COUNTRY = "Страна оформления"

DIRECTION_LABEL = "Направление: {value}"
SALARY_LABEL = "Зарплата: {value}"
CHECK = "✓ "
MATCH_MARK = "✓"
MISMATCH_MARK = "⚠"
BULLET = "•"
ELLIPSIS = "…"
MORE_SIMILAR = "ещё {count} похожих"

REASON_MATCH: dict[str, str] = {
    "profession": "Подходит профессия",
    "direction": "Подходит направление",
    "country": "Подходит страна",
    "hiring_country": "Вас могут оформить",
    "languages": "Языки совпадают",
    "certificates": "Нужные сертификаты у вас есть",
    "experience": "Подходит уровень опыта",
    "career_goal": "Совпадает с вашей целью",
}

REASON_MISMATCH: dict[str, str] = {
    "profession": "Другая профессия",
    "direction": "Другое направление",
    "country": "Не из ваших стран",
    "hiring_country": "Могут не оформить из вашей страны",
    "languages": "Нужен язык, которого у вас нет",
    "certificates": "Нужны сертификаты, которых у вас нет",
    "experience": "Другой уровень опыта",
    "career_goal": "Не совпадает с вашей целью",
}

CAVEATS: dict[str, str] = {
    "training_offer_signal_unavailable": "Обучение от работодателя я пока определить не могу.",
    "hiring_office_not_normalised": "Страну офиса найма определил не до конца.",
}

COMMAND_FEED = "Подборка сейчас"
COMMAND_PROFILE = "Что я о вас знаю"
COMMAND_PAUSE = "Приостановить утреннюю подборку"
COMMAND_RESUME = "Включить утреннюю подборку"
COMMAND_EXPORT = "Выгрузить ваши данные"
COMMAND_DELETE = "Удалить все ваши данные"
COMMAND_HELP = "Список команд"
