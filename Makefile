# Makefile für den X-Touch-One-System-Bridge (venv, Start im Vordergrund/Hintergrund,
# Integritätsprüfung ohne und mit Gerät).
#
#   make help            alle Ziele anzeigen
#   make venv            .venv anlegen/aktualisieren (pyproject.toml)
#   make run             Bridge im Vordergrund (Strg-C beendet sie)
#   make start           Bridge im Hintergrund (PID-File + Log)
#   make stop            Hintergrund-Bridge beenden
#   make restart         stop + start
#   make status          läuft sie? PID und letzte Log-Zeilen
#   make log             Log live mitlesen (bis Strg-C)
#   make ports           MIDI-Ports anzeigen (passender Port mit * markiert)
#   make check-env       Berechtigung, Audio-Backend, mido-Backend, Ports prüfen
#   make test            komplette Testsuite
#   make test-protocol   nur Protokollschicht (mcu.py, byte-exakt)
#   make test-engine     nur Zustandsmaschine (engine.py, Dummy-Backend)
#   make check           Tests + Umgebungsprüfung (ohne Hardware)
#   make selftest        Rauchtest am echten Gerät, Dummy-Backend, keine Systemaktionen
#   make check-all       check + selftest
#   make agent-install   LaunchAgent schreiben (startet nichts, gibt launchctl aus)
#   make agent-uninstall LaunchAgent entfernen (gibt launchctl aus)
#
# Überschreibbar, z. B.:  make start PORT="X-Touch" AUDIO=osascript
#                         make run ARGS="--log-level DEBUG --invert-jog"
#                         make selftest SELFTEST_SECONDS=10

VENV      ?= .venv
#: Python 3.12 ist Pflicht: python-rtmidi liefert nur bis cp312 Wheels.
PYTHON    ?= python3.12
PY         = $(VENV)/bin/python
PIP        = $(PY) -m pip
XT         = $(VENV)/bin/xtouch-one

#: Portname (Teilstring), den die Bridge öffnet.
PORT      ?= X-Touch
#: Audiosteuerung der Bridge: auto | coreaudio | osascript.
AUDIO     ?= auto
#: Zusätzliche Argumente für `make run` / `make start`.
ARGS      ?=

#: Log der Hintergrund-Bridge (und des LaunchAgents).
LOG       ?= /tmp/xtouch-one.log
#: Single-Instance-Lock. Leer = CLI-Default (stabil pro Benutzer:
#: ~/Library/Application Support/xtouch-one/bridge.lock). Nur setzen, wenn auch ein
#: manueller Start dasselbe --lock-file bekommt, sonst greift der Schutz nicht.
LOCKFILE  ?=
#: Log des Selbsttests (Dummy-Backend, wird nicht aufgeräumt, wenn er scheitert).
SELFTEST_LOG ?= /tmp/xtouch-one-selftest.log
#: Laufzeit des Rauchtests am Gerät.
SELFTEST_SECONDS ?= 6

.DEFAULT_GOAL := help

.PHONY: help venv run start stop restart status log ports check-env \
        test test-protocol test-engine test-macos test-cli check selftest check-all \
        agent-install agent-uninstall clean clean-venv

help: ## diese Übersicht anzeigen
	@echo "X-Touch One → macOS System-Bridge — Ziele:"
	@grep -E '^[a-zA-Z][a-zA-Z_-]*:.*?##' $(MAKEFILE_LIST) \
	    | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'
	@echo
	@echo "Variablen: VENV=$(VENV) PYTHON=$(PYTHON) PORT=$(PORT) AUDIO=$(AUDIO) ARGS='$(ARGS)'"

# ── Umgebung ─────────────────────────────────────────────────────────────────

venv: $(VENV)/bin/python $(VENV)/.deps ## .venv anlegen/aktualisieren (pyproject.toml)

$(VENV)/bin/python:
	$(PYTHON) -m venv $(VENV)

# Stamp: pip läuft nur, wenn pyproject.toml neuer ist als der letzte Lauf.
# --only-binary=:all: ist Absicht: es scheitert laut, wenn etwas kompiliert werden müsste.
$(VENV)/.deps: $(VENV)/bin/python pyproject.toml
	$(PIP) install --upgrade pip
	$(PIP) install --only-binary=:all: mido python-rtmidi pyobjc-framework-Cocoa pyobjc-framework-Quartz
	$(PIP) install -e .
	@touch $@
	@echo "Umgebung bereit: $(PY) (MIDI-Backend: mido über python-rtmidi/CoreMIDI)"

# ── Start ────────────────────────────────────────────────────────────────────

LOCKARG    = $(if $(LOCKFILE),--lock-file $(LOCKFILE),)

run: venv ## Bridge im Vordergrund starten (Strg-C beendet sie)
	$(XT) --port $(PORT) --audio $(AUDIO) $(LOCKARG) $(ARGS)

