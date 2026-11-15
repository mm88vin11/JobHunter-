-- Вакансии из всех источников
CREATE TABLE IF NOT EXISTS vacancies (
  id TEXT PRIMARY KEY,               -- source:externalId
  source TEXT NOT NULL,              -- hh, habr, telegram, vk, remoteok, ...
  external_id TEXT NOT NULL,
  url TEXT NOT NULL,
  title TEXT NOT NULL,
  company TEXT,
  location TEXT,
  remote INTEGER DEFAULT 0,
  salary_from INTEGER,
  salary_to INTEGER,
  currency TEXT,
  salary_usd INTEGER,                -- нормализованная месячная зарплата в USD (для сравнения рынков)
  lang TEXT DEFAULT 'ru',            -- язык вакансии: ru | en
  description TEXT,
  contacts TEXT,                     -- JSON: {emails:[], telegram:[], links:[]}
  published_at TEXT,
  found_at TEXT NOT NULL DEFAULT (datetime('now')),
  fingerprint TEXT,                  -- для склейки дублей между источниками
  status TEXT NOT NULL DEFAULT 'new',-- new | filtered | scored | queued | applied | manual | skipped | error
  fit INTEGER,                       -- 0..100 соответствие профилю
  conditions INTEGER,                -- 0..100 условия (деньги, удалёнка, рост)
  chance INTEGER,                    -- 0..100 вероятность получить оффер
  score INTEGER,                     -- итоговый рейтинг
  verdict TEXT,                      -- краткое объяснение ИИ
  red_flags TEXT,
  apply_channel TEXT,                -- hh | email | telegram | link
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_vac_status ON vacancies(status);
CREATE INDEX IF NOT EXISTS idx_vac_score ON vacancies(score DESC);
CREATE INDEX IF NOT EXISTS idx_vac_fp ON vacancies(fingerprint);

-- Отклики и их жизненный цикл
CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  vacancy_id TEXT NOT NULL UNIQUE,
  channel TEXT NOT NULL,             -- hh | email | telegram | link
  letter TEXT NOT NULL,
  lang TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft', -- draft | sent | viewed | invited | interview | offer | rejected | waiting_user
  external_ref TEXT,                 -- id отклика на hh / message-id письма
  sent_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_reply TEXT,                   -- последний ответ работодателя
  reply_kind TEXT,                   -- invite | question | reject | offer | other
  suggested_answer TEXT,             -- черновик ответа от ИИ
  hot INTEGER DEFAULT 0              -- 1 — хотят созвониться / берут
);
CREATE INDEX IF NOT EXISTS idx_app_status ON applications(status);

-- Переписка
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  application_id INTEGER NOT NULL,
  direction TEXT NOT NULL,           -- in | out
  body TEXT NOT NULL,
  external_id TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(application_id, external_id)
);

-- Настройки и состояние (key → JSON)
CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

-- Журнал событий для панели
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL DEFAULT (datetime('now')),
  level TEXT NOT NULL DEFAULT 'info',
  text TEXT NOT NULL
);
