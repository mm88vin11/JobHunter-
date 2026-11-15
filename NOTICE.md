# Использованный открытый код

JobHunter построен поверх двух открытых проектов. Их код скопирован целиком в `engine/vendor/` без изменений,
лицензии сохранены, а улучшения сделаны снаружи, в `engine/jobhunter_engine/`, `site/` и `config/`.

| Проект | Лицензия | Коммит | Где лежит | Что используем |
|---|---|---|---|---|
| [speedyapply/JobSpy](https://github.com/speedyapply/JobSpy) | MIT © 2023 Cullen Watson | `68b88d6909a2` | `engine/vendor/jobspy_repo` | LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google Jobs, Bayt, Naukri, BDJobs, HelloWork |
| [ev-mironov/dream-job-bot-open-source](https://github.com/ev-mironov/dream-job-bot-open-source) | MIT © 2026 Dream Job Bot contributors | `d613f61c4561` | `engine/vendor/dreamjob` | клиент hh.ru API, антискам (`candidate/traps.py`), ранжирование (`candidate/ranking.py`), правила «человечности» писем |

Оба проекта под MIT: можно копировать, изменять и продавать при сохранении уведомлений об авторских правах.

**Сознательно не используем:** [s3rgeym/hh-applicant-tool](https://github.com/s3rgeym/hh-applicant-tool). Это популярный инструмент
автооткликов на hh.ru, но его лицензия (Limited Non-Commercial) запрещает распространять изменённые версии и использовать код
в коммерческих продуктах, поэтому для продаваемого продукта он не подходит.
