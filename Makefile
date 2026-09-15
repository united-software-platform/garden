.PHONY: help init init-host init-env init-dirs init-gitignore init-ssh-key init-ssh-config \
	openspec-init model-build model-diff model-gen model-checks model-verify model-test \
	db-up db-down db-apply db-verify \
	format lint typecheck check

.DEFAULT_GOAL := help

# Список целей собирается из комментариев вида '## описание' в самом Makefile:
# описание живёт рядом с целью, поэтомуновая цель попадает в вывод без правки в двух местах.
help: ## Список команд с описаниями
	@printf 'Команды окружения SDD Developer Kit:\n\n'
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sed 's/:.*## /|/' \
		| awk -F'|' '{printf "  make %-16s %s\n", $$1, $$2}'
	@printf '\nПорядок установки и переменные окружения — в README.md проекта kit'"'"'а.\n'

# Значение переменной из .env: файл заполняется пользователем и на момент первого
# запуска init может быть неполным, поэтому пустое значение заменяется умолчанием
# из .env.example. Разбор построчный, а не через include: .env — файл секретов,
# и его содержимое не должно попадать в пространство имён переменных make.
env_value = $$(sed -n 's/^$(1)=//p' .env 2>/dev/null | tail -n 1)

# Разбор SSH-переменных из .env с умолчаниями. Собран в одном месте и переиспользуется
# шагами подготовки: значения не расходятся между шагами, а добавление переменной не
# требует править каждый шаг. Каждый шаг подставляет присвоения в начало своей строки
# рецепта — переменные shell не переживают переход к следующей строке.
ssh_vars = ssh_key="$(call env_value,SSH_KEY)"; ssh_key="$${ssh_key:-.ssh/id_ed25519}"; \
	ssh_config="$(call env_value,SSH_CONFIG)"; ssh_config="$${ssh_config:-.ssh/config}"; \
	git_host="$(call env_value,GIT_HOST)"; \
	git_user="$(call env_value,GIT_USER)"; git_user="$${git_user:-git}"; \
	container_ssh="$(call env_value,CONTAINER_SSH_DIR)"; container_ssh="$${container_ssh:-/home/claude/.ssh}"

# Единая точка входа подготовки: хостовая часть и инструменты SDD. Под-цели вызываются
# рецептом, а не перечислены зависимостями: зависимости при make -j пошли бы параллельно,
# а обе под-цели пишут в общие файлы корня проекта.
init: ## Полная подготовка проекта: хостовая часть и инструменты SDD
	@$(MAKE) --no-print-directory init-host
	@$(MAKE) --no-print-directory openspec-init
	@echo "Готово. Дальше: заполнить .env (GIT_HOST, CLAUDE_PROFILE) и создать каталог профиля в .claude-accounts/"

# Хостовая часть подготовки — всё, что делается до контейнера: файл секретов, каталоги ключей
# и профилей, записи в .gitignore, SSH-ключ проекта и конфигурация SSH для git-хоста. Шаги
# вызываются рецептом, а не перечислены зависимостями: зависимости при make -j пошли бы
# параллельно, а шаги пишут в общие файлы корня проекта.
#
# Цель остаётся отдельной: хостовую подготовку повторяют после заполнения .env, не трогая
# развёртывание инструментов SDD.
init-host: ## Хостовая подготовка: .env, каталоги, SSH-ключ и конфигурация SSH, .gitignore
	@$(MAKE) --no-print-directory init-env
	@$(MAKE) --no-print-directory init-dirs
	@$(MAKE) --no-print-directory init-gitignore
	@$(MAKE) --no-print-directory init-ssh-key
	@$(MAKE) --no-print-directory init-ssh-config

# Файл секретов. Существующий .env не перезаписывается: в нём заполненные пользователем значения,
# а .env.example — только умолчания.
init-env: ## Создать .env из .env.example
	@if [ -f .env ]; then \
		echo "  .env уже существует — оставлен без изменений"; \
	else \
		cp .env.example .env; \
		echo "  создан .env из .env.example"; \
	fi

# Каталоги ключей и профилей аккаунтов. Права 700 на .ssh — требование ssh-клиента: с более
# широкими правами он отказывается работать с лежащим внутри ключом.
init-dirs: ## Создать каталоги .ssh и .claude-accounts
	@mkdir -p .ssh .claude-accounts
	@chmod 700 .ssh

