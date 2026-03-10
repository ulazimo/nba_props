# NBA Prediction System – Multi-Agent Architecture

Automatizovani sistem za predviđanje NBA statistika koji radi bez nadzora na Linux VPS-u.

---

## Arhitektura

```
nba_predictor/
├── agents/
│   ├── agent_scout.py           # Povlači podatke (nba_api + retry/rate-limit)
│   ├── agent_matchup_expert.py  # Odbrambeni rejtinzi, pace, matchup analiza
│   ├── agent_mathematician.py   # Poissonova distribucija, verovatnoće
│   ├── agent_odds_specialist.py # The Odds API, Kelly Criterion, edge detekcija
│   └── agent_evaluator.py       # Backtesting, P&L, ROI
├── config/
│   ├── logging_config.py        # RotatingFileHandler, formatteri
│   └── settings.py              # Sve konfigurabilne vrednosti
├── data/
│   └── database.py              # SQLite DAL (thread-safe)
├── logs/                        # Automatski kreiran
│   └── nba_system.log           # Rotira na 10MB, čuva 5 backup-a
├── data/                        # Automatski kreiran
│   └── nba_predictions.db       # SQLite baza
├── main.py                      # Orchestrator (može se pokrenuti direktno)
├── scheduler.py                 # APScheduler – 3 dnevna termina
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── env.example
```

---

## Tok podataka

```
09:00  AgentEvaluator   ──► Proverava rezultate prethodne noći
                            Računa P&L i ROI
                            Upisuje u bet_results tabelu

10:00  AgentScout       ──► Povlači team stats (nba_api)
                            Povlači raspored mečeva
                            Upisuje u SQLite

22:00  AgentMatchupExpert ► Analizira def_rating, pace za svaki meč
       AgentMathematician ► Poisson verovatnoće (home/away win, over/under)
       AgentOddsSpecialist ► Povlači kvote, računa edge, Kelly stake
       Orchestrator      ──► Čuva predikcije u bazu
```

---

## Pokretanje

### Opcija A – Docker Compose (preporučeno za VPS)

```bash
# 1. Klonirajte repozitorijum
git clone <repo_url> nba_predictor
cd nba_predictor

# 2. Kreirajte .env fajl
cp env.example .env
nano .env
# Unesite ODDS_API_KEY i podesite ostale vrednosti

# 3. Pokrenite
docker compose up -d --build

# 4. Pratite logove
docker compose logs -f

# 5. Ručno pokrenite fazu (opciono)
docker compose exec nba-predictor python main.py --phase fetch
docker compose exec nba-predictor python main.py --phase predict
docker compose exec nba-predictor python main.py --phase evaluate
```

---

### Opcija B – nohup (bez Dockera)

```bash
# 1. Instalacija sistema (Ubuntu/Debian)
sudo apt update && sudo apt install -y python3.12 python3.12-venv python3-pip

# 2. Virtuelno okruženje
cd nba_predictor
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Konfiguracija
cp env.example .env
nano .env

# 4. Pokretanje u pozadini
nohup python scheduler.py > /dev/null 2>&1 &
echo $! > scheduler.pid
echo "Scheduler pokrenut sa PID: $(cat scheduler.pid)"

# 5. Praćenje logova
tail -f logs/nba_system.log

# 6. Zaustavljanje
kill $(cat scheduler.pid)
```

---

### Opcija C – systemd servis (najrobusnije za produkciju)

```bash
# 1. Kreirajte servis fajl
sudo nano /etc/systemd/system/nba-predictor.service
```

Sadržaj fajla:
```ini
[Unit]
Description=NBA Prediction System
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/nba_predictor
ExecStart=/home/ubuntu/nba_predictor/venv/bin/python scheduler.py
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal
EnvironmentFile=/home/ubuntu/nba_predictor/.env

[Install]
WantedBy=multi-user.target
```

```bash
# 2. Aktivirajte servis
sudo systemctl daemon-reload
sudo systemctl enable nba-predictor
sudo systemctl start nba-predictor

# 3. Status i logovi
sudo systemctl status nba-predictor
sudo journalctl -u nba-predictor -f
```

---

## Ručno pokretanje faza

```bash
# Samo fetch podataka
python main.py --phase fetch

# Predikcije za određeni datum
python main.py --phase predict --date 2025-03-15

# Evaluacija
python main.py --phase evaluate

# Sve tri faze odjednom (za testiranje)
python main.py --phase all
```

---

## Logovanje

Svi logovi se upisuju u `logs/nba_system.log`:

```
2025-03-10 10:00:01 | INFO     | AgentScout               | Fetching games for 2025-03-10
2025-03-10 10:00:03 | INFO     | AgentScout               |   Registered game 0022401234: Lakers vs Celtics
2025-03-10 22:00:01 | INFO     | AgentMatchupExpert       | Analyzing matchup: Lakers (home) vs Celtics (away)
2025-03-10 22:00:02 | INFO     | AgentMathematician       | Computing Poisson: λ_home=112.40 λ_away=108.90
2025-03-10 22:00:02 | INFO     | AgentOddsSpecialist      |   VALUE BET → home_win @ 2.10 | edge=6.2% | kelly=2.3%
2025-03-11 09:00:01 | INFO     | AgentEvaluator           | ✓ Game 0022401234 | bet=home_win @ 2.10 | P&L=+1.10
```

**Log rotacija:** Automatski na 10MB, čuva 5 backup fajlova (max ~50MB ukupno).

---

## Baza podataka

```bash
# Pregled predikcija
sqlite3 data/nba_predictions.db "SELECT * FROM predictions ORDER BY predicted_at DESC LIMIT 10;"

# Profitabilnost
sqlite3 data/nba_predictions.db "
SELECT
    COUNT(*) as ukupno,
    SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as pobede,
    ROUND(SUM(profit_loss), 2) as ukupni_profit,
    ROUND(SUM(profit_loss)/SUM(stake)*100, 2) as roi_pct
FROM bet_results;"
```

---

## API Ključevi

| Servis | URL | Napomena |
|--------|-----|----------|
| The Odds API | https://the-odds-api.com/ | 500 req/mesec besplatno |
| nba_api | - | Bez ključa, rate-limit 1s |

---

## Troubleshooting

**Problem: `ModuleNotFoundError`**
```bash
# Proverite da ste u virtualnom okruženju
source venv/bin/activate
pip install -r requirements.txt
```

**Problem: NBA API timeout**
```bash
# Povećajte delay u .env
NBA_API_DELAY=2.0
NBA_API_RETRIES=7
```

**Problem: Nema predikcija**
```bash
# Proverite da su team stats učitani
python main.py --phase fetch
# Proverite log
tail -100 logs/nba_system.log | grep ERROR
```

**Problem: Docker container se restartuje**
```bash
docker compose logs nba-predictor --tail=50
```