start: venv ## Bridge im Hintergrund starten (Logpfad: make status)
	@if $(XT) --lock-status $(LOCKARG) | grep -q "held by"; then \
	    echo "läuft bereits: $$($(XT) --lock-status $(LOCKARG)) — erst 'make stop'" >&2; exit 1; \
	fi
	@nohup $(XT) --port $(PORT) --audio $(AUDIO) $(LOCKARG) $(ARGS) >$(LOG) 2>&1 & \
	sleep 1; \
	if $(XT) --lock-status $(LOCKARG) | grep -q "held by"; then \
	    echo "gestartet: $$($(XT) --lock-status $(LOCKARG)), Log $(LOG)"; \
	else \
	    echo "Start fehlgeschlagen — Log:" >&2; cat $(LOG) >&2; exit 1; \
	fi

stop: ## laufende Bridge beenden (Halter aus dem Lock; SIGTERM, danach SIGKILL)
	@$(XT) --stop $(LOCKARG)

restart: stop start ## Hintergrund-Bridge neu starten

status: ## läuft die Bridge? Halter aus dem Lock und letzte Log-Zeilen
	@$(XT) --lock-status $(LOCKARG)
	@echo "--- $(LOG), letzte 10 Zeilen ---"
	@test -f $(LOG) && tail -n 10 $(LOG) || echo "(kein Log: $(LOG))"

log: ## Log der Hintergrund-Bridge mitlesen (bis Strg-C)
	@test -f $(LOG) || { echo "kein Log: $(LOG) — erst 'make start'"; exit 1; }
	tail -f $(LOG)

# ── Diagnose ─────────────────────────────────────────────────────────────────

ports: venv ## MIDI-Ports auflisten (passender Port mit * markiert)
	$(XT) --list

check-env: venv ## Berechtigung, Audio-Backend, mido-Backend und Ports anzeigen
	$(XT) --check --port $(PORT) --audio $(AUDIO)

# ── Tests ────────────────────────────────────────────────────────────────────

test: venv ## komplette Testsuite (unittest discover)
	$(PY) -m unittest discover -s tests -t . -v

test-protocol: venv ## nur Protokollschicht (mcu.py, byte-exakt)
	$(PY) -m unittest -v tests.test_mcu

test-engine: venv ## nur Zustandsmaschine (engine.py, Dummy-Backend)
	$(PY) -m unittest -v tests.test_engine

test-macos: venv ## nur macOS-Backend (Scrollachsen, braucht kein Gerät)
	$(PY) -m unittest -v tests.test_macos

test-cli: venv ## nur CLI-Lifecycle (Single-Instance-Lock)
	$(PY) -m unittest -v tests.test_cli

check: test check-env ## Integritätsprüfung ohne Hardware (Tests + Umgebungscheck)

# ── Rauchtest am Gerät ───────────────────────────────────────────────────────

selftest: venv ## Rauchtest am echten Gerät: Dummy-Backend, prüft Portaufbau und Feedbackpfad
	@echo "Selbsttest: Bridge mit --backend dummy für $(SELFTEST_SECONDS) s (Log: $(SELFTEST_LOG)) …"
	@$(XT) --port $(PORT) --backend dummy --log-level DEBUG >$(SELFTEST_LOG) 2>&1 & \
	pid=$$!; \
	sleep $(SELFTEST_SECONDS); \
	kill -TERM $$pid 2>/dev/null; \
	wait $$pid; \
	cat $(SELFTEST_LOG); \
	if grep -q "\[bridge\] opened" $(SELFTEST_LOG) && \
	   grep -q "\[dummy\] close" $(SELFTEST_LOG) && \
	   grep -q "\[bridge\] stopped after" $(SELFTEST_LOG) && \
	   ! grep -q "fatal" $(SELFTEST_LOG); then \
	    echo "OK: MIDI-Port geöffnet, Dummy-Backend im Loop, sauber beendet, kein Fatalfehler."; \
	else \
	    echo "FEHLER: Port nicht geöffnet, Backend nicht bedient oder Fatalfehler — 'make ports' und 'make check-env' prüfen." >&2; \
	    exit 1; \
	fi

check-all: check selftest ## Integritätsprüfung plus Rauchtest am Gerät

# ── Autostart ────────────────────────────────────────────────────────────────

agent-install: venv ## LaunchAgent-Plist schreiben (startet nichts, gibt launchctl-Kommandos aus)
	$(XT) --install-agent

agent-uninstall: venv ## LaunchAgent-Plist entfernen (gibt launchctl-Kommando aus)
	$(XT) --uninstall-agent

# ── Aufräumen ────────────────────────────────────────────────────────────────

clean: ## __pycache__/ und Logs entfernen (Lock bleibt: ein laufender Prozess hält ihn)
	find . -path ./$(VENV) -prune -o -name '__pycache__' -type d -print0 | xargs -0 rm -rf
	find . -path ./$(VENV) -prune -o -name '*.py[co]' -type f -print0 | xargs -0 rm -f
	rm -f $(LOG) $(SELFTEST_LOG)

clean-venv: ## .venv komplett entfernen
	rm -rf $(VENV)