# Записи в .gitignore. Каждая добавляется однократно: повторный прогон находит её точным
# совпадением строки и пропускает. Перевод строки дописывается перед записью, если файл им
# не заканчивается, — иначе запись склеилась бы с последней строкой.
init-gitignore: ## Добавить в .gitignore .env, .ssh/ и .claude-accounts/
	@touch .gitignore
	@for entry in .env .ssh/ .claude-accounts/; do \
		grep -qxF "$$entry" .gitignore >/dev/null 2>&1 && continue; \
		[ -s .gitignore ] && [ -n "$$(tail -c 1 .gitignore)" ] && printf '\n' >> .gitignore; \
		printf '%s\n' "$$entry" >> .gitignore; \
		echo "  в .gitignore добавлено: $$entry"; \
	done

# SSH-ключ проекта. Существующий ключ не перезаписывается: он может быть уже зарегистрирован
# в git-сервисе. Проверка ssh-keygen — часть этого шага: без утилиты бессмысленна именно
# генерация ключа, и при отдельном вызове цели проверка обязана выполниться.
init-ssh-key: ## Создать SSH-ключ проекта (ed25519)
	@if ! command -v ssh-keygen >/dev/null 2>&1; then \
		echo "Ошибка: не найдена утилита ssh-keygen — установите пакет openssh-client" >&2; \
		exit 1; \
	fi
	@$(ssh_vars); \
	mkdir -p "$$(dirname "$$ssh_key")"; \
	if [ -f "$$ssh_key" ]; then \
		echo "  SSH-ключ $$ssh_key уже существует — оставлен без изменений"; \
	else \
		ssh-keygen -q -t ed25519 -f "$$ssh_key" -N "" -C "sdd-developer-kit@$$(basename "$$(pwd)")"; \
		chmod 600 "$$ssh_key"; \
		chmod 644 "$$ssh_key.pub"; \
		echo "  создан SSH-ключ $$ssh_key (ed25519, без passphrase)"; \
		echo ""; \
		echo "  Добавьте публичный ключ в git-сервис — без этого push из контейнера не пройдёт:"; \
		echo ""; \
		cat "$$ssh_key.pub"; \
		echo ""; \
	fi

# Конфигурация SSH для git-хоста. Существующая конфигурация не перезаписывается: в ней могут быть
# правки пользователя. Путь ключа записывается от каталога SSH внутри контейнера — файл читает
# ssh из контейнера, а не с хоста.
init-ssh-config: ## Создать конфигурацию SSH для git-хоста из .env
	@$(ssh_vars); \
	mkdir -p "$$(dirname "$$ssh_config")"; \
	if [ -f "$$ssh_config" ]; then \
		echo "  $$ssh_config уже существует — оставлен без изменений"; \
	elif [ -z "$$git_host" ]; then \
		echo "  GIT_HOST не задан — $$ssh_config не создан;"; \
		echo "  заполните GIT_HOST в .env и выполните make init повторно"; \
	else \
		printf 'Host %s\n  HostName %s\n  User %s\n  IdentityFile %s/%s\n  IdentitiesOnly yes\n' \
			"$$git_host" "$$git_host" "$$git_user" "$$container_ssh" "$$(basename "$$ssh_key")" \
			> "$$ssh_config"; \
		chmod 644 "$$ssh_config"; \
		echo "  создан $$ssh_config: хост $$git_host, ключ $$container_ssh/$$(basename "$$ssh_key")"; \
	fi

# Инициализация OpenSpec в проекте. Skills, команды агента и каталог openspec/ создаёт сама
# утилита из образа, а не поставка kit'а: иначе они остаются от той версии, что лежала
# в архиве, и расходятся с OPENSPEC_VERSION образа. Язык артефактов задаётся ключом --language,
# поэтому openspec/config.yaml тоже не входит в поставку.
#
# Цель вызывается из init и остаётся отдельной: после смены версии OpenSpec в образе
# инструменты обновляются повторным вызовом, без прохода по хостовой части подготовки.
openspec-init: ## Развернуть инструменты SDD: openspec init в контейнере агента
	@if [ ! -f .env ]; then \
		echo "Ошибка: нет файла .env — сначала выполните make init" >&2; \
		exit 1; \
	fi
	@profile="$${CLAUDE_PROFILE:-$(call env_value,CLAUDE_PROFILE)}"; \
	profile="$${profile:-__no_profile__}"; \
	accounts="$${CLAUDE_ACCOUNTS_DIR:-$(call env_value,CLAUDE_ACCOUNTS_DIR)}"; \
	accounts="$${accounts:-.claude-accounts}"; \
	mkdir -p "$$accounts/$$profile"
	docker compose --profile claude run --rm -T claude \
		openspec init --tools claude --language ru

# Запуск инструментов модели. Пакет лежит в tools/, поэтому путь добавляется явно:
# устанавливать проект в окружение ради вызова из Makefile не требуется.
model_run = PYTHONPATH=tools uv run --quiet python -m garden_model

