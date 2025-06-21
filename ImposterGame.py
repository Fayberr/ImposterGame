from flask import Flask, render_template, request, redirect, url_for, session, jsonify, make_response, send_file
import random
import threading
import webbrowser
import secrets
import os
import socket
import requests
import sys
import time
import json
from datetime import datetime, timedelta
import re

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

CONTROL_PASSWORD = "admineger"
CONTROL_USERS = {"Control", "Admin"}

LEADERBOARD_FILE = 'leaderboard.json'
leaderboard = {
    'players': {}, 
    'impostors': {}  
}

players = []
game_players = {}
player_sessions = {}
session_heartbeats = {}
game_started = False
assigned_words = {}
spicy_mode = False
force_spicy = False
revealed = False
lobby_messages = []  # Separate chat for lobby
game_messages = []   # Separate chat for game
current_starter = ""
votes = {}
voting_active = False
vote_results = {}
game_ended = False
winning_word = ""
impostor_guess_used = False
start_votes = set()

HEARTBEAT_TIMEOUT = 6
CLEANUP_INTERVAL = 6

announce_spicy_mode = True

kicked_sessions = set()

def load_leaderboard():
    """Lädt das Leaderboard aus der JSON-Datei"""
    global leaderboard
    if os.path.exists(LEADERBOARD_FILE):
        try:
            with open(LEADERBOARD_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if 'players' not in data:
                    data['players'] = {}
                if 'impostors' not in data:
                    data['impostors'] = {}
                leaderboard = data
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            print(f"[load_leaderboard] Error loading leaderboard: {e}")
            save_leaderboard()
    else:
        save_leaderboard()

def save_leaderboard():
    """Speichert das Leaderboard in die JSON-Datei"""
    with open(LEADERBOARD_FILE, 'w', encoding='utf-8') as f:
        json.dump(leaderboard, f, ensure_ascii=False, indent=4)

def add_win(player_name, role):
    """Fügt einen Gewinn zum Leaderboard hinzu"""
    key = player_name.lower()
    if role == 'impostor':
        leaderboard['impostors'][key] = leaderboard['impostors'].get(key, 0) + 1
    else:
        leaderboard['players'][key] = leaderboard['players'].get(key, 0) + 1
    save_leaderboard()

def format_name(name):
    """Formatiert Namen mit erstem Buchstaben groß"""
    if not name or not isinstance(name, str):
        return ''
    return name[0].upper() + name[1:].lower()

def get_top_winners(role, limit=None):
    """Gibt sortierte Liste der Gewinner zurück"""
    data = leaderboard.get(role, {})
    sorted_list = sorted(data.items(), key=lambda x: x[1], reverse=True)
    if limit:
        return sorted_list[:limit]
    return sorted_list

def update_leaderboard_on_game_end(vote_results):
    """Aktualisiert das Leaderboard basierend auf Spielergebnis"""
    if vote_results.get('impostor_won'):
        impostor_name = vote_results.get('impostor')
        if impostor_name:
            add_win(impostor_name, 'impostor')
    elif vote_results.get('impostor_failed'):
        for player in game_players.keys():
            if player != vote_results.get('impostor'):
                add_win(player, 'player')
    elif vote_results.get('is_impostor'):
        for player in game_players.keys():
            if player != vote_results.get('voted_out'):
                add_win(player, 'player')
    elif vote_results.get('voted_out') and not vote_results.get('is_impostor'):
        for player, word in assigned_words.items():
            if "IMPOSTOR" in word:
                add_win(player, 'impostor')
                break

load_leaderboard()

def get_public_ip():
    """Ermittelt die öffentliche IP-Adresse"""
    try:
        services = [
            'https://api.ipify.org',
            'https://ipinfo.io/ip',
            'https://icanhazip.com',
            'https://ident.me'
        ]
        
        for service in services:
            try:
                response = requests.get(service, timeout=5)
                if response.status_code == 200:
                    return response.text.strip()
            except requests.RequestException as e:
                print(f"[get_public_ip] Error with {service}: {e}")
                continue
        
        return get_local_ip()
    except Exception as e:
        print(f"[get_public_ip] General error: {e}")
        return "localhost"

def get_local_ip():
    """Ermittelt die lokale IP-Adresse als Fallback"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except OSError as e:
        print(f"[get_local_ip] Error: {e}")
        return "localhost"

def has_display():
    """Prüft ob ein Display verfügbar ist (für GUI)"""
    try:
        if sys.platform.startswith('win'):
            return True
        
        if 'DISPLAY' in os.environ:
            return True
        
        if sys.platform == 'darwin':
            return True
        
        return False
    except Exception as e:
        print(f"[has_display] Error: {e}")
        return False

def generate_session_id():
    """Generiert eine eindeutige Session-ID"""
    return secrets.token_hex(16)

def update_heartbeat(session_id):
    """Aktualisiert den Heartbeat für eine Session"""
    session_heartbeats[session_id] = datetime.now()

def cleanup_inactive_sessions():
    """Entfernt inaktive Sessions und Spieler"""
    global players, game_players, player_sessions, session_heartbeats
    current_time = datetime.now()
    inactive_sessions = []
    for session_id, last_heartbeat in session_heartbeats.items():
        if current_time - last_heartbeat > timedelta(seconds=HEARTBEAT_TIMEOUT):
            inactive_sessions.append(session_id)
    for session_id in inactive_sessions:
        if session_id in player_sessions:
            player_name = player_sessions[session_id]
            if player_name in CONTROL_USERS or session.get('control_logged_in', False):
                continue
            if not game_started and player_name in players:
                players.remove(player_name)
                # Add game event for player timeout
                add_game_event('player_timeout', f"⏰ {player_name} wurde wegen Inaktivität entfernt!", "⏰")
                
                # Check if not enough players to start the game
                if len(players) < 3:
                    add_game_event('lobby_not_ready', f"⏳ Nicht genug Spieler zum Starten ({len(players)}/3)", "⏳")
                    
            if game_started and player_name in game_players:
                del game_players[player_name]
                # Add game event for player timeout during game
                add_game_event('player_timeout', f"⏰ {player_name} wurde wegen Inaktivität aus dem Spiel entfernt!", "⏰")
            if player_name in assigned_words:
                del assigned_words[player_name]
            if player_name in votes:
                del votes[player_name]
            del player_sessions[session_id]
        del session_heartbeats[session_id]

def start_cleanup_thread():
    """Startet den Cleanup-Thread"""
    def cleanup_loop():
        while True:
            time.sleep(CLEANUP_INTERVAL)
            cleanup_inactive_sessions()
    
    cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
    cleanup_thread.start()

def load_words():
    """Lädt normale Wörter aus words.txt"""
    if os.path.exists("words.txt"):
        try:
            with open("words.txt", "r", encoding="utf-8") as f:
                words = [line.strip() for line in f if line.strip()]
            if not words:
                print("[load_words] Warning: words.txt is empty, using fallback words.")
                return ["Apfel", "Banane", "Auto", "Haus", "Baum"]
            return words
        except Exception as e:
            print(f"[load_words] Error: {e}")
            return ["Apfel", "Banane", "Auto", "Haus", "Baum"]
    return ["Apfel", "Banane", "Auto", "Haus", "Baum"]

def load_spicy_words():
    """Lädt spicy Wörter aus spicy_words.txt"""
    if os.path.exists("spicy_words.txt"):
        try:
            with open("spicy_words.txt", "r", encoding="utf-8") as f:
                words = [line.strip() for line in f if line.strip()]
            if not words:
                print("[load_spicy_words] Warning: spicy_words.txt is empty, using fallback words.")
                return ["Massage", "Kuss", "Romantik", "Verführung"]
            return words
        except Exception as e:
            print(f"[load_spicy_words] Error: {e}")
            return ["Massage", "Kuss", "Romantik", "Verführung"]
    return ["Massage", "Kuss", "Romantik", "Verführung"]

def get_word_list():
    """Gibt die aktuelle Wortliste basierend auf dem Modus zurück"""
    normal_words = load_words()
    spicy_words = load_spicy_words()
    
    if force_spicy:
        return spicy_words
    elif spicy_mode:
        return normal_words + spicy_words
    else:
        return normal_words

def select_starter(players_list, impostor):
    """Wählt einen Starter aus - Impostor hat 20% Chance, normale Spieler 80%"""
    if random.random() < 0.2:
        return impostor
    else:
        normal_players = [p for p in players_list if p != impostor]
        return random.choice(normal_players)

def is_control_user():
    """Prüft ob aktuelle Session ein Control-User ist"""
    return session.get('control_logged_in', False) or session.get('player_name') in CONTROL_USERS

def check_all_voted():
    """Prüft ob alle aktiven Spieler gevoted haben"""
    active_game_players = list(game_players.keys()) if game_started else players
    return len(votes) >= len(active_game_players)

def auto_end_voting():
    """Beendet automatisch das Voting wenn alle gevoted haben"""
    global voting_active, vote_results, game_ended, winning_word
    
    if not voting_active or not check_all_voted():
        return
    
    voting_active = False
    
    vote_counts = {}
    for voted_player in votes.values():
        vote_counts[voted_player] = vote_counts.get(voted_player, 0) + 1
    
    if vote_counts:
        max_votes = max(vote_counts.values())
        most_voted = [player for player, count in vote_counts.items() if count == max_votes]
        
        if len(most_voted) == 1:
            voted_out = most_voted[0]
            is_impostor = "IMPOSTOR" in assigned_words.get(voted_out, "")
            
            if is_impostor:
                game_ended = True
                for player, word in assigned_words.items():
                    if not "IMPOSTOR" in word:
                        winning_word = word.replace("Dein Wort: ", "")
                        break
                # Add game event for impostor caught
                add_game_event('voting_result', f"🎉 {voted_out} war der IMPOSTOR! Die Spieler haben gewonnen!", "🎉")
            else:
                # Add game event for innocent player voted out
                add_game_event('voting_result', f"😔 {voted_out} war unschuldig! Der Impostor ist noch da!", "😔")
            
            vote_results = {
                'voted_out': voted_out,
                'is_impostor': is_impostor,
                'votes': vote_counts,
                'game_ended': game_ended,
                'winning_word': winning_word
            }
            
            if game_ended:
                update_leaderboard_on_game_end(vote_results)
        else:
            # Add game event for tie
            tied_names = ", ".join(most_voted)
            add_game_event('voting_result', f"🤝 Unentschieden zwischen: {tied_names}", "🤝")
            vote_results = {
                'tie': True,
                'tied_players': most_voted,
                'votes': vote_counts
            }
    # Removed the "no votes" case since players always need to vote

def check_impostor_word_guess(guessed_word, actual_word):
    """Prüft ob Impostor das richtige Wort erraten hat (case insensitive)"""
    return guessed_word.strip().lower() == actual_word.strip().lower()

@app.route("/api/leaderboard")
def api_leaderboard():
    """API-Endpoint für Leaderboard-Daten"""
    player_wins = get_top_winners('players')
    impostor_wins = get_top_winners('impostors')
    
    formatted_players = [
        {'name': format_name(name), 'wins': wins} 
        for name, wins in player_wins
    ]
    formatted_impostors = [
        {'name': format_name(name), 'wins': wins} 
        for name, wins in impostor_wins
    ]
    
    return jsonify({
        'players': formatted_players,
        'impostors': formatted_impostors
    })

@app.route("/api/status")
def api_status():
    """API-Endpoint für Live-Updates mit Heartbeat"""
    session_id = session.get('session_id')
    player_name = session.get('player_name')
    
    if session_id:
        update_heartbeat(session_id)
    
    player_word = ""
    
    if revealed and player_name and player_name in assigned_words:
        player_word = assigned_words[player_name]
    
    active_players = list(game_players.keys()) if game_started else players
    
    return jsonify({
        'players': active_players,
        'game_started': game_started,
        'spicy_mode': spicy_mode,
        'force_spicy': force_spicy,
        'revealed': revealed,
        'player_word': player_word,
        'is_logged_in': player_name is not None and (player_name in players or player_name in game_players),
        'player_name': player_name,
        'chat_messages': get_current_chat()[-50:],
        'current_starter': current_starter,
        'voting_active': voting_active,
        'votes': votes,
        'vote_results': vote_results,
        'game_ended': game_ended,
        'winning_word': winning_word,
        'all_voted': check_all_voted(),
        'is_control': is_control_user(),
        'impostor_guess_used': impostor_guess_used,
        'can_rejoin': game_started and player_name in assigned_words and player_name not in game_players,
        'announce_spicy_mode': announce_spicy_mode,
        'start_votes': list(start_votes) if not game_started else []
    })

@app.route("/", methods=["GET", "POST"])
def index():
    global players, game_players, player_sessions, session_heartbeats, kicked_sessions
    error_message = None
    kicked_message = None
    session_id = session.get('session_id')
    if session_id and session_id in kicked_sessions:
        kicked_sessions.remove(session_id)
        session.clear()
        kicked_message = "Du wurdest aus dem Spiel entfernt. Du kannst erneut beitreten."
        return render_template("index.html",
                             players=list(game_players.keys()) if game_started else players,
                             game_started=game_started,
                             spicy_mode=spicy_mode,
                             force_spicy=force_spicy,
                             revealed=revealed,
                             assigned_words=assigned_words,
                             player_word="",
                             is_logged_in=False,
                             player_name=None,
                             chat_messages=get_current_chat()[-20:],
                             current_starter=current_starter,
                             voting_active=voting_active,
                             votes=votes,
                             vote_results=vote_results,
                             public_ip=get_public_ip(),
                             game_ended=game_ended,
                             winning_word=winning_word,
                             is_control=is_control_user(),
                             impostor_guess_used=impostor_guess_used,
                             can_rejoin=False,
                             error_message=error_message,
                             kicked_message=kicked_message)
    
    if request.method == "POST":
        action = request.form.get("action")
        
        if action == "join":
            name = request.form["name"].strip()
            if not (3 <= len(name) <= 20) or not re.match(r'^[A-Za-z0-9äöüÄÖÜß ]+$', name):
                error_message = "Name muss 3-20 Zeichen lang sein und darf nur Buchstaben, Zahlen und Leerzeichen enthalten."
            elif name in players or name in game_players:
                error_message = "Name ist bereits vergeben. Bitte wähle einen anderen Namen."
            elif 'session_id' in session and session.get('player_name') in players + list(game_players.keys()):
                error_message = "Du bist bereits im Spiel!"
            else:
                session_id = generate_session_id()
                session['session_id'] = session_id
                session['player_name'] = name
                player_sessions[session_id] = name
                update_heartbeat(session_id)
                if not game_started and name not in players:
                    players.append(name)
                    # Add game event for player join
                    add_game_event('player_join', f"👋 {name} ist der Lobby beigetreten!", "👋")
                    
                    # Check if enough players joined to start the game
                    if len(players) == 3:
                        add_game_event('lobby_ready', "🎮 Genug Spieler zum Starten! (3/3)", "🎮")
                    elif len(players) > 3:
                        add_game_event('lobby_ready', f"🎮 Genug Spieler zum Starten! ({len(players)}/3)", "🎮")
                        
                elif game_started and name in assigned_words and name not in game_players:
                    game_players[name] = session_id
                    # Add game event for player rejoin
                    add_game_event('player_rejoin', f"🔄 {name} ist wieder dem Spiel beigetreten!", "🔄")
                return redirect(url_for("index"))
        
    if 'session_id' not in session:
        session['session_id'] = generate_session_id()
    session_id = session.get('session_id')
    player_name = session.get('player_name')
    if session_id:
        update_heartbeat(session_id)
    player_word = ""
    if revealed and player_name and player_name in assigned_words:
        player_word = assigned_words[player_name]
    is_active_player = False
    if player_name:
        if game_started:
            is_active_player = player_name in game_players
        else:
            is_active_player = player_name in players
    can_rejoin = (game_started and player_name in assigned_words and 
                  player_name not in game_players)
    return render_template("index.html", 
                         players=list(game_players.keys()) if game_started else players,
                         game_started=game_started,
                         spicy_mode=spicy_mode,
                         force_spicy=force_spicy,
                         revealed=revealed,
                         assigned_words=assigned_words,
                         player_word=player_word,
                         is_logged_in=is_active_player,
                         player_name=player_name,
                         chat_messages=get_current_chat()[-20:],
                         current_starter=current_starter,
                         voting_active=voting_active,
                         votes=votes,
                         vote_results=vote_results,
                         public_ip=get_public_ip(),
                         game_ended=game_ended,
                         winning_word=winning_word,
                         is_control=is_control_user(),
                         impostor_guess_used=impostor_guess_used,
                         can_rejoin=can_rejoin,
                         error_message=error_message,
                         kicked_message=kicked_message)

@app.route("/rejoin")
def rejoin_game():
    """Re-Join zu laufendem Spiel"""
    global game_players
    
    session_id = session.get('session_id')
    player_name = session.get('player_name')
    
    if (game_started and player_name and player_name in assigned_words and 
        player_name not in game_players and session_id):
        
        game_players[player_name] = session_id
        update_heartbeat(session_id)
    
    return redirect(url_for("index"))

@app.route("/leave_lobby")
def leave_lobby():
    """Verlasse die Lobby"""
    global players
    player_name = session.get('player_name')
    session_id = session.get('session_id')
    was_control = session.get('control_logged_in', False)
    if player_name and session_id:
        if not game_started and player_name in players and not was_control:
            players.remove(player_name)
            # Add game event for player leave
            add_game_event('player_leave', f"👋 {player_name} hat die Lobby verlassen!", "👋")
            
            # Check if not enough players to start the game
            if len(players) < 3:
                add_game_event('lobby_not_ready', f"⏳ Nicht genug Spieler zum Starten ({len(players)}/3)", "⏳")
        if session_id in player_sessions:
            del player_sessions[session_id]
        if session_id in session_heartbeats:
            del session_heartbeats[session_id]
        session.clear()
        if was_control:
            session['control_logged_in'] = True
    return redirect(url_for("index"))

@app.route("/word/<name>")
def word(name):
    session_id = session.get('session_id')
    player_name = session.get('player_name')
    
    if player_name != name:
        return "Zugriff verweigert! Du kannst nur dein eigenes Wort sehen."
    
    if game_started and name not in game_players:
        return "Du bist nicht aktiv im Spiel. Bitte rejoine zuerst."
    
    if name not in assigned_words:
        return "Spiel hat noch nicht gestartet oder du bist kein Spieler."
    
    if session_id:
        update_heartbeat(session_id)
    
    return render_template("word.html", name=name, role=assigned_words[name])

@app.route("/send_message", methods=["POST"])
def send_message():
    player_name = session.get('player_name')
    session_id = session.get('session_id')
    
    # Allow messages in both lobby and game phases
    if not player_name:
        return jsonify({'error': 'Du bist nicht eingeloggt'})
    
    # Check if player is in the appropriate phase
    if game_started:
        if player_name not in game_players:
            return jsonify({'error': 'Du bist kein aktiver Spieler'})
    else:
        if player_name not in players:
            return jsonify({'error': 'Du bist nicht in der Lobby'})
    
    if session_id:
        update_heartbeat(session_id)
    
    message = request.form.get('message', '').strip()
    if message and len(message) <= 200:
        add_player_message(player_name, message)
    
    return jsonify({'success': True})

@app.route("/guess_word", methods=["POST"])
def guess_word():
    global game_ended, vote_results, winning_word, impostor_guess_used
    
    if not game_started or game_ended:
        return jsonify({'error': 'Spiel nicht aktiv'})
    
    player_name = session.get('player_name')
    session_id = session.get('session_id')
    
    if not player_name or player_name not in game_players:
        return jsonify({'error': 'Du bist kein aktiver Spieler'})
    
    if session_id:
        update_heartbeat(session_id)
    
    if not ("IMPOSTOR" in assigned_words.get(player_name, "")):
        return jsonify({'error': 'Nur der Impostor kann das Wort erraten'})
    
    if impostor_guess_used:
        return jsonify({'error': 'Du kannst nur einmal raten!'})
    
    guessed_word = request.form.get('guessed_word', '').strip()
    
    if not guessed_word:
        return jsonify({'error': 'Kein Wort eingegeben'})
    
    impostor_guess_used = True
    
    actual_word = ""
    for player, word in assigned_words.items():
        if not "IMPOSTOR" in word:
            actual_word = word.replace("Dein Wort: ", "")
            break
    
    if check_impostor_word_guess(guessed_word, actual_word):
        game_ended = True
        winning_word = actual_word
        vote_results = {
            'impostor_won': True,
            'guessed_word': guessed_word,
            'actual_word': actual_word,
            'impostor': player_name,
            'game_ended': True,
            'winning_word': actual_word
        }

        update_leaderboard_on_game_end(vote_results)
        add_game_event('game', f"Richtig! {player_name} hat gewonnen!")
        return jsonify({'success': True, 'correct': True, 'message': 'Richtig! Du hast gewonnen!'})
    else:
        game_ended = True
        winning_word = actual_word
        vote_results = {
            'impostor_failed': True,
            'guessed_word': guessed_word,
            'actual_word': actual_word,
            'impostor': player_name,
            'game_ended': True,
            'winning_word': actual_word
        }

        update_leaderboard_on_game_end(vote_results)
        add_game_event('game', f"Falsch! {player_name} hat verloren!")
        return jsonify({'success': True, 'correct': False, 'message': 'Falsch! Die Spieler haben gewonnen!'})

@app.route("/kick_player", methods=["POST"])
def kick_player():
    global players, game_players, player_sessions, session_heartbeats, kicked_sessions
    player_to_kick = request.form.get('player_name')
    if not player_to_kick:
        return jsonify({'error': 'Kein Spieler angegeben'})
    if player_to_kick in players:
        players.remove(player_to_kick)
    if player_to_kick in game_players:
        del game_players[player_to_kick]
    if player_to_kick in assigned_words:
        del assigned_words[player_to_kick]
    if player_to_kick in votes:
        del votes[player_to_kick]
    session_to_remove = None
    for sid, name in player_sessions.items():
        if name == player_to_kick:
            session_to_remove = sid
            break
    if session_to_remove:
        kicked_sessions.add(session_to_remove)
        del player_sessions[session_to_remove]
        if session_to_remove in session_heartbeats:
            del session_heartbeats[session_to_remove]
    
    # Add game event for player kick
    add_game_event('player_kick', f"🚪 {player_to_kick} wurde aus dem Spiel entfernt!", "🚪")
    
    return jsonify({'success': True, 'message': f'{player_to_kick} wurde gekickt'})

@app.route("/vote", methods=["POST"])
def vote():
    global votes
    if not game_started or voting_active == False:
        return jsonify({'error': 'Voting nicht aktiv'})
    
    player_name = session.get('player_name')
    session_id = session.get('session_id')
    
    if not player_name or player_name not in game_players:
        return jsonify({'error': 'Du bist kein aktiver Spieler'})
    
    if session_id:
        update_heartbeat(session_id)
    
    voted_player = request.form.get('voted_player')
    if voted_player and voted_player in game_players and voted_player != player_name:
        votes[player_name] = voted_player
        
        if check_all_voted():
            auto_end_voting()
    
    return jsonify({'success': True})

@app.route("/start_voting")
def start_voting():
    global voting_active, votes, vote_results
    if game_started and not voting_active and not game_ended:
        voting_active = True
        votes = {}
        vote_results = {}
        # Add game event for voting start
        add_game_event('voting_start', "🗳️ Abstimmung hat begonnen", "🗳️")
    return redirect(url_for("index"))

@app.route("/end_voting")
def end_voting():
    if voting_active:
        auto_end_voting()
    return redirect(url_for("index"))

@app.route("/return_to_lobby")
def return_to_lobby():
    """Zurück zur Lobby - Reset für neues Spiel"""
    global game_started, assigned_words, revealed, game_messages, current_starter, votes, voting_active, vote_results, game_ended, winning_word, impostor_guess_used, game_players
    
    game_started = False
    assigned_words = {}
    revealed = False
    game_messages = []
    current_starter = ""
    votes = {}
    voting_active = False
    vote_results = {}
    game_ended = False
    winning_word = ""
    impostor_guess_used = False
    game_players = {}
    
    session.pop('player_name', None)
    
    # Add game event for return to lobby
    add_game_event('lobby_return', "🏠 Zurück in der Lobby - Neues Spiel kann beginnen!", "🏠")
    
    return redirect(url_for("index"))

@app.route("/start")
def start_game():
    global assigned_words, game_started, current_starter, game_ended, winning_word, impostor_guess_used, game_players, start_votes
    start_votes.clear()
    
    if not players or len(players) < 3:
        return "Mindestens 3 Spieler benötigt."
    
    word_list = get_word_list()
    word = random.choice(word_list)
    impostor = random.choice(players)
    
    current_starter = select_starter(players, impostor)
    winning_word = word
    
    for player in players:
        if player in player_sessions.values():
            for sid, name in player_sessions.items():
                if name == player:
                    game_players[player] = sid
                    break
    
    for p in players:
        if p == impostor:
            assigned_words[p] = "Du bist der IMPOSTOR!"
        else:
            assigned_words[p] = f"Dein Wort: {word}"
    
    game_started = True
    game_ended = False
    impostor_guess_used = False
    
    # Add game events
    add_game_event('game_start', f"🎬 Das Spiel hat begonnen!", "🎬")
    add_game_event('game_start', f"🎯 {current_starter} beginnt das Spiel!", "🎯")

    return redirect(url_for("index"))

@app.route("/reveal")
def reveal_words():
    global revealed
    if game_started and assigned_words and not game_ended:
        revealed = True
        # Word reveal event removed as requested
    return redirect(url_for("index"))

@app.route("/reset")
def reset_game():
    global players, game_started, assigned_words, revealed, game_messages, current_starter, votes, voting_active, vote_results, game_ended, winning_word, impostor_guess_used, game_players, player_sessions, session_heartbeats, start_votes
    start_votes.clear()
    
    # Add game event for game reset
    add_game_event('game_reset', "🔄 Das Spiel wurde zurückgesetzt!", "🔄")
    
    players = []
    game_started = False
    assigned_words = {}
    revealed = False
    game_messages = []
    current_starter = ""
    votes = {}
    voting_active = False
    vote_results = {}
    game_ended = False
    winning_word = ""
    impostor_guess_used = False
    game_players = {}
    player_sessions = {}
    session_heartbeats = {}
    
    return redirect(url_for("index"))

@app.route("/control", methods=["GET", "POST"])
def control_panel():
    if request.method == "POST":
        password = request.form.get("password")
        if password == CONTROL_PASSWORD:
            session['control_logged_in'] = True
            return redirect(url_for("control_panel"))
        else:
            return render_template("control_login.html", error="Falsches Passwort!")
    
    if not session.get('control_logged_in'):
        return render_template("control_login.html")
    
    return render_template("control_panel.html", 
                         spicy_mode=spicy_mode,
                         force_spicy=force_spicy,
                         public_ip=get_public_ip())

@app.route("/control/toggle_spicy")
def control_toggle_spicy():
    global spicy_mode
    if session.get('control_logged_in'):
        spicy_mode = not spicy_mode
    return redirect(url_for("control_panel"))

@app.route("/control/toggle_force_spicy")
def control_toggle_force_spicy():
    global force_spicy
    if session.get('control_logged_in'):
        force_spicy = not force_spicy
    return redirect(url_for("control_panel"))

@app.route("/control/start_game")
def control_start_game():
    if session.get('control_logged_in'):
        return start_game()
    return redirect(url_for("control_panel"))

@app.route("/control/reset_game")
def control_reset_game():
    if session.get('control_logged_in'):
        return reset_game()
    return redirect(url_for("control_panel"))

@app.route("/control/reveal")
def control_reveal():
    if session.get('control_logged_in'):
        return reveal_words()
    return redirect(url_for("control_panel"))

@app.route('/api/start_votes')
def api_start_votes():
    global start_votes
    return jsonify({
        'votes': list(start_votes),
        'total': len(players),
        'players': players,
        'game_started': game_started
    })

@app.route('/start_vote', methods=['POST'])
def start_vote():
    global start_votes
    if game_started:
        return jsonify({'error': 'Game already started'}), 400
    player_name = session.get('player_name')
    if not player_name or player_name not in players:
        return jsonify({'error': 'Not a valid player'}), 400
    start_votes.add(player_name)
    
    if len(start_votes) == len(players) and len(players) >= 3:
        start_votes.clear()
        return start_game()
    return jsonify({'success': True, 'votes': list(start_votes), 'total': len(players)})

@app.route('/api/heartbeat')
def api_heartbeat():
    session_id = session.get('session_id')
    if session_id:
        update_heartbeat(session_id)
        return jsonify({'success': True})
    return jsonify({'success': False}), 401

@app.route('/api/control_stats')
def api_control_stats():
    session_id = session.get('session_id')
    player_name = session.get('player_name')
    stats = {
        'game_state': 'lobby' if not game_started else ('ended' if game_ended else ('voting' if voting_active else 'running')),
        'player_count': len(players) if not game_started else len(game_players),
        'players': list(players) if not game_started else list(game_players.keys()),
        'impostor': next((p for p, w in assigned_words.items() if 'IMPOSTOR' in w), None) if game_started else None,
        'spicy_mode': 'forced' if force_spicy else ('possible' if spicy_mode else 'disabled'),
        'round': 1 if game_started else 0,
        'game_started': game_started,
        'game_ended': game_ended,
        'voting_active': voting_active,
        'current_starter': current_starter,
        'winning_word': winning_word,
        'settings': {
            'min_players': 3,
            'heartbeat_timeout': HEARTBEAT_TIMEOUT,
            'cleanup_interval': CLEANUP_INTERVAL,
            'announce_spicy_mode': announce_spicy_mode
        }
    }
    return jsonify(stats)

@app.route('/api/kick_player', methods=['POST'])
def api_kick_player():
    if not is_control_user():
        return jsonify({'success': False, 'error': 'Not authorized'}), 403
    data = request.get_json(silent=True) or {}
    player_to_kick = data.get('player_name')
    if not player_to_kick:
        return jsonify({'success': False, 'error': 'No player specified'}), 400
    if player_to_kick in players:
        players.remove(player_to_kick)
    if player_to_kick in game_players:
        del game_players[player_to_kick]
    if player_to_kick in assigned_words:
        del assigned_words[player_to_kick]
    if player_to_kick in votes:
        del votes[player_to_kick]
    session_to_remove = None
    for sid, name in player_sessions.items():
        if name == player_to_kick:
            session_to_remove = sid
            break
    if session_to_remove:
        del player_sessions[session_to_remove]
        if session_to_remove in session_heartbeats:
            del session_heartbeats[session_to_remove]
    return jsonify({'success': True})

@app.route('/api/reset_leaderboard', methods=['POST'])
def api_reset_leaderboard():
    if not is_control_user():
        return jsonify({'success': False, 'error': 'Not authorized'}), 403
    data = request.get_json(silent=True) or {}
    which = data.get('which')
    if which == 'players':
        leaderboard['players'] = {}
    elif which == 'impostors':
        leaderboard['impostors'] = {}
    elif which == 'both':
        leaderboard['players'] = {}
        leaderboard['impostors'] = {}
    else:
        return jsonify({'success': False, 'error': 'Invalid option'}), 400
    save_leaderboard()
    return jsonify({'success': True})

@app.route('/api/spicy_mode', methods=['GET', 'POST'])
def api_spicy_mode():
    global spicy_mode, force_spicy
    if request.method == 'GET':
        mode = 'forced' if force_spicy else ('possible' if spicy_mode else 'disabled')
        return jsonify({'mode': mode})
    if not is_control_user():
        return jsonify({'success': False, 'error': 'Not authorized'}), 403
    data = request.get_json(silent=True) or {}
    mode = data.get('mode')
    if mode == 'disabled':
        spicy_mode = False
        force_spicy = False
    elif mode == 'possible':
        spicy_mode = True
        force_spicy = False
    elif mode == 'forced':
        spicy_mode = False
        force_spicy = True
    else:
        return jsonify({'success': False, 'error': 'Invalid mode'}), 400
    return jsonify({'success': True})

@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    global HEARTBEAT_TIMEOUT, CLEANUP_INTERVAL, announce_spicy_mode
    if request.method == 'GET':
        return jsonify({
            'min_players': 3,
            'heartbeat_timeout': HEARTBEAT_TIMEOUT,
            'cleanup_interval': CLEANUP_INTERVAL,
            'announce_spicy_mode': announce_spicy_mode
        })
    if not is_control_user():
        return jsonify({'success': False, 'error': 'Not authorized'}), 403
    data = request.get_json(silent=True) or {}
    if 'heartbeat_timeout' in data:
        try:
            HEARTBEAT_TIMEOUT = int(data['heartbeat_timeout'])
        except Exception:
            pass
    if 'cleanup_interval' in data:
        try:
            CLEANUP_INTERVAL = int(data['cleanup_interval'])
        except Exception:
            pass
    if 'announce_spicy_mode' in data:
        announce_spicy_mode = bool(data['announce_spicy_mode'])
    return jsonify({'success': True})

@app.route('/api/console_output')
def api_console_output():
    log_path = 'server.log'
    if os.path.exists(log_path):
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()[-40:]
        return jsonify({'output': ''.join(lines)})
    else:
        return jsonify({'output': 'No log file found or logging not enabled.'})

@app.route('/api/change_password', methods=['POST'])
def api_change_password():
    global CONTROL_PASSWORD
    if not is_control_user():
        return jsonify({'success': False, 'error': 'Not authorized'}), 403
    data = request.get_json(silent=True) or {}
    new_password = data.get('new_password', '').strip()
    if not new_password or len(new_password) < 4:
        return jsonify({'success': False, 'error': 'Passwort zu kurz (min. 4 Zeichen)'}), 400
    CONTROL_PASSWORD = new_password
    return jsonify({'success': True})

@app.route('/api/am_i_kicked')
def api_am_i_kicked():
    session_id = session.get('session_id')
    is_kicked = session_id in kicked_sessions if session_id else False
    return jsonify({'was_kicked': is_kicked})

def add_game_event(event_type, message, emoji="🎮"):
    """Adds a game event message with special styling"""
    global lobby_messages, game_messages
    
    event_message = {
        'player': 'SYSTEM',
        'message': message,
        'timestamp': datetime.now().strftime('%H:%M:%S'),
        'is_event': True,
        'event_type': event_type,
        'emoji': emoji
    }
    
    # Add to the appropriate chat based on game state
    if game_started:
        game_messages.append(event_message)
    else:
        lobby_messages.append(event_message)

def get_current_chat():
    """Returns the appropriate chat based on game state"""
    return game_messages if game_started else lobby_messages

def add_player_message(player_name, message):
    """Adds a player message to the appropriate chat"""
    global lobby_messages, game_messages
    
    player_message = {
        'player': player_name,
        'message': message,
        'timestamp': datetime.now().strftime('%H:%M:%S'),
        'is_event': False
    }
    
    if game_started:
        game_messages.append(player_message)
    else:
        lobby_messages.append(player_message)

def start_server():
    """Startet Cleanup-Thread"""
    start_cleanup_thread()
    
    public_ip = get_public_ip()
    local_ip = get_local_ip()
    print(f"🎭 Impostor Game Server gestartet!")
    print(f"🌐 Öffentliche IP: {public_ip}:5000")
    print(f"🏠 Lokale IP: {local_ip}:5000")
    print(f"🔐 Control-Passwort: {CONTROL_PASSWORD}")
    print(f"⏱️ Präsenz-System aktiv (Timeout: {HEARTBEAT_TIMEOUT}s)")
    print(f"🏆 Leaderboard-System aktiv")
    
    if has_display():
        print("🖥️ GUI verfügbar - öffne Browser...")
        webbrowser.open("http://localhost:5000")
    else:
        print("🖥️ Kein Display erkannt - Server läuft im Headless-Modus")
        print(f"📱 Öffne http://{local_ip}:5000 in deinem Browser (lokales Netzwerk)")
        print(f"🌍 Öffne http://{public_ip}:5000 in deinem Browser (Internet)")

if __name__ == '__main__':
    is_railway = os.environ.get('RAILWAY_ENVIRONMENT') or os.environ.get('PORT')
    
    if is_railway:
        start_cleanup_thread()
        print("🚀 Railway-Modus: Gunicorn übernimmt Server-Start")
        print(f"🔐 Control-Passwort: {CONTROL_PASSWORD}")
        print(f"🏆 Leaderboard-System aktiv")
        
    else:
        if has_display():
            try:
                import tkinter as tk
                from tkinter import ttk
                
                def launch_server():
                    start_server()
                    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
                
                def open_control_panel():
                    webbrowser.open("http://localhost:5000/control")
                
                gui = tk.Tk()
                gui.title("Impostor Game Server - Control Panel")
                gui.geometry("450x350")
                gui.configure(bg='#f0f0f0')
                
                title_label = tk.Label(gui, text="🎭 Impostor Game Control Panel", 
                                      font=("Arial", 16, "bold"), 
                                      bg='#f0f0f0', fg='#333')
                title_label.pack(pady=20)
                
                public_ip = get_public_ip()
                local_ip = get_local_ip()
                server_label = tk.Label(gui, text=f"Server läuft auf:\nLokal: {local_ip}:5000\nÖffentlich: {public_ip}:5000", 
                                       font=("Arial", 12), 
                                       bg='#f0f0f0', fg='#666')
                server_label.pack(pady=10)
                
                button_frame = tk.Frame(gui, bg='#f0f0f0')
                button_frame.pack(pady=30)
                
                ttk.Button(button_frame, text="🌐 Website starten", 
                          command=launch_server, width=25).pack(pady=5)
                
                ttk.Button(button_frame, text="🔐 Web-Control öffnen", 
                          command=open_control_panel, width=25).pack(pady=5)
                
                info_label = tk.Label(gui, text=f"Control-Passwort: {CONTROL_PASSWORD}", 
                                     font=("Arial", 10, "bold"), 
                                     bg='#f0f0f0', fg='#e74c3c')
                info_label.pack(pady=10)
                
                info_label2 = tk.Label(gui, text="Live-Präsenz & Leaderboard-System aktiv", 
                                      font=("Arial", 9), 
                                      bg='#f0f0f0', fg='#888')
                info_label2.pack()
                
                server_thread = threading.Thread(target=lambda: [start_server(), app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)], daemon=True)
                server_thread.start()
                
                gui.mainloop()
                
            except ImportError:
                print("⚠️ Tkinter nicht verfügbar - starte im Headless-Modus")
                start_server()
                app.run(host='0.0.0.0', port=5000, debug=True)
        else:
            print("🖥️ Headless-Modus erkannt - starte Server ohne GUI")
            start_server()
            app.run(host='0.0.0.0', port=5000, debug=True)

else:
    start_cleanup_thread()