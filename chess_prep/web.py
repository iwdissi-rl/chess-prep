"""Optional browser board for practice games (``chessprep practice USER --web``).

Needs Flask: ``pip install "chess-prep[web]"``. Listens on 127.0.0.1 only.
"""

from __future__ import annotations

import threading
import uuid

import chess
import chess.pgn

from chess_prep.clone import CloneBot, OpponentBook
from chess_prep.engines import EngineSet
from chess_prep.paths import Player
from chess_prep.practice import GameSummary, history, save_game

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Practice vs {{ label }}</title>
<link rel="stylesheet" href="https://unpkg.com/@chrisoakman/chessboardjs@1.0.0/dist/chessboard-1.0.0.min.css">
<style>
 body{font-family:system-ui,sans-serif;background:#1d2430;color:#e6e6e6;margin:0}
 .wrap{display:flex;gap:24px;padding:20px;max-width:1100px;margin:auto;flex-wrap:wrap}
 #board{width:520px} h1{font-size:1.3em;color:#7cc4ff}
 button{padding:8px 14px;border:0;border-radius:6px;margin:0 6px 10px 0;cursor:pointer;font-weight:600}
 .main{background:#2f81f7;color:#fff} .alt{background:#30363d;color:#e6e6e6}
 .box{background:#252d3a;border-radius:8px;padding:12px;margin-bottom:12px}
 #moves{font-family:monospace;line-height:1.7;max-height:280px;overflow:auto}
 .src{font-size:.75em;color:#7cc4ff} td{padding:2px 8px}
</style></head><body><div class="wrap">
<div><h1>Practice vs {{ label }} clone</h1>
 <div class="box" id="status">Pick a colour to start.</div>
 <button class="main" onclick="newGame('white')">Play White</button>
 <button class="main" onclick="newGame('black')">Play Black</button>
 <button class="alt" onclick="resign()">Resign</button>
 <button class="alt" onclick="board.flip()">Flip</button>
 <div id="board"></div></div>
<div style="flex:1;min-width:280px">
 <h1>Moves</h1><div class="box" id="moves"></div>
 <h1>History</h1><div class="box"><table id="hist"></table></div></div>
</div>
<script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>
<script src="https://unpkg.com/@chrisoakman/chessboardjs@1.0.0/dist/chessboard-1.0.0.min.js"></script>
<script>
let board=null, gameId=null, myColor='white', active=false, fen='start';
const post=(u,b)=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json());
function show(d){ if(d.error){document.getElementById('status').textContent=d.error; board.position(fen); return;}
  fen=d.fen; board.position(fen,false); let h='';
  d.moves.forEach((m,i)=>{ if(i%2==0) h+=(i/2+1)+'. '; h+=m.san+(m.source&&m.source!='you'?'<span class="src">['+m.source+']</span>':'')+' '; });
  document.getElementById('moves').innerHTML=h; document.getElementById('status').textContent=d.status;
  if(d.over){active=false; loadHistory();} }
function newGame(c){ myColor=c; post('/api/new',{color:c}).then(d=>{gameId=d.game_id; active=true; board.orientation(c); show(d);}); }
function resign(){ if(!active) return; post('/api/resign',{game_id:gameId}).then(d=>{active=false; show(d);}); }
function onDragStart(s,p){ return active && p[0]===myColor[0]; }
function onDrop(from,to){ if(!active) return 'snapback';
  post('/api/move',{game_id:gameId,from:from,to:to}).then(show); return 'snapback'; }
function loadHistory(){ fetch('/api/history').then(r=>r.json()).then(d=>{
  document.getElementById('hist').innerHTML='<tr><th>Date</th><th>You</th><th>Result</th><th>Moves</th></tr>'+
  d.games.map(g=>'<tr><td>'+g.date+'</td><td>'+g.color+'</td><td>'+g.result+'</td><td>'+g.moves+'</td></tr>').join(''); }); }
$(function(){ board=Chessboard('board',{draggable:true,position:'start',onDragStart:onDragStart,onDrop:onDrop,
  pieceTheme:'https://unpkg.com/@chrisoakman/chessboardjs@1.0.0/dist/img/chesspieces/wikipedia/{piece}.png'}); loadHistory(); });
</script></body></html>"""


def parse_drag(board: chess.Board, from_sq: str, to_sq: str, promotion: str = "q") -> chess.Move | None:
    """Turn a drag (from, to) into a legal move. Pawns reaching the last rank promote."""
    try:
        mv = chess.Move.from_uci(f"{from_sq}{to_sq}")
    except ValueError:
        return None
    if mv in board.legal_moves:
        return mv
    try:
        promo = chess.Move.from_uci(f"{from_sq}{to_sq}{promotion or 'q'}")
    except ValueError:
        return None
    return promo if promo in board.legal_moves else None


def create_app(player: Player, book: OpponentBook, engines: EngineSet):
    from flask import Flask, jsonify, render_template_string, request

    app = Flask(__name__)
    games: dict[str, dict] = {}
    lock = threading.Lock()

    def payload(g: dict, status: str) -> dict:
        b = g["board"]
        over = b.is_game_over(claim_draw=True) or g.get("resigned", False)
        return {"game_id": g["id"], "fen": b.fen(), "moves": g["moves"], "status": status, "over": over}

    def finish(g: dict) -> str:
        b = g["board"]
        you = chess.WHITE if g["color"] == "white" else chess.BLACK
        if g.get("resigned"):
            result = "loss"
        else:
            outcome = b.outcome(claim_draw=True)
            result = "draw" if outcome.winner is None else ("win" if outcome.winner == you else "loss")
        pgn = str(chess.pgn.Game.from_board(b))
        save_game(player, GameSummary(g["color"], result, len(b.move_stack), g["book_exit"], pgn))
        g["bot"].close()
        return {"win": "You won!", "loss": "The clone won.", "draw": "Draw."}[result]

    def clone_move(g: dict) -> str:
        b = g["board"]
        mv, info = g["bot"].pick(b, g["sans"])
        san = b.san(mv)
        if info.source != "book" and g["book_exit"] is None:
            g["book_exit"] = len(g["sans"])
        b.push(mv)
        g["sans"].append(san)
        g["moves"].append({"san": san, "source": info.source})
        return f"Clone played {san} ({info.source})"

    @app.get("/")
    def index():
        return render_template_string(PAGE, label=player.label)

    @app.post("/api/new")
    def new():
        color = (request.get_json(silent=True) or {}).get("color", "white")
        clone_color = "black" if color == "white" else "white"
        g = {
            "id": uuid.uuid4().hex,
            "color": color,
            "board": chess.Board(),
            "sans": [],
            "moves": [],
            "book_exit": None,
            "bot": CloneBot(book, clone_color, engines),
        }
        with lock:
            for old in games.values():  # one board at a time: free engines of abandoned games
                old["bot"].close()
            games.clear()
            games[g["id"]] = g
            status = f"You play {color}."
            if color == "black":
                status = clone_move(g)
        return jsonify(payload(g, status))

    @app.post("/api/move")
    def move():
        data = request.get_json(silent=True) or {}
        with lock:
            g = games.get(data.get("game_id", ""))
            if g is None:
                return jsonify({"error": "No such game. Start a new one."}), 404
            b = g["board"]
            mv = parse_drag(b, data.get("from", ""), data.get("to", ""), data.get("promotion", "q"))
            if mv is None:
                return jsonify({**payload(g, "Illegal move."), "error": "Illegal move."})
            san = b.san(mv)
            b.push(mv)
            g["sans"].append(san)
            g["moves"].append({"san": san, "source": "you"})
            if b.is_game_over(claim_draw=True):
                return jsonify(payload(g, finish(g)))
            status = clone_move(g)
            if b.is_game_over(claim_draw=True):
                status = finish(g)
            return jsonify(payload(g, status))

    @app.post("/api/resign")
    def resign():
        data = request.get_json(silent=True) or {}
        with lock:
            g = games.pop(data.get("game_id", ""), None)
            if g is None:
                return jsonify({"error": "No such game."}), 404
            g["resigned"] = True
            return jsonify(payload(g, finish(g)))

    @app.get("/api/history")
    def hist():
        rows = history(player)[-20:]
        return jsonify(
            {
                "games": [
                    {
                        "date": r["played_at"],
                        "color": r["your_color"],
                        "result": r["result"],
                        "moves": (r["plies"] + 1) // 2,
                    }
                    for r in reversed(rows)
                ]
            }
        )

    return app
