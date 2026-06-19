# Novryn

## What This Is

Novryn — централизованный источник истины (Source of Truth) для персональной системы исполнения одного пользователя. Система хранит факты: задачи и их иерархию, зависимости, вложения, сессии выполнения, события, ежедневный фокус, долгосрочную память о пользователе и поведенческие паттерны. Доступ к данным идёт через MCP API. Решения на основе этих фактов принимает внешний агент Hermes — сам Novryn не планирует, не напоминает и не анализирует, он только хранит и отдаёт данные с полным аудитом.

## Core Value

Hermes может в любой момент полностью восстановить текущее состояние пользователя из Novryn, не храня собственного состояния. Если это работает — система выполняет свою задачу.

## Requirements

### Validated

(None yet — ship to validate)

### Active

- [ ] Хранение задач со всеми полями (id, title, description, status, due_date, parent_task_id, оценки времени, energy_required, blocked_reason, ai_context_json, временные метки)
- [ ] Хранение иерархии задач (parent_task_id) и получение дерева задачи
- [ ] Хранение и валидация зависимостей (запрет циклов и самозависимости)
- [ ] Хранение вложений (URL, DOCUMENT, PDF, GITHUB, GOOGLE_DOC, OTHER)
- [ ] Хранение сессий выполнения (planned/actual minutes, result, notes)
- [ ] Event Store — запись 100% значимых изменений с actor/entity/payload
- [ ] Хранение ежедневного фокуса (Daily Focus) с сохранением порядка
- [ ] Хранение пользовательской памяти (User Memory) с confidence 0.0–1.0
- [ ] Хранение поведенческих паттернов (Behavior Patterns)
- [ ] MCP API для чтения и изменения данных (Task, Dependency, Attachment, Session, Focus, Intelligence tools)
- [ ] Модель разрешений: разделение действий USER и HERMES
- [ ] Удалённый доступ к MCP-эндпоинту с аутентификацией и защитой транспорта (для пользователя и для агента)
- [ ] Производительность при объёме до 100 000 задач / 1 000 000 событий (get task < 100ms, search < 300ms, tree < 500ms)

### Out of Scope

- Telegram-интеграция — non-goal V1 (PRD §2)
- Уведомления — non-goal V1; Novryn хранит факты, не оповещает
- AI-планирование, AI-анализ, генерация подзадач (логика) — это ответственность Hermes, не Novryn
- Календарь, канбан — представления/UI вне объёма V1
- Совместная работа (collaboration) — система однопользовательская
- Сам агент Hermes (логика принятия решений) — внешний потребитель, строится отдельно
- Мультипользовательская изоляция / мульти-аренда — V1 рассчитан на одного пользователя

## Context

- Novryn — это слой данных в более крупной персональной системе исполнения; единственный потребитель его API сегодня — агент Hermes.
- Архитектурные принципы из PRD: Source of Truth (Hermes не хранит состояние локально), Event Driven (всё значимое → Event Store), AI Agnostic (не зависит от конкретной LLM), Auditability (любое изменение отслеживаемо).
- Hermes может работать на любой LLM; контракт между Novryn и Hermes — это MCP API + ai_context_json как нейтральный носитель контекста.
- «Удалённый доступ для меня и агента» означает сетевой MCP-транспорт (HTTP/SSE), а не только локальный stdio, со встроенной аутентификацией.

## Constraints

- **Tech stack**: Python — реализация MCP-сервера на Python (MCP Python SDK / FastMCP). Выбрано пользователем.
- **Storage**: PostgreSQL — единственное хранилище (PRD §14).
- **IDs**: UUID v7 для всех идентификаторов (PRD §14).
- **API**: MCP-compatible; транспорт с поддержкой удалённого доступа + аутентификация.
- **Audit**: 100% изменений должны попадать в Event Store — жёсткое требование, влияет на дизайн слоя записи.
- **Performance**: get task < 100ms, search < 300ms, tree < 500ms при 100k задач / 1M событий.
- **Security**: удалённый эндпоинт требует аутентификации (токен/ключ) и защищённого транспорта.

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Язык реализации — Python | Выбор пользователя; зрелый MCP Python SDK (FastMCP), хорошая работа с Postgres (SQLAlchemy/asyncpg) | — Pending |
| Hermes вне объёма V1 | Novryn — только источник истины; логика решений строится отдельно (PRD non-goals) | — Pending |
| Удалённый MCP-транспорт + аутентификация | Доступ нужен пользователю и агенту удалённо; недостаточно локального stdio | — Pending |
| PostgreSQL как единственное хранилище | Зафиксировано PRD; реляционная модель + JSON-поля покрывают домен и аудит | — Pending |
| UUID v7 для идентификаторов | Зафиксировано PRD; сортируемость по времени помогает индексам и производительности | — Pending |
| Event Store обязателен для всех изменений | Принцип Auditability; Hermes восстанавливает состояние и историю из событий | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-06-19 after initialization*
