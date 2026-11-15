from . import hh, jobspy_boards, russia, social, world

# имя → функция сбора. Ключи совпадают с переключателями источников в панели.
REGISTRY = {
    "hh": hh.collect,
    "habr": russia.habr,
    "getmatch": russia.getmatch,
    "trudvsem": russia.trudvsem,
    "fl": russia.fl,
    "telegram": social.telegram,
    "vk": social.vk,
    "jobspy": jobspy_boards.collect,  # linkedin, indeed, glassdoor, zip_recruiter, google, bayt, naukri, bdjobs, hellowork
    "remoteok": world.remoteok,
    "remotive": world.remotive,
    "himalayas": world.himalayas,
    "wwr": world.wwr,
    "workingnomads": world.workingnomads,
    "jobicy": world.jobicy,
    "arbeitnow": world.arbeitnow,
    "hn": world.hackernews,
}