# Сборка дескриптора рабочей модели: печатает версию и хеш содержания.
# Ошибки формата и валидации модели останавливают цель ненулевым кодом возврата.
model-build: ## Собрать дескриптор модели и напечатать версию с хешем
	@$(model_run) build

# Сравнение рабочей модели с последней выпущенной версией. Ничего не пишет на диск:
# показывает дельту и предупреждения, чтобы их можно было посмотреть до выпуска.
model-diff: ## Показать дельту рабочей модели против выпущенной версии
	@$(model_run) diff

# Выпуск версии: миграции, дескриптор выпущенной версии, запись в реестр. Останавливается
# на ошибках процесса — версия не повышена, версия понижена, номер выдан повторно.
# BASELINE=1 выпускает свёртку модели целиком: начальная схема для пустого хранилища
# или для нового бэкенда СУБД.
model-gen: ## Выпустить версию модели: миграции, дескриптор, запись в реестр
	@$(model_run) gen $${BASELINE:+--baseline}

# Запросы проверок целостности, которые нельзя выразить ограничениями схемы:
# нижние границы кардинальности, ацикличность, связи с устаревшей ревизией цели.
model-checks: ## Напечатать запросы проверок целостности
	@$(model_run) checks

# Сверка хранилища с моделью: неизменяемость выпущенных миграций, соответствие схемы
# дескриптору, прогон проверок целостности. Строка подключения передаётся в DSN.
model-verify: ## Сверить схему хранилища с моделью (DSN=postgresql://...)
	@if [ -z "$$DSN" ]; then \
		echo "Ошибка: не задан DSN — укажите make model-verify DSN=postgresql://..." >&2; \
		exit 1; \
	fi
	@$(model_run) verify --dsn "$$DSN"

# Прогон тестов инструментов модели. Часть тестов поднимает временный PostgreSQL,
# поэтому первый запуск дольше остальных.
model-test: ## Прогнать тесты инструментов модели
	@uv run --quiet --extra dev pytest -q

# Хранилище графа требований поднимается отдельным профилем db: сессия агента базу
# не требует — модель, дельта и миграции собираются без подключения к ней.
db-up: ## Поднять базу модели
	docker compose --profile db up -d postgres

# Остановка с удалением тома: следующий db-up даёт пустое хранилище. Именно поэтому
# цель удаляет данные — она существует ради сборки с нуля, а не ради паузы.
db-down: ## Остановить базу модели и удалить её данные
	docker compose --profile db down --volumes

# Применение выпущенных миграций. Порядок задаёт changelog/master.yaml, готовности базы
# Liquibase дожидается по healthcheck сервиса postgres.
db-apply: ## Применить миграции к базе модели
	docker compose --profile db run --rm liquibase

# Сборка схемы с нуля: пустое хранилище и полный прогон всех миграций. Проверяется не
# последний шаг, а то, что схема собирается целиком. Под-цели вызываются рецептом,
# а не зависимостями: при make -j они пошли бы параллельно, а порядок здесь обязателен.
db-verify: ## Собрать схему с нуля: пустая база и полный прогон миграций
	@$(MAKE) --no-print-directory db-down
	@$(MAKE) --no-print-directory db-up
	@$(MAKE) --no-print-directory db-apply
	@echo "Схема собрана с нуля. Сверка с моделью: make model-verify DSN=..."

# Инструменты проверки вызываются так же, как их вызывает пайплайн: тем же способом
# и по той же конфигурации в pyproject.toml. Расхождение вердиктов локального прогона
# и пайплайна обесценило бы локальный прогон.
tool_run = uv run --quiet --extra dev

# Единственная цель, правящая файлы: оформление приводится к норме на месте.
format: ## Привести оформление кода к норме
	@$(tool_run) ruff format

# Линтер: неиспользуемые имена и импорты, порядок импортов, устаревшие конструкции.
# Оформление проверяется отдельно — форматтером, а не набором правил.
lint: ## Проверить оформление и найти дефекты кода
	@$(tool_run) ruff format --check
	@$(tool_run) ruff check

# Строгая проверка типов. Состав проверяемых путей задан в pyproject.toml,
# поэтому цель не перечисляет их второй раз.
typecheck: ## Проверить типы
	@$(tool_run) mypy

# Точка входа набора проверок. Под-цели вызываются рецептом, а не зависимостями:
# при make -j они пошли бы параллельно и перемешали вывод.
check: ## Прогнать все проверки качества кода
	@$(MAKE) --no-print-directory lint
	@$(MAKE) --no-print-directory typecheck
